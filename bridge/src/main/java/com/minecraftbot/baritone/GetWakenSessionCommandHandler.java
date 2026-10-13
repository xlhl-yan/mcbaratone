package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import net.minecraft.client.Minecraft;

/**
 * 返回游戏侧记录的「该唤醒哪个 DSH 会话」。
 *
 * <p>DSH 的 dsh-mc-chat-watch 插件用它取 sessionId —— 玩家在游戏里敲
 * {@code !waken <id>} 就能改，不必去动 DSH 的配置文件。
 */
public class GetWakenSessionCommandHandler extends AbstractCommandHandler {

    @Override
    public String getCommandName() {
        return "get_waken_session";
    }

    @Override
    protected CommandResult execute(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        String sessionId = WakenSessionStore.read();
        JsonObject data = new JsonObject();
        data.addProperty("session_id", sessionId);
        data.addProperty("configured", !sessionId.isEmpty());
        return CommandResult.success(data);
    }
}
