package com.minecraftbot.baritone;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import baritone.api.IBaritone;
import baritone.api.process.IBuilderProcess;
import baritone.api.process.IMineProcess;
import com.google.gson.JsonObject;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ClientLevel;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

/**
 * 「mine 带坐标时数量被吞掉」的回归测试。
 *
 * <p>现场：{@code mine stone 3 17 58 -6} 想挖 3 个石头，实际从 y=63 一路挖穿到 y=55、
 * 多攒 54 个圆石。原因有两条：坐标分支排在 {@code block_type} 前面，而
 * {@code handleMiningAtPos} 走的是 {@code builder.clearArea(pos, pos)}（清掉那一格，
 * Builder 为了够到它会把沿途挡路的方块全挖穿）、签名里压根没有 count。
 *
 * <p>修完之后的约定：带坐标 + {@code count > 0} = 真的「挖 N 个」（MineProcess 自己寻路、
 * 挖够就停），方块类型优先看 {@code block_type}、没给就从坐标处现读一格；
 * 不带 count 才退回旧的「清掉这一格」。
 */
class MineCommandHandlerTest {

    @BeforeAll
    static void bootstrap() {
        // Blocks / BuiltInRegistries 必须先 bootstrap，否则取注册表就炸。
        net.minecraft.SharedConstants.tryDetectVersion();
        net.minecraft.server.Bootstrap.bootStrap();
    }

    @BeforeEach
    void resetTrackerBefore() {
        NavigationLifecycleTracker.getInstance().resetForTests();
    }

    @AfterEach
    void resetTrackerAfter() {
        NavigationLifecycleTracker.getInstance().resetForTests();
    }

    @Test
    void mineAtPositionHonoursCountInsteadOfClearingArea() throws Exception {
        Minecraft client = mock(Minecraft.class);
        IBaritone baritone = mock(IBaritone.class);
        IMineProcess mineProcess = mock(IMineProcess.class);
        IBuilderProcess builderProcess = mock(IBuilderProcess.class);
        when(baritone.getMineProcess()).thenReturn(mineProcess);
        when(baritone.getBuilderProcess()).thenReturn(builderProcess);

        JsonObject params = coords(17, 58, -6);
        params.addProperty("block_type", "minecraft:stone");
        params.addProperty("count", 3);

        CompletableFuture<CommandResult> result =
            new MineCommandHandler().execute(params, client, baritone, null);

        // 派发之前什么都不该发生：得等主线程任务真的跑起来
        verify(mineProcess, never()).mine(anyInt(), any(Block[].class));
        verify(builderProcess, never()).clearArea(any(), any());
        assertFalse(result.isDone());

        runScheduledTask(client);

        CommandResult done = result.get();
        assertTrue(done.isSuccess());
        assertEquals("mine_process", done.getData().get("mode").getAsString());
        assertEquals(3, done.getData().get("count").getAsInt());
        assertEquals("minecraft:stone", done.getData().get("block").getAsString());
        verify(mineProcess).mine(eq(3), any(Block[].class));
        // 旧实现这条路上走 clearArea(1×1)，顺手把沿途方块全挖了 —— 不允许再出现
        verify(builderProcess, never()).clearArea(any(), any());
    }

    @Test
    void mineAtPositionAcceptsQuantityAsWellAsCount() throws Exception {
        Minecraft client = mock(Minecraft.class);
        IBaritone baritone = mock(IBaritone.class);
        IMineProcess mineProcess = mock(IMineProcess.class);
        when(baritone.getMineProcess()).thenReturn(mineProcess);

        JsonObject params = coords(3, 30, 9);
        params.addProperty("block_type", "minecraft:diamond_ore");
        params.addProperty("quantity", 4);

        CompletableFuture<CommandResult> result =
            new MineCommandHandler().execute(params, client, baritone, null);
        runScheduledTask(client);

        CommandResult done = result.get();
        assertTrue(done.isSuccess());
        assertEquals(4, done.getData().get("count").getAsInt());
        verify(mineProcess).mine(eq(4), any(Block[].class));
    }

    @Test
    void mineAtPositionWithoutBlockTypeSamplesTheBlockThere() throws Exception {
        Minecraft client = mock(Minecraft.class);
        ClientLevel level = mock(ClientLevel.class);
        when(level.getBlockState(any(BlockPos.class))).thenReturn(Blocks.STONE.defaultBlockState());
        TestUtils.setField(client, "level", level);

        IBaritone baritone = mock(IBaritone.class);
        IMineProcess mineProcess = mock(IMineProcess.class);
        when(baritone.getMineProcess()).thenReturn(mineProcess);

        JsonObject params = coords(5, 40, -3);
        params.addProperty("count", 2);

        CompletableFuture<CommandResult> result =
            new MineCommandHandler().execute(params, client, baritone, null);
        runScheduledTask(client);

        CommandResult done = result.get();
        assertTrue(done.isSuccess());
        assertEquals("mine_process", done.getData().get("mode").getAsString());
        assertEquals(2, done.getData().get("count").getAsInt());
        assertEquals("minecraft:stone", done.getData().get("block").getAsString());
        verify(mineProcess).mine(eq(2), any(Block[].class));
    }

    @Test
    void mineAtPositionWithoutCountKeepsClearAreaBehaviour() throws Exception {
        Minecraft client = mock(Minecraft.class);
        IBaritone baritone = mock(IBaritone.class);
        IBuilderProcess builderProcess = mock(IBuilderProcess.class);
        when(baritone.getBuilderProcess()).thenReturn(builderProcess);

        CompletableFuture<CommandResult> result =
            new MineCommandHandler().execute(coords(1, 64, 2), client, baritone, null);

        assertFalse(result.isDone());
        runScheduledTask(client);

        CommandResult done = result.get();
        assertTrue(done.isSuccess());
        assertEquals("clear_area", done.getData().get("mode").getAsString());
        verify(builderProcess).clearArea(any(), any());
    }

    @Test
    void unknownBlockFailsBeforeTouchingTheMainThread() throws Exception {
        Minecraft client = mock(Minecraft.class);
        IBaritone baritone = mock(IBaritone.class);

        JsonObject params = coords(1, 64, 2);
        params.addProperty("block_type", "minecraft:not_a_real_block");
        params.addProperty("count", 3);

        CommandResult done = new MineCommandHandler().execute(params, client, baritone, null).get();

        assertFalse(done.isSuccess());
        assertTrue(done.getErrorMessage().contains("Unknown block"));
        verify(client, never()).execute(any(Runnable.class));
    }

    @Test
    void miningAirAtPositionReportsInsteadOfMiningAnything() throws Exception {
        Minecraft client = mock(Minecraft.class);
        ClientLevel level = mock(ClientLevel.class);
        when(level.getBlockState(any(BlockPos.class))).thenReturn(Blocks.AIR.defaultBlockState());
        TestUtils.setField(client, "level", level);

        IBaritone baritone = mock(IBaritone.class);
        IMineProcess mineProcess = mock(IMineProcess.class);
        when(baritone.getMineProcess()).thenReturn(mineProcess);

        JsonObject params = coords(0, 70, 0);
        params.addProperty("count", 1);

        CommandResult done = runAndGet(client, params, baritone);

        assertFalse(done.isSuccess());
        assertTrue(done.getErrorMessage().contains("Nothing to mine"));
        verify(mineProcess, never()).mine(anyInt(), any(Block[].class));
    }

    private static JsonObject coords(int x, int y, int z) {
        JsonObject params = new JsonObject();
        params.addProperty("x", x);
        params.addProperty("y", y);
        params.addProperty("z", z);
        return params;
    }

    private static CommandResult runAndGet(Minecraft client, JsonObject params, IBaritone baritone)
            throws Exception {
        CompletableFuture<CommandResult> result =
            new MineCommandHandler().execute(params, client, baritone, null);
        if (!result.isDone()) {
            runScheduledTask(client);
        }
        return result.get();
    }

    private static void runScheduledTask(Minecraft client) {
        ArgumentCaptor<Runnable> task = ArgumentCaptor.forClass(Runnable.class);
        verify(client).execute(task.capture());
        task.getValue().run();
    }
}
