package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.world.level.block.Block;

/**
 * Command handler for mining operations.
 */
public class MineCommandHandler extends AsyncCommandHandler {

    @Override
    public String getCommandName() {
        return "mine";
    }

    @Override
    public CompletableFuture<CommandResult> execute(
            JsonObject params,
            Minecraft client,
            IBaritone baritone,
            Socket clientSocket) {
        int count = params.has("count") ? params.get("count").getAsInt() :
                   (params.has("quantity") ? params.get("quantity").getAsInt() : 0);

        if (params.has("x") && params.has("y") && params.has("z")) {
            return handleMiningAtPos(params, client, baritone, count);
        } else if (params.has("block_type")) {
            return handleSingleBlock(params, client, baritone, count);
        } else if (params.has("blocks")) {
            return handleMultipleBlocks(params, client, baritone, count);
        } else {
            return CompletableFuture.completedFuture(CommandResult.error(
                "Missing block_type, blocks, or x,y,z parameters"));
        }
    }

    private CompletableFuture<CommandResult> handleMiningAtPos(
            JsonObject params, Minecraft client, IBaritone baritone, int count) {
        int x = params.get("x").getAsInt();
        int y = params.get("y").getAsInt();
        int z = params.get("z").getAsInt();

        // 方块类型优先看 block_type；没给就等主线程里从坐标处现读一格。
        // 纯参数校验留在主线程回调**之外** —— 校验失败不该消耗一个 goal_id。
        Block requested = null;
        if (params.has("block_type")) {
            String blockId = params.get("block_type").getAsString();
            Identifier id = Identifier.parse(blockId);
            if (!BuiltInRegistries.BLOCK.containsKey(id)) {
                return CompletableFuture.completedFuture(
                    CommandResult.error("Unknown block: " + blockId));
            }
            requested = BuiltInRegistries.BLOCK.getValue(id);
        }
        final Block requestedBlock = requested;
        net.minecraft.core.BlockPos pos = new net.minecraft.core.BlockPos(x, y, z);
        
        return executeOnMainThread(client, () -> {
            long goalId = NavigationLifecycleTracker.getInstance().beginGeneration(baritone);

            // count > 0 才是真的「挖 N 个」。旧实现不看数量，一律走
            // builder.clearArea(pos, pos) —— 那是"清掉这一格"，Builder 为了够到那格会把
            // 沿途挡路的方块一路挖穿（实测从 y=63 钻到 y=55、顺手多挖 54 个圆石），
            // 而 `mine stone 3 17 58 -6` 里的 "3" 被整个吞掉（本方法签名里根本没有 count）。
            if (count > 0) {
                Block block = requestedBlock;
                if (block == null) {
                    if (client.level == null) {
                        return CommandResult.error("World not loaded");
                    }
                    net.minecraft.world.level.block.state.BlockState state =
                        client.level.getBlockState(pos);
                    if (state.isAir()) {
                        return CommandResult.error(
                            "Nothing to mine at " + x + ", " + y + ", " + z);
                    }
                    block = state.getBlock();
                }
                baritone.getMineProcess().mine(count, block);
                JsonObject data = appliedData(client);
                data.addProperty("goal_id", goalId);
                data.addProperty("x", x);
                data.addProperty("y", y);
                data.addProperty("z", z);
                data.addProperty("block", BuiltInRegistries.BLOCK.getKey(block).toString());
                data.addProperty("count", count);
                data.addProperty("mode", "mine_process");
                return CommandResult.success(data);
            }

            // 没给数量：保持旧语义 —— 清掉这一格。
            baritone.getBuilderProcess().clearArea(pos, pos);
            JsonObject data = appliedData(client);
            data.addProperty("goal_id", goalId);
            data.addProperty("x", x);
            data.addProperty("y", y);
            data.addProperty("z", z);
            data.addProperty("mode", "clear_area");
            return CommandResult.success(data);
        });
    }

    private CompletableFuture<CommandResult> handleSingleBlock(
            JsonObject params, Minecraft client, IBaritone baritone, int count) {
        String blockId = params.get("block_type").getAsString();
        Identifier id = Identifier.parse(blockId);

        if (!BuiltInRegistries.BLOCK.containsKey(id)) {
            return CompletableFuture.completedFuture(
                CommandResult.error("Unknown block: " + blockId));
        }

        Block block = BuiltInRegistries.BLOCK.getValue(id);
        return executeOnMainThread(client, () -> {
            long goalId = NavigationLifecycleTracker.getInstance().beginGeneration(baritone);
            baritone.getMineProcess().mine(count, block);
            JsonObject data = appliedData(client);
            data.addProperty("goal_id", goalId);
            data.addProperty("block", blockId);
            data.addProperty("count", count);
            return CommandResult.success(data);
        });
    }

    private CompletableFuture<CommandResult> handleMultipleBlocks(
            JsonObject params, Minecraft client, IBaritone baritone, int count) {
        JsonArray blocksArray = params.getAsJsonArray("blocks");
        if (blocksArray.isEmpty()) {
            return CompletableFuture.completedFuture(
                CommandResult.error("No block IDs provided"));
        }

        List<Block> lookup = new ArrayList<>();
        for (JsonElement element : blocksArray) {
            if (!element.isJsonPrimitive()) continue;
            String blockId = element.getAsString();
            Identifier id = Identifier.parse(blockId);
            if (BuiltInRegistries.BLOCK.containsKey(id)) {
                lookup.add(BuiltInRegistries.BLOCK.getValue(id));
            }
        }

        if (lookup.isEmpty()) {
            return CompletableFuture.completedFuture(
                CommandResult.error("No valid blocks found to mine"));
        }

        Block[] blockArray = lookup.toArray(new Block[0]);
        return executeOnMainThread(client, () -> {
            long goalId = NavigationLifecycleTracker.getInstance().beginGeneration(baritone);
            baritone.getMineProcess().mine(count, blockArray);
            JsonObject data = appliedData(client);
            data.addProperty("goal_id", goalId);
            data.addProperty("blocks_found", lookup.size());
            data.addProperty("count", count);
            return CommandResult.success(data);
        });
    }

    private JsonObject appliedData(Minecraft client) {
        JsonObject data = new JsonObject();
        data.addProperty("started", true);
        data.addProperty("accepted", true);
        data.addProperty("applied", true);
        if (client.level != null) {
            data.addProperty("applied_tick", client.level.getGameTime());
        }
        return data;
    }
}
