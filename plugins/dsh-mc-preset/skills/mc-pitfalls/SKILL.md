---
name: mc-pitfalls
description: Minecraft 里已经踩过的坑和对应解法 —— 工具报错看不懂、操作没反应、东西凭空消失、Baritone 不动了的时候读它。
---

# 已知的坑

## 光标(cursor)卡着东西 → 所有容器操作静默失败

**症状**：点槽位没反应、报 `Observed effect deadline exceeded`、物品"凭空消失"。

**原因**：MC 的光标（鼠标拖着的那叠东西）上有物品时，所有容器点击都会失败。

**解法**：`close_screen()` 关一次界面，光标会清回背包 —— 这是 MC 原生行为。

**诊断**：`inventory_click` 成功但返回里有 `cursor_warning`，说明光标没清干净。

## `QUICK_MOVE` 点输出槽 = 批量执行

点一下会"能成交几次就几次"（实测点一次成交 7 次）。

**要恰好一次** → 用 `PICKUP`（普通左键），再把光标上的产物点空格放下。

## Baritone 长任务卡死

**症状**：`mine` / `surface` / `goto` / `tunnel` 接单了但人不动。
命令通道、转头、状态读取全部正常。

**诊断**：`get_player_pos` 坐标分毫未变、`navigation.ever_active` 一直是 false。

**解法**：**换等价工具常常一次就通**

| 卡住的 | 换成 |
|---|---|
| `surface` 不动 | `goal_coordinates` 指一个上方坐标 |
| `mine` 不动 | `dig_block` 一格一格挖 |
| `goto` 不动 | `goal_coordinates` 分段走 |

`cancel_command` / `repack` / `reload_all` 通常救不回来。
**最后手段是重启 MC 客户端**（这个要请用户做）。

## `place_block` 的三种失败

| 报错 | 原因 | 解法 |
|---|---|---|
| `Requested block does not match main hand` | 主手没拿着那个方块（主手只能从快捷栏 0-8 取） | 先把方块挪到快捷栏 |
| `Placement rejected by Minecraft: Fail[]` | 客户端/服务端判定不同步 | 先 `dig_block` 挖一格再放 |
| `Target position is already occupied` | 可能是自己正站在那格里 | 侧移一格再放 |

## `get_recipes` 不可用

MC 1.21.4 起客户端不再暴露配方表，直接报 "Recipe listing not available on client"。
→ 用 `craft`（bridge 有内置配方表），别指望查。

## `find` 的扫描中心不更新

它扫的是"已加载区块的几何中心"，**不随玩家位置更新**。
找远处的东西要先走过去把区块加载出来，或者用 `get_entities`。

## 找村庄用 `get_entities`，不要用 `find('bell')`

`find('bell')` 常常返回空（钟可能不在已加载区块里，或这个村庄没钟）。
→ `get_entities(64)` 找 `minecraft:villager`，走服务端实体列表，不受区块限制。

## 攻击有冷却，别连着挥

MC 1.9+ 的伤害按冷却进度打折。刚挥完立刻再挥，伤害只有零头 ——
表现为"看着砍了十几下，血几乎没掉"。

→ 读 `get_state` 的 `attack_cooldown`（0~1 进度），到 0.9 以上再挥。
（`entity_interact(attack)` 现在会自动等；自己写循环时要留意）

## `mine_quantity` 数的是掉落物

挖 `coal_ore` 掉的是 `coal`、`iron_ore` 掉 `raw_iron`。
判据要用**掉落物名**，不是方块名 —— 工具内部已处理，但看返回时别被 `block` 字段绕晕。

## 受伤提示看有没有攻击者后缀

- `⚠受伤-2`（无后缀）= **环境伤害**（溺水/摔落/岩浆/饥饿）
- `⚠受伤-2(zombie)` = 被谁打

**危险时先扫环境字段**：`air_supply` / `eyes_in_water` / `fall_distance` / `on_fire`。
溺水时 `surface` 可能卡死，改用 `goal_coordinates` 指上方坐标。

## 工具「报成功」≠「真的成功」

`craft` 曾经假成功（报 `crafted:true` 但背包不动）、`place_block` 会放进自己站的格子。

**每做完一步都验证**：挖矿查背包、走路查坐标、放方块查周围。

## `get_inventory` 的字段

- 顶层：`inventory`（36 条）+ `armor` + `offhand` + `selected_slot`
- 物品字段是 **`id`**（`minecraft:iron_sword`），不是 `item`
- `damage` 是**已损耗**，剩 `max_damage - damage`

## 背包槽号 vs 界面槽号

- `get_inventory` 给的是**背包索引**：0-8 快捷栏、9-35 主背包、36-39 盔甲、40 副手
- `inventory_click` 要的是**界面槽号**：从 `get_screen` 现读

⚠️ 别自己推算"背包索引 → 菜单槽"，会算错。`get_screen` 一次就读到了。
