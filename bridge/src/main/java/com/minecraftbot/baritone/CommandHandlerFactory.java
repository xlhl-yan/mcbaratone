package com.minecraftbot.baritone;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Factory for creating command handler instances based on command names.
 * Provides a registry for command handlers and manages their instantiation.
 */
public class CommandHandlerFactory {
    private static final Logger logger = LoggerFactory.getLogger(CommandHandlerFactory.class);

    private static final Map<String, Class<? extends CommandHandler>> handlerRegistry = new ConcurrentHashMap<>();
    private static final Map<String, CommandHandler> handlerInstances = new ConcurrentHashMap<>();

    static {
        // Register command handlers here as they are implemented
        // This will be expanded as individual command handlers are created

        // Movement commands
        registerHandler("goto", GotoCommandHandler.class);
        registerHandler("come", GotoCommandHandler.class);
        registerHandler("follow", GotoCommandHandler.class);

        // Mining commands
        registerHandler("mine", MineCommandHandler.class);

        // Inventory commands
        // Inventory commands
        registerHandler("get_inventory", GetInventoryCommandHandler.class);
        registerHandler("inventory_click", InventoryClickCommandHandler.class);
        registerHandler("select_trade", SelectTradeCommandHandler.class);

        // State commands
        registerHandler("get_state", StateCommandHandler.class);
        registerHandler("get_entities", StateCommandHandler.class);
        registerHandler("get_combat_snapshot", StateCommandHandler.class);
        registerHandler("get_player_pos", PlayerPositionCommandHandler.class);
        registerHandler("scan_biomes", BiomeScanCommandHandler.class);
        // 只读：准星当前指着什么（以前这个数据只能靠 use_item 顺带带出来，但调它会真右键）
        registerHandler("get_look_target", GetLookTargetCommandHandler.class);

        // Build commands
        registerHandler("build", BuildCommandHandler.class);
        registerHandler("sel", BuildCommandHandler.class);
        registerHandler("inspect_build_site", BuildSiteCommandHandler.class);

        // Mission commands
        registerHandler("mission", MissionCommandHandler.class);

        // Explicit fixes for commands previously relying on legacy fallback
        registerHandler("place_block", PlaceBlockCommandHandler.class);
        registerHandler("use_bucket", UseBucketCommandHandler.class);
        registerHandler("get_block", GetBlockCommandHandler.class);
        registerHandler("chat", ChatCommandHandler.class);
        registerHandler("select_slot", SelectSlotCommandHandler.class);
        registerHandler("respawn", RespawnCommandHandler.class);
        registerHandler("get_recipes", RecipeCommandHandler.class);
        registerHandler("craft", CraftCommandHandler.class);
        registerHandler("find_blocks", FindBlocksCommandHandler.class);
        registerHandler("interact_block", InteractBlockCommandHandler.class);
        registerHandler("look_at", LookAtCommandHandler.class);
        registerHandler("look", LookCommandHandler.class);
        registerHandler("cancel", CancelCommandHandler.class);
        registerHandler("use_item", UseItemCommandHandler.class);
        registerHandler("attack_entity", AttackEntityCommandHandler.class);
        registerHandler("attack_block", AttackBlockCommandHandler.class);
        registerHandler("swing_hand", SwingHandCommandHandler.class);
        registerHandler("get_view", GetViewCommandHandler.class);
        registerHandler("close_screen", CloseScreenCommandHandler.class);
        registerHandler("equip", EquipCommandHandler.class);
        registerHandler("get_version", GetVersionCommandHandler.class);

        // Newly migrated command handlers
        registerHandler("explore", ExploreCommandHandler.class);
        registerHandler("stop", StopCommandHandler.class);
        registerHandler("pause", PauseCommandHandler.class);
        registerHandler("goal", GoalCommandHandler.class);
        registerHandler("farm", FarmCommandHandler.class);
        registerHandler("tunnel", TunnelCommandHandler.class);
        registerHandler("get_events", GetEventsCommandHandler.class);
        registerHandler("get_screen", GetScreenCommandHandler.class);
        registerHandler("settings", SettingsCommandHandler.class);
        registerHandler("break_block", BreakBlockCommandHandler.class);
        registerHandler("dig_block", DigBlockCommandHandler.class);
        registerHandler("set_fast_break", SetFastBreakCommandHandler.class);
        registerHandler("get_dimension", GetDimensionCommandHandler.class);
        registerHandler("get_death_location", GetDeathLocationCommandHandler.class);
        registerHandler("throw_item", ThrowItemCommandHandler.class);
        registerHandler("axis", AxisMineCommandHandler.class);
        registerHandler("strip", StripMineCommandHandler.class);
        registerHandler("quarry", QuarryCommandHandler.class);

        // Remaining Phase 1 handlers
        registerHandler("path", PathCommandHandler.class);
        registerHandler("tunnel_wide", TunnelWideCommandHandler.class);
        registerHandler("place_torches", PlaceTorchesCommandHandler.class);
        registerHandler("screenshot", ScreenshotCommandHandler.class);
        registerHandler("auto_craft", AutoCraftCommandHandler.class);
        registerHandler("smelt_items", SmeltItemsCommandHandler.class);
        registerHandler("click_recipe", ClickRecipeCommandHandler.class);
        registerHandler("place_fire", PlaceFireCommandHandler.class);
        registerHandler("place_recipe", PlaceRecipeCommandHandler.class);

        // Phase 4: Advanced Features
        registerHandler("entity_interact", EntityInteractionCommandHandler.class);
        registerHandler("entity_transport", EntityTransportCommandHandler.class);
        registerHandler("advanced_goal", AdvancedGoalCommandHandler.class);
        registerHandler("craft_advanced", AdvancedCraftCommandHandler.class);

        // Sequence Commands with Rollback
        registerHandler("sequence", SequenceCommandHandler.class);

        // 游戏消息该唤醒哪个 DSH 会话（玩家在游戏里敲 !waken <id> 改它）
        registerHandler("get_waken_session", GetWakenSessionCommandHandler.class);
        registerHandler("set_waken_session", SetWakenSessionCommandHandler.class);

        logger.info("Command handler registry initialized with {} handlers", handlerRegistry.size());
    }


    /**
     * Register a command handler class for a specific command name.
     *
     * @param commandName The command name
     * @param handlerClass The handler class
     */
    public static void registerHandler(String commandName, Class<? extends CommandHandler> handlerClass) {
        handlerRegistry.put(commandName, handlerClass);
        logger.debug("Registered command handler: {} -> {}", commandName, handlerClass.getSimpleName());
    }

    /**
     * Register a pre-instantiated command handler for a specific command name.
     * Use this for handlers that require constructor dependencies.
     *
     * @param commandName The command name
     * @param handler The handler instance
     */
    public static void registerHandlerInstance(String commandName, CommandHandler handler) {
        handlerInstances.put(commandName, handler);
        logger.debug("Registered command handler instance: {} -> {}", commandName, handler.getClass().getSimpleName());
    }

    /**
     * Get a command handler instance for the given command name.
     * Handlers are cached as singletons for performance.
     *
     * @param commandName The command name
     * @return The command handler instance, or null if not found
     */
    public static CommandHandler getHandler(String commandName) {
        if (commandName == null) {
            return null;
        }

        // Check if we already have an instance
        CommandHandler handler = handlerInstances.get(commandName);
        if (handler != null) {
            return handler;
        }

        // Try to create a new instance
        Class<? extends CommandHandler> handlerClass = handlerRegistry.get(commandName);
        if (handlerClass != null) {
            try {
                handler = handlerClass.getDeclaredConstructor().newInstance();
                handlerInstances.put(commandName, handler);
                logger.debug("Created command handler instance: {} -> {}", commandName, handler.getClass().getSimpleName());
                return handler;
            } catch (Exception e) {
                logger.error("Failed to create command handler instance for: " + commandName, e);
                return null;
            }
        }

        logger.warn("No handler registered for command: {}", commandName);
        return null;
    }

    /**
     * Check if a command handler is registered for the given command name.
     *
     * @param commandName The command name
     * @return true if a handler is registered, false otherwise
     */
    public static boolean hasHandler(String commandName) {
        return commandName != null && (handlerRegistry.containsKey(commandName) || handlerInstances.containsKey(commandName));
    }

    /**
     * Get all registered command names.
     *
     * @return Array of command names
     */
    public static String[] getRegisteredCommands() {
        return handlerRegistry.keySet().toArray(new String[0]);
    }

    /**
     * Clear all registered handlers and instances.
     * Useful for testing or reconfiguration.
     */
    public static void clearRegistry() {
        handlerRegistry.clear();
        handlerInstances.clear();
        logger.info("Cleared command handler registry");
    }

    /**
     * Get the number of registered handlers.
     *
     * @return The number of registered handlers
     */
    public static int getRegisteredHandlerCount() {
        return handlerRegistry.size();
    }
}
