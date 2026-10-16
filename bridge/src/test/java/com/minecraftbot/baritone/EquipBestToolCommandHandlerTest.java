package com.minecraftbot.baritone;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * 「该拿什么工具」的判断规则 —— 纯逻辑，不碰世界。
 *
 * <p>两个测试环境的限制（踩过，别再试）：
 * <ul>
 *   <li>{@code new ItemStack(...)} 在单测里会抛
 *       {@code NullPointerException: Components not bound yet} —— 物品的数据组件要等
 *       数据包阶段才绑上。所以这里一律用 {@code mock(ItemStack.class)}，只桩出
 *       {@code rankOf} / {@code usable} 真正读的那几个方法。</li>
 *   <li>方块标签（{@code mineable/*}）同样来自数据包，单测里是空的 —— 所以
 *       {@link #toolKindComesFromVanillaTags()} 被 {@code @Disabled}，
 *       改由实机验证（调 {@code equip_best_tool} 看返回的 {@code wants}）。</li>
 * </ul>
 */
class EquipBestToolCommandHandlerTest {

    @BeforeAll
    static void bootstrap() {
        net.minecraft.SharedConstants.tryDetectVersion();
        net.minecraft.server.Bootstrap.bootStrap();
    }

    private static ItemStack stackOf(Item item) {
        ItemStack stack = mock(ItemStack.class);
        when(stack.isEmpty()).thenReturn(false);
        when(stack.getItem()).thenReturn(item);
        return stack;
    }

    private static int rankOf(Item item, String kind) {
        return EquipBestToolCommandHandler.rankOf(stackOf(item), kind);
    }

    @Test
    @Disabled("方块标签来自数据包，单测环境是空的 —— 见类注释，改由实机验证")
    void toolKindComesFromVanillaTags() {
        assertEquals("axe", EquipBestToolCommandHandler.toolKindFor(
            net.minecraft.world.level.block.Blocks.OAK_LOG.defaultBlockState()));
        assertEquals("pickaxe", EquipBestToolCommandHandler.toolKindFor(
            net.minecraft.world.level.block.Blocks.STONE.defaultBlockState()));
    }

    @Test
    void betterMaterialWinsAmongTheSameToolKind() {
        int iron = rankOf(Items.IRON_AXE, "axe");
        int stone = rankOf(Items.STONE_AXE, "axe");
        int wooden = rankOf(Items.WOODEN_AXE, "axe");
        assertTrue(iron > stone, "铁斧要排在石斧前面");
        assertTrue(stone > wooden, "石斧要排在木斧前面");
        assertTrue(rankOf(Items.IRON_PICKAXE, "axe") < 0, "镐不是斧");
        assertTrue(rankOf(Items.BREAD, "pickaxe") < 0, "面包不是镐");
    }

    @Test
    void weaponRankingPrefersSwordsThenAxesThenMaterial() {
        int ironSword = rankOf(Items.IRON_SWORD, "weapon");
        int stoneSword = rankOf(Items.STONE_SWORD, "weapon");
        int diamondSword = rankOf(Items.DIAMOND_SWORD, "weapon");
        int ironAxe = rankOf(Items.IRON_AXE, "weapon");
        assertTrue(diamondSword > ironSword, "钻石剑 > 铁剑");
        assertTrue(ironSword > stoneSword, "铁剑 > 石剑");
        assertTrue(ironSword > ironAxe, "同材质时剑优先于斧");
        assertTrue(rankOf(Items.BREAD, "weapon") < 0, "面包不是武器");
    }

    @Test
    void nearlyBrokenToolsAreSkipped() {
        ItemStack fresh = mock(ItemStack.class);
        when(fresh.isEmpty()).thenReturn(false);
        when(fresh.isDamageableItem()).thenReturn(true);
        when(fresh.getMaxDamage()).thenReturn(250);
        when(fresh.getDamageValue()).thenReturn(0);
        assertTrue(EquipBestToolCommandHandler.usable(fresh, 0.05f), "满耐久可用");

        ItemStack worn = mock(ItemStack.class);
        when(worn.isEmpty()).thenReturn(false);
        when(worn.isDamageableItem()).thenReturn(true);
        when(worn.getMaxDamage()).thenReturn(250);
        when(worn.getDamageValue()).thenReturn(249); // 只剩 1 点
        assertFalse(EquipBestToolCommandHandler.usable(worn, 0.05f), "只剩 1 点耐久的不该被选中");

        ItemStack unbreakable = mock(ItemStack.class);
        when(unbreakable.isEmpty()).thenReturn(false);
        when(unbreakable.isDamageableItem()).thenReturn(false);
        assertTrue(EquipBestToolCommandHandler.usable(unbreakable, 0.05f), "不可损坏的物品永远可用");

        assertFalse(EquipBestToolCommandHandler.usable(ItemStack.EMPTY, 0.05f));
    }

    @Test
    void blockIdsWithoutNamespaceGetMinecraftPrefixed() {
        assertEquals("minecraft:oak_log", EquipBestToolCommandHandler.parseBlockId("oak_log").toString());
        assertEquals("minecraft:stone",
            EquipBestToolCommandHandler.parseBlockId(" minecraft:stone ").toString());
    }

    @Test
    void materialRankOnlyKnowsTheVanillaTiers() {
        assertEquals(6, EquipBestToolCommandHandler.materialRank("netherite_pickaxe"));
        assertEquals(5, EquipBestToolCommandHandler.materialRank("diamond_pickaxe"));
        assertEquals(4, EquipBestToolCommandHandler.materialRank("iron_pickaxe"));
        assertEquals(3, EquipBestToolCommandHandler.materialRank("stone_pickaxe"));
        assertEquals(2, EquipBestToolCommandHandler.materialRank("golden_pickaxe"));
        assertEquals(1, EquipBestToolCommandHandler.materialRank("wooden_pickaxe"));
        assertEquals(0, EquipBestToolCommandHandler.materialRank("copper_pickaxe"),
            "铜工具不在原版层级表里 —— 排在最低但不为负");
    }

    @Test
    void indexOfItemScansTheWholeInventoryNotJustTheHotbar() {
        Inventory inv = mock(Inventory.class);
        // ⚠️ 先把 ItemStack mock 建好，再 stub Inventory —— 若在 thenReturn(...) 里现造 mock，
        // 参数求值时又开了一层 stubbing，Mockito 会抛 UnfinishedStubbingException。
        ItemStack dirt = stackOf(Items.DIRT);
        ItemStack granite = stackOf(Items.GRANITE);
        for (int i = 0; i < 36; i++) {
            when(inv.getItem(i)).thenReturn(ItemStack.EMPTY);
        }
        when(inv.getItem(0)).thenReturn(dirt);
        when(inv.getItem(35)).thenReturn(granite);

        assertEquals(35, EquipBestToolCommandHandler.indexOfItem(inv, "minecraft:granite"),
            "背包深处的方块也得找得到 —— place_block 原来只翻快捷栏 0-8，就栽在这儿");
        assertEquals(0, EquipBestToolCommandHandler.indexOfItem(inv, "minecraft:dirt"));
        assertEquals(-1, EquipBestToolCommandHandler.indexOfItem(inv, "minecraft:gold_ingot"));
    }
}
