package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import net.minecraft.client.Minecraft;

/**
 * Lightweight "is the bot busy?" query for callers that only need a heartbeat.
 *
 * dsh-mc-chat-watch's idle-autonomy engine polls this every 20s (default) just to
 * decide whether the character is idle. It used to call {@code get_state}, which
 * serialises position, health, hunger, inventory, navigation, damage source and
 * world info -- roughly a thousand times a day for one boolean.
 *
 * Note on {@code pathing}: {@code isPathing()} reports true both when a route is
 * being walked AND when a goal exists but no path was ever found (see the longer
 * note in StateCommandHandler). {@code has_path} is included so a caller can tell
 * a wedged bot from a working one without falling back to get_state.
 */
public class IsIdleCommandHandler extends AbstractCommandHandler {

    @Override
    public String getCommandName() {
        return "is_idle";
    }

    @Override
    protected CommandResult execute(JsonObject params, Minecraft client,
            IBaritone baritone, Socket clientSocket) {
        if (client.player == null) {
            return CommandResult.error("Player not available");
        }

        boolean pathing = false;
        boolean hasPath = false;
        try {
            pathing = baritone.getPathingBehavior().isPathing();
            if (pathing) {
                // getPath() 为 null = 有目标但找不到路（卡住），与"正在走"要区分开
                hasPath = baritone.getPathingBehavior().getPath() != null;
            }
        } catch (Exception ignored) {
            // Baritone 未初始化 / 接口变动都不该让整个查询失败
        }

        float health = client.player.getHealth();
        JsonObject data = new JsonObject();
        data.addProperty("pathing", pathing);
        data.addProperty("has_path", hasPath);
        data.addProperty("health", health);
        data.addProperty("dead", client.player.isDeadOrDying() || health <= 0f);
        data.addProperty("food", client.player.getFoodData().getFoodLevel());
        return CommandResult.success(data);
    }
}
