package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.Socket;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.item.ItemStack;

public class CraftCommandHandler implements CommandHandler {

    private static final Logger LOGGER = LoggerFactory.getLogger(CraftCommandHandler.class);

    @Override
    public CompletableFuture<CommandResult> handle(JsonObject params, Minecraft client, IBaritone baritone, Socket clientSocket) {
        if (client.player == null || client.gameMode == null) {
            return CompletableFuture.completedFuture(CommandResult.error("Player not available"));
        }
        
        // Accept both 'recipe', 'item', and 'recipe_id' parameters
        String recipeId = null;
        if (params.has("recipe")) {
            recipeId = params.get("recipe").getAsString();
        } else if (params.has("item")) {
            recipeId = params.get("item").getAsString(); 
        } else if (params.has("recipe_id")) {
            recipeId = params.get("recipe_id").getAsString();
        }
        
        if (recipeId == null || recipeId.isEmpty()) {
            return CompletableFuture.completedFuture(CommandResult.error("Missing recipe/item argument"));
        }
        
        int count = params.has("count") ? params.get("count").getAsInt() : 1;
        
        // Normalize recipe ID
        if (!recipeId.contains(":")) {
            recipeId = "minecraft:" + recipeId;
        }
        
        // Get recipe definition
        CraftRecipe recipe = getRecipeDefinition(recipeId);
        if (recipe == null) {
            JsonObject err = new JsonObject();
            err.addProperty("error", "Unknown recipe: " + recipeId);
            err.addProperty("note", "Recipe not in hardcoded list. Add to getRecipeDefinition().");
            return CompletableFuture.completedFuture(CommandResult.success(err)); // Return as success with error field to match old behavior or change to error? 
            // Old behavior: data.addProperty("error", ...) then return success(data) sometimes? 
            // Actually old behavior returns data with error. CommandHandler usually returns CommandResult.error for hard errors.
            // Let's return CommandResult.error but maybe with data?
            // The dispatcher wraps CommandResult.error into {status: error, error: msg}.
            // So return CommandResult.error("Unknown recipe: " + recipeId);
        }
        
        // Check if we need a crafting table
        boolean hasCraftingTable = client.player.containerMenu instanceof net.minecraft.world.inventory.CraftingMenu;
        if (recipe.requiresTable && !hasCraftingTable) {
            return CompletableFuture.completedFuture(CommandResult.error("Recipe requires crafting table but none is open"));
        }
        
        int crafted = 0;
        int syncId = client.player.containerMenu.containerId;
        JsonObject data = new JsonObject();
        
        // Craft the requested count
        for (int i = 0; i < count; i += recipe.outputCount) {
            try {
                // Check ingredients
                if (!hasIngredients(client, recipe)) {
                    if (crafted == 0) {
                        return CompletableFuture.completedFuture(CommandResult.error("Missing ingredients for " + recipeId + " (Have " + dumpIngredients(client, recipe) + ")"));
                    }
                    break;
                }
                
                // Clear crafting grid first - use actual screen type, not recipe requirement
                clearCraftingGrid(client, syncId, hasCraftingTable);
                Thread.sleep(100); // Increased delay
                
                // Place ingredients in grid - pass actual screen type for correct slot mapping
                String result = placeIngredients(client, syncId, recipe, hasCraftingTable);
                if (result != null) {
                    return CompletableFuture.completedFuture(CommandResult.error("Failed to place ingredients: " + result));
                }
                Thread.sleep(150); // Increased delay

                // Verify the grid actually holds what the recipe asked for. Each PUT only gets
                // 60ms, so a late one silently changes the recipe: stone_pickaxe minus one
                // cobblestone is exactly the shape of a stone hoe, and Minecraft happily crafts
                // that. The old code never looked, so it reported success for the wrong item.
                String gridError = verifyCraftingGrid(client, syncId, recipe, hasCraftingTable);
                if (gridError != null) {
                    if (crafted > 0) {
                        break;
                    }
                    return CompletableFuture.completedFuture(CommandResult.error(
                        "Crafting grid does not match " + recipeId + ": " + gridError));
                }

                // Click output slot (slot 0) to craft. Count the result before and after
                // instead of trusting the click: a QUICK_MOVE on an empty/unfilled output
                // does nothing at all, and the old code booked it as a craft anyway.
                int before = countItemInInventory(client, recipeId);
                client.execute(() -> {
                    client.gameMode.handleContainerInput(syncId, 0, 0, ContainerInput.QUICK_MOVE, client.player);
                });
                Thread.sleep(200); // Increased delay

                // QUICK_MOVE on the output slot sometimes leaves the crafted item on the cursor
                // instead of putting it into the inventory (confirmed live on 2026-10-16). Clear
                // the cursor first: otherwise the inventory shows "nothing gained" and a
                // successful craft is reported as a failure.
                clearCursorIntoInventory(client, syncId, hasCraftingTable);

                int gained = countItemInInventory(client, recipeId) - before;
                if (gained <= 0) {
                    if (crafted > 0) {
                        break;
                    }
                    return CompletableFuture.completedFuture(CommandResult.error(
                        "Craft produced no " + recipeId + " (grid held the right items, but the"
                            + " output slot did not yield; expected " + recipe.outputCount + ")"));
                }
                crafted += gained;            } catch (Exception e) {
                LOGGER.error("Crafting error", e);
                if (crafted == 0) {
                    return CompletableFuture.completedFuture(CommandResult.error("Crafting failed: " + e.getMessage()));
                }
                break;
            }
        }
        
        data.addProperty("crafted", crafted > 0);
        data.addProperty("count", crafted);
        data.addProperty("recipe", recipeId);
        if (crafted > 0) {
            data.addProperty("status", "ok");
        }
        
        return CompletableFuture.completedFuture(CommandResult.success(data));
    }

    @Override
    public String getCommandName() {
        return "craft";
    }

    // ================= Helper Methods =================

    private static class CraftRecipe {
        final String output;
        final int outputCount;
        final boolean requiresTable; // true = 3x3, false = 2x2
        final String[] grid; // 2x2 or 3x3 grid, null = empty slot, "any_log" = any log type
        final Map<String, Integer> ingredients; // Ingredient counts
        
        CraftRecipe(String output, int outputCount, boolean requiresTable, String[] grid) {
            this.output = output;
            this.outputCount = outputCount;
            this.requiresTable = requiresTable;
            this.grid = grid;
            this.ingredients = new java.util.HashMap<>();
            for (String s : grid) {
                if (s != null && !s.isEmpty()) {
                    ingredients.merge(s, 1, Integer::sum);
                }
            }
        }
    }
    
    private CraftRecipe getRecipeDefinition(String recipeId) {
        // Common early-game recipes
        switch (recipeId) {
            // Planks from logs
            case "minecraft:oak_planks":
            case "minecraft:spruce_planks":
            case "minecraft:birch_planks":
            case "minecraft:jungle_planks":
            case "minecraft:acacia_planks":
            case "minecraft:dark_oak_planks":
            case "minecraft:mangrove_planks":
            case "minecraft:cherry_planks":
                return new CraftRecipe(recipeId, 4, false, new String[]{"any_log", null, null, null});
            
            // Sticks
            case "minecraft:stick":
                return new CraftRecipe(recipeId, 4, false, new String[]{
                    "any_planks", null,
                    "any_planks", null
                });
            
            // Crafting Table
            case "minecraft:crafting_table":
                return new CraftRecipe(recipeId, 1, false, new String[]{
                    "any_planks", "any_planks",
                    "any_planks", "any_planks"
                });
            
            // Wooden Pickaxe (3x3)
            case "minecraft:wooden_pickaxe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "any_planks", "any_planks", "any_planks",
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            
            // Stone Pickaxe (3x3)
            case "minecraft:stone_pickaxe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:cobblestone", "minecraft:cobblestone", "minecraft:cobblestone",
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            
            // Stone Axe (3x3)
            case "minecraft:stone_axe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:cobblestone", "minecraft:cobblestone", null,
                    "minecraft:cobblestone", "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            
            // Stone Sword (3x3)
            case "minecraft:stone_sword":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:cobblestone", null,
                    null, "minecraft:cobblestone", null,
                    null, "minecraft:stick", null
                });
            
            // Furnace (3x3)
            case "minecraft:furnace":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:cobblestone", "minecraft:cobblestone", "minecraft:cobblestone",
                    "minecraft:cobblestone", null, "minecraft:cobblestone",
                    "minecraft:cobblestone", "minecraft:cobblestone", "minecraft:cobblestone"
                });
            
            case "minecraft:chest":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "any_planks", "any_planks", "any_planks",
                    "any_planks", null, "any_planks",
                    "any_planks", "any_planks", "any_planks"
                });

            // --- Stone Tools (Remaining) ---
            case "minecraft:stone_shovel":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:cobblestone", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:stone_hoe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:cobblestone", "minecraft:cobblestone", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });

            // --- Iron Tools ---
            case "minecraft:iron_pickaxe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", "minecraft:iron_ingot", "minecraft:iron_ingot",
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:iron_axe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", "minecraft:iron_ingot", null,
                    "minecraft:iron_ingot", "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:iron_sword":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:iron_ingot", null,
                    null, "minecraft:iron_ingot", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:iron_shovel":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:iron_ingot", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:iron_hoe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", "minecraft:iron_ingot", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });

            // --- Iron Armor ---
            case "minecraft:iron_helmet":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", "minecraft:iron_ingot", "minecraft:iron_ingot",
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot",
                    null, null, null
                });
            case "minecraft:iron_chestplate":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot",
                    "minecraft:iron_ingot", "minecraft:iron_ingot", "minecraft:iron_ingot",
                    "minecraft:iron_ingot", "minecraft:iron_ingot", "minecraft:iron_ingot"
                });
            case "minecraft:iron_leggings":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", "minecraft:iron_ingot", "minecraft:iron_ingot",
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot",
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot"
                });
            case "minecraft:iron_boots":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, null, null,
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot",
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot"
                });

            // --- Diamond Tools ---
            case "minecraft:diamond_pickaxe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:diamond", "minecraft:diamond", "minecraft:diamond",
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:diamond_axe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:diamond", "minecraft:diamond", null,
                    "minecraft:diamond", "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:diamond_sword":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:diamond", null,
                    null, "minecraft:diamond", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:diamond_shovel":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:diamond", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });
            case "minecraft:diamond_hoe":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:diamond", "minecraft:diamond", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:stick", null
                });

            // --- Diamond Armor ---
            case "minecraft:diamond_helmet":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:diamond", "minecraft:diamond", "minecraft:diamond",
                    "minecraft:diamond", null, "minecraft:diamond",
                    null, null, null
                });
            case "minecraft:diamond_chestplate":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:diamond", null, "minecraft:diamond",
                    "minecraft:diamond", "minecraft:diamond", "minecraft:diamond",
                    "minecraft:diamond", "minecraft:diamond", "minecraft:diamond"
                });
            case "minecraft:diamond_leggings":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:diamond", "minecraft:diamond", "minecraft:diamond",
                    "minecraft:diamond", null, "minecraft:diamond",
                    "minecraft:diamond", null, "minecraft:diamond"
                });
            case "minecraft:diamond_boots":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, null, null,
                    "minecraft:diamond", null, "minecraft:diamond",
                    "minecraft:diamond", null, "minecraft:diamond"
                });

            // --- Essentials ---
            case "minecraft:torch":
                // any_coal, not minecraft:coal -- charcoal works identically
                // and is what a bot smelts for itself early on.
                return new CraftRecipe(recipeId, 4, false, new String[]{
                    "any_coal", null,
                    "minecraft:stick", null
                });
            case "minecraft:bucket":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:iron_ingot", null, "minecraft:iron_ingot",
                    null, "minecraft:iron_ingot", null,
                    null, null, null
                });
            case "minecraft:flint_and_steel":
                return new CraftRecipe(recipeId, 1, false, new String[]{
                    "minecraft:iron_ingot", "minecraft:flint",
                    null, null
                });
            case "minecraft:shield":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "any_planks", "minecraft:iron_ingot", "any_planks",
                    "any_planks", "any_planks", "any_planks",
                    null, "any_planks", null
                });
            case "minecraft:bow":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, "minecraft:stick", "minecraft:string",
                    "minecraft:stick", null, "minecraft:string",
                    null, "minecraft:stick", "minecraft:string"
                });
            case "minecraft:arrow":
                return new CraftRecipe(recipeId, 4, true, new String[]{
                    null, "minecraft:flint", null,
                    null, "minecraft:stick", null,
                    null, "minecraft:feather", null
                });

            // --- Food & Other ---
            case "minecraft:bread":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:wheat", "minecraft:wheat", "minecraft:wheat",
                    null, null, null,
                    null, null, null
                });
            case "minecraft:sugar":
                return new CraftRecipe(recipeId, 1, false, new String[]{
                    "minecraft:sugar_cane", null,
                    null, null
                });
            case "minecraft:golden_apple":
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    "minecraft:gold_ingot", "minecraft:gold_ingot", "minecraft:gold_ingot",
                    "minecraft:gold_ingot", "minecraft:apple", "minecraft:gold_ingot",
                    "minecraft:gold_ingot", "minecraft:gold_ingot", "minecraft:gold_ingot"
                });
            case "minecraft:paper":
                return new CraftRecipe(recipeId, 3, true, new String[]{
                    "minecraft:sugar_cane", "minecraft:sugar_cane", "minecraft:sugar_cane",
                    null, null, null,
                    null, null, null
                });
            
             // --- Dyes ---
            case "minecraft:white_dye":
            case "minecraft:bone_meal":
                 return new CraftRecipe(recipeId, 3, false, new String[]{ "minecraft:bone", null, null, null });
            case "minecraft:red_dye":
                 return new CraftRecipe(recipeId, 1, false, new String[]{ "minecraft:poppy", null, null, null });
            case "minecraft:yellow_dye":
                 return new CraftRecipe(recipeId, 1, false, new String[]{ "minecraft:dandelion", null, null, null });
            case "minecraft:blue_dye":
                 return new CraftRecipe(recipeId, 1, false, new String[]{ "minecraft:cornflower", null, null, null });
            case "minecraft:black_dye":
                 return new CraftRecipe(recipeId, 1, false, new String[]{ "minecraft:ink_sac", null, null, null });

            // --- Beds ---
            case "minecraft:white_bed":
            case "minecraft:red_bed":
            case "minecraft:yellow_bed":
            case "minecraft:blue_bed":
            case "minecraft:black_bed":
            case "minecraft:bed": // Generic fallback
                return new CraftRecipe(recipeId, 1, true, new String[]{
                    null, null, null,
                    "any_wool", "any_wool", "any_wool",
                    "any_planks", "any_planks", "any_planks"
                });
            
            // --- Ender ---
            case "minecraft:blaze_powder":
                return new CraftRecipe(recipeId, 2, false, new String[]{ "minecraft:blaze_rod", null, null, null });
            case "minecraft:ender_eye":
                return new CraftRecipe(recipeId, 1, false, new String[]{ "minecraft:ender_pearl", "minecraft:blaze_powder", null, null });

            default:
                return null;
        }
    }
    
    private String dumpIngredients(Minecraft client, CraftRecipe recipe) {
        StringBuilder sb = new StringBuilder();
        Map<String, Integer> required = new java.util.HashMap<>();
        for (String s : recipe.grid) {
            if (s != null && !s.isEmpty()) {
                required.merge(s, 1, Integer::sum);
            }
        }
        for (String item : required.keySet()) {
            sb.append(item).append(":").append(countItemInInventory(client, item)).append(" ");
        }
        return sb.toString().trim();
    }

    private boolean hasIngredients(Minecraft client, CraftRecipe recipe) {
        Map<String, Integer> required = new java.util.HashMap<>();
        for (String s : recipe.grid) {
            if (s != null && !s.isEmpty()) {
                required.merge(s, 1, Integer::sum);
            }
        }
        
        for (Map.Entry<String, Integer> entry : required.entrySet()) {
            String item = entry.getKey();
            int needed = entry.getValue();
            int have = countItemInInventory(client, item);
            if (have < needed) {
                return false;
            }
        }
        return true;
    }
    
    /**
     * Single source of truth for whether an inventory stack satisfies a recipe
     * selector.
     *
     * <p>This logic used to be hand-inlined at three call sites
     * ({@link #countItemInInventory}, and twice in {@link #findItemSlot}), which
     * is exactly why "any_coal" was never added anywhere: the torch recipe
     * asked for literal {@code minecraft:coal} and a bot that had smelted its
     * own charcoal was told "Missing ingredients for minecraft:torch" forever.
     * Charcoal and coal are interchangeable in every recipe that burns them.
     * Confirmed live on 2026-07-31: Bot16 held 6 charcoal, 4 sticks and 0 coal
     * and could not craft a single torch.
     */
    static boolean matchesSelector(String selector, String stackId) {
        if (selector == null || stackId == null) {
            return false;
        }
        switch (selector) {
            case "any_log":
                return stackId.endsWith("_log") || stackId.contains("_wood");
            case "any_planks":
                return stackId.endsWith("_planks");
            case "any_coal":
                return stackId.equals("minecraft:coal")
                    || stackId.equals("minecraft:charcoal");
            default:
                return selector.equals(stackId);
        }
    }

    private int countItemInInventory(Minecraft client, String itemId) {
        Inventory inv = client.player.getInventory();
        int count = 0;

        for (int i = 0; i < inv.getContainerSize(); i++) {
            ItemStack stack = inv.getItem(i);
            if (stack.isEmpty()) continue;

            String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();

            if (matchesSelector(itemId, stackId)) {
                count += stack.getCount();
            }
        }
        
        if (count == 0) {
            LOGGER.warn("countItemInInventory: Found 0 of {}, but caller might think otherwise", itemId);
        }
        return count;
    }
    
    private int findItemSlot(Minecraft client, String itemId, int syncId, boolean isTable) {
        // ALWAYS check cursor stack first during multi-step placement
        ItemStack cursor = client.player.containerMenu.getCarried();
        if (!cursor.isEmpty()) {
            String cursorId = BuiltInRegistries.ITEM.getKey(cursor.getItem()).toString();
            if (matchesSelector(itemId, cursorId)) {
                LOGGER.info("  FOUND {} in CURSOR stack", itemId);
                return -2; // Special value for cursor
            }
        }

        Inventory inv = client.player.getInventory();
        LOGGER.info("findItemSlot: Searching for {}", itemId);
        
        for (int i = 0; i < 36; i++) {
            ItemStack stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            
            String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            
            if (matchesSelector(itemId, stackId)) {
                int dst = screenSlotFromInvSlot(i, isTable);
                LOGGER.info("  FOUND {} ({}) in inv {} -> screen {}", itemId, stackId, i, dst);
                return dst;
            }
        }
        
        LOGGER.warn("findItemSlot: FAILED to find {}", itemId);
        return -1;
    }
    
    private int screenSlotFromInvSlot(int invSlot, boolean isTable) {
        if (isTable) {
            if (invSlot < 9) {
                return invSlot + 37; // Hotbar
            } else {
                return invSlot + 1; // Main inventory
            }
        } else {
            if (invSlot < 9) {
                return invSlot + 36; // Hotbar
            } else {
                return invSlot; // Main inventory
            }
        }
    }
    
    private void clearCraftingGrid(Minecraft client, int syncId, boolean isTable) {
        int gridSize = isTable ? 9 : 4;
        int gridStart = 1; // Slot 0 is output, grid starts at 1
        
        for (int i = gridStart; i <= gridSize; i++) {
            final int slot = i;
            client.execute(() -> {
                client.gameMode.handleContainerInput(syncId, slot, 0, ContainerInput.QUICK_MOVE, client.player);
            });
            try { Thread.sleep(30); } catch (InterruptedException e) {}
        }
    }
    
    private String placeIngredients(Minecraft client, int syncId, CraftRecipe recipe, boolean isTable) {
        // isTable is passed in based on the actual screen type, not recipe.requiresTable
        
        for (int i = 0; i < recipe.grid.length; i++) {
            String item = recipe.grid[i];
            if (item == null || item.isEmpty()) continue;
            
            // Convert recipe grid index to screen grid slot
            // For 2x2 recipes on a 3x3 crafting table, the mapping is:
            // 2x2 grid:  0 1    3x3 slots: 1 2 3
            //            2 3               4 5 6
            //                              7 8 9
            // So 2x2 index 0→slot 1, 1→slot 2, 2→slot 4, 3→slot 5
            // Same mapping the post-placement verification uses - keep it in one place so the
            // two can never drift apart.
            int gridSlot = gridSlotFor(i, recipe, isTable);
            
            // Re-find slot every time to handle moving items (inefficient but safe)
            int sourceSlot = findItemSlot(client, item, syncId, isTable);
            
            if (sourceSlot == -1) {
                LOGGER.warn("Could not find {} for crafting", item);
                return "Slot not found for " + item + " (Inv count: " + countItemInInventory(client, item) + ")";
            }
            
            LOGGER.info("placeIngredients: Item {} found at sourceSlot={}, placing at gridSlot={}", item, sourceSlot, gridSlot);
            
            final int dst = gridSlot;
            
            if (sourceSlot == -2) {
                // Already in cursor! Just place one.
                client.execute(() -> {
                    client.gameMode.handleContainerInput(syncId, dst, 1, ContainerInput.PICKUP, client.player); // Right click = place 1
                });
                try { Thread.sleep(80); } catch (InterruptedException e) {}
            } else {
                final int src = sourceSlot;
                
                // Robust Strategy:
                // 1. Pickup ENTIRE stack (Left Click)
                // 2. Place ONE item in grid (Right Click)
                // 3. Return REMAINDER to source (Left Click)
                
                // 1. Pickup All
                client.execute(() -> {
                    client.gameMode.handleContainerInput(syncId, src, 0, ContainerInput.PICKUP, client.player); // Left click = pickup all
                });
                try { Thread.sleep(60); } catch (InterruptedException e) {}
                
                // 2. Place One
                client.execute(() -> {
                    client.gameMode.handleContainerInput(syncId, dst, 1, ContainerInput.PICKUP, client.player); // Right click = place 1
                });
                try { Thread.sleep(60); } catch (InterruptedException e) {}
                
                // 3. Return Remainder (if any)
                client.execute(() -> {
                    ItemStack cursor = client.player.containerMenu.getCarried();
                    if (!cursor.isEmpty()) {
                        client.gameMode.handleContainerInput(syncId, src, 0, ContainerInput.PICKUP, client.player); // Left click = drop all
                    }
                });
                try { Thread.sleep(60); } catch (InterruptedException e) {}
            }
        }
        
        return null; // success
    }

    /**
     * Recipe index to screen slot. Output is slot 0, so the grid starts at 1.
     *
     * <p>A 2x2 recipe placed on a 3x3 table has to be laid out row/column-wise; putting it at
     * slots 1,2,3,4 instead would leave the bottom row of the "2x2" in the wrong cells and
     * distort the shape. Everything else is a straight {@code 1 + i}.
     */
    private static int gridSlotFor(int index, CraftRecipe recipe, boolean isTable) {
        final int gridStart = 1;
        if (isTable && recipe.grid.length == 4) {
            int row = index / 2;
            int col = index % 2;
            return gridStart + (row * 3) + col;
        }
        return gridStart + index;
    }

    /**
     * Check that the crafting grid holds exactly what the recipe asked for, before the output
     * slot is clicked.
     *
     * <p>Ingredients go in one PUT at a time with only 60ms between them. When one of those
     * lands late (or not at all), the grid ends up holding a *different* valid shape and
     * Minecraft crafts that instead: a stone pickaxe missing one cobblestone is exactly a
     * stone hoe. That is how the bot came back with a hoe when asked for a pickaxe, and why the
     * failure was invisible - the output click still "succeeded" and nothing was ever re-read.
     *
     * @return null when the grid matches, otherwise a description of the first mismatch
     */
    private String verifyCraftingGrid(Minecraft client, int syncId, CraftRecipe recipe, boolean isTable) {
        for (int i = 0; i < recipe.grid.length; i++) {
            String expected = recipe.grid[i];
            int gridSlot = gridSlotFor(i, recipe, isTable);
            ItemStack actual = client.player.containerMenu.getSlot(gridSlot).getItem();
            String actualId = actual.isEmpty()
                ? ""
                : BuiltInRegistries.ITEM.getKey(actual.getItem()).toString();

            boolean wantsEmpty = expected == null || expected.isEmpty();
            if (wantsEmpty) {
                if (!actual.isEmpty()) {
                    return "slot " + gridSlot + " should be empty but holds " + actualId;
                }
            } else if (!matchesSelector(expected, actualId)) {
                return "slot " + gridSlot + " should be " + expected
                    + " but holds " + (actualId.isEmpty() ? "nothing" : actualId);
            }
        }
        return null;
    }

    /**
     * Put whatever is on the cursor back into the player's inventory.
     *
     * <p>{@code QUICK_MOVE} on a crafting output slot does not always move the result into the
     * inventory - it can land on the cursor instead (confirmed live on 2026-10-16). Anything
     * that reads the inventory afterwards then sees nothing, which is how a real craft got
     * reported as "no product". If there is no room we leave the item on the cursor rather than
     * dropping it into the world.
     */
    private void clearCursorIntoInventory(Minecraft client, int syncId, boolean isTable) {
        if (client.player == null) {
            return;
        }
        ItemStack cursor = client.player.containerMenu.getCarried();
        if (cursor.isEmpty()) {
            return;
        }
        int emptySlot = findEmptyPlayerSlot(client, syncId, isTable);
        if (emptySlot < 0) {
            LOGGER.warn("Cursor holds {} but the inventory has no empty slot to take it", cursor);
            return;
        }
        final int target = emptySlot;
        client.execute(() -> {
            client.gameMode.handleContainerInput(syncId, target, 0, ContainerInput.PICKUP, client.player);
        });
        try {
            Thread.sleep(80);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /**
     * First empty player-inventory slot, as a screen slot id.
     *
     * <p>The layout differs per screen: a 3x3 crafting table keeps the player inventory at
     * 10..45, while the 2x2 inventory screen keeps it at 1..36.
     */
    private int findEmptyPlayerSlot(Minecraft client, int syncId, boolean isTable) {
        int first = isTable ? 10 : 1;
        int last = isTable ? 45 : 36;
        for (int slot = first; slot <= last; slot++) {
            if (client.player.containerMenu.getSlot(slot).getItem().isEmpty()) {
                return slot;
            }
        }
        return -1;
    }
}
