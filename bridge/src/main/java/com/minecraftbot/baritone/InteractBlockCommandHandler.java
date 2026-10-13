package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

/**
 * Command handler for interact_block: Simulates a right-click on a block.
 *
 * <p><b>2026-10-13 重写瞄准逻辑。</b>老实现只朝**方块中心**打一条射线，对"薄方块"
 * （门 / 按钮 / 拉杆 / 火把 / 半砖…）必然失手：这些东西的碰撞形状只占方块的一角，
 * 瞄准中心的射线会从旁边那大半格空气里穿过去，于是报
 * "Target is not visible on a real block ray"。
 *
 * <p>实测证据：一扇关门（facing=west，门板只占 x∈[5.0,5.19]），站在**东侧**怎么
 * 都打不中，站到**西侧**才成 —— 而"站哪边"本来不该是调用方需要考虑的事。
 *
 * <p>现在的做法：把方块内部**密集采样**成一批候选瞄准点（先试中心和六个面中心，
 * 再铺 5×5×5 的网格），逐个做几何射线，取第一个真正命中目标方块的点。整块方块
 * 第一个候选就中，薄方块也能在网格里找到落在形状上的那个点。
 */
public class InteractBlockCommandHandler extends AbstractBaseCommandHandler {

    /** 每个轴向上采样的份数（越大越稳，但射线次数按三次方增长；5 已足够包容薄形状）。 */
    private static final int SAMPLES_PER_AXIS = 5;

    @Override
    public CompletableFuture<CommandResult> handle(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        try {
            return executeOnMainThread(client, () -> {
                if (client.player == null || client.level == null || client.gameMode == null) {
                    return CommandResult.error("Player not available");
                }

                int x = params.get("x").getAsInt();
                int y = params.get("y").getAsInt();
                int z = params.get("z").getAsInt();
                String handStr = params.has("hand") ? params.get("hand").getAsString().toUpperCase() : "MAIN_HAND";

                InteractionHand hand = "OFF_HAND".equals(handStr) ? InteractionHand.OFF_HAND : InteractionHand.MAIN_HAND;
                BlockPos pos = new BlockPos(x, y, z);

                Vec3 eye = client.player.getEyePosition();
                BlockHitResult hitResult = findReachableHit(client, pos, eye);
                if (hitResult == null) {
                    return CommandResult.error("Target is not visible on a real block ray; fluid use requires use_item");
                }

                // 转头看向真正打中的那个点，让动作看起来自然（也给服务器一个合理的朝向）
                Vec3 aim = hitResult.getLocation();
                Vec3 delta = aim.subtract(eye);
                client.player.setYRot((float) (Math.atan2(delta.z, delta.x) * 180 / Math.PI - 90));
                client.player.setXRot((float) (-Math.atan2(delta.y,
                        Math.sqrt(delta.x * delta.x + delta.z * delta.z)) * 180 / Math.PI));

                var actionResult = client.gameMode.useItemOn(client.player, hand, hitResult);
                client.player.swing(hand);

                JsonObject data = new JsonObject();
                data.addProperty("interacted", actionResult.consumesAction());
                data.addProperty("accepted", actionResult.consumesAction());
                data.addProperty("action_status", actionResult.consumesAction() ? "accepted" : "rejected");
                data.addProperty("postcondition_verified", false);
                data.addProperty("x", x);
                data.addProperty("y", y);
                data.addProperty("z", z);
                data.addProperty("result", actionResult.toString());
                data.addProperty("hit_x", hitResult.getBlockPos().getX());
                data.addProperty("hit_y", hitResult.getBlockPos().getY());
                data.addProperty("hit_z", hitResult.getBlockPos().getZ());
                // 打中的具体点位 —— 排查薄方块问题时一眼能看出瞄到哪儿了
                data.addProperty("aim_x", aim.x);
                data.addProperty("aim_y", aim.y);
                data.addProperty("aim_z", aim.z);
                return new CommandResult(actionResult.consumesAction(), data,
                        actionResult.consumesAction() ? null : "Interaction rejected");
            });
        } catch (Exception e) {
            return CompletableFuture.completedFuture(CommandResult.error("Interact failed: " + e.getMessage()));
        }
    }

    /**
     * 找出一个"从眼睛出发、真的能打中目标方块"的瞄准点。
     *
     * <p>先试直觉上的那几个点（方块中心、六个面的中心），再铺一层均匀网格兜底。
     * 返回 null 表示怎么试都打不中（比如被别的方块挡住、或者太远）。
     */
    private BlockHitResult findReachableHit(Minecraft client, BlockPos pos, Vec3 eye) {
        double reach = client.player.blockInteractionRange();
        for (Vec3 aim : candidateAimPoints(pos)) {
            if (eye.distanceTo(aim) > reach) {
                continue;
            }
            ClipContext context = new ClipContext(eye, aim, ClipContext.Block.OUTLINE,
                    ClipContext.Fluid.NONE, client.player);
            BlockHitResult hit = client.level.clip(context);
            if (hit.getType() != HitResult.Type.MISS && hit.getBlockPos().equals(pos)) {
                return hit;
            }
        }
        return null;
    }

    /** 候选瞄准点：中心 → 六面中心 → 均匀网格。 */
    private List<Vec3> candidateAimPoints(BlockPos pos) {
        List<Vec3> points = new ArrayList<>();
        double cx = pos.getX() + 0.5;
        double cy = pos.getY() + 0.5;
        double cz = pos.getZ() + 0.5;

        points.add(new Vec3(cx, cy, cz));
        points.add(new Vec3(cx, cy, pos.getZ() + 0.03));
        points.add(new Vec3(cx, cy, pos.getZ() + 0.97));
        points.add(new Vec3(pos.getX() + 0.03, cy, cz));
        points.add(new Vec3(pos.getX() + 0.97, cy, cz));
        points.add(new Vec3(cx, pos.getY() + 0.03, cz));
        points.add(new Vec3(cx, pos.getY() + 0.97, cz));

        for (int i = 0; i < SAMPLES_PER_AXIS; i++) {
            for (int j = 0; j < SAMPLES_PER_AXIS; j++) {
                for (int k = 0; k < SAMPLES_PER_AXIS; k++) {
                    points.add(new Vec3(
                            pos.getX() + (i + 0.5) / SAMPLES_PER_AXIS,
                            pos.getY() + (j + 0.5) / SAMPLES_PER_AXIS,
                            pos.getZ() + (k + 0.5) / SAMPLES_PER_AXIS));
                }
            }
        }
        return points;
    }

    @Override
    public String getCommandName() {
        return "interact_block";
    }
}
