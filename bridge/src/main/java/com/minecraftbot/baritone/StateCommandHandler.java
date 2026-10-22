package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.PlayerInfo;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.client.server.IntegratedServer;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.damagesource.DamageSource;
import net.minecraft.world.entity.projectile.Projectile;
import net.minecraft.world.phys.AABB;

/**
 * Command handler for state queries: get_state, get_entities, and combat snapshots.
 */
public class StateCommandHandler extends AsyncCommandHandler {

    @Override
    public String getCommandName() {
        return "state";
    }

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client, IBaritone baritone,
            Socket clientSocket) {
        // Determine action from explicit 'action' param or check for entity-specific
        // params
        String action = "get"; // default

        if (params.has("action")) {
            action = params.get("action").getAsString();
        } else if (params.has("radius") && !params.has("position")) {
            // If 'radius' is specified but not 'position', it's likely get_entities
            action = "entities";
        }

        switch (action) {
            case "get":
                // Capture the entire snapshot on one client tick. Mutable
                // inventory, effects, connection, screen and world fields must
                // not be mixed across a network worker and render thread.
                return executeOnMainThread(
                    client,
                    () -> handleGetState(client, baritone));
            case "entities":
                return CompletableFuture.completedFuture(
                    handleGetEntities(client, params));
            case "combat_snapshot":
                return CompletableFuture.completedFuture(
                    handleCombatSnapshot(client, baritone, params));
            default:
                return CompletableFuture.completedFuture(
                    CommandResult.error("Unknown state action: " + action));
        }
    }

    private CommandResult handleGetState(Minecraft client, IBaritone baritone) {
        // Read most state directly - these are volatile or immutable and safe to read
        // off-thread
        try {
            if (client.player == null) {
                return CommandResult.error("Player not available");
            }

            if (client.level == null) {
                return CommandResult.error("World not available");
            }

            LocalPlayer player = client.player;

            // Position (volatile double fields, safe to read)
            JsonObject position = new JsonObject();
            position.addProperty("x", player.getX());
            position.addProperty("y", player.getY());
            position.addProperty("z", player.getZ());
            position.addProperty("yaw", player.getYRot());
            position.addProperty("pitch", player.getXRot());

            // Block position (BlockPos is computed from position)
            var blockPos = player.blockPosition();
            JsonObject blockPosition = new JsonObject();
            blockPosition.addProperty("x", blockPos.getX());
            blockPosition.addProperty("y", blockPos.getY());
            blockPosition.addProperty("z", blockPos.getZ());

            // Health and status (float/int fields, volatile enough for our purposes)
            JsonObject data = new JsonObject();
            data.add("position", position);
            data.add("block_position", blockPosition);
            data.addProperty("health", player.getHealth());
            data.addProperty("max_health", player.getMaxHealth());
            data.addProperty("food_level", player.getFoodData().getFoodLevel());
            data.addProperty("air_supply", player.getAirSupply());
            data.addProperty("max_air_supply", player.getMaxAirSupply());
            data.addProperty("eyes_in_water", player.isEyeInFluid(net.minecraft.tags.FluidTags.WATER));
            data.addProperty("on_fire", player.isOnFire());
            data.addProperty("fall_distance", player.fallDistance);
            data.add("last_damage_source", lastDamageSourceSnapshot(player));
            data.add("navigation", NavigationLifecycleTracker.getInstance().snapshot());
            data.addProperty("saturation", player.getFoodData().getSaturationLevel());
            data.addProperty("experience_level", player.experienceLevel);
            data.addProperty("experience_total", player.totalExperience);
            data.addProperty("is_dead", player.isDeadOrDying());
            data.addProperty("entity_id", player.getId());
            data.addProperty("entity_uuid", player.getStringUUID());
            data.addProperty("player_name", player.getName().getString());
            data.addProperty("automation_profile", "non_op_client");
            data.addProperty("server_authority", false);
            data.addProperty("rcon_required", false);
            if (client.gameMode != null) {
                data.addProperty("game_mode", client.gameMode.getPlayerMode().getName());
            }

            JsonArray onlinePlayers = new JsonArray();
            if (client.getConnection() != null) {
                for (PlayerInfo playerInfo : client.getConnection().getOnlinePlayers()) {
                    onlinePlayers.add(playerInfo.getProfile().name());
                }
            }
            data.add("online_players", onlinePlayers);
            data.addProperty("snapshot_tick", client.level.getGameTime());
            data.addProperty("observed_at_ms", System.currentTimeMillis());

            // Player Flags
            data.addProperty("is_sprinting", player.isSprinting());
            data.addProperty("is_sneaking", player.isShiftKeyDown());
            data.addProperty("is_on_ground", player.onGround());
            data.addProperty("armor_points", player.getArmorValue());
            int armorCount = 0;
            for (int slot = 36; slot < 40; slot++) {
                if (!player.getInventory().getItem(slot).isEmpty()) armorCount++;
            }
            data.addProperty("armor_count", armorCount);
            data.addProperty("main_hand", BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString());
            data.addProperty("off_hand", BuiltInRegistries.ITEM.getKey(player.getOffhandItem().getItem()).toString());
            data.addProperty("is_using_item", player.isUsingItem());
            data.addProperty("is_blocking", player.isBlocking());
            data.addProperty("attack_cooldown", player.getAttackStrengthScale(0.0f));

            // Active Effects
            JsonArray effects = new JsonArray();
            player.getActiveEffects().forEach(effect -> {
                JsonObject eff = new JsonObject();
                // Fix: Handle RegistryEntry if needed, or check mappings.
                // In 1.21, getEffectType() returns RegistryEntry<StatusEffect>.
                // We need to call .value() to get the StatusEffect, or use getId() on the
                // entry.
                eff.addProperty("id", BuiltInRegistries.MOB_EFFECT.getKey(effect.getEffect().value()).toString());
                eff.addProperty("duration", effect.getDuration());
                eff.addProperty("amplifier", effect.getAmplifier());
                effects.add(eff);
            });
            data.add("effects", effects);

            // Velocity
            JsonObject velocity = new JsonObject();
            velocity.addProperty("x", player.getDeltaMovement().x);
            velocity.addProperty("y", player.getDeltaMovement().y);
            velocity.addProperty("z", player.getDeltaMovement().z);
            data.add("velocity", velocity);

            // Baritone status - wrap in try/catch as it could throw
            try {
                boolean isPathing = baritone.getPathingBehavior().isPathing();
                data.addProperty("is_pathing", isPathing);

                // is_pathing alone cannot distinguish "walking a route" from
                // "holding a goal it never found a path to". Baritone reports
                // true for both, so a wedged bot looks identical to a working
                // one -- A1Bot sat frozen for 15+ hours with is_pathing true, a
                // live GoalBlock, and zero movement, and nothing in the wire
                // protocol contradicted it. getPath() is null when no route
                // exists, which is the missing bit. This was already computed
                // by BaritoneAPIBridge.handleGetPathInfo and never routed
                // anywhere, so no caller could reach it.
                data.addProperty(
                    "path_available", baritone.getPathingBehavior().getPath() != null);
                data.addProperty(
                    "path_calculating",
                    baritone.getPathingBehavior().isPathing()
                        && baritone.getPathingBehavior().getPath() == null);

                if (isPathing && baritone.getPathingBehavior().getGoal() != null) {
                    data.addProperty("pathing_goal", true);
                    data.addProperty(
                        "pathing_goal_description",
                        baritone.getPathingBehavior().getGoal().toString());
                }
            } catch (Exception e) {
                logger.debug("Could not get pathing status", e);
                data.addProperty("is_pathing", false);
            }

            // World info (dimension key is immutable, time is volatile)
            data.addProperty("dimension", client.level.dimension().identifier().toString());
            data.addProperty("difficulty", client.level.getDifficulty().getSerializedName());
            data.addProperty("world_time", client.level.getOverworldClockTime());
            String biomeId = client.level.getBiome(blockPos).unwrapKey()
                .map(key -> key.identifier().toString())
                .orElse("unknown");
            data.addProperty("biome", biomeId);

            JsonObject worldIdentity = new JsonObject();
            worldIdentity.addProperty("version", 1);
            worldIdentity.addProperty("dimension_context", client.level.dimension().identifier().toString());

            // Add world seed for reset detection (only available on integrated server)
            if (client.getSingleplayerServer() != null) {
                IntegratedServer server = client.getSingleplayerServer();
                ServerLevel world = server.overworld();
                if (world != null) {
                    data.addProperty("world_seed", world.getSeed());
                    worldIdentity.addProperty("seed", world.getSeed());
                }
                worldIdentity.addProperty("world_name", server.getWorldData().getLevelName());
                worldIdentity.addProperty("scope", "singleplayer");
            } else if (client.getCurrentServer() != null) {
                worldIdentity.addProperty("server_address", client.getCurrentServer().ip);
                worldIdentity.addProperty("scope", "multiplayer");
            }
            data.add("world_identity", worldIdentity);

            // Screen info - may be slightly stale but acceptable
            if (client.gui.screen() != null) {
                data.addProperty("screen", client.gui.screen().getClass().getSimpleName());
                data.addProperty("has_gui", true);
            } else {
                data.addProperty("screen", "none");
                data.addProperty("has_gui", false);
            }

            return CommandResult.success(data);
        } catch (Exception e) {
            return CommandResult.error("Failed to get state: " + e.getMessage());
        }
    }

    /**
     * The player's last recorded damage, if any. Live A1 deaths repeatedly
     * showed sustained health loss with no attributable cause because
     * nothing recorded a source; ClientTickHandler already tracks the same
     * fields on a per-tick DAMAGE event, but that stream is separate from
     * this polled snapshot and was not what every state sample carried.
     */
    JsonObject lastDamageSourceSnapshot(LocalPlayer player) {
        JsonObject snapshot = new JsonObject();
        DamageSource source = player.getLastDamageSource();
        snapshot.addProperty("available", source != null);
        if (source == null) {
            return snapshot;
        }
        snapshot.addProperty("type", source.getMsgId());
        Entity attacker = source.getEntity();
        Entity directSource = source.getDirectEntity();
        if (attacker != null) {
            snapshot.addProperty("attacker_id", attacker.getId());
            snapshot.addProperty("attacker_uuid", attacker.getStringUUID());
            snapshot.addProperty("attacker_type", safeEntityType(attacker));
            // 与 ClientTickHandler 的 DAMAGE 事件保持一致：直接带名字，
            // 让消费方不用再反查一次 get_entities。getDisplayName() 可能为 null。
            var attackerName = attacker.getDisplayName();
            if (attackerName != null) snapshot.addProperty("attacker_name", attackerName.getString());
        }
        if (directSource != null && directSource != attacker) {
            snapshot.addProperty("direct_source_id", directSource.getId());
            snapshot.addProperty("direct_source_uuid", directSource.getStringUUID());
            snapshot.addProperty("direct_source_type", safeEntityType(directSource));
            var sourceName = directSource.getDisplayName();
            if (sourceName != null) snapshot.addProperty("direct_source_name", sourceName.getString());
        }
        snapshot.addProperty("is_projectile", directSource != null && directSource != attacker);
        return snapshot;
    }

    private CommandResult handleGetEntities(Minecraft client, JsonObject params) {
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<JsonObject> dataRef = new AtomicReference<>();
        AtomicReference<String> errorRef = new AtomicReference<>();

        int radius = params.has("radius") ? params.get("radius").getAsInt() : 64;

        client.execute(() -> {
            try {
                if (client.level == null || client.player == null) {
                    errorRef.set("World or player not available");
                    return;
                }

                LocalPlayer player = client.player;
                AABB box = new AABB(
                        player.getX() - radius, player.getY() - radius, player.getZ() - radius,
                        player.getX() + radius, player.getY() + radius, player.getZ() + radius);

                var entities = client.level.getEntities(null, box);
                JsonArray entityList = new JsonArray();
                JsonArray serializationErrors = new JsonArray();
                int skippedEntities = 0;

                for (Entity entity : entities) {
                    if (entity.distanceTo(player) > radius)
                        continue;

                    try {
                        JsonObject entityData = serializeEntity(entity, player);
                        entityList.add(entityData);
                    } catch (Exception e) {
                        skippedEntities++;
                        JsonObject error = new JsonObject();
                        error.addProperty("id", entity.getId());
                        error.addProperty("type", safeEntityType(entity));
                        error.addProperty("error", e.getClass().getSimpleName() + ": " + e.getMessage());
                        serializationErrors.add(error);
                        logger.warn("Skipping entity {} during state serialization", entity.getId(), e);
                    }
                }

                JsonObject data = new JsonObject();
                data.add("entities", entityList);
                data.addProperty("count", entityList.size());
                data.addProperty("radius", radius);
                data.addProperty("skipped_count", skippedEntities);
                if (!serializationErrors.isEmpty()) {
                    data.add("serialization_errors", serializationErrors);
                }
                dataRef.set(data);
            } catch (Exception e) {
                errorRef.set("Failed to get entities: " + e.getMessage());
            } finally {
                latch.countDown();
            }
        });

        try {
            if (!latch.await(5, TimeUnit.SECONDS)) {
                return CommandResult.error("Timeout waiting for entity data");
            }
        } catch (InterruptedException e) {
            return CommandResult.error("Interrupted while waiting for entity data");
        }

        if (errorRef.get() != null) {
            return CommandResult.error(errorRef.get());
        }

        return CommandResult.success(dataRef.get());
    }

    /**
     * Capture player readiness and nearby entities in one client-thread task so
     * combat policy never combines observations from different game ticks.
     */
    private CommandResult handleCombatSnapshot(Minecraft client, IBaritone baritone, JsonObject params) {
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<CommandResult> resultRef = new AtomicReference<>();
        int radius = Math.max(1, Math.min(64, params.has("radius") ? params.get("radius").getAsInt() : 16));

        client.execute(() -> {
            try {
                if (client.level == null || client.player == null) {
                    resultRef.set(CommandResult.error("World or player not available"));
                    return;
                }

                CommandResult stateResult = handleGetState(client, baritone);
                if (!stateResult.isSuccess()) {
                    resultRef.set(stateResult);
                    return;
                }

                LocalPlayer player = client.player;
                AABB box = player.getBoundingBox().inflate(radius);
                JsonArray entityList = new JsonArray();
                JsonArray serializationErrors = new JsonArray();
                int skippedEntities = 0;
                for (Entity entity : client.level.getEntities(player, box)) {
                    if (entity.distanceTo(player) > radius) continue;
                    try {
                        entityList.add(serializeEntity(entity, player));
                    } catch (Exception e) {
                        skippedEntities++;
                        JsonObject error = new JsonObject();
                        error.addProperty("id", entity.getId());
                        error.addProperty("type", safeEntityType(entity));
                        error.addProperty("error", e.getClass().getSimpleName() + ": " + e.getMessage());
                        serializationErrors.add(error);
                    }
                }

                JsonObject data = new JsonObject();
                data.addProperty("snapshot_version", 1);
                data.addProperty("tick", client.level.getGameTime());
                data.addProperty("radius", radius);
                data.add("player", stateResult.getData());
                data.add("entities", entityList);
                data.addProperty("count", entityList.size());
                data.addProperty("skipped_count", skippedEntities);
                if (!serializationErrors.isEmpty()) data.add("serialization_errors", serializationErrors);
                resultRef.set(CommandResult.success(data));
            } catch (Exception e) {
                resultRef.set(CommandResult.error("Failed to get combat snapshot: " + e.getMessage()));
            } finally {
                latch.countDown();
            }
        });

        try {
            if (!latch.await(5, TimeUnit.SECONDS)) {
                return CommandResult.error("Timeout waiting for combat snapshot");
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return CommandResult.error("Interrupted while waiting for combat snapshot");
        }
        return resultRef.get() != null ? resultRef.get() : CommandResult.error("Combat snapshot unavailable");
    }

    private JsonObject serializeEntity(Entity entity, LocalPlayer player) {
        JsonObject entityData = new JsonObject();
        entityData.addProperty("id", entity.getId());
        entityData.addProperty("uuid", entity.getStringUUID());
        entityData.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
        entityData.addProperty("name", entity.getDisplayName().getString());
        entityData.addProperty("distance", entity.distanceTo(player));

        JsonObject entityVelocity = new JsonObject();
        entityVelocity.addProperty("x", entity.getDeltaMovement().x);
        entityVelocity.addProperty("y", entity.getDeltaMovement().y);
        entityVelocity.addProperty("z", entity.getDeltaMovement().z);
        entityData.add("velocity", entityVelocity);

        JsonObject position = new JsonObject();
        position.addProperty("x", entity.getX());
        position.addProperty("y", entity.getY());
        position.addProperty("z", entity.getZ());
        entityData.add("position", position);

        boolean isLiving = entity instanceof LivingEntity;
        entityData.addProperty("is_living", isLiving);
        if (isLiving) {
            LivingEntity living = (LivingEntity) entity;
            entityData.addProperty("health", living.getHealth());
            entityData.addProperty("max_health", living.getMaxHealth());
            entityData.addProperty("age", living.isBaby() ? -1 : 0);
            entityData.addProperty("is_baby", living.isBaby());
        }

        if (entity instanceof Mob mob) {
            Entity target = mob.getTarget();
            if (target != null) {
                entityData.addProperty("target_id", target.getId());
                entityData.addProperty("target_uuid", target.getStringUUID());
                entityData.addProperty("target_type", safeEntityType(target));
            }
            entityData.addProperty("is_aggressive", target == player);
            entityData.addProperty("can_see_player", mob.hasLineOfSight(player));
        }

        if (entity instanceof Projectile projectile && projectile.getOwner() != null) {
            Entity owner = projectile.getOwner();
            entityData.addProperty("owner_id", owner.getId());
            entityData.addProperty("owner_type", safeEntityType(owner));
        }

        if (entity instanceof net.minecraft.world.entity.TamableAnimal tameable) {
            entityData.addProperty("is_tamed", tameable.isTame());
            if (tameable.getOwner() != null) {
                entityData.addProperty("owner_uuid", tameable.getOwner().getUUID().toString());
            }
        }

        if (entity instanceof net.minecraft.world.entity.npc.villager.Villager villager) {
            var villagerData = villager.getVillagerData();
            String profession = villagerData.profession().unwrapKey()
                .map(key -> key.identifier().toString())
                .orElse("unknown");
            entityData.addProperty("profession", profession);
            entityData.addProperty("level", villagerData.level());
            // getOffers() is server-only: on a client entity it throws
            // IllegalStateException("Cannot load Villager offers on the
            // client"). Unguarded, that escaped serializeEntity and the caller
            // dropped the WHOLE villager, so every villager within the scan
            // radius was invisible to the controller and no trading or
            // breeding objective could ever see one. Live 2026-08-17 A1Bot sat
            // beside a village returning 2 bats and a player from
            // get_entities, with both villagers in serialization_errors, and
            // logged the exception 1640 times in six hours.
            //
            // Offers legitimately readable only while a merchant menu is open;
            // GetScreenCommandHandler reads them there. Absent that, omit the
            // field rather than lose the entity.
            try {
                entityData.addProperty("offers_count", villager.getOffers().size());
            } catch (Exception e) {
                entityData.addProperty("offers_available", false);
            }
        }

        if (entity instanceof net.minecraft.world.entity.projectile.FishingHook bobber) {
            boolean hasCatch = bobber.getHookedIn() != null || bobber.isOpenWaterFishing();
            entityData.addProperty("has_catch", hasCatch);
        }
        return entityData;
    }

    private String safeEntityType(Entity entity) {
        try {
            return BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
        } catch (Exception ignored) {
            return "unknown";
        }
    }
}
