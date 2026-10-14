package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import net.minecraft.util.Mth;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.TamableAnimal;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.entity.animal.cow.Cow;
import net.minecraft.world.entity.animal.cow.MushroomCow;
import net.minecraft.world.entity.animal.sheep.Sheep;
import net.minecraft.world.entity.npc.villager.AbstractVillager;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.phys.EntityHitResult;

/**
 * Advanced entity interaction system supporting multiple interaction types with
 * safety checks.
 * Supports attack, tame, trade, look_at actions with distance validation and
 * interaction type detection.
 */
public class EntityInteractionCommandHandler extends AsyncCommandHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(EntityInteractionCommandHandler.class);

    /**
     * How long to wait before re-reading the held stack after an interaction.
     * The integrated server applies the result and syncs it back within a tick
     * or two, so this is comfortably past the round-trip while staying
     * imperceptible to a caller that only talks to the bridge every few
     * seconds.
     */
    private static final long SETTLE_DELAY_MS = 1200L;

    public enum InteractionType {
        ATTACK,
        TAME,
        TRADE,
        LOOK_AT,
        SHEAR,
        MILK,
        INTERACT
    }

    private static class InteractionValidation {
        boolean valid = true;
        String error = "";
        double distance = 0.0;
        InteractionType detectedType;

        static InteractionValidation success(double distance, InteractionType type) {
            InteractionValidation v = new InteractionValidation();
            v.distance = distance;
            v.detectedType = type;
            return v;
        }

        static InteractionValidation error(String message) {
            InteractionValidation v = new InteractionValidation();
            v.valid = false;
            v.error = message;
            return v;
        }
    }

    @Override
    public String getCommandName() {
        return "entity_interact";
    }

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client, IBaritone baritone,
            Socket clientSocket) {
        String action = params.has("action") ? params.get("action").getAsString() : "look_at";

        switch (action) {
            case "feed":
            case "interact":
                return handleDirectEntityInteraction(params, client, action);
            case "tame":
            case "shear":
            case "milk":
            // ATTACK / TRADE were declared in InteractionType and handled by
            // validateInteraction()/performInteraction(), but never routed here, so
            // they died with "Unknown interaction action". Route them through the
            // validated path (which also accepts entity_type and does the type checks).
            case "attack":
            case "trade":
                return handleEntityInteraction(params, client, action);
            case "look_at":
                return handleLookAtEntity(params, client);
            case "detect":
                return handleDetectInteractions(params, client);
            default:
                return CompletableFuture.completedFuture(CommandResult.error("Unknown interaction action: " + action));
        }
    }

    private CompletableFuture<CommandResult> handleEntityInteraction(JsonObject params, Minecraft client,
            String action) {
        return executeOnMainThread(client, () -> {
            if (client.player == null || client.level == null) {
                return CommandResult.error("Player or world not available");
            }

            if (!params.has("entity_id") && !params.has("entity_type")) {
                return CommandResult.error("Missing required parameter: entity_id or entity_type");
            }

            // 6.0 keeps the historic reach for the entity_id path (which does not use
            // maxDistance for lookup) now that validateInteraction() honours it.
            double maxDistance = params.has("max_distance") ? params.get("max_distance").getAsDouble() : 6.0;
            Entity target;
            String targetDescription;
            if (params.has("entity_id")) {
                int entityId = params.get("entity_id").getAsInt();
                target = client.level.getEntity(entityId);
                targetDescription = "entity id " + entityId;
            } else {
                String entityType = params.get("entity_type").getAsString();
                target = findNearestEntityOfType(client, entityType, maxDistance);
                targetDescription = entityType;
            }

            if (target == null) {
                return CommandResult.error(
                    "No valid " + targetDescription + " found within " + maxDistance + " blocks");
            }

            // Validate interaction
            InteractionValidation validation = validateInteraction(client, target, action, maxDistance);
            if (!validation.valid) {
                return CommandResult.error(validation.error);
            }

            // Check inventory requirements
            String inventoryError = checkInventoryRequirements(client, action);
            if (inventoryError != null) {
                return CommandResult.error(inventoryError);
            }

            // Having the item somewhere in the inventory is not the same as holding
            // it. The requirement check above only looks at the inventory, so "milk"
            // reported success while the hand held nothing: the server had no bucket
            // to fill and the interaction silently did nothing. Put the item in hand
            // ourselves instead of trusting the caller to have preselected a slot.
            String heldError = ensureRequiredItemInHand(client, validation.detectedType);
            if (heldError != null) {
                return CommandResult.error(heldError);
            }

            // Perform the interaction
            boolean success = performInteraction(client, target, validation.detectedType);

            JsonObject data = new JsonObject();
            data.addProperty("success", success);
            data.addProperty("entity_id", target.getId());
            data.addProperty("entity_type", BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
            data.addProperty("action", action);
            data.addProperty("distance", validation.distance);
            data.addProperty("interaction_type", validation.detectedType.name());

            return CommandResult.success(data);
        });
    }

    /**
     * Right-click a specific entity with the currently selected main-hand
     * item. Unlike crosshair-driven use_item, the entity id remains the target
     * even if it moves between sensing and interaction.
     */
    private CompletableFuture<CommandResult> handleDirectEntityInteraction(
            JsonObject params, Minecraft client, String action) {
        CompletableFuture<CommandResult> future = new CompletableFuture<>();
        client.execute(() -> {
            try {
                interactWithEntityDirectly(params, client, action, future);
            } catch (Exception e) {
                LOGGER.error("Direct entity interaction failed", e);
                future.complete(CommandResult.error("Interaction failed: " + e.getMessage()));
            }
        });
        return future;
    }

    private void interactWithEntityDirectly(
            JsonObject params, Minecraft client, String action, CompletableFuture<CommandResult> future) {
        if (client.player == null || client.level == null
                || client.gameMode == null) {
            future.complete(CommandResult.error("Player, world, or interaction manager not available"));
            return;
        }
        if (!params.has("entity_id")) {
            future.complete(CommandResult.error("Missing required parameter: entity_id"));
            return;
        }

        int entityId = params.get("entity_id").getAsInt();
        Entity target = client.level.getEntity(entityId);
        if (target == null) {
            future.complete(CommandResult.error("Target entity not found: " + entityId));
            return;
        }
        if ("feed".equals(action) && !(target instanceof Animal)) {
            future.complete(CommandResult.error("Feed target is not an animal: " + entityId));
            return;
        }

        double maxDistance = params.has("max_distance")
            ? params.get("max_distance").getAsDouble() : 6.0;
        double distance = distanceToPlayer(client, target);
        if (distance > maxDistance) {
            future.complete(CommandResult.error(
                "Entity too far: " + String.format("%.2f", distance)
                    + " blocks (max " + String.format("%.2f", maxDistance) + ")"));
            return;
        }

        if ("feed".equals(action)) {
            // Use whatever the animal actually eats instead of demanding the
            // caller hand us the right item: the old check rejected the feed
            // with "Held item is not food for this animal: minecraft:copper_sword"
            // even while the wheat was sitting in slot 7, unused.
            String foodError = ensureFoodInHand(client, (Animal) target);
            if (foodError != null) {
                future.complete(CommandResult.error(foodError));
                return;
            }
        }

        String heldItem = itemIdOf(client.player.getMainHandItem());
        int heldCountBefore = client.player.getMainHandItem().getCount();
        InteractionResult interactionResult = interactWithEntity(
            client, target, InteractionHand.MAIN_HAND);
        if (interactionResult.consumesAction()) {
            client.player.swing(InteractionHand.MAIN_HAND);
        }

        JsonObject data = new JsonObject();
        data.addProperty("success", interactionResult.consumesAction());
        data.addProperty("accepted", interactionResult.consumesAction());
        data.addProperty("result", interactionResult.toString());
        data.addProperty("action", action);
        data.addProperty("entity_id", target.getId());
        data.addProperty("entity_type",
            BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
        data.addProperty("distance", distance);
        data.addProperty("hand", "main_hand");
        data.addProperty("held_item", heldItem);
        data.addProperty("held_count_before", heldCountBefore);
        settleHandAndComplete(client, data, heldItem, future);
    }

    /**
     * Re-read the hand after the server round-trip, then complete the future.
     *
     * Reading it immediately after gameMode.interact() is worthless: the
     * interaction is applied by the integrated server and only then synced
     * back, so the instant read always returns the pre-interaction stack. That
     * made held_count_after silently lie -- a feed that really consumed one
     * wheat reported "held_count_before=64, held_count_after=64" -- and it cost
     * a debugging session chasing a phantom success that never existed. Wait
     * the round-trip out and report the settled value instead.
     */
    private void settleHandAndComplete(Minecraft client, JsonObject data, String heldItemBefore,
            CompletableFuture<CommandResult> future) {
        Thread settle = new Thread(() -> {
            try {
                Thread.sleep(SETTLE_DELAY_MS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            client.execute(() -> {
                try {
                    ItemStack after = client.player.getMainHandItem();
                    String afterId = itemIdOf(after);
                    int before = data.get("held_count_before").getAsInt();
                    data.addProperty("held_item_after", afterId);
                    data.addProperty("held_count_after", after.getCount());
                    data.addProperty("held_count_delta",
                        afterId.equals(heldItemBefore) ? after.getCount() - before : 0);
                    data.addProperty("settled", true);
                } catch (Exception e) {
                    data.addProperty("held_count_after", -1);
                    data.addProperty("settled", false);
                    data.addProperty("settle_error", String.valueOf(e.getMessage()));
                }
                data.addProperty("settle_delay_ms", SETTLE_DELAY_MS);
                future.complete(CommandResult.success(data));
            });
        }, "mcbaratone-hand-settle");
        settle.setDaemon(true);
        settle.start();
    }

    private CompletableFuture<CommandResult> handleLookAtEntity(JsonObject params, Minecraft client) {
        return executeOnMainThread(client, () -> {
            if (client.player == null) {
                return CommandResult.error("Player not available");
            }

            Entity target = null;
            if (params.has("entity_id")) {
                int entityId = params.get("entity_id").getAsInt();
                target = client.level.getEntity(entityId);
            } else if (params.has("entity_type")) {
                // Find nearest entity of type
                String entityType = params.get("entity_type").getAsString();
                target = findNearestEntityOfType(client, entityType, 16.0); // Use larger range for look_at
            }

            if (target == null) {
                return CommandResult.error("Target entity not found");
            }

            // Look at the entity
            lookAtEntity(client, target);

            JsonObject data = new JsonObject();
            data.addProperty("target_entity_id", target.getId());
            data.addProperty("entity_type", BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
            Vec3 pos = new Vec3(target.getX(), target.getY(), target.getZ());
            data.addProperty("target_x", pos.x);
            data.addProperty("target_y", pos.y);
            data.addProperty("target_z", pos.z);

            return CommandResult.success(data);
        });
    }

    private CompletableFuture<CommandResult> handleDetectInteractions(JsonObject params, Minecraft client) {
        return executeOnMainThread(client, () -> {
            if (client.player == null || client.level == null) {
                return CommandResult.error("Player or world not available");
            }

            double maxDistance = params.has("max_distance") ? params.get("max_distance").getAsDouble() : 16.0;

            JsonArray interactions = new JsonArray();
            for (Entity entity : client.level.entitiesForRendering()) {
                // entitiesForRendering() includes the local player; reporting "you can
                // attack yourself" as the nearest interaction is pure noise.
                if (entity == client.player) {
                    continue;
                }
                double distance = new Vec3(client.player.getX(), client.player.getY(), client.player.getZ())
                        .distanceTo(new Vec3(entity.getX(), entity.getY(), entity.getZ()));
                if (distance <= maxDistance) {
                    for (InteractionType type : InteractionType.values()) {
                        InteractionValidation validation = validateInteraction(client, entity,
                                type.name().toLowerCase(), maxDistance);
                        if (validation.valid) {
                            JsonObject interaction = new JsonObject();
                            interaction.addProperty("entity_id", entity.getId());
                            interaction.addProperty("entity_type",
                                    BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                            interaction.addProperty("interaction_type", type.name());
                            interaction.addProperty("distance", distance);
                            interactions.add(interaction);
                        }
                    }
                }
            }

            JsonObject data = new JsonObject();
            data.add("available_interactions", interactions);
            return CommandResult.success(data);
        });
    }

    private String checkInventoryRequirements(Minecraft client, String action) {
        if (client.player == null || client.player.getInventory() == null) {
            return "Player inventory not available";
        }

        switch (action) {
            case "shear":
                // Check for shears
                for (int i = 0; i < client.player.getInventory().getContainerSize(); i++) {
                    var item = client.player.getInventory().getItem(i);
                    if (!item.isEmpty()
                            && BuiltInRegistries.ITEM.getKey(item.getItem()).toString().equals("minecraft:shears")) {
                        return null; // Found shears
                    }
                }
                return "Shears required in inventory to shear sheep";
            case "milk":
                // Check for empty bucket
                for (int i = 0; i < client.player.getInventory().getContainerSize(); i++) {
                    var item = client.player.getInventory().getItem(i);
                    if (!item.isEmpty()
                            && BuiltInRegistries.ITEM.getKey(item.getItem()).toString().equals("minecraft:bucket")) {
                        return null; // Found bucket
                    }
                }
                return "Empty bucket required in inventory to milk cows";
            case "tame":
            // attack / trade need no item either; without these the helper fell into
            // default and rejected the interaction with "Unknown action for inventory
            // check" even though nothing was actually required.
            case "attack":
            case "trade":
                // No special items required for taming
                return null;
            default:
                return "Unknown action for inventory check";
        }
    }

    private InteractionValidation validateInteraction(
            Minecraft client, Entity target, String action, double maxDistance) {
        Vec3 playerPos = new Vec3(client.player.getX(), client.player.getY(), client.player.getZ());
        Vec3 targetPos = new Vec3(target.getX(), target.getY(), target.getZ());
        double distance = playerPos.distanceTo(targetPos);

        // Distance check. This used to be a hard-coded 6.0, which silently
        // overrode the caller's max_distance: detect with max_distance=64 still
        // only ever saw entities within 6 blocks, and reported the player's own
        // entity as the only reachable interaction. Callers own the limit now.
        if (distance > maxDistance) {
            return InteractionValidation.error("Entity too far: " + String.format("%.2f", distance)
                    + " blocks (max " + String.format("%.2f", maxDistance) + ")");
        }

        // Action-specific validation
        try {
            InteractionType type = InteractionType.valueOf(action.toUpperCase());

            switch (type) {
                case ATTACK:
                    // Any entity can be attacked
                    return InteractionValidation.success(distance, InteractionType.ATTACK);

                case TAME:
                    if (!(target instanceof TamableAnimal)) {
                        return InteractionValidation.error(
                                "Entity cannot be tamed: " + BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
                    }
                    TamableAnimal tameable = (TamableAnimal) target;
                    if (tameable.isTame()) {
                        return InteractionValidation.error("Entity is already tamed");
                    }
                    String entityTypeId = BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString();
                    if (!entityTypeId.equals("minecraft:wolf") &&
                            !entityTypeId.equals("minecraft:cat") &&
                            !entityTypeId.equals("minecraft:parrot")) {
                        return InteractionValidation.error("Only wolves, cats, and parrots can be tamed");
                    }
                    return InteractionValidation.success(distance, InteractionType.TAME);

                case SHEAR:
                    if (!(target instanceof Sheep)) {
                        return InteractionValidation.error("Entity cannot be sheared: "
                                + BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
                    }
                    Sheep sheep = (Sheep) target;
                    if (sheep.isSheared()) {
                        return InteractionValidation.error("Sheep is already sheared");
                    }
                    return InteractionValidation.success(distance, InteractionType.SHEAR);

                case MILK:
                    if (!(target instanceof Cow) && !(target instanceof MushroomCow)) {
                        return InteractionValidation.error("Entity cannot be milked: "
                                + BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
                    }
                    return InteractionValidation.success(distance, InteractionType.MILK);

                case TRADE:
                    // AbstractVillager is the shared parent of Villager and
                    // WanderingTrader -- they are siblings, not parent/child, so the
                    // original `instanceof Villager` check silently excluded wandering
                    // traders. Verified against the 26.2 merged jar:
                    //   ...npc.villager.Villager             extends ...npc.villager.AbstractVillager
                    //   ...npc.wanderingtrader.WanderingTrader extends ...npc.villager.AbstractVillager
                    if (!(target instanceof AbstractVillager)) {
                        return InteractionValidation.error("Entity cannot be traded with: "
                                + BuiltInRegistries.ENTITY_TYPE.getKey(target.getType()).toString());
                    }
                    return InteractionValidation.success(distance, InteractionType.TRADE);

                case LOOK_AT:
                    return InteractionValidation.success(distance, InteractionType.LOOK_AT);

                case INTERACT:
                    return InteractionValidation.success(distance, InteractionType.INTERACT);
            }
        } catch (IllegalArgumentException e) {
            return InteractionValidation.error("Unknown interaction type: " + action);
        }

        return InteractionValidation.error("Unsupported interaction: " + action);
    }

    private boolean performInteraction(Minecraft client, Entity target, InteractionType type) {
        try {
            switch (type) {
                case ATTACK:
                    client.gameMode.attack(client.player, target);
                    return true;

                case TAME:
                    if (target instanceof Animal) {
                        // Attempt to tame by interacting
                        interactWithEntity(client, target, InteractionHand.MAIN_HAND);
                        return true;
                    }
                    break;

                case TRADE:
                    // No instanceof Villager guard: WanderingTrader is not a Villager
                    // subclass, so that check made trading with a wandering trader
                    // impossible. validateInteraction() already restricted this to
                    // villagers / wandering traders.
                    interactWithEntity(client, target, InteractionHand.MAIN_HAND);
                    return true;

                case SHEAR:
                case MILK:
                    // Report what the game actually said instead of an unconditional
                    // true. ensureRequiredItemInHand() has already put the right item
                    // in hand, so a false here means the interaction was really
                    // rejected rather than merely unverifiable.
                    return interactWithEntity(
                        client, target, InteractionHand.MAIN_HAND).consumesAction();

                case LOOK_AT:
                    lookAtEntity(client, target);
                    return true;

                case INTERACT:
                    return interactWithEntity(
                        client, target, InteractionHand.MAIN_HAND).consumesAction();
            }
        } catch (Exception e) {
            LOGGER.error("Interaction failed", e);
        }
        return false;
    }

    private InteractionResult interactWithEntity(
            Minecraft client, Entity target, InteractionHand hand) {
        EntityHitResult hitResult = new EntityHitResult(target, target.getBoundingBox().getCenter());
        return client.gameMode.interact(client.player, target, hitResult, hand);
    }

    private void lookAtEntity(Minecraft client, Entity target) {
        Vec3 targetPos = new Vec3(target.getX(), target.getY(), target.getZ());
        double dx = targetPos.x - client.player.getX();
        double dy = (targetPos.y + target.getBbHeight() / 2)
                - (client.player.getY() + client.player.getEyeHeight(client.player.getPose()));
        double dz = targetPos.z - client.player.getZ();

        double dist = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) (Mth.atan2(dz, dx) * (180.0 / Math.PI)) - 90.0f;
        float pitch = (float) -(Mth.atan2(dy, dist) * (180.0 / Math.PI));

        client.player.setYRot(yaw);
        client.player.setXRot(pitch);
    }

    private Entity findNearestEntityOfType(Minecraft client, String entityType, double maxDistance) {
        Entity nearest = null;
        double minDistance = Double.MAX_VALUE;

        for (Entity entity : client.level.entitiesForRendering()) {
            String typeId = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
            if (typeId.equals(entityType) || typeId.endsWith(":" + entityType)) {
                double distance = new Vec3(client.player.getX(), client.player.getY(), client.player.getZ())
                        .distanceTo(new Vec3(entity.getX(), entity.getY(), entity.getZ()));
                if (distance <= maxDistance && distance < minDistance) {
                    minDistance = distance;
                    nearest = entity;
                }
            }
        }

        return nearest;
    }

    private double distanceToPlayer(Minecraft client, Entity target) {
        return new Vec3(client.player.getX(), client.player.getY(), client.player.getZ())
            .distanceTo(new Vec3(target.getX(), target.getY(), target.getZ()));
    }

    private static String itemIdOf(ItemStack stack) {
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }

    /**
     * Select a hotbar slot the same way select_slot does, so the server sees
     * the change too.
     */
    private void selectHotbarSlot(Minecraft client, int slot) {
        client.player.getInventory().selected = slot;
        if (client.player.connection != null) {
            client.player.connection.send(new ServerboundSetCarriedItemPacket(slot));
        }
    }

    /**
     * Make sure the given item is held, selecting a hotbar slot that has it.
     * Returns null once the item is in hand, or an error message.
     *
     * Only slots 0-8 can be held; moving an item out of the main inventory
     * would need a container-click round-trip, so that case is reported rather
     * than silently failing.
     */
    private String ensureItemInHand(Minecraft client, String itemId, String label) {
        if (client.player == null) {
            return "Player not available";
        }
        if (itemIdOf(client.player.getMainHandItem()).equals(itemId)) {
            return null;
        }
        var inventory = client.player.getInventory();
        for (int slot = 0; slot < 9; slot++) {
            if (itemIdOf(inventory.getItem(slot)).equals(itemId)) {
                selectHotbarSlot(client, slot);
                return null;
            }
        }
        for (int slot = 9; slot < inventory.getContainerSize(); slot++) {
            if (itemIdOf(inventory.getItem(slot)).equals(itemId)) {
                return label + " is in the inventory but not in the hotbar (slots 0-8);"
                    + " move it to the hotbar first";
            }
        }
        return label + " required in inventory";
    }

    /**
     * Hold food this animal accepts, picking the first matching hotbar slot.
     * Returns null when something edible is held, or an error message.
     */
    private String ensureFoodInHand(Minecraft client, Animal animal) {
        if (client.player == null) {
            return "Player not available";
        }
        if (animal.isFood(client.player.getMainHandItem())) {
            return null;
        }
        var inventory = client.player.getInventory();
        for (int slot = 0; slot < 9; slot++) {
            if (animal.isFood(inventory.getItem(slot))) {
                selectHotbarSlot(client, slot);
                return null;
            }
        }
        for (int slot = 9; slot < inventory.getContainerSize(); slot++) {
            if (animal.isFood(inventory.getItem(slot))) {
                return "Food for this animal is in the inventory but not in the hotbar"
                    + " (slots 0-8); move it to the hotbar first";
            }
        }
        return "No food for this animal in the inventory (held "
            + itemIdOf(client.player.getMainHandItem()) + ")";
    }

    private String ensureRequiredItemInHand(Minecraft client, InteractionType type) {
        switch (type) {
            case MILK:
                return ensureItemInHand(client, "minecraft:bucket", "Empty bucket");
            case SHEAR:
                return ensureItemInHand(client, "minecraft:shears", "Shears");
            default:
                return null;
        }
    }
}
