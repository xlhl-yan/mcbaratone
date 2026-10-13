package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import net.minecraft.client.Minecraft;

/**
 * 由 DSH 侧写入「该唤醒哪个 DSH 会话」（等价于玩家在游戏里敲 {@code !waken <id>}）。
 *
 * <p>留一份可编程的入口，是为了让驱动脚本能在不进游戏的情况下改设置，
 * 也方便排查「到底写上没有」。
 */
public class SetWakenSessionCommandHandler extends AbstractCommandHandler {

    @Override
    public String getCommandName() {
        return "set_waken_session";
    }

    @Override
    protected CommandResult execute(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        String sessionId = params.has("session_id") ? params.get("session_id").getAsString().trim() : "";
        boolean written = WakenSessionStore.write(sessionId);
        JsonObject data = new JsonObject();
        data.addProperty("written", written);
        data.addProperty("session_id", sessionId);
        if (!written) {
            return CommandResult.error("failed to write " + WakenSessionStore.class.getSimpleName());
        }
        return CommandResult.success(data);
    }
}
