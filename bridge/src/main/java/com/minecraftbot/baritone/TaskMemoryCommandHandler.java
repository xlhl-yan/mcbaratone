package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import baritone.api.pathing.goals.Goal;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;

/**
 * Handler for the {@code task_memory} command.
 *
 * <p>自动战斗要"打完接着干原来的活"（用户 2026-11-18 选的 C 方案）：
 * 进战斗前把 Baritone 正在做的任务记下来、cancel 掉，退出战斗时重新发起。
 *
 * <p><b>为什么记 Goal 对象而不是它的字符串</b>：{@code Goal.toString()} 出来是
 * {@code GoalBlock{x=100,y=64,z=200}} 这种调试表示，格式随版本变、不同类型还不一样，
 * 解析回来等于自己实现一遍 Baritone 的反序列化。这里直接拿住
 * {@code getCustomGoalProcess().getGoal()} 的引用，恢复时原样
 * {@code setGoalAndPath(saved)} —— 不解析、不失真。
 *
 * <p><b>为什么不记"原始命令"</b>：命令是 agent → Python MCP → bridge 这条链发的，
 * 插件根本不经过那条路，看不到原始命令文本。能拿到的最接近的东西就是这个 Goal。
 *
 * <p>已知局限：{@code mine} / {@code farm} 这类走 Baritone 其它 process 的任务，
 * {@code getGoal()} 可能是 null。那时 {@code has_saved} 为 false，恢复不了 ——
 * 调用方应当把这件事告诉 agent，而不是假装恢复了。
 *
 * <p>状态是 static：一个 JVM 一个游戏实例，一份就够。
 */
public class TaskMemoryCommandHandler implements CommandHandler {

    private static volatile Goal savedGoal = null;
    private static volatile long savedAt = 0L;

    /**
     * 存下当前任务。
     *
     * @param params {@code action="save"} —— 保存并返回是否存到了东西；
     *               {@code action="restore"} —— 把保存的任务重新交给 Baritone；
     *               {@code action="query"}（默认）—— 只看有没有存。
     */
    @Override
    public CompletableFuture<CommandResult> handle(JsonObject params, Minecraft client, IBaritone baritone,
            Socket clientSocket) {
        String action = params.has("action") ? params.get("action").getAsString().toLowerCase() : "query";
        JsonObject data = new JsonObject();
        data.addProperty("action", action);

        if ("save".equals(action)) {
            Goal current = baritone.getCustomGoalProcess().getGoal();
            if (current == null) {
                savedGoal = null;
                savedAt = 0L;
                data.addProperty("has_saved", false);
                data.addProperty("reason", "当前没有可保存的 Baritone 目标");
                return CompletableFuture.completedFuture(CommandResult.success(data));
            }
            savedGoal = current;
            savedAt = System.currentTimeMillis();
            data.addProperty("has_saved", true);
            data.addProperty("goal", current.toString());
            return CompletableFuture.completedFuture(CommandResult.success(data));
        }

        if ("restore".equals(action)) {
            Goal goal = savedGoal;
            if (goal == null) {
                data.addProperty("restored", false);
                data.addProperty("reason", "没有保存过任务");
                return CompletableFuture.completedFuture(CommandResult.success(data));
            }
            baritone.getCustomGoalProcess().setGoalAndPath(goal);
            data.addProperty("restored", true);
            data.addProperty("goal", goal.toString());
            data.addProperty("saved_at", savedAt);
            // 恢复后清掉：这个任务已经交回去了，再 restore 一次会重复发起
            savedGoal = null;
            savedAt = 0L;
            return CompletableFuture.completedFuture(CommandResult.success(data));
        }

        if ("clear".equals(action)) {
            savedGoal = null;
            savedAt = 0L;
            data.addProperty("has_saved", false);
            return CompletableFuture.completedFuture(CommandResult.success(data));
        }

        // query / 其它
        data.addProperty("has_saved", savedGoal != null);
        if (savedGoal != null) {
            data.addProperty("goal", savedGoal.toString());
            data.addProperty("saved_at", savedAt);
        }
        return CompletableFuture.completedFuture(CommandResult.success(data));
    }

    @Override
    public String getCommandName() {
        return "task_memory";
    }

    /** 只碰一个静态字段 + Baritone 的 goal 句柄，玩家没加载也能查。 */
    @Override
    public boolean requiresPlayer() {
        return false;
    }

    @Override
    public boolean isOfflineSafe() {
        return true;
    }
}
