package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import baritone.api.process.ICustomGoalProcess;
import baritone.api.pathing.goals.Goal;
import com.google.gson.JsonObject;
import java.util.concurrent.ExecutionException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

/**
 * Focused tests for the task_memory command.
 *
 * <p>只验"存 -> 恢复 -> 清空"这条状态机，Baritone 用 mock 代替 —— 真正
 * {@code setGoalAndPath} 的效果属于实机验证范围。
 */
class TaskMemoryCommandHandlerTest {

    private TaskMemoryCommandHandler handler;
    private IBaritone baritone;
    private ICustomGoalProcess goalProcess;

    @BeforeEach
    void setUp() throws Exception {
        handler = new TaskMemoryCommandHandler();
        baritone = mock(IBaritone.class);
        goalProcess = mock(ICustomGoalProcess.class);
        when(baritone.getCustomGoalProcess()).thenReturn(goalProcess);
        // 每个用例从干净状态开始
        run("clear");
    }

    private JsonObject params(String action) {
        JsonObject p = new JsonObject();
        if (action != null) {
            p.addProperty("action", action);
        }
        return p;
    }

    private JsonObject run(String action) throws InterruptedException, ExecutionException {
        CommandResult result = handler.handle(params(action), null, baritone, null).get();
        assertTrue(result.isSuccess(), "应当成功: " + result.getErrorMessage());
        return result.getData();
    }

    @Test
    void saveWithNoGoalReportsNothingToSave() throws Exception {
        when(goalProcess.getGoal()).thenReturn(null);
        JsonObject data = run("save");
        assertFalse(data.get("has_saved").getAsBoolean());
        assertTrue(data.get("reason").getAsString().contains("没有"));
    }

    @Test
    void saveThenQueryThenRestore() throws Exception {
        Goal goal = mock(Goal.class);
        when(goal.toString()).thenReturn("GoalBlock{x=100,y=64,z=200}");
        when(goalProcess.getGoal()).thenReturn(goal);

        JsonObject saved = run("save");
        assertTrue(saved.get("has_saved").getAsBoolean());
        assertEquals("GoalBlock{x=100,y=64,z=200}", saved.get("goal").getAsString());

        JsonObject queried = run("query");
        assertTrue(queried.get("has_saved").getAsBoolean());

        JsonObject restored = run("restore");
        assertTrue(restored.get("restored").getAsBoolean());
        // 存的必须是**同一个对象**，不是字符串重建出来的
        verify(goalProcess).setGoalAndPath(goal);
    }

    @Test
    void restoreClearsTheSavedTaskSoItCannotFireTwice() throws Exception {
        Goal goal = mock(Goal.class);
        when(goal.toString()).thenReturn("GoalBlock{x=1,y=2,z=3}");
        when(goalProcess.getGoal()).thenReturn(goal);

        run("save");
        run("restore");

        JsonObject again = run("restore");
        assertFalse(again.get("restored").getAsBoolean(), "第二次恢复应该拒绝");
        assertTrue(again.get("reason").getAsString().contains("没有保存"));
    }

    @Test
    void restoreWithoutSaveReportsReason() throws Exception {
        JsonObject data = run("restore");
        assertFalse(data.get("restored").getAsBoolean());
        assertEquals("没有保存过任务", data.get("reason").getAsString());
    }

    @Test
    void clearDropsTheSavedTask() throws Exception {
        Goal goal = mock(Goal.class);
        when(goal.toString()).thenReturn("GoalBlock{x=9,y=9,z=9}");
        when(goalProcess.getGoal()).thenReturn(goal);

        run("save");
        run("clear");
        assertFalse(run("query").get("has_saved").getAsBoolean());
    }

    @Test
    void defaultsToQueryWhenNoActionGiven() throws Exception {
        JsonObject data = run(null);
        assertEquals("query", data.get("action").getAsString());
    }

    @Test
    void isUsableWithoutAPlayer() {
        assertFalse(handler.requiresPlayer());
        assertTrue(handler.isOfflineSafe());
        assertEquals("task_memory", handler.getCommandName());
    }
}
