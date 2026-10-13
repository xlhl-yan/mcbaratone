package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;

/**
 * 只读：报告准星（玩家视线）当前指着什么。
 *
 * <p>背景：{@code client.hitResult} 这个数据一直存在，但以前只有 {@code use_item}
 * 的返回里顺带带出来 —— 而调 use_item 会**真的触发一次右键**。所以想"看一眼"
 * 就得"动一下手"。加了这个 handler 之后就能纯看不动手。
 *
 * <p>返回：视线类型（block / entity / miss）、命中点坐标、距离、以及命中方块或
 * 实体的详细信息。顺带带上 yaw/pitch 和交互距离，便于排查"够不够得着"。
 */
public class GetLookTargetCommandHandler extends AsyncCommandHandler {

    @Override
    public String getCommandName() {
        return "get_look_target";
    }

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client,
            IBaritone baritone, Socket clientSocket) {
        return executeOnMainThread(client, () -> {
            if (client.player == null || client.level == null) {
                return CommandResult.error("Player or world not available");
            }

            JsonObject data = new JsonObject();
            data.addProperty("yaw", client.player.getYRot());
            data.addProperty("pitch", client.player.getXRot());
            data.addProperty("eye_x", client.player.getEyePosition().x);
            data.addProperty("eye_y", client.player.getEyePosition().y);
            data.addProperty("eye_z", client.player.getEyePosition().z);
            data.addProperty("reach", client.player.blockInteractionRange());

            HitResult hit = client.hitResult;
            if (hit == null) {
                data.addProperty("type", "none");
                return CommandResult.success(data);
            }

            data.addProperty("type", hit.getType().name().toLowerCase(java.util.Locale.ROOT));
            data.addProperty("hit_x", hit.getLocation().x);
            data.addProperty("hit_y", hit.getLocation().y);
            data.addProperty("hit_z", hit.getLocation().z);
            data.addProperty("distance",
                    client.player.getEyePosition().distanceTo(hit.getLocation()));

            if (hit instanceof EntityHitResult entityHit) {
                Entity entity = entityHit.getEntity();
                data.addProperty("entity_id", entity.getId());
                data.addProperty("entity_type",
                        BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                data.addProperty("entity_name", entity.getName().getString());
                data.addProperty("entity_distance", client.player.distanceTo(entity));
            } else if (hit instanceof BlockHitResult blockHit) {
                BlockPos pos = blockHit.getBlockPos();
                data.addProperty("block_x", pos.getX());
                data.addProperty("block_y", pos.getY());
                data.addProperty("block_z", pos.getZ());
                data.addProperty("side",
                        blockHit.getDirection().name().toLowerCase(java.util.Locale.ROOT));
                data.addProperty("block",
                        BuiltInRegistries.BLOCK.getKey(
                                client.level.getBlockState(pos).getBlock()).toString());
                // 整条 block state 也带上 —— 判断"门开没开""拉杆朝哪儿"这类问题很有用
                data.addProperty("block_state", client.level.getBlockState(pos).toString());
            }
            return CommandResult.success(data);
        });
    }
}
