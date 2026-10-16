package com.minecraftbot.baritone;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import net.minecraft.world.level.block.Blocks;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/**
 * 火把放置的**后置校验**判据 —— 纯逻辑，不碰世界。
 *
 * <p>这个判据是这次改造的重点：以前 handler 把 `result.consumesAction()`（客户端本地预测）
 * 当成"插好了"，实测在 65 格外也回 {@code placed:true}。现在改成回读那一格，这里钉住
 * "什么才算火把"。
 */
class PlaceTorchesCommandHandlerTest {

    @BeforeAll
    static void bootstrap() {
        net.minecraft.SharedConstants.tryDetectVersion();
        net.minecraft.server.Bootstrap.bootStrap();
    }

    @Test
    void isTorchAcceptsBothKinds() {
        assertTrue(PlaceTorchesCommandHandler.isTorch(Blocks.TORCH.defaultBlockState()));
        assertTrue(PlaceTorchesCommandHandler.isTorch(Blocks.SOUL_TORCH.defaultBlockState()));
        // ⚠️ 墙上火把是**另一个方块** —— 漏掉它，"插在墙上"会被后置校验误判成失败
        assertTrue(PlaceTorchesCommandHandler.isTorch(Blocks.WALL_TORCH.defaultBlockState()));
        assertTrue(PlaceTorchesCommandHandler.isTorch(Blocks.SOUL_WALL_TORCH.defaultBlockState()));
    }

    @Test
    void isTorchRejectsEverythingElse() {
        assertFalse(PlaceTorchesCommandHandler.isTorch(Blocks.STONE.defaultBlockState()));
        assertFalse(PlaceTorchesCommandHandler.isTorch(Blocks.AIR.defaultBlockState()));
        assertFalse(PlaceTorchesCommandHandler.isTorch(Blocks.CAMPFIRE.defaultBlockState()));
    }
}
