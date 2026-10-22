package com.minecraftbot.baritone;

import com.minecraftbot.baritone.events.ClientTickHandler;
import com.minecraftbot.baritone.events.FabricEventRegistrar;

import baritone.api.BaritoneAPI;
import baritone.api.IBaritone;
import baritone.api.schematic.ISchematic;
import baritone.api.schematic.IStaticSchematic;
import baritone.api.schematic.format.ISchematicFormat;
import baritone.api.utils.BlockOptionalMeta;
import com.goebl.david.Webb;
import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonArray;
import net.fabricmc.api.ModInitializer;
import com.minecraftbot.baritone.mcp.McpServer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.Block;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientLifecycleEvents;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.*;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Baritone API Bridge for Minecraft Bot Control
 * Uses Native Baritone API for robust control.
 */
public class BaritoneAPIBridge implements ModInitializer, MissionBridgeAdapter {

    public static final Logger LOGGER = LoggerFactory.getLogger("baritone-api-bridge");
    private static final int DEFAULT_PORT = 5555;
    private static final String BRIDGE_PORT_PROPERTY = "baritone.bridge.port";
    private static final int MAX_CONNECTIONS = 10;
    private static final int SOCKET_TIMEOUT_MS = 30000; // 30 seconds
    private static final int UPLOAD_TIMEOUT_MS = 300000; // 5 minutes for uploads
    private static final int THREAD_POOL_CORE_SIZE = 5;
    private static final int THREAD_POOL_MAX_SIZE = 20;
    private static final int THREAD_POOL_KEEP_ALIVE_SECONDS = 60;
    private static final Gson GSON = new Gson();

    // Legacy network fields (to be removed after full migration)
    @Deprecated
    private ServerSocket serverSocket;
    private ExecutorService executor;
    private boolean running = false;
    private File schematicDir;
    private UploadManager uploadManager;

    // New modular network layer
    private NetworkServer networkServer;
    private ConnectionHandler connectionHandler;
    private RequestProcessor requestProcessor;

    // MCP Server (Native Java MCP implementation)
    private McpServer mcpServer;

    /**
     * 事件推送用 WebSocket 服务器（`-Dbaritone.ws.port`，默认 5559）。
     *
     * 以前只是"写好放在那儿"：端口写死 5556、和裸 TCP 撞车，于是从没被启动过。
     * 现在真正接线 —— 客户端订阅后可以收到 damage / chat / inventory_change 等事件，
     * 不必再靠每秒轮询 get_events。
     */
    private WebSocketEventServer eventWebSocketServer;

    // Modular event system
    private ClientTickHandler clientTickHandler;
    private FabricEventRegistrar fabricEventRegistrar;
    private final DeathTracker deathTracker = new DeathTracker();

    // Connection tracking
    private final Set<Socket> activeConnections = ConcurrentHashMap.newKeySet();
    private final AtomicLong lastSeq = new AtomicLong(0);

    // Event management
    private final EventManager eventManager = new EventManager(2048, 300000L);
    private final MissionController missionController = new MissionController(this);

    // Weather and time fields removed - migrated to ClientTickHandler
    private CommandDispatcher commandDispatcher;
    private IPlayerContext playerContext;

    public BaritoneAPIBridge() {
        // Initialize player context
        playerContext = new MinecraftPlayerContext();

        // The factory normally constructs handlers with no dependencies. Event
        // polling must share the same manager used by tick/Fabric publishers.
        CommandHandlerFactory.registerHandlerInstance(
            "get_events",
            new GetEventsCommandHandler(eventManager)
        );
        CommandHandlerFactory.registerHandlerInstance(
            "get_death_location",
            new GetDeathLocationCommandHandler(deathTracker)
        );

        // Initialize command dispatcher with legacy handler
        commandDispatcher = new CommandDispatcher(missionController, this::handleLegacyCommandInternal);
    }
    // Tick event and block update fields removed - migrated to ClientTickHandler

    // New event types and caching
    private final Map<String, JsonObject> blockCache = new ConcurrentHashMap<>();
    private final Map<String, JsonObject> entityCache = new ConcurrentHashMap<>();
    private final Map<String, Long> cacheTimestamps = new ConcurrentHashMap<>();
    private static final long CACHE_TTL_MS = 5000; // 5 second cache TTL

    // Rate limiting
    private final Map<Socket, Long> lastRequestTimes = new ConcurrentHashMap<>();
    private final Map<Socket, Integer> requestCounts = new ConcurrentHashMap<>();
    private static final int RATE_LIMIT_REQUESTS = 500; // requests per window (increased for automation)
    private static final long RATE_LIMIT_WINDOW_MS = 10000; // 10 second window

    // Retry and reconnection logic
    private final Map<String, Integer> retryCounts = new ConcurrentHashMap<>();
    private static final int MAX_RETRIES = 3;

    // Death tracking fields removed - migrated to ClientTickHandler

    // Player Context Abstraction for Testing
    public interface IPlayerContext {
        boolean isPlayerNull();

        double getX();

        double getY();

        double getZ();

        float getYaw();

        float getPitch();

        float getHealth();

        float getMaxHealth();

        int getFoodLevel();

        float getSaturationLevel();

        float getArmor();

        int getTotalExperience();

        BlockPos getBlockPos();

        String getDimension();

        LocalPlayer getPlayer(); // For cases where we really need the entity, but try to avoid
    }

    private class MinecraftPlayerContext implements IPlayerContext {
        @Override
        public boolean isPlayerNull() {
            return Minecraft.getInstance().player == null;
        }

        @Override
        public double getX() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getX() : 0;
        }

        @Override
        public double getY() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getY() : 0;
        }

        @Override
        public double getZ() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getZ() : 0;
        }

        @Override
        public float getYaw() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getYRot() : 0;
        }

        @Override
        public float getPitch() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getXRot() : 0;
        }

        @Override
        public float getHealth() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getHealth() : 0;
        }

        @Override
        public float getMaxHealth() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getMaxHealth() : 0;
        }

        @Override
        public int getFoodLevel() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getFoodData().getFoodLevel() : 0;
        }

        @Override
        public float getSaturationLevel() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getFoodData().getSaturationLevel() : 0;
        }

        @Override
        public float getArmor() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.getArmorValue() : 0;
        }

        @Override
        public int getTotalExperience() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.totalExperience : 0;
        }

        @Override
        public BlockPos getBlockPos() {
            LocalPlayer player = Minecraft.getInstance().player;
            return player != null ? player.blockPosition() : BlockPos.ZERO;
        }

        @Override
        public LocalPlayer getPlayer() {
            return Minecraft.getInstance().player;
        }

        @Override
        public String getDimension() {
            Minecraft client = Minecraft.getInstance();
            return client.level != null ? client.level.dimension().identifier().toString() : "minecraft:overworld";
        }
    }

    public void setPlayerContext(IPlayerContext context) {
        this.playerContext = context;
    }

    @Override
    public void onInitialize() {
        // Use bounded thread pool to prevent resource exhaustion.
        // 🔴 线程一律设成 daemon：即使 CLIENT_STOPPING 因为某种原因没跑到，
        // 这些池线程也不会把 JVM 钉住、拖到 MC 的关闭看门狗超时崩溃
        // （2026-10-22 的 `Watchdog (Client shutdown from post-main)` 就是这么来的）。
        final java.util.concurrent.atomic.AtomicInteger poolSeq = new java.util.concurrent.atomic.AtomicInteger();
        executor = new ThreadPoolExecutor(
                THREAD_POOL_CORE_SIZE,
                THREAD_POOL_MAX_SIZE,
                THREAD_POOL_KEEP_ALIVE_SECONDS,
                TimeUnit.SECONDS,
                new LinkedBlockingQueue<>(100), // Queue up to 100 tasks
                runnable -> {
                    Thread thread = new Thread(runnable, "baritone-bridge-" + poolSeq.incrementAndGet());
                    thread.setDaemon(true);
                    return thread;
                },
                new ThreadPoolExecutor.CallerRunsPolicy() // Reject policy: run on calling thread if queue full
        );
        schematicDir = new File(Minecraft.getInstance().gameDirectory, "schematics");
        if (!schematicDir.exists()) {
            schematicDir.mkdirs();
        }

        // Initialize upload manager
        uploadManager = new UploadManager(schematicDir);

        // Register schematic upload handler with all upload commands
        SchematicUploadHandler uploadHandler = new SchematicUploadHandler(uploadManager);
        CommandHandlerFactory.registerHandlerInstance("schematic_init", uploadHandler);
        CommandHandlerFactory.registerHandlerInstance("schematic_chunk", uploadHandler);
        CommandHandlerFactory.registerHandlerInstance("schematic_commit", uploadHandler);
        CommandHandlerFactory.registerHandlerInstance("upload_progress", uploadHandler);
        CommandHandlerFactory.registerHandlerInstance("upload_list", uploadHandler);
        CommandHandlerFactory.registerHandlerInstance("upload_cancel", uploadHandler);
        CommandHandlerFactory.registerHandlerInstance("upload_stats", uploadHandler);

        // Initialize modular event system
        fabricEventRegistrar = new FabricEventRegistrar(eventManager);
        fabricEventRegistrar.registerEvents();

        clientTickHandler = new ClientTickHandler(playerContext, eventManager, deathTracker);
        ClientTickEvents.END_CLIENT_TICK.register(clientTickHandler::onClientTick);

        // Initialize modular network layer
        initializeNetworkLayer();

        // 事件推送 WebSocket：订阅后按事件类型推送，替代消费侧的轮询。
        //
        // **只有显式给了 -Dbaritone.ws.port 才启用**。没配就一个字节都不推，
        // 消费端自然回退到轮询（行为与没有这个功能时一致）。
        // 起不来（端口被占/库缺失）只记日志，不能影响 bridge 主通道。
        final Integer wsPort = readWsPort();
        if (wsPort == null) {
            LOGGER.info(
                "Event WebSocket disabled (set -D{}=<port> to enable)",
                WebSocketEventServer.PORT_PROPERTY
            );
        } else {
            try {
                eventWebSocketServer = new WebSocketEventServer(eventManager, wsPort);
                eventWebSocketServer.startServing();
            } catch (Throwable error) {
                eventWebSocketServer = null;
                LOGGER.warn(
                    "Event WebSocket server failed to start on port {} (bridge still works)",
                    wsPort,
                    error
                );
            }
        }

        // Initialize MCP Server after client is fully started
        ClientLifecycleEvents.CLIENT_STARTED.register(client -> {
            initializeMcpServer();
        });

        // 🔴 2026-10-22 用户报「MC 关闭时跳出错误」：崩溃报告是
        //   java.lang.Error: Watchdog (Client shutdown from post-main)
        //   at ClientShutdownWatchdog... 
        // 线程 dump 里 `pool-4-thread-1` 卡在 `Net.accept`（NetworkServer 的 acceptLoop），
        // 另有一个 WebSocketSelector 线程也还活着。**因为从来没注册 CLIENT_STOPPING**，
        // `shutdown()` 一次都没被调用过 —— 那些非守护线程让 JVM 无法退出，
        // MC 的关闭看门狗等超时就强制报了 Error。
        // 这里补上退出钩子；同时下面的 threadFactory 把它们都设成 daemon 作双保险。
        ClientLifecycleEvents.CLIENT_STOPPING.register(client -> {
            LOGGER.info("Client stopping — shutting down Baritone API bridge");
            shutdown();
        });

        LOGGER.info("Baritone API Bridge initialized on port " + configuredBridgePort());
    }

    static int configuredBridgePort() {
        String configured = System.getProperty(BRIDGE_PORT_PROPERTY);
        if (configured == null || configured.isBlank()) {
            return DEFAULT_PORT;
        }
        try {
            int port = Integer.parseInt(configured);
            if (port < 1 || port > 65535) {
                throw new IllegalArgumentException(
                        BRIDGE_PORT_PROPERTY + " must be between 1 and 65535: " + configured);
            }
            return port;
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(
                    BRIDGE_PORT_PROPERTY + " must be an integer: " + configured, e);
        }
    }

    /**
     * 读事件推送 WS 的端口（`-Dbaritone.ws.port=`）。
     *
     * **返回 null = 没配 = 不启用**。这里刻意不给默认值：
     * 两个 MC 客户端都装 bridge，写死一个默认端口必然让它们抢同一个端口，
     * 而 bind 失败只留一条容易被忽略的 warn。所以"要不要开"必须是显式动作。
     *
     * 配了但不是合法端口也不算数（按没配处理），并打一条 warn ——
     * 拼错的参数静默失效比直接不启动更难查。
     */
    static Integer readWsPort() {
        String configured = System.getProperty(WebSocketEventServer.PORT_PROPERTY);
        if (configured == null || configured.isBlank()) {
            return null;
        }
        try {
            int port = Integer.parseInt(configured.trim());
            if (port < 1 || port > 65535) {
                LOGGER.warn(
                    "Ignoring -D{}=({} is not a valid port)",
                    WebSocketEventServer.PORT_PROPERTY,
                    configured
                );
                return null;
            }
            return port;
        } catch (NumberFormatException e) {
            LOGGER.warn(
                "Ignoring -D{}=({} is not an integer)",
                WebSocketEventServer.PORT_PROPERTY,
                configured
            );
            return null;
        }
    }

    /**
     * Initialize the native Java MCP server on port 5557.
     */
    private void initializeMcpServer() {
        if (!Boolean.parseBoolean(System.getProperty("mcp.enabled", "true"))) {
            LOGGER.info("MCP server disabled via system property");
            return;
        }

        int mcpPort = Integer.parseInt(System.getProperty("mcp.port", "5557"));
        try {
            mcpServer = McpServer.create(mcpPort, commandDispatcher, eventManager);
            mcpServer.start();
            LOGGER.info("MCP server started on ws://localhost:" + mcpPort);

            // Register shutdown hook to clean up MCP server
            Runtime.getRuntime().addShutdownHook(new Thread(() -> {
                if (mcpServer != null) {
                    LOGGER.info("Shutting down MCP server...");
                    mcpServer.shutdown();
                }
            }, "mcp-shutdown-hook"));

        } catch (Exception e) {
            LOGGER.error("Failed to start MCP server on port " + mcpPort, e);
        }
    }

    // registerEventListeners removed - migrated to FabricEventRegistrar

    // onClientTick and related fields removed - migrated to ClientTickHandler

    @Override
    public void publishMissionEvent(String reason, JsonObject payload) {
        JsonObject eventPayload = new JsonObject();
        eventPayload.addProperty("reason", reason);
        eventPayload.addProperty("phase", missionController.getPhaseValue());
        if (payload != null) {
            eventPayload.add("payload", payload);
        }
        eventManager.publishEvent(EventManager.EventType.MISSION, eventPayload, EventManager.Priority.NORMAL,
                "mission_controller");
    }

    // emitTickEvent removed - migrated to ClientTickHandler

    // Legacy tracking methods removed - migrated to ClientTickHandler

    /**
     * Initialize the modular network layer components.
     * Creates RequestProcessor, ConnectionHandler, and NetworkServer,
     * then starts the server on the default port.
     */
    private void initializeNetworkLayer() {
        int bridgePort = configuredBridgePort();
        // Create the request processor with all necessary dependencies
        requestProcessor = new RequestProcessor(
                commandDispatcher,
                this::getBaritone,
                this::getMinecraftClient,
                playerContext,
                eventManager,
                clientTickHandler::emitTickEvent,
                this::isOfflineCommand);

        // Create connection handler
        connectionHandler = new ConnectionHandler(
                requestProcessor,
                missionController,
                uploadManager);

        // Create and start the network server
        networkServer = new NetworkServer(
                connectionHandler,
                activeConnections,
                MAX_CONNECTIONS,
                SOCKET_TIMEOUT_MS);

        try {
            networkServer.start(bridgePort, executor);
            running = true;
        } catch (Exception e) {
            LOGGER.error("Failed to start network server", e);
            running = false;
            // A second asynchronous bind attempt would hide port ownership
            // failures and could leave a live game with no working bridge.
            throw new IllegalStateException(
                    "Unable to bind Baritone API bridge on port " + bridgePort,
                    e);
        }
    }

    /**
     * @deprecated Use {@link #initializeNetworkLayer()} instead.
     *             This method is kept for fallback purposes during migration.
     */
    @Deprecated
    private void startAPIServer(int bridgePort) {
        executor.submit(() -> {
            try {
                serverSocket = new ServerSocket(bridgePort);
                running = true;
                LOGGER.info("Baritone API server listening on port " + bridgePort);

                while (running) {
                    try {
                        Socket clientSocket = serverSocket.accept();

                        // Check connection limit
                        if (activeConnections.size() >= MAX_CONNECTIONS) {
                            LOGGER.warn("Connection limit reached, rejecting client: {}",
                                    clientSocket.getRemoteSocketAddress());
                            try {
                                clientSocket.close();
                            } catch (IOException e) {
                                LOGGER.debug("Error closing rejected connection", e);
                            }
                            continue;
                        }

                        // Set socket timeout
                        clientSocket.setSoTimeout(SOCKET_TIMEOUT_MS);

                        // Track connection
                        activeConnections.add(clientSocket);

                        executor.submit(() -> handleClient(clientSocket));
                    } catch (IOException e) {
                        if (running)
                            LOGGER.error("Error accepting client connection", e);
                    }
                }
            } catch (IOException e) {
                LOGGER.error("Failed to start API server", e);
            }
        });
    }

    private void handleClient(Socket clientSocket) {
        String clientId = clientSocket.getRemoteSocketAddress().toString();
        try (
                BufferedReader in = new BufferedReader(new InputStreamReader(clientSocket.getInputStream()));
                PrintWriter out = new PrintWriter(clientSocket.getOutputStream(), true)) {
            LOGGER.debug("Client connected: {}", clientId);

            String line;
            while ((line = in.readLine()) != null) {
                JsonObject request = null;
                String requestId = null;
                try {
                    request = GSON.fromJson(line, JsonObject.class);
                    requestId = request.has("id") ? request.get("id").getAsString() : null;
                    JsonObject response = handleCommand(request, clientSocket);
                    out.println(GSON.toJson(response));
                } catch (Exception e) {
                    LOGGER.error("Error handling command from {}", clientId, e);
                    JsonObject response = new JsonObject();
                    response.addProperty("seq", lastSeq.incrementAndGet());
                    response.addProperty("timestamp", System.currentTimeMillis());

                    if (requestId != null) {
                        response.addProperty("id", requestId);
                    } else if (request != null && request.has("id")) {
                        response.addProperty("id", request.get("id").getAsString());
                    } else {
                        // Try to parse just the ID from the line as fallback
                        try {
                            JsonObject fallbackRequest = GSON.fromJson(line, JsonObject.class);
                            if (fallbackRequest.has("id")) {
                                response.addProperty("id", fallbackRequest.get("id").getAsString());
                            } else {
                                response.addProperty("id", (String) null);
                            }
                        } catch (Exception parseEx) {
                            response.addProperty("id", (String) null);
                        }
                    }
                    response.addProperty("status", "error");
                    response.addProperty("error", e.getMessage());
                    out.println(GSON.toJson(response));
                }
            }
        } catch (java.net.SocketTimeoutException e) {
            LOGGER.warn("Client connection timeout: {}", clientId);
        } catch (IOException e) {
            LOGGER.debug("Client connection error: {}", clientId, e);
        } finally {
            // Cleanup on disconnect
            cleanupClientResources(clientSocket);
            activeConnections.remove(clientSocket);
            try {
                if (!clientSocket.isClosed()) {
                    clientSocket.close();
                }
            } catch (IOException e) {
                LOGGER.debug("Error closing client socket", e);
            }
            LOGGER.debug("Client disconnected: {}", clientId);
        }
    }

    private void cleanupClientResources(Socket clientSocket) {
        missionController.releaseOwner(clientSocket);
        // Cleanup uploads owned by this client
        uploadManager.cleanupClientUploads(clientSocket);
    }

    private JsonObject handleCommand(JsonObject request) {
        return handleCommand(request, null);
    }

    /**
     * Handle legacy commands that haven't been migrated to the handler pattern.
     * 
     * @deprecated All commands have been migrated to CommandHandler pattern.
     *             This method is kept as a fallback for any commands that may have
     *             been missed.
     */
    @Deprecated
    private CommandResult handleLegacyCommandInternal(String command, JsonObject params,
            Minecraft client, IBaritone baritone, Socket clientSocket) {
        // All commands have been migrated to CommandHandler pattern
        // This fallback should never be reached
        LOGGER.warn("Legacy handler called for command that should have a handler: {}", command);
        return CommandResult.error("Unknown command or no handler found: " + command);
    }

    JsonObject handleCommand(JsonObject request, Socket clientSocket) {
        JsonObject response = new JsonObject();
        response.addProperty("seq", lastSeq.incrementAndGet());
        response.addProperty("timestamp", System.currentTimeMillis());

        // Set request ID if present
        String id = request != null && request.has("id") ? request.get("id").getAsString() : null;
        response.addProperty("id", id);

        // Basic validation
        if (request == null || !request.has("command")) {
            response.addProperty("status", "error");
            response.addProperty("error", "Missing command");
            return response;
        }

        try {
            String command = request.get("command").getAsString();

            if ("debug_reset_circuit".equals(command)) {
                commandDispatcher.resetCircuitBreaker();
                response.addProperty("status", "ok");
                JsonObject data = new JsonObject();
                data.addProperty("reset", true);
                response.add("data", data);
                return response;
            }

            Minecraft client = getMinecraftClient();
            IBaritone baritone = getBaritone();

            // Basic precondition checks (rate limiting and other checks are handled by
            // dispatcher)
            if (baritone == null) {
                response.addProperty("status", "error");
                response.addProperty("error",
                        "Baritone not available. Make sure Baritone mod is installed and loaded.");
                return response;
            }

            // Allow some commands without player (e.g. status checks, uploads)
            if (playerContext.isPlayerNull() && !isOfflineCommand(command)) {
                response.addProperty("status", "error");
                response.addProperty("error", "Player not available");
                return response;
            }

            // Check for damage events on each command - DELETED (handled by onClientTick ->
            // trackPlayerDeath/health monitoring)
            // checkPlayerDamage(client);

            clientTickHandler.emitTickEvent(baritone);

            // Dispatch through the command dispatcher
            CommandResult result = commandDispatcher.dispatchCommand(request, clientSocket, client, baritone);

            // Preserve the full CommandResult contract, including typed errors.
            for (var entry : result.toJson().entrySet()) {
                response.add(entry.getKey(), entry.getValue());
            }

        } catch (Exception e) {
            response.addProperty("status", "error");
            response.addProperty("error", e.getMessage());
            LOGGER.error("Command dispatch error", e);
        }

        return response;
    }

    private boolean isOfflineCommand(String command) {
        return command.startsWith("schematic_") || command.startsWith("upload_")
                || missionController.isOfflineSafe(command);
    }

    private boolean validateCoordinates(JsonObject params, JsonObject data) {
        if (params.has("x") && params.has("y") && params.has("z")) {
            int x = params.get("x").getAsInt();
            int y = params.get("y").getAsInt();
            int z = params.get("z").getAsInt();

            // Minecraft coordinate bounds
            if (x < -30000000 || x > 30000000 || y < -64 || y > 320 || z < -30000000 || z > 30000000) {
                data.addProperty("error", "Coordinates out of valid Minecraft range");
                return false;
            }
        }
        return true;
    }

    private boolean validateBlockId(String blockId, JsonObject data) {
        if (blockId == null || blockId.trim().isEmpty()) {
            data.addProperty("error", "Block ID cannot be null or empty");
            return false;
        }
        // Basic validation - could be enhanced
        if (!blockId.contains(":") && !blockId.startsWith("minecraft:")) {
            // Assume minecraft namespace
            blockId = "minecraft:" + blockId;
        }
        return true;
    }

    private boolean validateFilePath(String path, JsonObject data) {
        if (path == null) {
            data.addProperty("error", "File path cannot be null");
            return false;
        }
        if (path.contains("..") || path.contains("/") || path.contains("\\")) {
            data.addProperty("error", "Invalid file path - path traversal not allowed");
            return false;
        }
        return true;
    }

    // --- Implementations ---

    @Override
    public JsonObject collectTelemetry(Minecraft client, IBaritone baritone) {
        JsonObject telemetry = new JsonObject();
        if (!playerContext.isPlayerNull()) {
            telemetry.addProperty("health", playerContext.getHealth());
            telemetry.addProperty("food", playerContext.getFoodLevel());
            telemetry.addProperty("saturation", playerContext.getSaturationLevel());
            telemetry.addProperty("armor", playerContext.getArmor());
            telemetry.addProperty("experience", playerContext.getTotalExperience());
            telemetry.addProperty("dimension",
                    client.level != null ? client.level.dimension().identifier().toString() : "unknown");

            BlockPos pos = playerContext.getBlockPos();
            JsonObject position = new JsonObject();
            position.addProperty("x", pos.getX());
            position.addProperty("y", pos.getY());
            position.addProperty("z", pos.getZ());
            telemetry.add("position", position);

            JsonObject rotation = new JsonObject();
            rotation.addProperty("yaw", playerContext.getYaw());
            rotation.addProperty("pitch", playerContext.getPitch());
            telemetry.add("rotation", rotation);

            telemetry.addProperty("is_pathing", baritone.getPathingBehavior().isPathing());
        } else {
            telemetry.addProperty("player", "not_loaded");
        }

        if (baritone.getCustomGoalProcess().getGoal() != null) {
            telemetry.addProperty("goal", baritone.getCustomGoalProcess().getGoal().toString());
        }
        telemetry.addProperty("eventBufferSize", eventManager.getBufferSize());
        telemetry.addProperty("missionQueue", missionController.queueSize());
        telemetry.addProperty("timestamp", System.currentTimeMillis());
        return telemetry;
    }

    @Override
    public void handleSelection(IBaritone baritone, JsonObject params, JsonObject data) {
        // Delegated to BuildCommandHandler
        CommandHandler handler = CommandHandlerFactory.getHandler("sel");
        if (handler != null) {
            CommandResult result = handler.handle(params, Minecraft.getInstance(), baritone, null).join();
            if (result.isSuccess()) {
                data.add("result", result.getData());
            } else {
                data.addProperty("error", result.getErrorMessage());
            }
        } else {
            data.addProperty("error", "Selection handler not available");
        }
    }

    @Override

    public void handleMine(IBaritone baritone, JsonObject params, JsonObject data) {
        // Baritone's mine process needs to be run on main thread in 1.21+
        int count = params.has("count") ? params.get("count").getAsInt()
                : (params.has("quantity") ? params.get("quantity").getAsInt() : 0);

        if (params.has("block_type")) {
            String blockId = params.get("block_type").getAsString();
            Identifier id = Identifier.parse(blockId);
            if (BuiltInRegistries.BLOCK.containsKey(id)) {
                Block block = BuiltInRegistries.BLOCK.getValue(id);
                Minecraft.getInstance().execute(() -> baritone.getMineProcess().mine(count, block));
                data.addProperty("started", true);
            } else {
                data.addProperty("error", "Unknown block: " + blockId);
            }
        } else if (params.has("blocks")) {
            JsonArray blocksArray = params.getAsJsonArray("blocks");
            if (blocksArray.isEmpty()) {
                data.addProperty("error", "No block IDs provided");
                return;
            }

            Minecraft.getInstance().execute(() -> {
                List<BlockOptionalMeta> lookup = new ArrayList<>();
                for (JsonElement element : blocksArray) {
                    if (!element.isJsonPrimitive())
                        continue;
                    String blockId = element.getAsString();
                    Identifier id = Identifier.parse(blockId);
                    if (BuiltInRegistries.BLOCK.containsKey(id)) {
                        lookup.add(new BlockOptionalMeta(BuiltInRegistries.BLOCK.getValue(id)));
                    }
                }

                if (!lookup.isEmpty()) {
                    BlockOptionalMeta[] blockArray = lookup.toArray(new BlockOptionalMeta[0]);
                    baritone.getMineProcess().mine(count, blockArray);
                }
            });
            data.addProperty("started", true);
        } else {
            data.addProperty("error", "Missing block_type or blocks");
        }
    }

    // handleFarm and handleExplore deleted (migrated)

    // Schematic upload handlers migrated to SchematicUploadHandler.java

    // Schematic Upload Handlers

    private void handleSchematicInit(JsonObject params, JsonObject data, Socket clientSocket) throws Exception {
        String name = params.get("name").getAsString();
        long expectedSize = params.has("size") ? params.get("size").getAsLong() : -1;
        UploadManager.UploadPriority priority = params.has("priority")
                ? UploadManager.UploadPriority.valueOf(params.get("priority").getAsString().toUpperCase())
                : UploadManager.UploadPriority.NORMAL;

        boolean started = uploadManager.startUpload(name, expectedSize, clientSocket, priority);
        if (started) {
            data.addProperty("ready", true);
        } else {
            data.addProperty("error", "Failed to start upload - queue full or upload already exists");
        }
    }

    private void handleSchematicChunk(JsonObject params, JsonObject data) throws Exception {
        String name = params.get("name").getAsString();
        String b64 = params.get("data").getAsString();
        byte[] bytes = Base64.getDecoder().decode(b64);

        boolean success = uploadManager.processChunk(name, bytes);
        if (success) {
            data.addProperty("received", bytes.length);
        } else {
            throw new IOException("Failed to process chunk for upload: " + name);
        }
    }

    private void handleSchematicCommit(JsonObject params, JsonObject data) throws Exception {
        String name = params.get("name").getAsString();
        String expectedSha256 = params.has("sha256") ? params.get("sha256").getAsString() : null;

        boolean success = uploadManager.completeUpload(name, expectedSha256);
        if (success) {
            data.addProperty("saved", true);
            // Add SHA256 hash if available
            Map<String, Object> progress = uploadManager.getUploadProgress(name);
            if (progress.containsKey("sha256")) {
                data.addProperty("sha256", progress.get("sha256").toString());
            }
        } else {
            throw new IOException("Failed to complete upload: " + name);
        }
    }

    private void handleUploadProgress(JsonObject params, JsonObject data) throws Exception {
        String name = params.get("name").getAsString();
        Map<String, Object> progress = uploadManager.getUploadProgress(name);

        if (progress.containsKey("error")) {
            data.addProperty("error", progress.get("error").toString());
        } else {
            data.addProperty("name", progress.get("name").toString());
            data.addProperty("status", progress.get("status").toString());
            data.addProperty("progress_percentage", (Double) progress.get("progress_percentage"));
            data.addProperty("received_bytes", (Long) progress.get("received_bytes"));
            data.addProperty("expected_size", (Long) progress.get("expected_size"));
            data.addProperty("start_time", (Long) progress.get("start_time"));
            data.addProperty("last_activity", (Long) progress.get("last_activity"));
            data.addProperty("priority", progress.get("priority").toString());
        }
    }

    private void handleUploadList(JsonObject data) throws Exception {
        List<Map<String, Object>> uploads = uploadManager.getActiveUploads();
        JsonArray uploadArray = new JsonArray();
        for (Map<String, Object> upload : uploads) {
            JsonObject uploadObj = new JsonObject();
            uploadObj.addProperty("name", upload.get("name").toString());
            uploadObj.addProperty("status", upload.get("status").toString());
            uploadObj.addProperty("progress_percentage", (Double) upload.get("progress_percentage"));
            uploadObj.addProperty("received_bytes", (Long) upload.get("received_bytes"));
            uploadObj.addProperty("expected_size", (Long) upload.get("expected_size"));
            uploadObj.addProperty("priority", upload.get("priority").toString());
            uploadObj.addProperty("owner", upload.get("owner").toString());
            uploadArray.add(uploadObj);
        }
        data.add("uploads", uploadArray);
    }

    private void handleUploadCancel(JsonObject params, JsonObject data) throws Exception {
        String name = params.get("name").getAsString();
        boolean success = uploadManager.cancelUpload(name);
        data.addProperty("cancelled", success);
        if (!success) {
            data.addProperty("error", "Upload not found or could not be cancelled");
        }
    }

    private void handleUploadStats(JsonObject data) throws Exception {
        Map<String, Object> stats = uploadManager.getStatistics();
        data.addProperty("active_uploads", (Integer) stats.get("active_uploads"));
        data.addProperty("queued_uploads", (Integer) stats.get("queued_uploads"));
        data.addProperty("total_completed", (Long) stats.get("total_completed"));
        data.addProperty("total_failed", (Long) stats.get("total_failed"));
        data.addProperty("total_bytes_uploaded", (Long) stats.get("total_bytes_uploaded"));
        data.addProperty("max_concurrent_uploads", (Integer) stats.get("max_concurrent_uploads"));
        data.addProperty("max_queue_size", (Integer) stats.get("max_queue_size"));
    }

    @Override
    public void handleSettings(JsonObject params, JsonObject data) {
        if (params.has("get")) {
            String key = params.get("get").getAsString();
            try {
                baritone.api.Settings.Setting<?> setting = BaritoneAPI.getSettings().allSettings.stream()
                        .filter(s -> s.getName().equalsIgnoreCase(key))
                        .findFirst().orElse(null);

                if (setting != null) {
                    data.addProperty("key", setting.getName());
                    data.addProperty("value", setting.value.toString());
                } else {
                    data.addProperty("error", "Setting not found: " + key);
                }
            } catch (Exception e) {
                data.addProperty("error", "Failed to get setting: " + e.getMessage());
            }
        } else if (params.has("set")) {
            String key = params.get("set").getAsString();
            String val = params.get("value").getAsString();

            try {
                // Run on main thread to be safe, though most settings can be set off-thread.
                // Baritone settings are usually primitive wrappers or Enums.
                Minecraft.getInstance().execute(() -> {
                    baritone.api.Settings.Setting<?> setting = BaritoneAPI.getSettings().allSettings.stream()
                            .filter(s -> s.getName().equalsIgnoreCase(key))
                            .findFirst().orElse(null);

                    if (setting != null) {
                        try {
                            // Using Baritone's internal string parsing if available,
                            // but safest is to handle common types or let Baritone handle it.
                            // In 1.15.0, Setting has a generic value field.
                            // We attempt to cast and set.
                            modifySettingSafely(setting, val);
                            LOGGER.info("Successfully set Baritone setting {} to {}", key, val);
                        } catch (Exception e) {
                            LOGGER.error("Failed to set Baritone setting {}: {}", key, e.getMessage());
                        }
                    }
                });
                data.addProperty("status", "requested");
            } catch (Exception e) {
                data.addProperty("error", "Failed to dispatch setting update: " + e.getMessage());
            }
        }
    }

    /**
     * Safely modify a Baritone setting by parsing the string value.
     */
    @SuppressWarnings("unchecked")
    private void modifySettingSafely(baritone.api.Settings.Setting<?> setting, String val) {
        Class<?> type = (Class<?>) setting.getType();
        if (type == Boolean.class) {
            ((baritone.api.Settings.Setting<Boolean>) setting).value = Boolean.parseBoolean(val);
        } else if (type == Integer.class) {
            ((baritone.api.Settings.Setting<Integer>) setting).value = Integer.parseInt(val);
        } else if (type == Double.class) {
            ((baritone.api.Settings.Setting<Double>) setting).value = Double.parseDouble(val);
        } else if (type == Float.class) {
            ((baritone.api.Settings.Setting<Float>) setting).value = Float.parseFloat(val);
        } else if (type == Long.class) {
            ((baritone.api.Settings.Setting<Long>) setting).value = Long.parseLong(val);
        } else {
            // Fallback for strings or other types if applicable
            // Note: Enum settings might need more complex parsing
            try {
                // Try set via generic approach if possible
                setting.getClass().getMethod("setValue", Object.class); // Check if exists
                // But usually .value is public in most 1.15.0/1.12.2 versions
            } catch (Exception e) {
                // log
            }
        }
    }

    // --- Basic Handlers ---

    // --- Legacy Handlers Deleted (Migrated to CommandHandlers) ---
    // handleStop, handlePause, handleCancel, handleGoal removed.

    @Override
    public void handleGetState(Minecraft client, IBaritone baritone, JsonObject data) {
        try {
            // Using playerContext to allow testing without Entity class loading
            client.submit(() -> {
                if (playerContext.isPlayerNull()) {
                    data.addProperty("error", "Player not available");
                    return null;
                }

                // Position
                JsonObject position = new JsonObject();
                position.addProperty("x", playerContext.getX());
                position.addProperty("y", playerContext.getY());
                position.addProperty("z", playerContext.getZ());
                position.addProperty("yaw", playerContext.getYaw());
                position.addProperty("pitch", playerContext.getPitch());
                data.add("position", position);

                // Block position
                BlockPos blockPos = playerContext.getBlockPos();
                JsonObject blockPosition = new JsonObject();
                blockPosition.addProperty("x", blockPos.getX());
                blockPosition.addProperty("y", blockPos.getY());
                blockPosition.addProperty("z", blockPos.getZ());
                data.add("block_position", blockPosition);

                // Health and status
                data.addProperty("health", playerContext.getHealth());
                data.addProperty("max_health", playerContext.getMaxHealth());
                data.addProperty("food_level", playerContext.getFoodLevel());
                data.addProperty("saturation", playerContext.getSaturationLevel());
                data.addProperty("experience_level", 0 /* playerContext.experienceLevel */); // Not available in context
                                                                                             // yet
                data.addProperty("experience_total", playerContext.getTotalExperience());
                data.addProperty("is_dead", playerContext.getHealth() <= 0);

                // Baritone status
                try {
                    boolean isPathing = baritone.getPathingBehavior().isPathing();
                    data.addProperty("is_pathing", isPathing);

                    if (isPathing) {
                        data.addProperty("pathing_goal", baritone.getPathingBehavior().getGoal() != null);
                    }
                } catch (Exception e) {
                    LOGGER.debug("Could not get pathing status", e);
                    data.addProperty("is_pathing", false);
                }

                // World info
                data.addProperty("dimension", playerContext.getDimension());
                return null;
            }).get(2, TimeUnit.SECONDS);
        } catch (Exception e) {
            data.addProperty("error", "Failed to get state: " + e.getMessage());
        }
    }

    // handleGetEntities deleted

    @Override
    public void handleGetInventory(Minecraft client, JsonObject data) {
        try {
            client.submit(() -> {
                if (client.player == null) {
                    data.addProperty("error", "Player not available");
                    return null;
                }

                Inventory inv = client.player.getInventory();

                // Main Inventory (0-35)
                JsonArray mainInventory = new JsonArray();
                for (int i = 0; i < 36; i++) {
                    mainInventory.add(serializeItemStack(inv.getItem(i), i));
                }
                data.add("inventory", mainInventory);

                // Armor (Slots 36-39)
                JsonArray armorInventory = new JsonArray();
                for (int i = 0; i < 4; i++) {
                    armorInventory.add(serializeItemStack(inv.getItem(36 + i), i + 36));
                }
                data.add("armor", armorInventory);

                // Offhand (Slot 40)
                JsonArray offhandInventory = new JsonArray();
                offhandInventory.add(serializeItemStack(inv.getItem(40), 40));
                data.add("offhand", offhandInventory);

                data.addProperty("selected_slot", inv.selected);

                Map<String, Integer> counts = flattenInventory(mainInventory, armorInventory, offhandInventory);
                emitInventoryChangeEvent(counts, inv.selected);
                return null;
            }).get(2, TimeUnit.SECONDS);
        } catch (Exception e) {
            data.addProperty("error", "Failed to retrieve inventory: " + e.getMessage());
            LOGGER.error("Inventory retrieval failed", e);
        }
    }

    private JsonObject serializeItemStack(ItemStack stack, int slot) {
        JsonObject itemData = new JsonObject();
        itemData.addProperty("slot", slot);

        if (stack.isEmpty()) {
            itemData.addProperty("id", "minecraft:air");
            itemData.addProperty("count", 0);
        } else {
            // Safe off-thread access: ID and Count (primitives/registry lookup)
            itemData.addProperty("id", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
            itemData.addProperty("count", stack.getCount());
            itemData.addProperty("max_count", stack.getMaxStackSize());
            itemData.addProperty("damage", stack.getDamageValue());
            itemData.addProperty("max_damage", stack.getMaxDamage());

            // SKIP name translation to prevent deadlock/freeze on off-thread access
            // itemData.addProperty("name", stack.getName().getString());
            itemData.addProperty("name", stack.getItem().toString()); // Safer fallback
        }

        return itemData;
    }

    private Map<String, Integer> flattenInventory(JsonArray... inventories) {
        Map<String, Integer> counts = new HashMap<>();
        if (inventories == null) {
            return counts;
        }
        for (JsonArray array : inventories) {
            if (array == null) {
                continue;
            }
            for (JsonElement element : array) {
                if (!element.isJsonObject()) {
                    continue;
                }
                JsonObject obj = element.getAsJsonObject();
                if (!obj.has("id") || !obj.has("count")) {
                    continue;
                }
                String id = obj.get("id").getAsString();
                int count = obj.get("count").getAsInt();
                if (count <= 0 || "minecraft:air".equals(id)) {
                    continue;
                }
                counts.put(id, counts.getOrDefault(id, 0) + count);
            }
        }
        return counts;
    }

    private void emitInventoryChangeEvent(Map<String, Integer> counts, int selectedSlot) {
        if (eventManager == null) {
            return;
        }
        JsonObject payload = new JsonObject();
        payload.addProperty("selected_slot", selectedSlot);
        JsonObject countData = new JsonObject();
        if (counts != null) {
            for (Map.Entry<String, Integer> entry : counts.entrySet()) {
                countData.addProperty(entry.getKey(), entry.getValue());
            }
        }
        payload.add("counts", countData);
        eventManager.publishEvent(EventManager.EventType.INVENTORY_CHANGE, payload, EventManager.Priority.NORMAL,
                "inventory_change");
    }

    // All legacy handlers deleted (handleInventoryClick ... handleRetryCommand)
    // handleInventoryClick, handleInteractBlock, handleGetScreen, handleCloseScreen
    // deleted (migrated)

    // handleGetRecipes, handleGetEvents deleted (migrated)

    // checkPlayerDamage deleted (migrated to onClientTick)

    // handleLookAt removed - migrated to LookAtCommandHandler

    // handleUseItem removed - migrated to UseItemCommandHandler

    // handleAttackEntity removed - migrated to AttackEntityCommandHandler

    // handleSelectSlot removed - migrated to SelectSlotCommandHandler

    // handleGetBlock removed - migrated to GetBlockCommandHandler

    // handlePath removed - migrated to PathCommandHandler

    // handleTunnel removed - migrated to TunnelCommandHandler

    // --- Advanced Command Implementations (Chat Fallback) ---

    // handleAxisMine removed - migrated to AxisMineCommandHandler

    // handleStripMine removed - migrated to StripMineCommandHandler

    // handleQuarry removed - migrated to QuarryCommandHandler

    // handleWideTunnel removed - migrated to TunnelWideCommandHandler

    // handlePlaceTorches removed - migrated to PlaceTorchesCommandHandler

    // handleHarvest removed - migrated to HarvestCommandHandler

    // handlePlant removed - migrated to PlantCommandHandler

    // ========== Automation Command Handlers ==========

    /**
     * Craft recipes (simplified hardcoded for common items).
     * Works with 2x2 player inventory crafting (when no crafting table open).
     * 
     * Player Inventory Slot Layout (syncId=0):
     * Slot 0: Craft output
     * Slots 1-4: 2x2 crafting grid (1=TL, 2=TR, 3=BL, 4=BR)
     * Slots 5-8: Armor
     * Slots 9-35: Main inventory
     * Slots 36-44: Hotbar
     * Slot 45: Offhand
     *
     * Crafting Table Slot Layout (syncId depends on open screen):
     * Slot 0: Craft output
     * Slots 1-9: 3x3 crafting grid
     * Slots 10-36: Main inventory
     * Slots 37-45: Hotbar
     */
    // handleCraft removed - migrated to CraftCommandHandler

    /**
     * Simple recipe definition for hardcoded recipes.
     */
    // CraftRecipe and getRecipeDefinition removed - unused after handleCraft
    // migration
    // Helper methods (hasIngredients, countItemInInventory, findItemSlot,
    // screenSlotFromInvSlot) removed - unused after handleCraft migration
    // clearCraftingGrid and placeIngredients removed - unused after handleCraft
    // migration

    // handleClickRecipe removed - migrated to ClickRecipeCommandHandler

    // handleSmeltItems removed - migrated to SmeltItemsCommandHandler

    // handleAutoCraft, performCrafting, findIngredientSlot removed - migrated to
    // AutoCraftCommandHandler

    // handlePlaceFire removed - migrated to PlaceFireCommandHandler

    // handlePlaceBlock removed - migrated to PlaceBlockCommandHandler

    // handleBreakBlock removed - migrated to BreakBlockCommandHandler

    // handleFindBlocks removed - migrated to FindBlocksCommandHandler

    // ========== New Commands for Vertical Slice ==========

    // handleThrowItem removed - migrated to ThrowItemCommandHandler

    // handleRespawn removed - migrated to RespawnCommandHandler

    // handleGetDimension removed - migrated to GetDimensionCommandHandler

    // handleGetDeathLocation removed - migrated to GetDeathLocationCommandHandler

    // ========== New Enhanced Handlers ==========

    private void handleGetWorldInfo(Minecraft client, JsonObject data) {
        if (client.level == null) {
            data.addProperty("error", "World not available");
            return;
        }

        data.addProperty("dimension", client.level.dimension().identifier().toString());
        data.addProperty("time", client.level.getGameTime());
        data.addProperty("difficulty", client.level.getDifficulty().getSerializedName());
        data.addProperty("is_raining", client.level.isRaining());
        data.addProperty("is_thundering", client.level.isThundering());

        if (client.level.getServer() != null) {
            data.addProperty("server_brand", client.level.getServer().getServerModName());
        }
    }

    private void handleGetPathInfo(IBaritone baritone, JsonObject data) {
        try {
            boolean isPathing = baritone.getPathingBehavior().isPathing();
            data.addProperty("is_pathing", isPathing);

            if (isPathing) {
                data.addProperty("goal_type",
                        baritone.getPathingBehavior().getGoal() != null
                                ? baritone.getPathingBehavior().getGoal().getClass().getSimpleName()
                                : "unknown");
                // Note: Path details are complex in Baritone API, simplified here
                data.addProperty("path_available", baritone.getPathingBehavior().getPath() != null);
            }

            // Add basic pathfinding settings (simplified)
            JsonObject settings = new JsonObject();
            settings.addProperty("note", "Detailed pathfinding settings require Baritone settings API access");
            data.add("settings", settings);

        } catch (Exception e) {
            LOGGER.warn("Could not get pathfinding info", e);
            data.addProperty("error", "Failed to get pathfinding info: " + e.getMessage());
        }
    }

    private void handleGetBaritoneConfig(IBaritone baritone, JsonObject data) {
        try {
            JsonObject config = new JsonObject();
            config.addProperty("primary_baritone_available", baritone != null);

            if (baritone != null) {
                config.addProperty("has_goal", baritone.getCustomGoalProcess().getGoal() != null);
                config.addProperty("is_pathing", baritone.getPathingBehavior().isPathing());

                // Add some key settings to the config response
                JsonObject currentSettings = new JsonObject();
                baritone.api.Settings settings = BaritoneAPI.getSettings();
                currentSettings.addProperty("allowSprint", settings.allowSprint.value);
                currentSettings.addProperty("allowParkour", settings.allowParkour.value);
                currentSettings.addProperty("allowBreak", settings.allowBreak.value);
                currentSettings.addProperty("autoTool", settings.autoTool.value);
                config.add("current_settings", currentSettings);
            }

            data.add("config", config);
        } catch (Exception e) {
            data.addProperty("error", "Failed to get Baritone config: " + e.getMessage());
        }
    }

    private void handleGetCachedBlocks(JsonObject params, JsonObject data) {
        // Return cached block information
        JsonArray cachedBlocks = new JsonArray();
        for (Map.Entry<String, JsonObject> entry : blockCache.entrySet()) {
            if (System.currentTimeMillis() - cacheTimestamps.getOrDefault(entry.getKey(), 0L) < CACHE_TTL_MS) {
                JsonObject cached = entry.getValue().deepCopy();
                cached.addProperty("cache_key", entry.getKey());
                cachedBlocks.add(cached);
            }
        }
        data.add("cached_blocks", cachedBlocks);
        data.addProperty("cache_size", blockCache.size());
        data.addProperty("cache_ttl_ms", CACHE_TTL_MS);
    }

    private void handleGetCachedEntities(JsonObject params, JsonObject data) {
        // Return cached entity information
        JsonArray cachedEntities = new JsonArray();
        for (Map.Entry<String, JsonObject> entry : entityCache.entrySet()) {
            if (System.currentTimeMillis() - cacheTimestamps.getOrDefault(entry.getKey(), 0L) < CACHE_TTL_MS) {
                JsonObject cached = entry.getValue().deepCopy();
                cached.addProperty("cache_key", entry.getKey());
                cachedEntities.add(cached);
            }
        }
        data.add("cached_entities", cachedEntities);
        data.addProperty("cache_size", entityCache.size());
        data.addProperty("cache_ttl_ms", CACHE_TTL_MS);
    }

    private void handleSetPathfindingSettings(IBaritone baritone, JsonObject params) {
        // This is now redundant with handleSettings but kept for backward compatibility
        // Iterate through all params and try to set them as Baritone settings
        Minecraft.getInstance().execute(() -> {
            for (Map.Entry<String, JsonElement> entry : params.entrySet()) {
                String key = entry.getKey();
                String val = entry.getValue().getAsString();

                baritone.api.Settings.Setting<?> setting = BaritoneAPI.getSettings().allSettings.stream()
                        .filter(s -> s.getName().equalsIgnoreCase(key))
                        .findFirst().orElse(null);

                if (setting != null) {
                    modifySettingSafely(setting, val);
                }
            }
        });
    }

    private void handleClearCache(JsonObject params, JsonObject data) {
        String cacheType = params.has("type") ? params.get("type").getAsString() : "all";

        int clearedBlocks = 0;
        int clearedEntities = 0;

        if ("all".equals(cacheType) || "blocks".equals(cacheType)) {
            clearedBlocks = blockCache.size();
            blockCache.clear();
        }

        if ("all".equals(cacheType) || "entities".equals(cacheType)) {
            clearedEntities = entityCache.size();
            entityCache.clear();
        }

        cacheTimestamps.clear();

        data.addProperty("cleared_blocks", clearedBlocks);
        data.addProperty("cleared_entities", clearedEntities);
        data.addProperty("cache_type", cacheType);
        data.addProperty("total_cleared", clearedBlocks + clearedEntities);
    }

    private void handleReconnect(Socket clientSocket, JsonObject data) {
        if (clientSocket == null) {
            data.addProperty("error", "No client socket available");
            return;
        }

        try {
            // Simple reconnection logic - in a real implementation this would handle
            // connection recovery, state synchronization, etc.
            data.addProperty("reconnected", true);
            data.addProperty("client_address", clientSocket.getRemoteSocketAddress().toString());
            data.addProperty("note",
                    "Reconnection logic placeholder - full implementation would require connection state management");

            // Reset rate limiting for this client
            lastRequestTimes.remove(clientSocket);
            requestCounts.remove(clientSocket);

        } catch (Exception e) {
            data.addProperty("error", "Reconnection failed: " + e.getMessage());
        }
    }

    private void handleRetryCommand(JsonObject params, JsonObject data, Minecraft client, IBaritone baritone,
            Socket clientSocket) {
        String commandId = params.has("command_id") ? params.get("command_id").getAsString() : null;
        JsonObject originalParams = params.has("original_params") && params.get("original_params").isJsonObject()
                ? params.getAsJsonObject("original_params")
                : new JsonObject();

        if (commandId == null) {
            data.addProperty("error", "Missing command_id for retry");
            return;
        }

        int currentRetries = retryCounts.getOrDefault(commandId, 0);
        if (currentRetries >= MAX_RETRIES) {
            data.addProperty("error", "Maximum retry attempts exceeded for command: " + commandId);
            return;
        }

        retryCounts.put(commandId, currentRetries + 1);

        try {
            // Execute the retried command
            JsonObject retryResponse = handleCommand(originalParams, clientSocket);
            data.add("retry_result", retryResponse);
            data.addProperty("retry_attempt", currentRetries + 1);
            data.addProperty("max_retries", MAX_RETRIES);

            if ("ok".equals(retryResponse.get("status").getAsString())) {
                retryCounts.remove(commandId); // Success, remove retry count
                data.addProperty("retry_successful", true);
            }

        } catch (Exception e) {
            data.addProperty("error", "Retry failed: " + e.getMessage());
            data.addProperty("retry_attempt", currentRetries + 1);
        }
    }

    /**
     * 关掉 Baritone 上游自己创建、且**从不关闭**的线程池。
     *
     * 🔴 为什么需要这个（2026-10-22 用户两次报「关闭 MC 时崩溃」）：
     * 崩溃报告是 `java.lang.Error: Watchdog (Client shutdown from post-main)`
     * —— MC 主线程结束后，`ClientShutdownWatchdog` 等 JVM 退出，等超时就强制报错。
     * 线程 dump 里剩下的钉子是一对 **非守护** 的 `pool-4-thread-1/2`，栈顶是
     * `baritone.o$a.run` / `baritone.o$b`（后者在 sleep 里做定时存盘）。
     *
     * 那些池的创建者是 `baritone.a`（`IBaritone` 的实现类）的**静态字段**
     * `private static final ThreadPoolExecutor a`，用
     * `new ThreadPoolExecutor(int,int,long,TimeUnit,BlockingQueue)` 建的 —— 没给
     * ThreadFactory，所以线程名是默认的 `pool-N-thread-M`，也**没设 daemon**。
     * Baritone 的公开 API（`ICachedWorld` / `IWorldData` / `IBaritone`）**只提供
     * save()，不提供 shutdown()**，所以只能反射。
     *
     * 用反射而不是依赖混淆后的字段名：按「静态 + 类型是 ExecutorService」筛选，
     * 这样 Baritone 换版本重新混淆也不会失配。
     */
    private void shutdownBaritoneThreadPools() {
        try {
            IBaritone primary = BaritoneAPI.getProvider().getPrimaryBaritone();
            if (primary == null) {
                return;
            }
            // primary 的实际类型就是 baritone.a（IBaritone 的唯一实现）
            int closed = 0;
            for (java.lang.reflect.Field field : primary.getClass().getDeclaredFields()) {
                if (!java.lang.reflect.Modifier.isStatic(field.getModifiers())) {
                    continue;
                }
                if (!java.util.concurrent.ExecutorService.class.isAssignableFrom(field.getType())) {
                    continue;
                }
                try {
                    field.setAccessible(true);
                    Object pool = field.get(null);
                    if (pool instanceof java.util.concurrent.ExecutorService service) {
                        service.shutdownNow();
                        closed++;
                    }
                } catch (Throwable ignored) {
                    // 单个字段拿不到不影响其他；Baritone 内部结构变了也不该让退出流程失败
                }
            }
            LOGGER.info("Baritone thread pools shut down: {}", closed);
        } catch (Throwable error) {
            // 这里是"尽力而为"的清理：任何异常都只能降级为日志，
            // 否则一次反射失败会让 MC 关不干净、又触发看门狗崩溃。
            LOGGER.debug("Baritone thread pool shutdown skipped", error);
        }
    }

    public void shutdown() {
        running = false;
        // 🔴 最后一步专门处理 Baritone 上游的线程池（见方法内注释），
        // 放在最前面是因为它和下面的网络层无关、失败也不影响后续。
        shutdownBaritoneThreadPools();
        try {
            // 🔴 事件推送 WS 服务器必须在这里关掉。不带这一步的话，它会像今天的
            // McpServer 一样留下一个非守护 selector 线程，把 JVM 钉住、逼出
            // `Watchdog (Client shutdown from post-main)` 崩溃。教训已经吃过两次。
            if (eventWebSocketServer != null) {
                eventWebSocketServer.stopServing();
                eventWebSocketServer = null;
            }

            // 🔴 2026-10-22：关 MCP WebSocket 服务器（端口 mcp.port，默认 5557）。
            // 它内部是 `org.java_websocket.server.WebSocketServer`，自带一个非守护的
            // `WebSocketSelector-N` 线程。之前这里漏了它，只在 JVM shutdown hook 里关 ——
            // 而 JVM 正因为这个线程退不了，hook 迟迟不跑，成了互相等待：
            // MC 主线程结束 → ClientShutdownWatchdog 计时 → 超时 → 抛
            // `java.lang.Error: Watchdog (Client shutdown from post-main)` 崩溃。
            if (mcpServer != null) {
                mcpServer.shutdown();
                mcpServer = null;
            }

            // Shutdown new network layer if initialized
            if (networkServer != null) {
                networkServer.stop();
            }

            // Legacy shutdown - close all active connections (if using legacy server)
            for (Socket socket : activeConnections) {
                try {
                    socket.close();
                } catch (IOException e) {
                    LOGGER.debug("Error closing socket during shutdown", e);
                }
            }
            activeConnections.clear();

            // Shutdown upload manager
            if (uploadManager != null) {
                uploadManager.shutdown();
            }

            // Legacy server socket shutdown
            if (serverSocket != null && !serverSocket.isClosed()) {
                serverSocket.close();
            }
            if (executor != null) {
                executor.shutdownNow();
            }
            // 命令层的清理线程池同样不能留着（CommandCache/UploadManager 里各有一个）
            if (commandDispatcher != null) {
                commandDispatcher.shutdown();
            }
        } catch (IOException e) {
            LOGGER.error("Error shutting down API server", e);
        }
    }

    // handleGetView and handleScreenshot deleted
    // handleGetView and handleScreenshot deleted (migrated)

    // Helper methods for testing
    protected IBaritone getBaritone() {
        return BaritoneAPI.getProvider().getPrimaryBaritone();
    }

    protected Minecraft getMinecraftClient() {
        return Minecraft.getInstance();
    }

    // Checking if this method is needed for testing
    public void setExecutor(ExecutorService executor) {
        if (this.executor != null) {
            this.executor.shutdown();
        }
        this.executor = executor;
    }

}
