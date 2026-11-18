package com.minecraftbot.baritone;

import com.google.gson.JsonObject;
import java.util.concurrent.ExecutionException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Focused tests for the combat_mode switch.
 *
 * <p>状态是 static 的，所以每个用例开头先显式设回 manual，不依赖执行顺序。
 * 不需要顾及 Minecraft 运行时 —— 这个 handler 只读写一个字符串。
 */
class CombatModeCommandHandlerTest {

    private CombatModeCommandHandler handler;

    @BeforeEach
    void setUp() throws Exception {
        handler = new CombatModeCommandHandler();
        // 状态是 static 的，必须显式设回 manual —— 只发查询（不带 mode）是改不回来的。
        handler.handle(params("manual"), null, null, null).get();
    }

    private static JsonObject params(String mode) {
        JsonObject p = new JsonObject();
        if (mode != null) {
            p.addProperty("mode", mode);
        }
        return p;
    }

    private JsonObject run(String mode) throws InterruptedException, ExecutionException {
        CommandResult result = handler.handle(params(mode), null, null, null).get();
        assertTrue(result.isSuccess(), "应当成功，实际: " + result.getErrorMessage());
        return result.getData();
    }

    @Test
    void startsInManual() {
        assertEquals("manual", CombatModeCommandHandler.currentMode());
    }

    @Test
    void queryWithoutArgumentReturnsCurrentButDoesNotChange() throws Exception {
        run("auto");
        JsonObject data = run(null);
        assertEquals("auto", data.get("mode").getAsString());
        assertFalse(data.get("changed").getAsBoolean(), "查询不该报告 changed");
    }

    @Test
    void switchesToAutoAndBack() throws Exception {
        JsonObject toAuto = run("auto");
        assertEquals("auto", toAuto.get("mode").getAsString());
        assertTrue(toAuto.get("changed").getAsBoolean());
        assertEquals("auto", CombatModeCommandHandler.currentMode());

        JsonObject toManual = run("manual");
        assertEquals("manual", toManual.get("mode").getAsString());
        assertTrue(toManual.get("changed").getAsBoolean());
        assertEquals("manual", CombatModeCommandHandler.currentMode());
    }

    @Test
    void settingSameModeReportsNoChange() throws Exception {
        run("auto");
        JsonObject again = run("auto");
        assertFalse(again.get("changed").getAsBoolean());
    }

    @Test
    void isCaseInsensitiveAndTrims() throws Exception {
        run("  AUTO  ");
        assertEquals("auto", CombatModeCommandHandler.currentMode());
    }

    @Test
    void rejectsUnknownModeAndKeepsCurrent() throws Exception {
        run("auto");
        CommandResult result = handler.handle(params("berserk"), null, null, null).get();
        assertFalse(result.isSuccess(), "非法模式应当失败");
        assertTrue(result.getErrorMessage().contains("berserk"));
        assertEquals("auto", CombatModeCommandHandler.currentMode(), "拒绝后状态不该变");
    }

    @Test
    void exposesThresholdsSoCallerKnowsWhenItActs() throws Exception {
        JsonObject data = run("auto");
        assertEquals(CombatModeCommandHandler.EAT_BELOW_PERCENT, data.get("eat_below_percent").getAsInt());
        assertEquals(CombatModeCommandHandler.FLEE_BELOW_PERCENT, data.get("flee_below_percent").getAsInt());
        assertTrue(data.get("description").getAsString().contains("自动"));
    }

    @Test
    void isUsableWithoutAPlayer() {
        assertFalse(handler.requiresPlayer(), "玩家没加载时也该能查改模式");
        assertTrue(handler.isOfflineSafe());
        assertEquals("combat_mode", handler.getCommandName());
    }
}
