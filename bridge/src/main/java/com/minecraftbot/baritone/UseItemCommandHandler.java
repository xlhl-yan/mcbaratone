package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;

/**
 * Handler for the use_item command.
 * Simulates a right-click (use) action, optionally held for a duration.
 * Used for eating food, using bows, interacting with items in hand, etc.
 */
public class UseItemCommandHandler extends AsyncCommandHandler {

    private static final int ACTION_SCHEDULE_TIMEOUT_SECONDS = 2;

    private static final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(runnable -> {
        Thread thread = new Thread(runnable, "bridge-use-item-release");
        thread.setDaemon(true);
        return thread;
    });

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client,
            IBaritone baritone, Socket clientSocket) {
        int durationMs = params.has("duration_ms") ? params.get("duration_ms").getAsInt() : 0;
        if (durationMs < 0) {
            return CompletableFuture.completedFuture(
                CommandResult.error("duration_ms must be non-negative"));
        }
        final InteractionHand requestedHand;
        try {
            requestedHand = resolveHand(params);
        } catch (IllegalArgumentException exception) {
            return CompletableFuture.completedFuture(
                CommandResult.error(exception.getMessage()));
        }

        return executeOnMainThread(client, () -> {
            if (client.player == null) {
                return CommandResult.error("Player not available");
            }
            if (client.gameMode == null) {
                return CommandResult.error("Game mode not available");
            }

            JsonObject hit = describeCrosshairTarget(client);
            JsonObject data = new JsonObject();
            data.add("hit", hit);
            data.addProperty("hit_type", hit.get("type").getAsString());
            ItemStack heldItem = client.player.getItemInHand(requestedHand);
            data.addProperty("held_item", BuiltInRegistries.ITEM.getKey(
                heldItem.getItem()).toString());

            InteractionResult interaction = client.gameMode.useItem(
                client.player, requestedHand);
            if (!interaction.consumesAction()) {
                return CommandResult.error(
                    "Item use was rejected for " + requestedHand.name() + ": held "
                        + BuiltInRegistries.ITEM.getKey(heldItem.getItem()));
            }
            data.addProperty("hand", requestedHand.name());
            data.addProperty("interaction_result", interaction.toString());
            data.addProperty("used", true);
            addUseState(client.player, data);

            if (durationMs > 0) {
                // Long-press items (food, bows) keep going on their own once
                // the use has started, so this only has to hold the key state
                // and release it later.
                client.options.keyUse.setDown(true);
                scheduleRelease(client, durationMs);
                data.addProperty("holding", true);
                data.addProperty("duration_ms", durationMs);
            }
            return CommandResult.success(data);
        }).completeOnTimeout(
            CommandResult.error("Timed out before item use reached the client thread"),
            ACTION_SCHEDULE_TIMEOUT_SECONDS,
            TimeUnit.SECONDS);
    }

    static InteractionHand requestedHand(JsonObject params) {
        if (params == null || !params.has("hand")) {
            return null;
        }
        String value = params.get("hand").getAsString().trim().toUpperCase();
        return switch (value) {
            case "MAIN_HAND" -> InteractionHand.MAIN_HAND;
            case "OFF_HAND" -> InteractionHand.OFF_HAND;
            default -> throw new IllegalArgumentException(
                "hand must be MAIN_HAND or OFF_HAND");
        };
    }

    /**
     * Resolve the hand to actually use, defaulting to the main hand.
     *
     * An absent {@code hand} used to fall through to a keyUse tap instead.
     * That tap never worked: Minecraft drains use presses through
     * {@code KeyMapping.consumeClick()}, and {@code setDown(true)} does not
     * raise a click, so the handler could answer "accepted" without
     * {@code startUseItem()} ever running. Anything that relied on the
     * automatic hand (filling a bucket, right-clicking a block) silently did
     * nothing.
     */
    static InteractionHand resolveHand(JsonObject params) {
        InteractionHand parsed = requestedHand(params);
        return parsed != null ? parsed : InteractionHand.MAIN_HAND;
    }

    static void addUseState(LocalPlayer player, JsonObject data) {
        boolean usingItem = player.isUsingItem();
        addUseState(
            data,
            usingItem,
            player.isBlocking(),
            usingItem ? player.getUsedItemHand() : null);
    }

    static void addUseState(JsonObject data, boolean usingItem, boolean blocking,
            InteractionHand activeHand) {
        data.addProperty("is_using_item", usingItem);
        data.addProperty("is_blocking", blocking);
        if (usingItem && activeHand != null) {
            data.addProperty("active_hand", activeHand.name());
        }
    }

    private void scheduleRelease(Minecraft client, int delayMs) {
        scheduler.schedule(
            () -> client.execute(() -> client.options.keyUse.setDown(false)),
            delayMs,
            TimeUnit.MILLISECONDS);
    }

    private JsonObject describeCrosshairTarget(Minecraft client) {
        JsonObject hitData = new JsonObject();
        HitResult hit = client.hitResult;
        if (hit == null) {
            hitData.addProperty("type", "unknown");
            return hitData;
        }

        hitData.addProperty("type", hit.getType().name().toLowerCase());
        hitData.addProperty("x", hit.getLocation().x);
        hitData.addProperty("y", hit.getLocation().y);
        hitData.addProperty("z", hit.getLocation().z);

        if (hit instanceof EntityHitResult entityHit) {
            hitData.addProperty("entity_id", entityHit.getEntity().getId());
            hitData.addProperty("entity_type", BuiltInRegistries.ENTITY_TYPE.getKey(
                entityHit.getEntity().getType()).toString());
        } else if (hit instanceof BlockHitResult blockHit) {
            hitData.addProperty("block_x", blockHit.getBlockPos().getX());
            hitData.addProperty("block_y", blockHit.getBlockPos().getY());
            hitData.addProperty("block_z", blockHit.getBlockPos().getZ());
            hitData.addProperty("side", blockHit.getDirection().name().toLowerCase());
            if (client.level != null) {
                hitData.addProperty("block", BuiltInRegistries.BLOCK.getKey(
                    client.level.getBlockState(blockHit.getBlockPos()).getBlock()).toString());
            }
        }
        return hitData;
    }

    @Override
    public String getCommandName() {
        return "use_item";
    }
}
