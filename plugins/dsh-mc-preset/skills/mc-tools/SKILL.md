---
name: mc-tools
description: Minecraft 全部工具的速查表 —— 什么时候用哪个、关键参数是什么。需要查某个具体工具怎么调的时候读它。
---

# Minecraft 工具速查

> 106 个工具都在 `mcp__baritone__*` 前缀下。这里只列常驻的那二十几个。

## 看状态

```
get_combat_snapshot   血/饿/护甲/附近威胁（最常用）
get_player_pos        坐标
get_inventory         背包 —— 字段是 id 不是 item；damage 是「已损耗」，剩 max_damage - damage
                      主手 = inventory 里 slot == selected_slot 那条，副手看 offhand
get_state             位置 + 维度 + 难度 + 屏幕 + attack_cooldown
get_death_location    上次死在哪（回去捡装备用）
```

## 看环境

```
get_view(radius=3)        周围一片方块，默认按类型聚合（sand×99 water×48）；detail=true 给坐标
get_block(x,y,z)          某个方块是什么
find(block_type)          找方块 —— 只扫已加载区块，扫描中心不随玩家位置更新
scan_biomes(radius=128)   附近有哪些群系 + 最近距离
get_entities(radius=64)   附近生物（找村民/怪靠它，不受区块加载限制）
get_look_target           准星正对着什么（只读，不会触发右键）
```

## 看画面

```
screenshot                    完整分辨率截图 → 用 read_image 读
capture_frame(downscale=1)    抓一帧到固定路径 latest_frame.png，可反复看
frame_stream(action=start)    每 1 秒自动刷新，默认 downscale=2（看完必须 stop）
```

⚠️ **读一帧约 5.9 秒**。视觉用来回答"在哪、周围什么样"，**不适合认具体物品**
（实测 9 个物品错 5 个：石锄看成镐、炼药锅看成熔炉）。
要确认"手里是什么、耐久多少" → `get_inventory`。

## 移动

```
goal_coordinates(x,y,z)      走某处（最可靠）
follow_player(player_name)   跟着玩家
cancel_command               取消当前任务
thisway(blocks)              朝当前朝向走 N 格
surface                      往地表走（⚠️ 溺水时可能卡死，改用 goal_coordinates 指上方）
```

## 动作

```
dig_block(x,y,z)             挖一格（会先切对的工具）
place_block(block,x,y,z)     放方块（主手必须拿着它；会自动检查是不是自己站的格子）
attack_nearest(swings=5, hostile_only=True, use_weapon=True)
                             use_weapon=False 是空手打（几乎不掉血，用来"别真打伤"）
eat(food="")                 吃东西，自动找食物、自动换快捷栏、会验证真吃进去
equip(item, slot)            穿装备，slot = head/chest/legs/feet/offhand
select_slot(slot)            换手持（只能 0-8）
throw_item(slot, all=false)  丢东西
```

## 移动物品 / 容器

```
get_screen                   当前界面里有什么（默认只列非空）
inventory_click(slot, button=0, click_type="PICKUP")
                             点一个槽 —— slot 是**界面槽号**，从 get_screen 读
close_screen                 关界面（光标卡东西时也靠它清）
```

🔴 **`QUICK_MOVE` 是批量**：shift+点击会一直执行到材料/次数用完
（点合成输出槽连续合成、点交易结果槽连续成交）。要"恰好一次"用 `PICKUP`。

## 合成 / 熔炉 / 交易

```
craft(item, count)           3×3 配方要先开工作台界面（interact_block 工作台）
smelt_items(input_slot, fuel_slot, expected_output)
                             熔炉：place_block 放炉子 → interact_block 打开 → get_screen 读槽位
                             槽号是**界面槽号**，熔炉 0=输入 1=燃料 2=产物，背包区从 3 起
select_trade(index, count)   村民交易：先 entity_interact(action="trade") 开界面 → get_screen 看报价
```

## 交互

```
interact_block(x,y,z)        右键方块（开门/按钮/箱子/熔炉）
entity_interact(action, entity_id, entity_type)
                             detect / look_at / interact / feed / tame / shear / milk / attack / trade
                             ⚠️ interact 和 feed **必须给 entity_id**，不认 entity_type
```

## 说话

```
say(message)                 唯一该用的"说话"工具
```

## 写文件（可选）

```
read_file(path)              读本地文件
```
