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
| MCP 工具面 | 部分 bridge handler 未暴露 | **99 个工具**（bridge 共 72 个 handler）|
| 主要分支 | `main` | `dsh/mcp-tooling`（`main` 保持只读）|

**没打算合并回上游**，这是自用适配分支。

---

## 二、能干什么（都已实机验证）

**聊天**
- 游戏内双向：你打字 → AI 收到（带你的名字）；AI 说话 → 出现在聊天栏

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

> **动手前会自动换对东西**（bridge 的 `equip_best_tool`）：挖方块按原版 `mineable/*` 标签挑
> 镐/斧/铲，打怪按 `剑 > 斧 + 材质` 排序，而放方块 / 插火把 / 用桶 / 挤奶 / 剪毛会把**指定的**
> 那件物品从背包**任意位置**弄到手上（深处用容器点击互换）。所以调用方不用操心"手里拿的对不对" ——
> 只有**真的没有**那件东西时才会报 `no <item> in inventory`。

**观察**
- `get_look_target`：**只读**报告准星指着什么（方块 / 实体 / 空气），带完整 `block_state`，能一眼看出「门开没开」
- `get_entities` / `get_combat_snapshot` / `get_inventory` / `get_block` / `get_dimension` …
- `get_view` / `scan_biomes`：一次看一片方块（按类型聚合）、扫附近生物群系
- `capture_frame` / `frame_stream`：**真的看见画面** —— 抓帧到固定路径，或按节奏自动刷新，
  再用 `read_image` 读。实测读一帧约 5~6 秒（关思考模式），适合确认场景，不适合躲怪

**给 agent 读的手册**（都在仓库根，配 `DSH-READHERE.md` §5.4）

| 手册 | 用途 |
| --- | --- |
| `陪玩会话启动提示.md` | **新开陪玩会话的第一份读物**：三段链路、会被什么唤醒、条件反射表、七条铁律、工具速查、已知的坑 |
| `游戏态势应对手册.md` | 收到「（游戏态势）…」消息时，低 token 查表判断该动不动 |
| `空闲自主行为手册.md` | 空闲自主时怎么用 DSH 的 goal 机制立目标、拆步骤、推进 |

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

**Baritone 本体不用去别处找 —— 就在本仓库 `bridge/libs/` 里**：`baritone-api-fabric-1.15.0-9-gd93f1582.jar`
（来自上游 [`d93f1582`](https://github.com/cabaletta/baritone/commit/d93f1582)，LGPL-3.0）。它**一个文件两用**
——既是编译期依赖，**也是要装进 mods 的 Baritone 本体**。bridge 对它是**精确匹配**，换成别的版本会在启动时报
`Incompatible mods found`。**Fabric API 要自己下**（跟 MC 版本匹配，如 `fabric-api-0.161.0+26.2.jar`）。

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

**⚠️ 多开必做：关掉「失焦暂停」（`pauseOnLostFocus`）**

一台机器上跑两个客户端时，**不在前台的那个会失去输入焦点**，MC 于是**暂停整个世界** ——
角色不动、Baritone 停摆、bridge 命令看起来"发出去没反应"。两个客户端的设置都得改：

```
<MC_GAME_DIR>\versions\<MC_VERSION>\options.txt
    pauseOnLostFocus:true   →   pauseOnLostFocus:false
```

- 路径在**版本隔离**目录（`versions\<版本>\options.txt`），**不是**游戏根目录；
- **必须先完全退出客户端再改** —— 游戏退出时会用内存里的旧值把这个文件覆盖回去；
- 改一次长期有效；只有换版本 / 换游戏目录 / 重置配置才会回到默认值；
- 游戏里**没有**这个开关（语言文件里连标签都没有），只能改文件。

> 踩坑现场：AI 端停在聊天界面时一切正常，一旦它打开工作台/熔炉，工具界面关掉、焦点丢失，
> 立刻弹「游戏已暂停」。原因是 MC 只在「**失焦 且 屏幕上没有 GUI**」时才暂停 ——
> 有 GUI 顶着就不暂停，所以"停在聊天页"能凑合，但一点都不可靠。

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

### 5. 装 DSH 侧的配套插件

光有 MCP 工具还不够 —— 得有东西**盯着游戏内聊天**，玩家说话时把 agent 叫醒（还有游戏态势与
空闲自主）。那就是随本仓库提供的 `plugins/dsh-mc-chat-watch`。

它在 profile 的 `package.json` 里要占**两个**位置才算装载：

```jsonc
{
  "dependencies": { "dsh-mc-chat-watch": "link:<插件目录>" },
  "dsh": { "profile": { "bundles": [ "…", "dsh-mc-chat-watch" ] } }
}
```

> ⚠️ **`dependencies` ≠ 装载** —— `dsh.profile.bundles` 才决定加载。这个坑真踩过：依赖在、
> 插件没跑，日志里连一行 `[apply]` 都没有。

装完**重启 DSH**，再看 `<DSH_HOME>/mc-chat-watch/watch.log` 有没有 `[apply] …` 自检行。
说明、配置项、设置页用法见 [`plugins/README.md`](plugins/README.md) 和
[`plugins/dsh-mc-chat-watch/README.md`](plugins/dsh-mc-chat-watch/README.md)。

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
| 9 | bridge `mine` handler | 带坐标时 `count` 被吞（要挖 3 个却挖了 56 个），且 `clearArea` 会挖穿沿途 | 有 `count` 就走 MineProcess，`count=0` 才保留 clearArea |
| 10 | `PlaceTorchesCommandHandler.java` | **假成功**：手工拼 `BlockHitResult`、不转头、把 `result.consumesAction()`（**客户端本地预测**）当成结果 —— 实测目标在 65 格外也回 `placed:true`，而方块没出现、火把没消耗 | 距离检查 + 真实射线 + 转头 + `ObservedMutation` 后置校验（回读那格真是火把才算） |

> 第 7 条特别隐蔽：工具**报成功但世界没变**。这也是本 fork 反复强调「看世界状态，不看返回值」的原因。

### C. 新增的游戏内命令与 handler

- **`唤醒会话 <sessionId>`**（也可用 `!waken <id>` / `waken <id>`）
  在游戏聊天里直接指定「游戏事件要投给哪个 DSH 会话」 —— 不用回来改配置文件。
  存储于 `config/mcbaratone-waken-session.txt`。敲 `唤醒会话 clear` 清除。
  - 前缀特意用**中文**：`!` 会被 Baritone 抢去当命令解析，刷一屏「未知或不完整的命令」。
- **`get_look_target`**（只读）：报告准星指着什么，含完整 block_state。
- bridge 另加 `get_waken_session` / `set_waken_session` 两个 handler 供 DSH 侧读写。
- **`equip_best_tool`**（**不**暴露成 MCP 工具，供其它工具内部调用）：把对的工具或**指定的**
  物品弄到手上。三种 `kind` —— `dig`（按方块的原版 `mineable/*` 标签判类别）、`weapon`
  （`剑 > 斧` + 材质排序）、`item`（把指定物品弄到手上）。换手两条路：**快捷栏直接切手持；
  背包深处用容器点击与选中槽互换** —— 只改客户端 `Inventory` 服务端不认账，会变成
  "本地拿上了、服务端认为空手"，攻击和挖掘全都静默不作数。

---

## 五、MCP 工具速查（共 101 个，这里是常用的）

| 分类 | 工具 |
| --- | --- |
| 说话 | `say` |
| 观察 | `snapshot` `get_inventory` `get_entities` `get_player_pos` `get_block` `get_look_target` `get_combat_snapshot` `get_dimension` `find` `get_setting` `get_events`（酌情用，见 §六）|
| 看图 | `screenshot`（**唯一能真正"看见"画面**的工具：存 PNG 到 `<gameDir>/screenshots/`，返回绝对路径，直接当图片读）|
| 移动 | `goal_coordinates` `set_goal_block` `goto_block_type` `come` `thisway` `path` `explore` `surface` `tunnel` |
| 采集 | `mine_quantity` `start_mining` `stop_mining` `farm` `dig_block` |
| 战斗 | `attack_nearest` |
| 界面 | `get_screen`（界面里有什么 + 是否真有容器；默认只列非空格，看全布局传 `include_empty=True`）`close_screen`（关掉，带回读确认）|
| 交互 | `interact_block` `place_block` `place_torches` `use_bucket` `eat` `equip` `throw_item` `select_slot` `look_at` `look_around` `respawn` |
| 合成 | `craft` `craft_advanced` `auto_craft` |
| 建造 | `build_schematic` `inspect_build_site` `preview_build_plan` |
| 兜底 | `run_command` `batch_command` `cancel_command` `help_command` |

> 上表里「插火把 / 放方块 / 用桶 / 挤奶 / 剪毛」都**自带换手**（见 §四.C 的 `equip_best_tool`）：
> 东西躺在背包深处也会自己掏出来，只有**真的没有**时才报 `no <item> in inventory`。
>
> 仓库另带 **`scripts/mc.py`**：绕过 MCP 直接给 bridge 发命令的调试工具。
> 出问题先用它分清「是工具层坏了还是游戏层坏了」，用法见 [`DSH-READHERE.md`](DSH-READHERE.md) §6。

---

## 六、已知限制

- **`get_recipes` 在 MC 1.21.4 不可用** —— 上游限制，客户端不再暴露配方表。别靠它查配方，`craft` 用的是内置配方表。
- **`interact_block` 对薄方块仍然挑剔** —— 已改成采样多点，但极端角度仍可能打不中（换一侧靠近即可）。
- **`mine_quantity` 大单不等待** —— 数量 > 5 时只负责「启动」，由调用方轮询背包；且**超时会自动 cancel**，避免残留任务把角色拖着到处跑。
- **`get_events` 要酌情调用** —— 聊天不用拉（插件会推）；不带 `event_type` 过滤会混进 `tick_update` 这类高频事件刷屏。**事件名一律小写**（`tick_update` / `damage` / `death` … 共 23 个，见 `EventManager.EventType`），Python 侧会 `lower()` 归一化。⚠ 早期版本误用 `upper()`，导致**带过滤的查询永远静默返回 0 条**（已修）。另外 **bridge 的 `get_events` 不带 `after_seq` 时会走 `pollEvents()`，那里有 `iterator.remove()` 会把事件从缓冲区抽走** —— 所以这个工具在实现上**永远走游标模式**，不要绕过它裸调。
- **`close_screen` 关不掉非容器界面** —— 它调的是 `player.closeContainer()`，只能关箱子/熔炉/交易/工作台这类容器界面；PauseScreen、DeathScreen 关不掉（会返回 `closed: false` + hint），那类界面需要在 bridge 侧用 `setScreen(null)`。
- **`screenshot` 是异步写盘、且只出 PNG** —— bridge 侧是 `AsyncCommandHandler`，文件由 IO 线程写完后才回调，所以以返回里的 `saved` 为准，别刚发完命令就去读文件；`filename` 会被清洗成 `[A-Za-z0-9_-]` 再拼时间戳和自增序号（重名不覆盖，中文名会变成一串下划线，用英文短名）；截图落在**客户端**的 `<gameDir>\screenshots\`，返回的是绝对路径。
- **`inventory_click` 的两种"槽号"别混** —— `get_inventory` 给的是**背包索引**（0-8 快捷栏、9-35 主背包、36-39 盔甲、40 副手），而 `inventory_click` 的 `slot` 是**菜单槽位号**：在玩家自带的 `InventoryMenu`（`sync_id=0`，**不用开任何界面**就能用）里，9-35 恰好等于主背包索引，但**快捷栏是 36-44**，45 是副手；`SWAP` 的 `button` 指的是"快捷栏第几格"，不是槽号。返回里带两份完整槽位快照，只想看结果请用 `get_screen`。
- **bridge 无认证层** —— 只能跑在回环地址或可信网络，别暴露到公网。
- **工具的运行时常量**：Python 侧轮询上限必须小于 DSH 的 `toolCallTimeoutMs`（现配 120 秒），否则会先报超时。

---

## 七、测试

### Python

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

本 fork 当前实测：**2725 passed, 1 skipped**。

默认配置**排除**标记为 `live_readonly` / `live_mutating` 的用例 —— 那些会真的连进 Minecraft 并改动世界，
按 `tests/functional/README.md` 的说明**逐个显式选择**，不要对着有价值的存档跑未过滤的套件。

### Java（bridge）

```powershell
cd bridge
gradle test
```

本 fork 当前实测：**42 个测试类 / 396 tests / 0 failed（117 skipped）**。

#### ⚠ 别删 `gradle.properties` 里的 `-Dfile.encoding=GBK`

`bridge/gradle.properties` 的 `org.gradle.jvmargs=-Xmx1G -Dfile.encoding=GBK` 是**必需**的，
它对应一条很长的链路：

1. Gradle 把测试 worker 的 classpath 写进一个 **argfile**（`E:\repository\.tmp\gradle-worker-classpath*.txt`），
   再以 `@argfile` 形式递给 worker JVM —— 因为 classpath 有 167 个条目，超过 Windows 命令行长度上限。
2. **Gradle 用 daemon 的 `file.encoding` 写这个文件**，**JVM 却用 `sun.jnu.encoding` 读它**。
3. 中文 Windows 上 `sun.jnu.encoding` 是 **GBK**，而且 **`-Dsun.jnu.encoding=…` 覆盖不了**（JDK-8321268）。
4. 结果：daemon 按 UTF-8 写、JVM 按 GBK 读 → **classpath 里凡是含中文的条目全部乱码**（本仓库正好躺在 `…\调研AI陪玩MC\…` 下）
   → 每个测试类都 `ClassNotFoundException`，可 `.class` 文件明明就在 `build\classes\java\test\` 里躺着。

**让两边编码一致**就行：daemon 也用 GBK 写，JVM 用 GBK 读，对上了。
这也是 `bridge/build.gradle` 里显式写 `it.options.encoding = 'UTF-8'` 的原因 ——
daemon 现在是 GBK，源码里的中文注释和常量必须显式按 UTF-8 编译。
（上游和 CI 上仓库路径是纯 ASCII，两处设置在那边都是 no-op。）

上游 issue：[gradle#30304](https://github.com/gradle/gradle/issues/30304)、
[gradle#30391](https://github.com/gradle/gradle/issues/30391)、
[JDK-8321268](https://bugs.openjdk.org/browse/JDK-8321268)、
[spring-boot#43051](https://github.com/spring-projects/spring-boot/issues/43051)。

#### 测试隔离约定

`CommandHandlerFactory.clearRegistry()` 清的是 **JVM 级静态注册表**，而默认注册只在静态初始化块里跑**一次**。
所以凡是调它的测试，**必须**在 `@AfterEach` 里补一句 `CommandHandlerFactory.registerDefaultHandlers()` 复原；
否则同一个 JVM 里后面所有查真实命令的测试都会失败（`CommandHandlerFactoryTest` 早期就漏了这一步，见它的注释）。

---

## 八、许可与致谢

- 上游：[jhwodchuck/mcbaratone](https://github.com/jhwodchuck/mcbaratone)，**MIT**
- 本 fork 的一方差量同样以 MIT 提供；随附的第三方组件（Baritone / Fabric / Minecraft 映射等）保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- Minecraft 是 Microsoft 的商标；Baritone 是独立开源项目。本项目与 Microsoft、Mojang Studios、Baritone 维护者均无隶属或背书关系。
