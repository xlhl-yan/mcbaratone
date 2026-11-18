package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import net.minecraft.world.Container;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;

/**
 * Handler for equip command.
 * Equips an armor/offhand item by moving it into the target slot.
 */
public class EquipCommandHandler extends AsyncCommandHandler {

    private static final int ACTION_SCHEDULE_TIMEOUT_SECONDS = 2;

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client,
            IBaritone baritone, Socket clientSocket) {
        return executeOnMainThread(client, () -> {
            try {
                if (client.player == null || client.gameMode == null) {
                    return CommandResult.error("Player not available");
                }
                String slotName = params.has("slot") ? params.get("slot").getAsString().toLowerCase() : "";
                String itemId = params.has("item") ? params.get("item").getAsString() : "";

                if (slotName.isBlank()) {
                    return CommandResult.error("Missing slot parameter");
                }
                if (itemId.isBlank()) {
                    return CommandResult.error("Missing item parameter");
                }

                // 主手不是固定索引 —— 它由"快捷栏当前选中格"决定，所以 mainhand
                // 走一套和护甲/副手完全不同的逻辑：找到物品，把它弄进快捷栏，
                // 再选中那一格。护甲槽那套 getTargetInventoryIndex 返回 null，
                // 以前调用方传 "mainhand" 只会拿到 Unsupported equip slot。
                if ("mainhand".equals(slotName) || "hand".equals(slotName)) {
                    Inventory handInv = client.player.getInventory();
                    int found = findItemSlot(handInv, itemId);
                    if (found < 0) {
                        return CommandResult.error("Item not found in inventory: " + itemId);
                    }
                    AbstractContainerMenu handHandler = client.player.containerMenu;
                    if (handHandler != client.player.inventoryMenu) {
                        return CommandResult.error(
                            "Close the active container screen before equipping");
                    }
                    if (!handHandler.getCarried().isEmpty()) {
                        return CommandResult.error(
                            "Cannot equip while the inventory cursor is occupied");
                    }

                    int heldSlot;
                    String mode;
                    if (found <= 8) {
                        // 已经在快捷栏：切过���就行，不动物品
                        handInv.selected = found;
                        client.player.connection.send(new ServerboundSetCarriedItemPacket(found));
                        heldSlot = found;
                        mode = "select_slot";
                    } else {
                        // 在背包深处：跟当前选中格互换，然后选中它
                        int target = handInv.selected;
                        Integer fromScreen = findScreenSlotId(handHandler, handInv, found);
                        Integer toScreen = findScreenSlotId(handHandler, handInv, target);
                        if (fromScreen == null || toScreen == null) {
                            return CommandResult.error("Unable to resolve slot ids for equip");
                        }
                        int syncId = handHandler.containerId;
                        client.gameMode.handleContainerInput(
                            syncId, fromScreen, 0, ContainerInput.PICKUP, client.player);
                        client.gameMode.handleContainerInput(
                            syncId, toScreen, 0, ContainerInput.PICKUP, client.player);
                        if (!handHandler.getCarried().isEmpty()) {
                            // 目标格原来装着别的 → 塞回物品原来的格子
                            client.gameMode.handleContainerInput(
                                syncId, fromScreen, 0, ContainerInput.PICKUP, client.player);
                        }
                        if (!handHandler.getCarried().isEmpty()) {
                            return CommandResult.error("Swap left the cursor occupied");
                        }
                        heldSlot = target;
                        mode = "swap_into_hand";
                    }

                    ItemStack held = handInv.getItem(handInv.selected);
                    boolean heldOk = !held.isEmpty()
                        && BuiltInRegistries.ITEM.getKey(held.getItem())
                            .equals(Identifier.parse(itemId));
                    JsonObject handData = new JsonObject();
                    handData.addProperty("equipped", heldOk);
                    handData.addProperty("slot", "mainhand");
                    handData.addProperty("item", itemId);
                    handData.addProperty("mode", mode);
                    handData.addProperty("held_slot", heldSlot);
                    handData.addProperty("cursor_empty", true);
                    if (!heldOk) {
                        return CommandResult.error("Equip postcondition failed for mainhand");
                    }
                    return CommandResult.success(handData);
                }

                Integer targetInventoryIndex = getTargetInventoryIndex(slotName);
                if (targetInventoryIndex == null) {
                    return CommandResult.error("Unsupported equip slot: " + slotName);
                }

                Inventory inv = client.player.getInventory();
                int sourceInventoryIndex = findItemSlot(inv, itemId);
                if (sourceInventoryIndex < 0) {
                    return CommandResult.error("Item not found in inventory: " + itemId);
                }

                AbstractContainerMenu handler = client.player.containerMenu;
                if (handler != client.player.inventoryMenu) {
                    return CommandResult.error(
                        "Close the active container screen before equipping");
                }
                if (!handler.getCarried().isEmpty()) {
                    return CommandResult.error(
                        "Cannot equip while the inventory cursor is occupied");
                }
                Integer sourceSlotId = findScreenSlotId(handler, inv, sourceInventoryIndex);
                Integer targetSlotId = findScreenSlotId(handler, inv, targetInventoryIndex);

                if (sourceSlotId == null || targetSlotId == null) {
                    return CommandResult.error("Unable to resolve slot ids for equip");
                }

                int syncId = handler.containerId;
                client.gameMode.handleContainerInput(syncId, sourceSlotId, 0, ContainerInput.PICKUP, client.player);
                client.gameMode.handleContainerInput(syncId, targetSlotId, 0, ContainerInput.PICKUP, client.player);
                // PICKUP swaps an occupied target onto the cursor. Put that
                // displaced stack back into the source slot before returning.
                if (!handler.getCarried().isEmpty()) {
                    client.gameMode.handleContainerInput(
                        syncId, sourceSlotId, 0, ContainerInput.PICKUP, client.player);
                }

                ItemStack equipped = inv.getItem(targetInventoryIndex);
                boolean targetVerified = !equipped.isEmpty()
                    && BuiltInRegistries.ITEM.getKey(equipped.getItem()).equals(
                        Identifier.parse(itemId));
                if (!targetVerified || !handler.getCarried().isEmpty()) {
                    return CommandResult.error(
                        "Equip postcondition failed for " + slotName);
                }

                JsonObject data = new JsonObject();
                data.addProperty("equipped", true);
                data.addProperty("slot", slotName);
                data.addProperty("item", itemId);
                data.addProperty("cursor_empty", true);
                return CommandResult.success(data);
            } catch (Exception e) {
                return CommandResult.error("Equip failed: " + e.getMessage());
            }
        }).completeOnTimeout(
            CommandResult.error("Timed out before equip reached the client thread"),
            ACTION_SCHEDULE_TIMEOUT_SECONDS,
            java.util.concurrent.TimeUnit.SECONDS);
    }

    static Integer getTargetInventoryIndex(String slotName) {
        switch (slotName) {
            case "head":
            case "helmet":
                return 39;
            case "chest":
            case "chestplate":
                return 38;
            case "legs":
            case "leggings":
                return 37;
            case "feet":
            case "boots":
                return 36;
            case "offhand":
                return 40;
            default:
                return null;
        }
    }

    private int findItemSlot(Inventory inv, String itemId) {
        Identifier itemIdentifier;
        try {
            itemIdentifier = Identifier.parse(itemId);
        } catch (IllegalArgumentException e) {
            return -1;
        }

        for (int i = 0; i < 36; i++) {
            ItemStack stack = inv.getItem(i);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).equals(itemIdentifier)) {
                return i;
            }
        }
        return -1;
    }

    private Integer findScreenSlotId(AbstractContainerMenu handler, Container inv, int inventoryIndex) {
        for (int i = 0; i < handler.slots.size(); i++) {
            Slot slot = handler.slots.get(i);
            if (slot.container == inv && slot.getContainerSlot() == inventoryIndex) {
                return i;
            }
        }
        return null;
    }

    @Override
    public String getCommandName() {
        return "equip";
    }
}
