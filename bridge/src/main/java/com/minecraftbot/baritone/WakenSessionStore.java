package com.minecraftbot.baritone;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import net.fabricmc.loader.api.FabricLoader;

/**
 * 存「游戏消息该唤醒哪个 DSH 会话」——由玩家在游戏里直接改，不用去翻 DSH 的配置。
 *
 * <p>存成一个纯文本文件（{@code config/mcbaratone-waken-session.txt}），内容就是一行
 * sessionId。DSH 侧的 dsh-mc-chat-watch 插件通过 bridge 的 {@code get_waken_session}
 * 命令把它读走，当作投递目标。
 *
 * <p>为什么不做成 GameRule：原版 {@code GameRules} 只有 BooleanValue / IntegerValue
 * 两种值类型，塞不下 "session-xxxxxxxx-..." 这种字符串。
 */
public final class WakenSessionStore {

    private static final String FILE_NAME = "mcbaratone-waken-session.txt";

    /** 玩家在聊天里敲这些前缀来设置。 */
    public static final String[] PREFIXES = {
        // 中文前缀最安全：不撞任何客户端命令前缀（"!" 会被 Baritone 抢走并报
        // 「未知或不完整的命令」，虽然消息还是会发出去，但刷屏很难看）。
        "唤醒会话",
        "唤醒",
        // 兼容写法，方便打不出中文时用。
        "!waken",
        "waken",
    };

    /** 敲这个值等于清空（回到「自动挑最近的会话」）。 */
    public static final String CLEAR_WORD = "clear";

    private WakenSessionStore() {
    }

    private static Path file() {
        return FabricLoader.getInstance().getConfigDir().resolve(FILE_NAME);
    }

    /** 读当前值；文件不存在或读不到就当没设置。 */
    public static String read() {
        try {
            Path path = file();
            if (!Files.exists(path)) {
                return "";
            }
            return new String(Files.readAllBytes(path), StandardCharsets.UTF_8).trim();
        } catch (IOException e) {
            return "";
        }
    }

    /** 写值；传空串表示清除。返回是否写成功。 */
    public static boolean write(String sessionId) {
        try {
            Files.write(file(), sessionId.getBytes(StandardCharsets.UTF_8));
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    /**
     * 判断一条聊天是不是设置命令，是就返回要写入的值。
     *
     * <p>返回 {@code null} = 不是这条命令；返回空串 = 玩家要求清除。
     */
    public static String matchCommand(String rawMessage) {
        if (rawMessage == null) {
            return null;
        }
        String text = stripSenderPrefix(rawMessage.trim());
        for (String prefix : PREFIXES) {
            if (text.length() <= prefix.length()) {
                continue; // 只写了前缀、后面没东西，当没写
            }
            if (!text.regionMatches(true, 0, prefix, 0, prefix.length())) {
                continue;
            }
            String argument = text.substring(prefix.length()).trim();
            if (argument.isEmpty()) {
                continue;
            }
            if (CLEAR_WORD.equalsIgnoreCase(argument)) {
                return "";
            }
            return argument;
        }
        return null;
    }

    /**
     * 聊天行在多人服上通常渲染成 {@code <玩家名> 内容}，命令解析前要先把前缀剥掉，
     * 否则 {@code <xlhl_world> !waken xxx} 会被当成不以 !waken 开头而漏掉。
     */
    private static String stripSenderPrefix(String text) {
        if (!text.startsWith("<")) {
            return text;
        }
        int close = text.indexOf("> ");
        if (close <= 0) {
            return text;
        }
        return text.substring(close + 2).trim();
    }
}
