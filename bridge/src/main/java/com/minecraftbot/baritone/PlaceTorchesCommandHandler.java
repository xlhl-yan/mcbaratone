package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.InteractionResult;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

/**
 * 在指定位置插火把。
 *
 * <p>⚠️ 这个 handler 以前是**假成功**的典型：它手工拼一个 {@link BlockHitResult}、不转头、
 * 也不做任何后置校验，最后把 {@code result.consumesAction()}（**客户端本地预测**）当成
 * "插好了"报出去 —— 2026-10-16 实测：目标在 65 格外照样回 {@code placed:true}，而方块
 * 根本没出现、火把也没消耗。
 *
 * <p>现在照 {@link UseBucketCommandHandler} 的模子补齐四件事：**距离检查**、**真实射线**、
 * **转头**、**{@link ObservedMutation} 后置校验**（回读那一格真的是火把才算成功）。
 *
 * <p>火把只从**快捷栏 0-8** 找，找不到就报错 —— Python 侧的 `place_torches` 会先把火把从
 * 背包任意位置弄到手上，所以这里通常不必操心。
 */
public class PlaceTorchesCommandHandler extends AsyncCommandHandler {

    @Override
    public String getCommandName() {
        return "place_torches";
    }

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client,
            IBaritone baritone, Socket clientSocket) {
        if (!params.has("x") || !params.has("y") || !params.has("z")) {
            return CompletableFuture.completedFuture(
                CommandResult.error("Coordinates required for torch placement"));
        }

        int x = params.get("x").getAsInt();
        int y = params.get("y").getAsInt();
        int z = params.get("z").getAsInt();

        return ObservedMutation.run(client::execute, () -> client.level.getGameTime(), () -> {
            if (client.player == null || client.level == null || client.gameMode == null) {
                return CommandResult.error("Player/World not available");
            }

            // ① 火把得在快捷栏（Python 侧一般已经弄到手上，这里是兜底）
            int slot = -1;
            for (int i = 0; i < 9; i++) {
                ItemStack stack = client.player.getInventory().getItem(i);
                if (stack.getItem() == Items.TORCH || stack.getItem() == Items.SOUL_TORCH) {
                    slot = i;
                    break;
                }
            }
            if (slot == -1) {
                return CommandResult.error("No torches in hotbar");
            }
            if (client.player.getInventory().selected != slot) {
                client.player.getInventory().selected = slot;
                if (client.player.connection != null) {
                    client.player.connection.send(new ServerboundSetCarriedItemPacket(slot));
                }
            }

            BlockPos targetPos = new BlockPos(x, y, z);
            if (!client.level.getBlockState(targetPos).canBeReplaced()) {
                return CommandResult.error("Target position is already occupied: " + targetPos);
            }

            // ② 找一个能依附的实心邻居（沿用原来的顺序：DOWN/UP/NORTH/...）
            Direction placeFace = Direction.UP;
            BlockPos placeAgainst = targetPos.below();
            boolean foundNeighbor = false;
            for (Direction dir : Direction.values()) {
                BlockPos adjacent = targetPos.relative(dir);
                if (client.level.getBlockState(adjacent).canBeReplaced()) continue;
                placeAgainst = adjacent;
                placeFace = dir.getOpposite();
                foundNeighbor = true;
                break;
            }
            if (!foundNeighbor && client.level.getBlockState(targetPos.below()).canBeReplaced()) {
                return CommandResult.error("No solid block found to place against at " + targetPos);
            }

            // ③ 瞄准支撑面往里一点点 —— 照 use_bucket：射线必须真打在这个面上
            Vec3 eye = client.player.getEyePosition();
            Vec3 aim = Vec3.atCenterOf(placeAgainst).add(
                -placeFace.getStepX() * 0.499,
                -placeFace.getStepY() * 0.499,
                -placeFace.getStepZ() * 0.499);

            // ④ 距离检查 —— 以前缺的就是这一步，够不着也报成功
            double reach = client.player.blockInteractionRange();
            if (eye.distanceTo(aim) > reach) {
                return CommandResult.error(String.format(
                    "Torch target out of reach (%.1f blocks, limit %.1f)",
                    eye.distanceTo(aim), reach));
            }

            // ⑤ 真实射线：确认视线真能打到那个面（而不是自己造一个 hit 出来）
            BlockHitResult hit = client.level.clip(new ClipContext(eye, aim,
                ClipContext.Block.OUTLINE, ClipContext.Fluid.NONE, client.player));
            if (hit.getType() != HitResult.Type.BLOCK
                || !hit.getBlockPos().equals(placeAgainst)
                || hit.getDirection() != placeFace) {
                return CommandResult.error(
                    "Torch placement ray obstructed (not aimed at the support face)");
            }

            // ⑥ 转头看向它
            Vec3 delta = aim.subtract(eye);
            client.player.setYRot((float) (Math.atan2(delta.z, delta.x) * 180 / Math.PI - 90));
            client.player.setXRot((float) (-Math.atan2(delta.y,
                Math.sqrt(delta.x * delta.x + delta.z * delta.z)) * 180 / Math.PI));

            InteractionResult result = client.gameMode.useItemOn(
                client.player, InteractionHand.MAIN_HAND, hit);
            client.player.swing(InteractionHand.MAIN_HAND);

            JsonObject data = new JsonObject();
            data.addProperty("placed", result.consumesAction()); // 只表示"客户端认为该生效"
            data.addProperty("status", result.toString());
            data.addProperty("x", x);
            data.addProperty("y", y);
            data.addProperty("z", z);
            if (!result.consumesAction()) {
                return CommandResult.error("Placement failed: " + result.toString());
            }
            return CommandResult.success(data);
        }, data -> {
            // ⑦ 后置校验：回读那一格，真变成火把才算数 —— 假成功的解药
            BlockState now = client.level.getBlockState(new BlockPos(x, y, z));
            data.addProperty("observed_block",
                BuiltInRegistries.BLOCK.getKey(now.getBlock()).toString());
            return isTorch(now);
        }, "torch_placed_verified");
    }

    /**
     * 那格是不是火把。抽成静态方法是为了能单测 —— 它当场就抓到一个真 bug：**墙上火把是
     * 另一个方块**（{@code WALL_TORCH}），只认 {@code TORCH} 会把"插在墙上"误判成失败。
     */
    static boolean isTorch(BlockState state) {
        return state.is(Blocks.TORCH)
            || state.is(Blocks.WALL_TORCH)
            || state.is(Blocks.SOUL_TORCH)
            || state.is(Blocks.SOUL_WALL_TORCH);
    }
}
