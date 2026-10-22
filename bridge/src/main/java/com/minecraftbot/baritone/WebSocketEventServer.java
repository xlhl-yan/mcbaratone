package com.minecraftbot.baritone;

import org.java_websocket.WebSocket;
import org.java_websocket.handshake.ClientHandshake;
import org.java_websocket.server.WebSocketServer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonArray;
import com.minecraftbot.baritone.EventManager;

import java.net.InetSocketAddress;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * WebSocket server for real-time event streaming.
 * Supports event subscription and filtering.
 */
public class WebSocketEventServer extends WebSocketServer implements EventManager.EventListener {

    private static final Logger LOGGER = LoggerFactory.getLogger("websocket-event-server");

    /**
     * 启用本服务器所需的 JVM 参数：`-Dbaritone.ws.port=<端口>`。
     *
     * **故意不设默认端口**，本类也不自己读它 —— 由 {@link BaritoneAPIBridge} 判断
     * "没配就完全不启动"。原因：两个 MC 客户端都装 bridge，一个写死的默认值必然让
     * 它们抢同一个端口，而 bind 失败只会留一条容易被忽略的 warn。把"开不开"变成
     * 一个显式动作，比让它默默撞车安全。
     */
    static final String PORT_PROPERTY = "baritone.ws.port";

    private static final Gson GSON = new Gson();

    private final EventManager eventManager;
    private final Map<WebSocket, Set<String>> clientSubscriptions = new ConcurrentHashMap<>();

    /** 端口必须显式传入（见 {@link #PORT_PROPERTY} 的说明）。 */
    public WebSocketEventServer(EventManager eventManager, int port) {
        super(new InetSocketAddress(port));
        this.eventManager = eventManager;
    }

    /** 配置完成后真正开始监听，并把自身挂到事件总线上。 */
    public void startServing() {
        start();
        eventManager.subscribe(this);
        LOGGER.info("Event WebSocket server started on port {}", getPort());
    }

    /** 停服并从事件总线下线 —— 一定要被 shutdown() 调到，否则线程会钉住 JVM。 */
    public void stopServing() {
        try {
            eventManager.unsubscribe(this);
        } catch (Throwable ignored) {
            // 下线失败不影响后续关闭
        }
        try {
            stop(1000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch (Throwable error) {
            LOGGER.warn("Error stopping event WebSocket server", error);
        }
    }

    @Override
    public void onOpen(WebSocket conn, ClientHandshake handshake) {
        LOGGER.info("WebSocket client connected: {}", conn.getRemoteSocketAddress());
        clientSubscriptions.put(conn, new HashSet<>()); // Start with no subscriptions
    }

    @Override
    public void onClose(WebSocket conn, int code, String reason, boolean remote) {
        LOGGER.info("WebSocket client disconnected: {} - {}", conn.getRemoteSocketAddress(), reason);
        clientSubscriptions.remove(conn);
    }

    @Override
    public void onMessage(WebSocket conn, String message) {
        try {
            JsonObject request = GSON.fromJson(message, JsonObject.class);
            String type = request.has("type") ? request.get("type").getAsString() : "";

            switch (type) {
                case "subscribe":
                    handleSubscribe(conn, request);
                    break;
                case "unsubscribe":
                    handleUnsubscribe(conn, request);
                    break;
                case "ping":
                    conn.send("{\"type\":\"pong\"}");
                    break;
                default:
                    conn.send("{\"error\":\"Unknown message type\"}");
                    break;
            }
        } catch (Exception e) {
            LOGGER.error("Error handling WebSocket message", e);
            conn.send("{\"error\":\"Invalid message format\"}");
        }
    }

    private void handleSubscribe(WebSocket conn, JsonObject request) {
        if (!request.has("events")) {
            conn.send("{\"error\":\"Missing events array\"}");
            return;
        }

        JsonArray eventsArray = request.getAsJsonArray("events");
        Set<String> subscriptions = clientSubscriptions.get(conn);
        if (subscriptions == null) return;

        for (int i = 0; i < eventsArray.size(); i++) {
            String eventType = eventsArray.get(i).getAsString();
            subscriptions.add(eventType);
        }

        JsonObject response = new JsonObject();
        response.addProperty("type", "subscribed");
        response.add("events", eventsArray);
        conn.send(GSON.toJson(response));
        LOGGER.info("Client {} subscribed to events: {}", conn.getRemoteSocketAddress(), subscriptions);
    }

    private void handleUnsubscribe(WebSocket conn, JsonObject request) {
        if (!request.has("events")) {
            conn.send("{\"error\":\"Missing events array\"}");
            return;
        }

        JsonArray eventsArray = request.getAsJsonArray("events");
        Set<String> subscriptions = clientSubscriptions.get(conn);
        if (subscriptions == null) return;

        for (int i = 0; i < eventsArray.size(); i++) {
            String eventType = eventsArray.get(i).getAsString();
            subscriptions.remove(eventType);
        }

        JsonObject response = new JsonObject();
        response.addProperty("type", "unsubscribed");
        response.add("events", eventsArray);
        conn.send(GSON.toJson(response));
    }

    @Override
    public void onError(WebSocket conn, Exception ex) {
        LOGGER.error("WebSocket error", ex);
        if (conn != null) {
            clientSubscriptions.remove(conn);
        }
    }

    @Override
    public void onStart() {
        LOGGER.info("Event WebSocket server listening on port {}", getPort());
    }

    /**
     * Send an event to all subscribed clients.
     */
    public void broadcastEvent(String eventType, JsonObject eventData) {
        String eventTypeStr = eventType.toLowerCase();
        JsonObject message = new JsonObject();
        message.addProperty("type", "event");
        message.addProperty("event_type", eventTypeStr);
        message.addProperty("timestamp", System.currentTimeMillis());
        message.add("data", eventData);

        String messageStr = GSON.toJson(message);

        // Compress if message is large
        if (messageStr.length() > 1024) {
            try {
                net.jpountz.lz4.LZ4Factory factory = net.jpountz.lz4.LZ4Factory.fastestInstance();
                net.jpountz.lz4.LZ4Compressor compressor = factory.fastCompressor();
                byte[] data = messageStr.getBytes(java.nio.charset.StandardCharsets.UTF_8);
                int maxCompressedLength = compressor.maxCompressedLength(data.length);
                byte[] compressed = new byte[maxCompressedLength];
                int compressedLength = compressor.compress(data, 0, data.length, compressed, 0, maxCompressedLength);
                String compressedStr = java.util.Base64.getEncoder().encodeToString(java.util.Arrays.copyOf(compressed, compressedLength));
                messageStr = "{\"compressed\":\"lz4\",\"data\":\"" + compressedStr + "\"}";
            } catch (Exception e) {
                LOGGER.warn("Compression failed, sending uncompressed", e);
            }
        }

        for (Map.Entry<WebSocket, Set<String>> entry : clientSubscriptions.entrySet()) {
            WebSocket conn = entry.getKey();
            Set<String> subs = entry.getValue();

            if (subs.isEmpty() || subs.contains(eventTypeStr) || subs.contains("*")) {
                try {
                    conn.send(messageStr);
                } catch (Exception e) {
                    LOGGER.warn("Failed to send event to client {}", conn.getRemoteSocketAddress(), e);
                }
            }
        }
    }

    /**
     * Shutdown the server.
     */
    public void shutdown() {
        try {
            this.stop();
            clientSubscriptions.clear();
            LOGGER.info("WebSocket event server stopped");
        } catch (Exception e) {
            LOGGER.error("Error stopping WebSocket server", e);
        }
    }

    @Override
    public void onEvent(EventManager.Event event) {
        try {
            // Try to parse as EventType enum
            String typeStr = event.getType();
            EventManager.EventType eventType = EventManager.EventType.valueOf(typeStr.toUpperCase());
            broadcastEvent(eventType.name().toLowerCase(), event.getData());
        } catch (IllegalArgumentException e) {
            // Not a standard event type, broadcast with string type
            LOGGER.debug("Broadcasting non-enum event type: {}", event.getType());
            broadcastEvent(event.getType(), event.getData());
        }
    }
}