package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.Socket;
import java.util.*;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;

/**
 * Command handler for executing sequences of commands with rollback capabilities.
 * Supports the format: sequence <command1> [args1] ; <command2> [args2] ; ...
 */
public class SequenceCommandHandler extends AbstractCommandHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(SequenceCommandHandler.class);

    /**
     * Represents a parsed command in the sequence with its parameters and rollback operation.
     */
    static class SequenceCommand {
        public final String command;
        public final JsonObject params;
        public final String rawCommand;
        public RollbackOperation rollbackOperation;

        SequenceCommand(String command, JsonObject params, String rawCommand) {
            this.command = command;
            this.params = params;
            this.rawCommand = rawCommand;
            this.rollbackOperation = null;
        }

        void setRollbackOperation(RollbackOperation operation) {
            this.rollbackOperation = operation;
        }

        boolean hasRollback() {
            return rollbackOperation != null;
        }
    }

    @Override
    protected CommandResult execute(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        // Extract the sequence string
        String sequence;
        if (params.has("sequence")) {
            sequence = params.get("sequence").getAsString();
        } else if (params.has("commands")) {
            sequence = params.get("commands").getAsString();
        } else {
            return CommandResult.error("Missing sequence parameter. Use 'sequence' or 'commands' to specify the command sequence.");
        }

        if (sequence == null || sequence.trim().isEmpty()) {
            return CommandResult.error("Empty sequence provided");
        }

        try {
            // Parse the sequence into individual commands
            List<SequenceCommand> commands = parseSequence(sequence);
            if (commands.isEmpty()) {
                return CommandResult.error("No valid commands found in sequence");
            }

            // Validate the entire sequence before execution. Validation failures
            // are reported as a success envelope carrying "validation_errors";
            // nothing may execute in that case (previously this fell through and
            // ran the valid prefix of an invalid sequence).
            CommandResult validationResult = validateSequence(commands, client, baritone, clientSocket);
            if (!validationResult.isSuccess()
                    || (validationResult.getData() != null && validationResult.getData().has("validation_errors"))) {
                return validationResult;
            }

            // Execute the sequence with rollback capability
            return executeSequence(commands, client, baritone, clientSocket);

        } catch (Exception e) {
            LOGGER.error("Error processing sequence command", e);
            return CommandResult.error("Sequence execution failed: " + e.getMessage());
        }
    }

    @Override
    public String getCommandName() {
        return "sequence";
    }

    /**
     * The sequence handler only delegates; each sub-command enforces its own
     * player requirement. Requiring a player here would also block sequences
     * of offline-safe commands (settings, get_version, ...).
     */
    @Override
    public boolean requiresPlayer() {
        return false;
    }

    /**
     * Parse a sequence string into individual commands.
     * Format: "command1 arg1 arg2 ; command2 arg3 ; command3"
     */
    List<SequenceCommand> parseSequence(String sequence) {
        List<SequenceCommand> commands = new ArrayList<>();

        // Split by semicolon, but be careful with semicolons inside quotes
        String[] commandStrings = sequence.split(";(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)");

        for (String cmdStr : commandStrings) {
            cmdStr = cmdStr.trim();
            if (cmdStr.isEmpty()) continue;

            // Parse individual command
            SequenceCommand parsed = parseCommand(cmdStr);
            if (parsed != null) {
                commands.add(parsed);
            }
        }

        return commands;
    }

    /**
     * Parse a single command string into a SequenceCommand object.
     */
    SequenceCommand parseCommand(String commandStr) {
        String[] parts = commandStr.trim().split("\\s+", 2);
        if (parts.length == 0 || parts[0].isEmpty()) {
            return null;
        }

        String command = parts[0];
        JsonObject params = new JsonObject();

        // Store the full command string
        params.addProperty("_raw_command", commandStr);

        // Parse additional arguments based on command type
        if (parts.length > 1) {
            parseCommandArgs(command, parts[1], params);
        }

        return new SequenceCommand(command, params, commandStr);
    }

    /**
     * Parse command-specific arguments into the params object.
     */
    private void parseCommandArgs(String command, String args, JsonObject params) {
        switch (command) {
            case "craft":
                // craft <item> [count]
                String[] craftParts = args.split("\\s+", 2);
                params.addProperty("item", craftParts[0]);
                if (craftParts.length > 1) {
                    try {
                        params.addProperty("count", Integer.parseInt(craftParts[1]));
                    } catch (NumberFormatException e) {
                        params.addProperty("count", 1);
                    }
                } else {
                    params.addProperty("count", 1);
                }
                break;

            case "goto":
                // goto <x> <y> <z>
                String[] gotoParts = args.split("\\s+");
                if (gotoParts.length >= 3) {
                    try {
                        params.addProperty("x", Integer.parseInt(gotoParts[0]));
                        params.addProperty("y", Integer.parseInt(gotoParts[1]));
                        params.addProperty("z", Integer.parseInt(gotoParts[2]));
                    } catch (NumberFormatException e) {
                        LOGGER.warn("Invalid coordinates in goto command: {}", args);
                    }
                }
                break;

            case "mine":
                // 两种参数顺序都要吃，因为调用方来自两个不同年代的约定：
                //   mine <block_type> [count]          <- 老写法
                //   mine <count> <block_type>          <- MCP 的 mine_quantity 就是这么拼的
                // 而且真正的 handler 认的是 block_type / quantity，
                // 老代码写的 "block" / "count" 名字对不上（实测报 Missing block_type）。
                String[] mineParts = args.split("\\s+");
                if (mineParts.length >= 1 && !mineParts[0].isEmpty()) {
                    int next = 1;
                    Integer lead = tryParseInt(mineParts[0]);
                    if (lead != null && mineParts.length >= 2) {
                        params.addProperty("quantity", lead);
                        params.addProperty("count", lead);
                        params.addProperty("block_type", mineParts[1]);
                        params.addProperty("block", mineParts[1]);
                        next = 2;
                    } else {
                        params.addProperty("block_type", mineParts[0]);
                        params.addProperty("block", mineParts[0]);
                        if (mineParts.length > 1) {
                            Integer n = tryParseInt(mineParts[1]);
                            if (n != null) {
                                params.addProperty("quantity", n);
                                params.addProperty("count", n);
                                next = 2;
                            }
                        }
                    }
                    // 后面若还有三组数字，当坐标
                    if (mineParts.length >= next + 3) {
                        Integer mx = tryParseInt(mineParts[next]);
                        Integer my = tryParseInt(mineParts[next + 1]);
                        Integer mz = tryParseInt(mineParts[next + 2]);
                        if (mx != null && my != null && mz != null) {
                            params.addProperty("x", mx);
                            params.addProperty("y", my);
                            params.addProperty("z", mz);
                        }
                    }
                }
                break;

            case "follow":
                // follow player <name> | follow entity <type> | follow <target>
                // GotoCommandHandler 靠 params.action 区分 goto/come/follow，
                // 缺了它就会掉进 goto 分支去要 x/y/z（实测报 Missing coordinate parameters）。
                params.addProperty("action", "follow");
                String followArgs = args.trim();
                String[] followParts = followArgs.split("\\s+", 2);
                if (followParts.length == 2
                        && (followParts[0].equalsIgnoreCase("player")
                                || followParts[0].equalsIgnoreCase("entity"))) {
                    params.addProperty("entity", followParts[1]);
                } else if (!followArgs.isEmpty()) {
                    params.addProperty("entity", followArgs);
                } else {
                    params.addProperty("entity", "player");
                }
                break;

            case "come":
                params.addProperty("action", "come");
                break;

            default:
                // Generic parameter handling - store the raw args
                params.addProperty("args", args);
                break;
        }
    }

    /** 能解析成整数就返回它，否则 null —— 用来容忍两种参数顺序。 */
    private static Integer tryParseInt(String value) {
        if (value == null) {
            return null;
        }
        try {
            return Integer.parseInt(value.trim());
        } catch (NumberFormatException e) {
            return null;
        }
    }

    /**
     * Validate the entire sequence before execution.
     */
    private CommandResult validateSequence(List<SequenceCommand> commands, Minecraft client, IBaritone baritone, Socket clientSocket) {
        JsonObject validationErrors = new JsonObject();
        boolean hasErrors = false;

        for (int i = 0; i < commands.size(); i++) {
            SequenceCommand cmd = commands.get(i);
            CommandResult singleValidation = validateCommand(cmd, client, baritone, clientSocket);

            if (!singleValidation.isSuccess()) {
                validationErrors.addProperty("command_" + i, singleValidation.getErrorMessage());
                hasErrors = true;
            }
        }

        if (hasErrors) {
            JsonObject result = new JsonObject();
            result.add("validation_errors", validationErrors);
            result.addProperty("error", "Sequence validation failed");
            return CommandResult.success(result); // Return as success with error info
        }

        return CommandResult.success();
    }

    /**
     * Validate a single command.
     */
    private CommandResult validateCommand(SequenceCommand cmd, Minecraft client, IBaritone baritone, Socket clientSocket) {
        // Check if the command handler exists
        CommandHandler handler = CommandHandlerFactory.getHandler(cmd.command);
        if (handler == null) {
            return CommandResult.error("Unknown command: " + cmd.command);
        }

        // For now, basic validation - could be extended for command-specific validation
        return CommandResult.success();
    }

    /**
     * Execute the sequence with rollback capability.
     */
    private CommandResult executeSequence(List<SequenceCommand> commands, Minecraft client, IBaritone baritone, Socket clientSocket) {
        List<SequenceCommand> executedCommands = new ArrayList<>();
        JsonObject results = new JsonObject();
        JsonObject partialResults = new JsonObject();

        BlockPos originalPosition = (client.player != null) ? client.player.blockPosition() : null;

        try {
            for (int i = 0; i < commands.size(); i++) {
                SequenceCommand cmd = commands.get(i);
                LOGGER.info("Executing sequence command {}: {}", i + 1, cmd.rawCommand);

                // Execute the command
                CommandResult result = executeSingleCommand(cmd, client, baritone, clientSocket);

                // Store result
                partialResults.add("step_" + i, result.toJson());

                if (result.isSuccess()) {
                    executedCommands.add(cmd);

                    // Set up rollback operation based on command type
                    setupRollbackOperation(cmd, result, originalPosition);

                } else {
                    // Command failed - rollback all previous commands
                    LOGGER.warn("Sequence command {} failed: {}", i + 1, result.getErrorMessage());

                    CommandResult rollbackResult = rollbackExecutedCommands(executedCommands, client, baritone, clientSocket);

                    results.add("partial_results", partialResults);
                    results.addProperty("failed_at_step", i);
                    results.addProperty("error", result.getErrorMessage());
                    if (!rollbackResult.isSuccess()) {
                        results.addProperty("rollback_error", rollbackResult.getErrorMessage());
                    }
                    results.addProperty("rollback_performed", true);

                    return CommandResult.success(results);
                }
            }

            // All commands succeeded
            results.add("partial_results", partialResults);
            results.addProperty("status", "completed");
            results.addProperty("steps_executed", commands.size());
            return CommandResult.success(results);

        } catch (Exception e) {
            LOGGER.error("Sequence execution failed with exception", e);

            // Rollback on exception
            CommandResult rollbackResult = rollbackExecutedCommands(executedCommands, client, baritone, clientSocket);

            results.add("partial_results", partialResults);
            results.addProperty("error", "Execution failed: " + e.getMessage());
            if (!rollbackResult.isSuccess()) {
                results.addProperty("rollback_error", rollbackResult.getErrorMessage());
            }
            results.addProperty("rollback_performed", true);

            return CommandResult.success(results);
        }
    }

    /**
     * Execute a single command in the sequence.
     */
    private CommandResult executeSingleCommand(SequenceCommand cmd, Minecraft client, IBaritone baritone, Socket clientSocket) {
        CommandHandler handler = CommandHandlerFactory.getHandler(cmd.command);
        if (handler == null) {
            return CommandResult.error("Handler not found for command: " + cmd.command);
        }

        try {
            // Execute the command
            CompletableFuture<CommandResult> future = handler.handle(cmd.params, client, baritone, clientSocket);
            CommandResult result = future.get(); // Wait for completion

            return result;

        } catch (Exception e) {
            LOGGER.error("Command execution failed: {}", cmd.command, e);
            return CommandResult.error("Command '" + cmd.command + "' failed: " + e.getMessage());
        }
    }

    /**
     * Set up rollback operation for a successfully executed command.
     */
    private void setupRollbackOperation(SequenceCommand cmd, CommandResult result, BlockPos originalPosition) {
        switch (cmd.command) {
            case "craft":
                // For crafting, we could track ingredients used
                // This would require more complex inventory tracking
                if (result.getData().has("crafted") && result.getData().get("crafted").getAsBoolean()) {
                    // Note: Full crafting rollback is complex to implement
                    // For now, we'll create a placeholder
                    cmd.setRollbackOperation(new RollbackOperations.CraftingRollback(
                        cmd.params.has("item") ? cmd.params.get("item").getAsString() : "unknown",
                        cmd.params.has("count") ? cmd.params.get("count").getAsInt() : 1,
                        new HashMap<>() // Would need to track actual ingredients used
                    ));
                }
                break;

            case "goto":
                // Movement rollback - if this moved the player, we can path back
                if (originalPosition != null) {
                    cmd.setRollbackOperation(new RollbackOperations.MovementRollback(originalPosition));
                }
                break;

            // Add more command-specific rollback setup as needed
        }
    }

    /**
     * Rollback all executed commands in reverse order.
     */
    private CommandResult rollbackExecutedCommands(List<SequenceCommand> executedCommands, Minecraft client, IBaritone baritone, Socket clientSocket) {
        if (executedCommands.isEmpty()) {
            return CommandResult.success();
        }

        LOGGER.info("Rolling back {} executed commands", executedCommands.size());

        // Rollback in reverse order
        List<CompletableFuture<Void>> rollbackFutures = new ArrayList<>();
        for (int i = executedCommands.size() - 1; i >= 0; i--) {
            SequenceCommand cmd = executedCommands.get(i);
            if (cmd.hasRollback()) {
                LOGGER.info("Rolling back command: {}", cmd.rawCommand);
                CompletableFuture<Void> future = cmd.rollbackOperation.rollback(client, baritone, clientSocket);
                rollbackFutures.add(future);
            }
        }

        try {
            // Wait for all rollbacks to complete
            CompletableFuture.allOf(rollbackFutures.toArray(new CompletableFuture[0])).get();
            return CommandResult.success();

        } catch (Exception e) {
            LOGGER.error("Rollback failed", e);
            return CommandResult.error("Rollback failed: " + e.getMessage());
        }
    }
}