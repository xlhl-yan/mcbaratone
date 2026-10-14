package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.Socket;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.PriorityBlockingQueue;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;
import net.minecraft.client.Minecraft;
import java.util.concurrent.TimeUnit;
import java.util.Comparator;

/**
 * Command priority levels for command execution.
 */
enum CommandPriority {
    HIGH,   // Emergency commands like stops
    NORMAL, // Regular commands
    LOW     // Background/low-priority commands
}

/**
 * Wrapper for command requests with priority information.
 */
class PrioritizedCommand {
    final long id;
    final JsonObject request;
    final Socket clientSocket;
    final long enqueueTime;
    final CommandPriority priority;

    PrioritizedCommand(long id, JsonObject request, Socket clientSocket, CommandPriority priority) {
        this.id = id;
        this.request = request;
        this.clientSocket = clientSocket;
        this.enqueueTime = System.currentTimeMillis();
        this.priority = priority;
    }
}

/**
 * CommandDispatcher coordinates command execution using the handler pattern.
 * Central command orchestration point that replaces direct command handling logic.
 */
public class CommandDispatcher {
    private static final Logger logger = LoggerFactory.getLogger(CommandDispatcher.class);

    private final MissionController missionController;
    private final LegacyCommandHandler legacyHandler;

    // Rate limiting
    private final Map<Socket, Long> lastRequestTimes = new ConcurrentHashMap<>();
    private final Map<Socket, Integer> requestCounts = new ConcurrentHashMap<>();
    private static final int RATE_LIMIT_REQUESTS = 2000; // increased for automation
    private static final long RATE_LIMIT_WINDOW_MS = 10000; // 10 second window

    // Timeout management
    private static final long COMMAND_TIMEOUT_MS = 30000; // 30 seconds

    // Error recovery components
    private final CircuitBreaker circuitBreaker;
    private final RetryHandler retryHandler;
    private final CommandCache commandCache;

    // Priority queue and metrics
    private final PriorityBlockingQueue<PrioritizedCommand> commandQueue;
    private final MetricsCollector metricsCollector;



    // Commands that change state and should invalidate cache
    private static final Set<String> STATE_CHANGING_COMMANDS = Set.of(
        "goto", "mine", "build", "explore", "stop", "pause", "cancel",
        "goal", "path", "tunnel", "farm", "interact_block", "attack_entity", "sel",
        "use_item", "use_bucket", "place_block", "break_block", "throw_item", "select_slot",
        "equip", "inventory_click", "chat", "set_fast_break",
        "settings", "smelt_items", "craft", "auto_craft", "craft_advanced", "click_recipe",
        "place_fire", "place_recipe", "respawn", "screenshot",
        "come", "follow", "look", "look_at", "attack_block", "dig_block",
        "select_trade", "entity_interact", "entity_transport", "advanced_goal",
        "sequence", "axis", "strip", "quarry", "tunnel_wide", "place_torches",
        "mission", "close_screen", "schematic_init", "schematic_chunk",
        "schematic_commit", "upload_cancel"
    );

    private static final Set<String> READ_ONLY_COMMANDS = Set.of(
        "get_inventory", "get_state", "get_entities", "get_combat_snapshot",
        "get_player_pos", "scan_biomes", "inspect_build_site", "get_block",
        "get_recipes", "find_blocks", "get_view", "get_version", "get_events",
        "get_screen", "get_dimension", "get_death_location", "upload_progress",
        "upload_list", "upload_stats"
    );

    private static final Set<String> CIRCUIT_BYPASS_COMMANDS = Set.of(
        "cancel", "stop", "pause", "debug_reset_circuit"
    );

    public CommandDispatcher(MissionController missionController, LegacyCommandHandler legacyHandler) {
        this.missionController = missionController;
        this.legacyHandler = legacyHandler;
        this.circuitBreaker = new CircuitBreaker("CommandDispatcher");
        this.retryHandler = new RetryHandler();
        this.commandCache = new CommandCache();

        // Initialize priority queue with custom comparator
        this.commandQueue = new PriorityBlockingQueue<>(10, new Comparator<PrioritizedCommand>() {
            @Override
            public int compare(PrioritizedCommand c1, PrioritizedCommand c2) {
                // Higher priority first (HIGH > NORMAL > LOW) - reverse natural order
                int priorityCompare = c2.priority.compareTo(c1.priority);
                if (priorityCompare != 0) {
                    return priorityCompare;
                }
                // Same priority: FIFO based on enqueue time
                return Long.compare(c1.enqueueTime, c2.enqueueTime);
            }
        });

        this.metricsCollector = new MetricsCollector();
    }

    /**
     * Determine the priority for a command.
     *
     * @param command The command name
     * @return The priority level
     */
    CommandPriority determinePriority(String command) {
        // High priority commands that need immediate execution
        if ("stop".equals(command) || "cancel".equals(command) || "pause".equals(command)) {
            return CommandPriority.HIGH;
        }
        // Low priority commands that can be delayed
        if ("settings".equals(command) || "screenshot".equals(command)) {
            return CommandPriority.LOW;
        }
        // Default to normal priority
        return CommandPriority.NORMAL;
    }

    /**
     * Simplified dispatch method for MCP use.
     * Uses current MinecraftClient and Baritone instances.
     *
     * @param command The command name
     * @param params Command parameters
     * @return CommandResult from command execution
     */
    public CommandResult dispatch(String command, JsonObject params) {
        Minecraft client = Minecraft.getInstance();
        IBaritone baritoneInstance = null;
        
        // Try to get Baritone instance
        try {
            if (client != null && client.player != null) {
                baritoneInstance = baritone.api.BaritoneAPI.getProvider().getPrimaryBaritone();
            }
        } catch (Exception e) {
            logger.debug("Could not get Baritone instance: {}", e.getMessage());
        }
        
        // Build request object
        JsonObject request = new JsonObject();
        request.addProperty("command", command);
        request.add("params", params != null ? params : new JsonObject());
        
        return dispatchCommand(request, null, client, baritoneInstance);
    }

    /**
     * Dispatch a command request through the handler pattern.
     *
     * @param request The command request containing command name and parameters
     * @param clientSocket The client socket (may be null for testing)
     * @param client Minecraft client instance
     * @param baritone Baritone API instance
     * @return CommandResult containing the execution outcome
     */
    public CommandResult dispatchCommand(JsonObject request, Socket clientSocket, Minecraft client, IBaritone baritone) {
        long startTime = System.currentTimeMillis();

        try {
            // Rate limiting check
            CommandResult rateLimitResult = checkRateLimit(clientSocket);
            if (rateLimitResult != null) {
                metricsCollector.recordCommandExecution("rate_limited", false, System.currentTimeMillis() - startTime);
                metricsCollector.recordError("rate_limited");
                return rateLimitResult;
            }

            if (request == null || !request.has("command")) {
                metricsCollector.recordCommandExecution("invalid_request", false, System.currentTimeMillis() - startTime);
                metricsCollector.recordError("invalid_request");
                return CommandResult.error("Missing command");
            }

            String command = request.get("command").getAsString();
            JsonObject params = request.has("params") ? request.getAsJsonObject("params") : new JsonObject();
            if ("get_entities".equals(command) && !params.has("action")) {
                params.addProperty("action", "entities");
            } else if ("get_combat_snapshot".equals(command) && !params.has("action")) {
                params.addProperty("action", "combat_snapshot");
            } else if (("come".equals(command) || "follow".equals(command))
                    && !params.has("action")) {
                params.addProperty("action", command);
            }

            logger.debug("Dispatching command: {}", command);

            // Circuit breaker check
            if (!READ_ONLY_COMMANDS.contains(command)
                    && !CIRCUIT_BYPASS_COMMANDS.contains(command)
                    && !circuitBreaker.allowRequest()) {
                logger.warn("Circuit breaker is OPEN, rejecting command: {}", command);
                CommandResult result = CommandResult.error("Service temporarily unavailable (circuit breaker open)");
                metricsCollector.recordCommandExecution(command, false, System.currentTimeMillis() - startTime);
                metricsCollector.recordError("circuit_breaker_open");
                return result;
            }

            // Try cache for idempotent commands
            if (commandCache.isIdempotentCommand(command)) {
                CommandResult cachedResult = commandCache.get(command, params);
                if (cachedResult != null) {
                    logger.debug("Cache hit for command: {}", command);
                    circuitBreaker.recordSuccess();
                    metricsCollector.recordCacheAccess(true);
                    metricsCollector.recordCommandExecution(command, true, System.currentTimeMillis() - startTime);
                    return cachedResult;
                }
                metricsCollector.recordCacheAccess(false);
            }

            // Invalidate all cached reads when state changes
            if (STATE_CHANGING_COMMANDS.contains(command)) {
                commandCache.invalidateAll();
            }

            CommandResult result;

            // Try mission controller first (highest priority)
            if (missionController != null) {
                JsonObject missionData = new JsonObject();
                if (missionController.tryHandle(command, params, missionData, client, baritone, clientSocket)) {
                    logger.debug("Command handled by mission controller: {}", command);
                    result = CommandResult.success(missionData);
                } else {
                    result = null;
                }
            } else {
                result = null;
            }

            if (result == null) {
                // Try command handler factory
                CommandHandler handler;
                try {
                    handler = CommandHandlerFactory.getHandler(command);
                } catch (IllegalStateException e) {
                    // Registered but not constructible: an internal defect,
                    // not an unknown command. Surface it, otherwise the legacy
                    // path below answers "Unknown command or no handler found"
                    // and hides the real cause.
                    logger.error("Handler instantiation failed for: {}", command, e);
                    handler = null;
                    result = CommandResult.error(e.getMessage());
                }
                if (handler != null) {
                    logger.debug("Found handler for command: {}", command);

                    // Enhanced validation before execution
                    try {
                        CommandResult validationResult = validateCommandParameters(command, params);
                        if (validationResult != null) {
                            logger.debug("Command validation failed for: {}", command);
                            result = validationResult;
                        } else {
                            // Execute with timeout monitoring and retry logic
                            result = executeWithTimeoutAndRetry(handler, params, client, baritone, clientSocket, startTime, command);
                        }
                    } catch (CommandValidationException e) {
                        logger.warn("Command validation exception for {}: {}", command, e.getMessage());
                        result = e.toCommandResult();
                    }
                } else if (result == null) {
                    // Fallback to legacy processing
                    logger.debug("No handler found, falling back to legacy processing for: {}", command);
                    if (legacyHandler != null) {
                        result = legacyHandler.handleLegacyCommand(command, params, client, baritone, clientSocket);
                    } else {
                        result = CommandResult.error("Unknown command: " + command + " (no legacy handler configured)");
                    }
                }
            }

            // Cache successful results for idempotent commands
            if (result.isSuccess() && commandCache.isIdempotentCommand(command)) {
                commandCache.put(command, params, result);
            }

            // A normal command rejection (invalid parameters, no path, missing
            // resources, occupied block, etc.) is not an infrastructure
            // outage. Only unexpected dispatch exceptions count as circuit
            // failures; successful calls continue to close/recover the circuit.
            if (result.isSuccess()) {
                circuitBreaker.recordSuccess();
            }

            // Record metrics
            metricsCollector.recordCommandExecution(command, result.isSuccess(), System.currentTimeMillis() - startTime);
            if (!result.isSuccess()) {
                metricsCollector.recordError("command_failure");
            }

            return result;

        } catch (Exception e) {
            logger.error("Command dispatch error for command: " +
                (request != null && request.has("command") ? request.get("command").getAsString() : "unknown"), e);
            circuitBreaker.recordFailure();
            CommandResult result = CommandResult.error("Command execution failed: " + e.getMessage());
            metricsCollector.recordCommandExecution("unknown", false, System.currentTimeMillis() - startTime);
            metricsCollector.recordError("dispatch_exception");
            return result;
        }
    }

    /**
     * Check rate limiting for the client socket.
     *
     * @param clientSocket The client socket to check
     * @return CommandResult if rate limited, null if allowed
     */
    private CommandResult checkRateLimit(Socket clientSocket) {
        if (clientSocket == null) return null; // Allow for testing

        long now = System.currentTimeMillis();

        // Entries are keyed by Socket and would otherwise accumulate forever
        // across reconnects; lazily evict sockets whose window has long expired.
        if (lastRequestTimes.size() > 16) {
            lastRequestTimes.entrySet().removeIf(e -> (now - e.getValue()) > 2 * RATE_LIMIT_WINDOW_MS);
            requestCounts.keySet().retainAll(lastRequestTimes.keySet());
        }

        Long lastTime = lastRequestTimes.get(clientSocket);

        if (lastTime == null || (now - lastTime) > RATE_LIMIT_WINDOW_MS) {
            // New window
            lastRequestTimes.put(clientSocket, now);
            requestCounts.put(clientSocket, 1);
            return null;
        }

        int count = requestCounts.getOrDefault(clientSocket, 0) + 1;
        requestCounts.put(clientSocket, count);

        if (count > RATE_LIMIT_REQUESTS) {
            JsonObject data = new JsonObject();
            data.addProperty("retry_after_ms", RATE_LIMIT_WINDOW_MS - (now - lastTime));
            return CommandResult.error("Rate limit exceeded. Maximum " + RATE_LIMIT_REQUESTS +
                " requests per " + (RATE_LIMIT_WINDOW_MS / 1000) + " seconds");
        }

        return null;
    }

    /**
     * Execute a command from the priority queue.
     *
     * @param pCmd The prioritized command to execute
     * @param client Minecraft client
     * @param baritone Baritone API instance
     * @return CommandResult from command execution
     */
    private CommandResult executeCommand(PrioritizedCommand pCmd, Minecraft client, IBaritone baritone) {
        JsonObject request = pCmd.request;
        Socket clientSocket = pCmd.clientSocket;
        String command = request.get("command").getAsString();
        JsonObject params = request.has("params") ? request.getAsJsonObject("params") : new JsonObject();

        long startTime = System.currentTimeMillis();

        try {
            logger.debug("Executing command: {} id: {}", command, pCmd.id);

            // Circuit breaker check
            if (!circuitBreaker.allowRequest()) {
                logger.warn("Circuit breaker is OPEN, rejecting command: {}", command);
                CommandResult result = CommandResult.error("Service temporarily unavailable (circuit breaker open)");
                metricsCollector.recordCommandExecution(command, false, System.currentTimeMillis() - startTime);
                return result;
            }

            // Try cache for idempotent commands
            if (commandCache.isIdempotentCommand(command)) {
                CommandResult cachedResult = commandCache.get(command, params);
                if (cachedResult != null) {
                    logger.debug("Cache hit for command: {}", command);
                    circuitBreaker.recordSuccess();
                    metricsCollector.recordCacheAccess(true);
                    metricsCollector.recordCommandExecution(command, true, System.currentTimeMillis() - startTime);
                    return cachedResult;
                }
                metricsCollector.recordCacheAccess(false);
            }

            // Invalidate all cached reads when state changes
            if (STATE_CHANGING_COMMANDS.contains(command)) {
                commandCache.invalidateAll();
            }

            CommandResult result;

            // Try mission controller first (highest priority)
            if (missionController != null) {
                JsonObject missionData = new JsonObject();
                if (missionController.tryHandle(command, params, missionData, client, baritone, clientSocket)) {
                    logger.debug("Command handled by mission controller: {}", command);
                    result = CommandResult.success(missionData);
                } else {
                    result = null;
                }
            } else {
                result = null;
            }

            if (result == null) {
                // Try command handler factory
                CommandHandler handler;
                try {
                    handler = CommandHandlerFactory.getHandler(command);
                } catch (IllegalStateException e) {
                    logger.error("Handler instantiation failed for: {}", command, e);
                    handler = null;
                    result = CommandResult.error(e.getMessage());
                }
                if (handler != null) {
                    logger.debug("Found handler for command: {}", command);

                    // Execute with timeout monitoring and retry logic
                    result = executeWithTimeoutAndRetry(handler, params, client, baritone, clientSocket, startTime, command);
                } else if (result == null) {
                    // Fallback to legacy processing
                    logger.debug("No handler found, falling back to legacy processing for: {}", command);
                    if (legacyHandler != null) {
                        result = legacyHandler.handleLegacyCommand(command, params, client, baritone, clientSocket);
                    } else {
                        result = CommandResult.error("Unknown command: " + command + " (no legacy handler configured)");
                    }
                }
            }

            // Cache successful results for idempotent commands
            if (result.isSuccess() && commandCache.isIdempotentCommand(command)) {
                commandCache.put(command, params, result);
            }

            // Record circuit breaker success/failure
            if (result.isSuccess()) {
                circuitBreaker.recordSuccess();
            } else {
                circuitBreaker.recordFailure();
            }

            // Record metrics
            metricsCollector.recordCommandExecution(command, result.isSuccess(), System.currentTimeMillis() - startTime);

            // Record error if failed
            if (!result.isSuccess()) {
                metricsCollector.recordError("command_failure");
            }

            return result;

        } catch (Exception e) {
            logger.error("Command execution error for command: {} id: {}", command, pCmd.id, e);
            circuitBreaker.recordFailure();
            CommandResult result = CommandResult.error("Command execution failed: " + e.getMessage());
            metricsCollector.recordCommandExecution(command, false, System.currentTimeMillis() - startTime);
            metricsCollector.recordError("execution_exception");
            return result;
        }
    }

    /**
     * Execute a command handler with timeout monitoring and retry logic.
     *
     * @param handler The command handler to execute
     * @param params Command parameters
     * @param client Minecraft client
     * @param baritone Baritone API instance
     * @param clientSocket Client socket
     * @param startTime Start time for timeout calculation
     * @param commandName Name of the command for logging
     * @return CommandResult from handler execution
     */
    private CommandResult executeWithTimeoutAndRetry(CommandHandler handler, JsonObject params,
            Minecraft client, IBaritone baritone, Socket clientSocket, long startTime, String commandName) {

        Supplier<CommandResult> operation = () -> {
            CompletableFuture<CommandResult> future = null;
            try {
                // Execute handler asynchronously and wait for completion with timeout
                future = handler.handle(params, client, baritone, clientSocket);

                // Calculate remaining timeout
                long remainingTimeout = Math.max(1, COMMAND_TIMEOUT_MS - (System.currentTimeMillis() - startTime));

                // Wait for the result with timeout
                CommandResult result = future.get(remainingTimeout, TimeUnit.MILLISECONDS);

                long elapsed = System.currentTimeMillis() - startTime;
                if (elapsed > COMMAND_TIMEOUT_MS) {
                    logger.warn("Command execution exceeded timeout: {}ms for handler: {}",
                        elapsed, handler.getCommandName());
                    // Note: We still return the result even if it timed out, as the handler may have partially succeeded
                }

                return result;
            } catch (java.util.concurrent.TimeoutException e) {
                if (future != null) {
                    future.cancel(true);
                }
                throw new RuntimeException("Command execution timed out for handler: " + handler.getCommandName(), e);
            } catch (java.util.concurrent.ExecutionException e) {
                throw new RuntimeException("Command execution failed for handler: " + handler.getCommandName(), e.getCause());
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException("Command execution was interrupted for handler: " + handler.getCommandName(), e);
            }
        };

        // Retry only commands explicitly classified as read-only. Treat every
        // unknown or future route as a possible mutation so additions fail
        // safe instead of being replayed after an uncertain timeout.
        if (!READ_ONLY_COMMANDS.contains(commandName)
                || "get_events".equals(commandName)) {
            return operation.get();
        }
        return retryHandler.executeWithRetry(operation, "command:" + commandName);
    }







    /**
     * Validate command parameters using the enhanced validation framework.
     *
     * @param command The command name
     * @param params The parameters to validate
     * @return CommandResult with validation errors if any, null if valid
     * @throws CommandValidationException if validation fails critically
     */
    private CommandResult validateCommandParameters(String command, JsonObject params) throws CommandValidationException {
        Map<String, ParameterValidator.Validator> validations = new HashMap<>();

        // Define validation rules for each command
        switch (command) {
            case "goto":
                validations.put("coordinates", ParameterValidator.coordinates());
                validations.put("radius", ParameterValidator.integer(0, 100));
                break;

            case "mine":
                validations.put("block", ParameterValidator.blockId());
                validations.put("quantity", ParameterValidator.integer(1, 1000));
                break;

            case "craft":
            case "auto_craft":
                validations.put("item", ParameterValidator.itemName());
                validations.put("quantity", ParameterValidator.integer(1, 1000));
                break;

            case "settings":
                // Settings can be any key-value pairs, basic validation only
                break;

            case "tunnel":
                validations.put("length", ParameterValidator.integer(1, 1000));
                validations.put("width", ParameterValidator.integer(1, 10));
                validations.put("height", ParameterValidator.integer(1, 10));
                break;

            case "build":
                validations.put("schematic", ParameterValidator.string(null, 1, 255));
                break;

            case "explore":
                validations.put("radius", ParameterValidator.integer(10, 10000));
                break;

            case "farm":
                validations.put("range", ParameterValidator.integer(0, 512));
                break;

            case "select_slot":
                validations.put("slot", ParameterValidator.integer(0, 8));
                break;

            case "place_block":
            case "break_block":
            case "dig_block":
            case "interact_block":
            case "attack_block":
                validations.put("coordinates", ParameterValidator.coordinates());
                if ("place_block".equals(command)) {
                    validations.put("block", ParameterValidator.blockId());
                }
                break;

            case "use_item":
                validations.put("duration_ms", ParameterValidator.integer(0, 60000));
                validations.put("hand", ParameterValidator.oneOf(
                    "MAIN_HAND", "OFF_HAND", "main_hand", "off_hand"));
                validations.put("slot", ParameterValidator.integer(0, 35));
                validations.put("quantity", ParameterValidator.integer(1, 64));
                break;

            case "throw_item":
                validations.put("slot", ParameterValidator.integer(0, 35));
                validations.put("quantity", ParameterValidator.integer(1, 64));
                break;

            case "smelt_items":
                validations.put("item", ParameterValidator.itemName());
                validations.put("quantity", ParameterValidator.integer(1, 64));
                break;

            case "screenshot":
                validations.put("filename", ParameterValidator.string(null, 1, 255));
                break;

            // Add more command validations as needed
            default:
                // No specific validation for unknown commands
                break;
        }

        if (!validations.isEmpty()) {
            var errors = ParameterValidator.validateAll(params, validations);
            return ParameterValidator.createResult(errors);
        }

        return null; // No validation errors
    }

    /**
     * Clear rate limiting data for a specific socket (useful for testing or cleanup).
     *
     * @param clientSocket The socket to clear
     */
    public void clearRateLimit(Socket clientSocket) {
        lastRequestTimes.remove(clientSocket);
        requestCounts.remove(clientSocket);
    }

    /**
     * Reset circuit breaker to closed state (debug/testing).
     */
    public void resetCircuitBreaker() {
        circuitBreaker.reset();
        metricsCollector.recordCircuitBreakerStateChange(false);
    }

    /**
     * Get the metrics collector for accessing performance metrics.
     */
    public MetricsCollector getMetricsCollector() {
        return metricsCollector;
    }

    /**
     * Shutdown the command dispatcher and cleanup resources.
     * Should be called when the bridge is shutting down.
     */
    public void shutdown() {
        commandCache.shutdown();
        logger.info("CommandDispatcher shutdown complete");
    }
}
