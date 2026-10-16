package com.minecraftbot.baritone;

import baritone.api.IBaritone;
import com.google.gson.JsonObject;
import java.net.Socket;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import net.minecraft.resources.Identifier;
import net.minecraft.tags.TagKey;
import net.minecraft.world.Container;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockState;

/**
 * 「把最合适的工具拿到手上」—— 给攻击/挖掘类命令在动手之前调用。
 *
 * <p>为什么需要它：Baritone 的 {@code autoTool} 只管**它自己挖**（{@code #mine}）；
 * bridge 的手动动作（攻击实体、{@code dig_block}）压根不换手，于是出现
 * "手里拿着镐去砍树""空着手打怪"。
 *
 * <p>「该拿什么」不是猜的：
 * <ul>
 *   <li><b>挖方块</b>：看方块挂在哪个原版标签下 —— {@code mineable/pickaxe}、
 *       {@code mineable/axe}、{@code mineable/shovel}、{@code mineable/hoe}。
 *       这是**游戏自己的数据**（数据包标签），比手写"什么方块配什么工具"的表可靠得多；
 *       标签用 {@code TagKey.create(...)} 现造，避免不同版本常量改名。</li>
 *   <li><b>打怪</b>：按物品名排序 —— {@code *_sword} &gt; {@code *_axe}，同类别再比材质
 *       （下界合金 &gt; 钻石 &gt; 铁 &gt; 石 &gt; 金 &gt; 木）。武器伤害在新版挂在数据组件上，
 *       取值要绕一圈，命名规则更稳。</li>
 * </ul>
 *
 * <p>换手分两种情况：目标在快捷栏（0-8）直接切手持；在背包深处（9-35）则用与
 * {@link EquipCommandHandler} 相同的容器点击，把**选中槽与它互换** —— 只改客户端
 * {@code Inventory} 会导致"本地以为拿上了、服务端认为空手"，攻击和挖掘都不作数。
 *
 * <p>{@code kind=item} 是同一套搬运能力的通用形态：把**指定的**物品（方块、火把、桶……）
 * 弄到手上，供 {@code place_block} / {@code place_torches} 这类"主手必须拿着某样东西"的动作调用。
 * 以前它们只会在快捷栏 0-8 里找，东西躺在背包深处就直接失败。
 *
 * <p>注意「没找到」不是错误：空手也能挖（只是慢），所以返回 {@code equipped:false}
 * 加一个 {@code reason}，让调用方自己决定要不要继续。
 */
public class EquipBestToolCommandHandler extends AsyncCommandHandler {

    private static final int ACTION_SCHEDULE_TIMEOUT_SECONDS = 2;
    /** 耐久低于这个比例的工具不选。 */
    private static final float DEFAULT_MIN_DURABILITY_RATIO = 0.05f;

    private final java.util.function.Consumer<Minecraft> inventoryLease;

    public EquipBestToolCommandHandler() {
        this(InventoryMutationLease::hold);
    }

    EquipBestToolCommandHandler(java.util.function.Consumer<Minecraft> inventoryLease) {
        this.inventoryLease = inventoryLease;
    }

    @Override
    public String getCommandName() {
        return "equip_best_tool";
    }

    @Override
    public CompletableFuture<CommandResult> execute(JsonObject params, Minecraft client,
            IBaritone baritone, Socket clientSocket) {
        String kind = params.has("kind") ? params.get("kind").getAsString().trim().toLowerCase() : "";
        String blockId = params.has("block") ? params.get("block").getAsString().trim() : "";
        String itemName = params.has("item") ? params.get("item").getAsString().trim() : "";
        float minRatio = params.has("min_durability_ratio")
            ? params.get("min_durability_ratio").getAsFloat()
            : DEFAULT_MIN_DURABILITY_RATIO;

        if (!kind.equals("dig") && !kind.equals("weapon")) {
            return CompletableFuture.completedFuture(
                CommandResult.error("kind must be 'dig', 'weapon' or 'item'"));
        }
        if (kind.equals("dig") && blockId.isEmpty()) {
            return CompletableFuture.completedFuture(
                CommandResult.error("kind 'dig' needs a block id"));
        }
        if (kind.equals("item") && itemName.isEmpty()) {
            return CompletableFuture.completedFuture(
                CommandResult.error("kind 'item' needs an item id"));
        }

        // 注册表校验放在主线程回调之外：查不到方块/物品就直接报错，不消耗 goal_id。
        final String wantKind;
        final String wantedItemId;
        if (kind.equals("dig")) {
            Identifier id;
            try {
                id = parseBlockId(blockId);
            } catch (Exception e) {
                return CompletableFuture.completedFuture(
                    CommandResult.error("Bad block id: " + blockId));
            }
            if (!BuiltInRegistries.BLOCK.containsKey(id)) {
                return CompletableFuture.completedFuture(
                    CommandResult.error("Unknown block: " + blockId));
            }
            Block block = BuiltInRegistries.BLOCK.getValue(id);
            wantKind = toolKindFor(block.defaultBlockState());
            wantedItemId = null;
        } else if (kind.equals("weapon")) {
            wantKind = "weapon";
            wantedItemId = null;
        } else {
            // kind=item：把**指定的**那件东西弄到手上（方块、火把、桶、食物……都行）
            Identifier id;
            try {
                id = parseBlockId(itemName);
            } catch (Exception e) {
                return CompletableFuture.completedFuture(
                    CommandResult.error("Bad item id: " + itemName));
            }
            if (!BuiltInRegistries.ITEM.containsKey(id)) {
                return CompletableFuture.completedFuture(
                    CommandResult.error("Unknown item: " + itemName));
            }
            wantKind = "item";
            wantedItemId = id.toString();
        }

        return executeOnMainThread(client, () -> {
            try {
                if (client.player == null || client.gameMode == null) {
                    return CommandResult.error("Player not available");
                }
                Inventory inv = client.player.getInventory();
                int selected = inv.selected;

                JsonObject data = new JsonObject();
                data.addProperty("kind", kind);
                if (kind.equals("dig")) {
                    data.addProperty("block", blockId);
                }
                data.addProperty("selected_before", selected);

                if (wantKind == null) {
                    // 这个方块不需要工具（徒手/任意物品都行）
                    data.addProperty("equipped", false);
                    data.addProperty("reason", "no tool needed for this block");
                    return CommandResult.success(data);
                }
                data.addProperty("wants", wantKind);

                // ① 选：kind=item 按 id 找那件指定的；否则按用途挑最好的
                int bestIndex = -1;
                int bestRank = -1;
                if (wantedItemId != null) {
                    bestIndex = indexOfItem(inv, wantedItemId);
                } else {
                    for (int i = 0; i < 36; i++) {
                        ItemStack stack = inv.getItem(i);
                        if (!usable(stack, minRatio)) continue;
                        int rank = rankOf(stack, wantKind);
                        if (rank > bestRank) {
                            bestRank = rank;
                            bestIndex = i;
                        }
                    }
                }

                if (bestIndex < 0) {
                    data.addProperty("equipped", false);
                    data.addProperty("reason",
                        wantedItemId != null
                            ? "no " + wantedItemId + " in inventory"
                            : "no " + wantKind + " in inventory");
                    return CommandResult.success(data);
                }

                String toolId = itemId(inv.getItem(bestIndex));
                data.addProperty("tool", toolId);
                data.addProperty("from_index", bestIndex);
                data.addProperty("rank", bestRank);

                if (bestIndex == selected) {
                    data.addProperty("equipped", true);
                    data.addProperty("changed", false);
                    data.addProperty("held_slot", selected);
                    return CommandResult.success(data);
                }

                // ② 拿：快捷栏直接切手持
                if (bestIndex <= 8) {
                    inventoryLease.accept(client);
                    inv.selected = bestIndex;
                    client.player.connection.send(new ServerboundSetCarriedItemPacket(bestIndex));
                    data.addProperty("changed", true);
                    data.addProperty("mode", "select_slot");
                    data.addProperty("held_slot", bestIndex);
                } else {
                    // ②' 背包深处：选中槽 ↔ 工具槽 互换（服务端也要认账）
                    AbstractContainerMenu handler = client.player.containerMenu;
                    if (handler != client.player.inventoryMenu) {
                        data.addProperty("equipped", false);
                        data.addProperty("reason", "a container screen is open; close it first");
                        return CommandResult.success(data);
                    }
                    if (!handler.getCarried().isEmpty()) {
                        data.addProperty("equipped", false);
                        data.addProperty("reason", "inventory cursor is occupied");
                        return CommandResult.success(data);
                    }
                    Integer selectedScreen = findScreenSlotId(handler, inv, selected);
                    Integer toolScreen = findScreenSlotId(handler, inv, bestIndex);
                    if (selectedScreen == null || toolScreen == null) {
                        return CommandResult.error("Unable to resolve inventory slot ids");
                    }
                    int syncId = handler.containerId;
                    client.gameMode.handleContainerInput(syncId, toolScreen, 0, ContainerInput.PICKUP, client.player);
                    client.gameMode.handleContainerInput(syncId, selectedScreen, 0, ContainerInput.PICKUP, client.player);
                    if (!handler.getCarried().isEmpty()) {
                        // 选中槽原来是别的东西，被换到光标上了 → 放回工具原来的格子
                        client.gameMode.handleContainerInput(syncId, toolScreen, 0, ContainerInput.PICKUP, client.player);
                    }
                    if (!handler.getCarried().isEmpty()) {
                        return CommandResult.error("Swap left the cursor occupied");
                    }
                    data.addProperty("changed", true);
                    data.addProperty("mode", "swap_into_hand");
                    data.addProperty("held_slot", selected);
                }

                // ③ 回读客户端状态（服务端要下一个 tick 才反映，这里只看本地是否就位）
                ItemStack held = inv.getItem(inv.selected);
                boolean ok = !held.isEmpty() && itemId(held).equals(toolId);
                data.addProperty("equipped", ok);
                if (!ok) {
                    data.addProperty("reason", "postcondition failed");
                }
                return CommandResult.success(data);
            } catch (Exception e) {
                return CommandResult.error("equip_best_tool failed: " + e.getMessage());
            }
        }).completeOnTimeout(
            CommandResult.error("Timed out before equip_best_tool reached the client thread"),
            ACTION_SCHEDULE_TIMEOUT_SECONDS,
            java.util.concurrent.TimeUnit.SECONDS);
    }

    /** 不含 namespace 的 id 补上 {@code minecraft:}（方块名、物品名通用 —— 只负责补前缀）。 */
    static Identifier parseBlockId(String raw) {
        String s = raw.trim();
        return Identifier.parse(s.indexOf(':') < 0 ? "minecraft:" + s : s);
    }

    /**
     * 这个方块该用哪类工具 —— 看它挂在哪个原版 {@code mineable/*} 标签下。
     * 返回 null 表示「不需要工具」（徒手就行）。
     *
     * <p>⚠️ 标签数据来自**数据包**：真实游戏里一定有（单人=集成服、多人=服务端同步），
     * 但单元测试环境里是空的 —— 所以这条规则只能在实机验证，单测里 {@code state.is(tag)}
     * 一律为 false。见 {@code EquipBestToolCommandHandlerTest} 的类注释。
     */
    static String toolKindFor(BlockState state) {
        if (state.is(blockTag("mineable/pickaxe"))) return "pickaxe";
        if (state.is(blockTag("mineable/axe"))) return "axe";
        if (state.is(blockTag("mineable/shovel"))) return "shovel";
        if (state.is(blockTag("mineable/hoe"))) return "hoe";
        return null;
    }

    private static TagKey<Block> blockTag(String path) {
        return TagKey.create(Registries.BLOCK, Identifier.parse("minecraft:" + path));
    }

    /** 背包 0-35 里第一件该 id 的物品在哪个槽；-1 = 没有。 */
    static int indexOfItem(Inventory inv, String fullItemId) {
        for (int i = 0; i < 36; i++) {
            ItemStack stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            if (itemId(stack).equals(fullItemId)) return i;
        }
        return -1;
    }

    /** 能不能用：非空、且（若可损坏）还剩够用的耐久。 */
    static boolean usable(ItemStack stack, float minRatio) {
        if (stack.isEmpty()) return false;
        if (!stack.isDamageableItem()) return true;
        int max = stack.getMaxDamage();
        if (max <= 0) return true;
        int remaining = max - stack.getDamageValue();
        return remaining > 0 && (float) remaining / max >= minRatio;
    }

    /** 越合适越大；-1 = 不是这个用途的东西。 */
    static int rankOf(ItemStack stack, String wantKind) {
        String path = BuiltInRegistries.ITEM.getKey(stack.getItem()).getPath();
        if (wantKind.equals("weapon")) {
            if (path.equals("trident") || path.equals("mace")) return 100;
            if (path.endsWith("_sword")) return materialRank(path) * 10 + 2;
            if (path.endsWith("_axe")) return materialRank(path) * 10 + 1;
            return -1;
        }
        if (path.equals("shears") && wantKind.equals("hoe")) return 1;
        if (!path.endsWith("_" + wantKind)) return -1;
        return materialRank(path);
    }

    /** 材质等级：下界合金 &gt; 钻石 &gt; 铁 &gt; 石 &gt; 金 &gt; 木。0 = 不认识。 */
    static int materialRank(String itemPath) {
        if (itemPath.startsWith("netherite_")) return 6;
        if (itemPath.startsWith("diamond_")) return 5;
        if (itemPath.startsWith("iron_")) return 4;
        if (itemPath.startsWith("stone_")) return 3;
        if (itemPath.startsWith("golden_")) return 2;
        if (itemPath.startsWith("wooden_")) return 1;
        return 0;
    }

    private static String itemId(ItemStack stack) {
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }

    private static Integer findScreenSlotId(AbstractContainerMenu handler, Container inv, int inventoryIndex) {
        for (int i = 0; i < handler.slots.size(); i++) {
            Slot slot = handler.slots.get(i);
            if (slot.container == inv && slot.getContainerSlot() == inventoryIndex) {
                return i;
            }
        }
        return null;
    }
}
