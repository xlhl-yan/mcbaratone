# mcbaratone（DSH 陪玩适配版）

> **这是 [jhwodchuck/mcbaratone](https://github.com/jhwodchuck/mcbaratone) 的个人 fork。**
>
> 上游的目标是「无干预从出生点打到末影龙」；本 fork 改成 **让 DeepSeek Harness（DSH）里的 AI 陪我玩 Minecraft** ——
> 我在游戏里打字它能听见，能跟着我跑、帮我挖矿打怪，偶尔还会主动开口聊天。
>
> **成分表：100% 纯 AI 出品，无人工添加。**（连这句玩笑也是它自己写的 🐋）
>
> ---
>
> **English:** A personal fork of [jhwodchuck/mcbaratone](https://github.com/jhwodchuck/mcbaratone).
> Upstream aims at an unattended, no-cheat run from world spawn to the Ender Dragon; this fork retargets it so a
> **DeepSeek Harness (DSH) agent plays Minecraft _with_ you** — it hears what you type in game chat, follows you
> around, mines and fights on request, and occasionally speaks up on its own.
>
> _Ingredients: 100% pure AI, no human additives. (This joke included.)_
>
> Everything below this line is in Chinese.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Java 25](https://img.shields.io/badge/Java-25-ED8B00.svg)](bridge/gradle.properties)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)

---

## 一、和上游的关系

上游把**骨架**全写好了：Python 控制器、Java/Fabric bridge、安全门、检查点、可观测后置条件、遥测、仪表盘。
本 fork **只做「适配 DSH 陪玩」这一件事**，上游的功劳一点不减。

| | 上游 | 本 fork |
| --- | --- | --- |
| 目标 | 自主无干预通关末影龙 | **DSH 驱动的陪玩伙伴** |
| 驱动方式 | 检查点规划器 | **游戏内聊天 + MCP 工具调用** |
| MCP 工具面 | 部分 bridge handler 未暴露 | **97 个工具**（bridge 共 72 个 handler）|
| 主要分支 | `main` | `dsh/mcp-tooling`（`main` 保持只读）|

**没打算合并回上游**，这是自用适配分支。

---

## 二、能干什么（都已实机验证）

**聊天**
- 游戏内双向：你打字 → AI 收到（带你的名字）；AI 说话 → 出现在聊天栏
- 人类可读的**中文**前缀命令，见下面「游戏内命令」

**行动**
| 能力 | 工具 |
| --- | --- |
| 跟随（遇沟坎会自己搭方块）| `follow_player` / `follow_entity` |
| 挖矿（挖到指定数量自动停）| `mine_quantity` |
| 战斗（自动索敌 + 连续挥刀）| `attack_nearest` |
| 进食回血 | `eat`（自动找食物、自动换快捷栏、**会验证真的吃进去**）|
| 穿装备 | `equip`（按物品名自动推断槽位）|
| 丢东西给玩家 | `throw_item` |
| 插火把 / 放方块 / 挖方块 | `place_torches` / `place_block` / `dig_block` |
| 开关门、按钮、拉杆 | `interact_block` |
| 装水 / 倒水 | `use_bucket` |
| 合成 | `craft` |
| 找方块 / 走坐标 / 回地表 | `find` / `goal_coordinates` / `surface` |

**观察**
- `get_look_target`：**只读**报告准星指着什么（方块 / 实体 / 空气），带完整 `block_state`，能一眼看出「门开没开」
- `get_entities` / `get_combat_snapshot` / `get_inventory` / `get_block` / `get_dimension` …

---

## 三、跑起来

### 1. 编译 bridge

需要 **Java 25**（Gradle 版本与 JDK 有官方兼容下限：Java 25 需 Gradle 9.1+）。

```powershell
Set-Location bridge
.\gradlew.bat --no-daemon --console=plain build -x test
```

产物：`bridge/build/libs/baritone-api-bridge-1.0.40.jar`

> `-x test` 是为了跳过依赖客户端环境的 Java 测试；jar 仍会正常产出。

### 2. 让 Minecraft 加载它

把 jar 放进目标客户端的 `versions/<版本>/mods/`，同时确保 **Baritone 本体**和 **Fabric API** 也在（缺 Baritone 会报 `Incompatible mods found`）。

### 3. 双客户端 + 端口分离（真陪玩的关键）

陪玩要的是「AI 有**自己的角色**」，所以开两个客户端、两个游戏目录：

| | 游戏目录 | 账号 | bridge 端口 | 内置 MCP 端口 |
| --- | --- | --- | --- | --- |
| 你 | `...\.minecraft` | 你的账号 | 5555 | 5557 |
| AI | `...\.minecraft-ai` | 离线账号即可 | **5556** | **5558** |

**端口靠 JVM 参数控制**，不要改源码默认值再编译：

```
-Dbaritone.bridge.port=5556 -Dmcp.port=5558
```

> HMCL 里填在「实例设置 → JVM 参数」。**只给 AI 那个实例填**，你自己那个不填（用默认 5555）。
>
> ⚠️ 如果两个客户端同时抢 5555，后启动的会静默失败 —— 这是最常见的「工具突然全哑」原因。

然后两边都进同一个世界（联机 / 局域网开放），AI 就是一个真实存在的玩家。

### 4. 接进 DSH

在 `~/.dsh/profiles/<profile>/cordis.patch.yml` 里插一条 MCP 客户端：

```yaml
- insert:
    - id: mcp-baritone
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: baritone
        transport: stdio
        command: '<venv>\Scripts\python.exe'
        args:
          - '<REPO>\scripts\mcp_stdio_wrapper.py'
          - "--host"
          - "localhost"
          - "--port"
          - "5556"            # ← AI 客户端的 bridge 端口
        toolCallTimeoutMs: 120000
```

**为什么要 wrapper？** 上游 `mcp_server.py` 的 `BridgeConfig` 默认 `transport_type="websocket"`，而 CLI 没有对应参数 ——
按官方方式启动会去连 `ws://localhost:5555`，但那个端口是**裸 TCP**，必然连不上。
wrapper 只做一件事：把 `transport_type` 强制设成 `"tcp"`。核心就一行：

```python
config = BridgeConfig(host=args.host, port=args.port, timeout=args.timeout)
config.transport_type = "tcp"        # ★ 上游默认 websocket，会连错协议
```

---

## 四、这个 fork 改了什么

### A. 补全 MCP 工具面（`mcp_server.py` +796 行）

上游不少工具走 `command.run("字符串")`，而 bridge 的字符串解析层**只映射了 craft/goto/mine**，
其余命令的参数会被静默丢掉（甚至被回退逻辑重复执行一次）。

改成**直接发结构化参数**：参数名照 handler 源码里的 `params.get("xxx")` 抄，不再让 bridge 猜。
顺带把没暴露的 handler 都补成工具。

### B. 修掉的上游缺陷（都是实机踩出来的）

| # | 位置 | 症状 | 修法 |
| --- | --- | --- | --- |
| 1 | `transport.py` | 一遇中文就超时（bridge 用 GB18030 写，Python 按 UTF-8 解）| 解码先试 gb18030 再退 utf-8 |
| 2 | `command_dispatcher.py` | 所有命令报 `Unknown error`；后来 sequence 又被误判失败 | `status` 为 `ok`/`completed`/缺失都算成功 |
| 3 | `core/facades/commands.py` | `run_command("craft …")` 静默失效（被当聊天消息发出去）| 不带 `#` 先走 structured，被拒再回退 |
| 4 | `events/FabricEventRegistrar.java` | **收不到玩家聊天**（只注册了 GAME 频道）| 补注册 `ClientReceiveMessageEvents.CHAT` |
| 5 | `SequenceCommandHandler.java` | `follow` / `mine` 的参数解析不对 | 支持 `follow player <名>`、两种 mine 顺序 |
| 6 | `InteractBlockCommandHandler.java` | **门/按钮打不中**（死瞄方块中心，薄方块必擦过）| 采样 132 个候选点逐个试射线 |
| 7 | bridge `mine` handler | 返回 `applied:true` 但角色不动（MineProcess 起不来）| 工具改走 Baritone 原生 `#mine <方块>` |
| 8 | follow 命令 | 发 `#follow <名>` 无效（Baritone 要 `#follow player <名>`）| 补上限定词 |

> 第 7 条特别隐蔽：工具**报成功但世界没变**。这也是本 fork 反复强调「看世界状态，不看返回值」的原因。

### C. 新增的游戏内命令与 handler

- **`唤醒会话 <sessionId>`**（也可用 `!waken <id>` / `waken <id>`）
  在游戏聊天里直接指定「游戏事件要投给哪个 DSH 会话」 —— 不用回来改配置文件。
  存储于 `config/mcbaratone-waken-session.txt`。敲 `唤醒会话 clear` 清除。
  - 前缀特意用**中文**：`!` 会被 Baritone 抢去当命令解析，刷一屏「未知或不完整的命令」。
- **`get_look_target`**（只读）：报告准星指着什么，含完整 block_state。
- bridge 另加 `get_waken_session` / `set_waken_session` 两个 handler 供 DSH 侧读写。

---

## 五、MCP 工具速查（共 97 个，常用的）

| 分类 | 工具 |
| --- | --- |
| 说话 | `say` |
| 观察 | `snapshot` `get_inventory` `get_entities` `get_player_pos` `get_block` `get_look_target` `get_combat_snapshot` `get_dimension` `find` `get_setting` `get_events`（酌情用，见 §六）|
| 移动 | `goal_coordinates` `set_goal_block` `goto_block_type` `come` `thisway` `path` `explore` `surface` `tunnel` |
| 采集 | `mine_quantity` `start_mining` `stop_mining` `farm` `dig_block` |
| 战斗 | `attack_nearest` |
| 界面 | `get_screen`（界面里有什么 + 是否开着）`close_screen`（关掉，带回读确认）|
| 交互 | `interact_block` `place_block` `place_torches` `use_bucket` `eat` `equip` `throw_item` `select_slot` `look_at` `look_around` `respawn` |
| 合成 | `craft` `craft_advanced` `auto_craft` |
| 建造 | `build_schematic` `inspect_build_site` `preview_build_plan` |
| 兜底 | `run_command` `batch_command` `cancel_command` `help_command` |

> 仓库另带 **`scripts/mc.py`**：绕过 MCP 直接给 bridge 发命令的调试工具。
> 出问题先用它分清「是工具层坏了还是游戏层坏了」，用法见 [`DSH-READHERE.md`](DSH-READHERE.md) §6。

---

## 六、已知限制

- **`get_recipes` 在 MC 1.21.4 不可用** —— 上游限制，客户端不再暴露配方表。别靠它查配方，`craft` 用的是内置配方表。
- **`interact_block` 对薄方块仍然挑剔** —— 已改成采样多点，但极端角度仍可能打不中（换一侧靠近即可）。
- **`mine_quantity` 大单不等待** —— 数量 > 5 时只负责「启动」，由调用方轮询背包；且**超时会自动 cancel**，避免残留任务把角色拖着到处跑。
- **`get_events` 要酌情调用** —— 聊天不用拉（插件会推）；不带 `event_type` 过滤会混进 `TICK_UPDATE` 这类高频事件刷屏。另外 **bridge 的 `get_events` 不带 `after_seq` 时会走 `pollEvents()`，那里有 `iterator.remove()` 会把事件从缓冲区抽走** —— 所以这个工具在实现上**永远走游标模式**，不要绕过它裸调。
- **`close_screen` 关不掉非容器界面** —— 它调的是 `player.closeContainer()`，只能关箱子/熔炉/交易/工作台这类容器界面；PauseScreen、DeathScreen 关不掉（会返回 `closed: false` + hint），那类界面需要在 bridge 侧用 `setScreen(null)`。
- **bridge 无认证层** —— 只能跑在回环地址或可信网络，别暴露到公网。
- **工具的运行时常量**：Python 侧轮询上限必须小于 DSH 的 `toolCallTimeoutMs`（现配 120 秒），否则会先报超时。

---

## 七、测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

本 fork 当前实测：**2721 passed, 1 skipped**。

默认配置**排除**标记为 `live_readonly` / `live_mutating` 的用例 —— 那些会真的连进 Minecraft 并改动世界，
按 `tests/functional/README.md` 的说明**逐个显式选择**，不要对着有价值的存档跑未过滤的套件。

---

## 八、许可与致谢

- 上游：[jhwodchuck/mcbaratone](https://github.com/jhwodchuck/mcbaratone)，**MIT**
- 本 fork 的一方差量同样以 MIT 提供；随附的第三方组件（Baritone / Fabric / Minecraft 映射等）保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- Minecraft 是 Microsoft 的商标；Baritone 是独立开源项目。本项目与 Microsoft、Mojang Studios、Baritone 维护者均无隶属或背书关系。
