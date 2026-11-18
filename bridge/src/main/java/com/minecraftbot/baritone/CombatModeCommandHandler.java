package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;

/**
 * Handler for the combat_mode command.
 *
 * <p>两种模式：
 * <ul>
 *   <li>{@code manual}（默认）—— 受伤只上报给 agent，要不要还手、用不用武器由它决定。
 *       这是项目原本的设计：只给事实，不替 AI 下结论。</li>
 *   <li>{@code auto} —— agent 把"挨打后怎么反应"这段**条件反射**授权给插件执行：
 *       怪物攻击时自动切武器反击；血 ≤ 50% 自动进食；血 &lt; 30% 自动撤离。</li>
 * </ul>
 *
 * <p>为什么需要 auto：agent 的决策链是「事件 → 唤醒 → 模型思考 → 调工具」，
 * 实测这一段要几秒到十几秒，而 MC 里僵尸咬死一个裸装角色只要五秒。
 * 这不是模型判断能力的问题，是**物理延迟**的问题 —— 思考可以慢，反射不能。
 *
 * <p>状态放在 bridge 而不是插件里：插件和 Python MCP server 是两条互不相通的链路，
 * 只有 bridge 是双方都能读写的共同点。
 */
public class CombatModeCommandHandler implements CommandHandler {

    public static final String MODE_MANUAL = "manual";
    public static final String MODE_AUTO = "auto";

    /** 自动模式下的血量阈值（百分比，0-100）。 */
    public static final int EAT_BELOW_PERCENT = 50;
    public static final int FLEE_BELOW_PERCENT = 30;

    /** volatile：插件线程写、别的线程读。 */
    private static volatile String mode = MODE_MANUAL;

    public static String currentMode() {
        return mode;
    }

    @Override
    public CompletableFuture<CommandResult> handle(JsonObject params, Minecraft client, IBaritone baritone,
            Socket clientSocket) {
        JsonObject data = new JsonObject();

        if (params.has("mode")) {
            String want = params.get("mode").getAsString().trim().toLowerCase();
            if (!MODE_AUTO.equals(want) && !MODE_MANUAL.equals(want)) {
                return CompletableFuture.completedFuture(CommandResult.error(
                        "mode 只能是 auto 或 manual，收到: " + want));
            }
            boolean changed = !want.equals(mode);
            mode = want;
            data.addProperty("changed", changed);
        } else {
            data.addProperty("changed", false);
        }

        data.addProperty("mode", mode);
        data.addProperty("description", describe(mode));
        data.addProperty("eat_below_percent", EAT_BELOW_PERCENT);
        data.addProperty("flee_below_percent", FLEE_BELOW_PERCENT);
        return CompletableFuture.completedFuture(CommandResult.success(data));
    }

    private static String describe(String which) {
        if (MODE_AUTO.equals(which)) {
            return "自动反击：怪物攻击时自动切武器反击；血 <=" + EAT_BELOW_PERCENT
                    + "% 自动进食；血 <" + FLEE_BELOW_PERCENT + "% 自动撤离。";
        }
        return "手动：受伤只上报给你，要不要还手、用什么由你决定。";
    }

    @Override
    public String getCommandName() {
        return "combat_mode";
    }

    /** 只读写一个静态开关，玩家没加载时也应该能查能改。 */
    @Override
    public boolean requiresPlayer() {
        return false;
    }

    @Override
    public boolean isOfflineSafe() {
        return true;
    }
}
