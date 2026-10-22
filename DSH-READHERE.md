# DSH-READHERE

> **这份文档写给「另一个 DSH」看。**
> 目标：让一个完全不了解本项目的 DSH 会话，照着这份说明**独立完成配置**，把「AI 陪玩 Minecraft」跑起来。
>
> 阅读顺序建议：先扫 §1 前置条件 → §2 路径约定 → §3 照做一遍 → §4 逐项验收。
> 卡住时直接查 §6 故障排查表。

---

## 1. 前置条件（先确认这些都在）

| 依赖 | 版本要求 | 为什么 |
| --- | --- | --- |
| **Java** | **25** | bridge 用 Java 25 编译。注意 Gradle 与 JDK 有官方兼容下限（25→Gradle 9.1+）|
| **Python** | 3.10+ | Python 控制器 / MCP server |
| **Minecraft + Fabric Loader** | bridge 面向 **26.2** | 客户端侧运行环境 |
| **Baritone（Fabric 版）** | 就用仓库里的 `bridge/libs/baritone-api-fabric-1.15.0-9-gd93f1582.jar` | **bridge 是 Baritone 的桥，没有它必崩**。这个 jar **一个文件两用**：既是编译期依赖，**也是要装进 mods 的本体** —— 别去别处下别的版本，bridge 对它是**精确匹配**（`=1.15.0-9-gd93f1582`），下错版本启动就报 `Incompatible mods found` |
| **Fabric API** | 与 MC 版本匹配 | 事件 API 依赖 |
| **DSH 侧插件** | 随本仓库 `plugins/` 提供 | 盯游戏内聊天、叫醒 agent（少了它就只有工具面、没有陪玩 —— 见 §3.7）|

**先跑这几条确认：**

```powershell
java -version            # 期望 25.x
python --version         # 期望 3.10+
Test-Path "<MC_GAME_DIR>\versions\<MC_VERSION>-Fabric\mods"   # mods 目录存在
```

> ⚠️ **最常见的开局失败**：mods 目录里只有 bridge，忘了 Baritone 本体 →
> Fabric 启动时报 `Incompatible mods found: 模组 baritone-api-bridge 需要 baritone …`。

---

## 2. 路径与命名约定

本文用尖括号占位，请自行替换（**不要照抄尖括号**）：

| 占位符 | 含义 | 怎么找到它 |
| --- | --- | --- |
| `<REPO>` | 本仓库根目录（含 `README.md` 和 `bridge\`）| 你 clone/解压的位置 |
| `<VENV>` | 给本项目建的 Python 虚拟环境 | §3.1 会创建 |
| `<MC_GAME_DIR>` | Minecraft 游戏目录 | 启动器里的「游戏目录」设置 |
| `<MC_VERSION>` | 版本名 | `versions\` 下那个目录名，如 `26.2-Fabric` |
| `<DSH_HOME>` | DSH 数据目录 | 一般为 `%USERPROFILE%\.dsh`，也可能被 `DSH_HOME` 环境变量覆盖 |

```powershell
# 确认 DSH 目录与 profile 列表
if ($env:DSH_HOME) { $env:DSH_HOME } else { "$env:USERPROFILE\.dsh" }
Get-ChildItem "$env:USERPROFILE\.dsh\profiles" -Directory | Select-Object -ExpandProperty Name
```

**双客户端约定**（陪玩的关键，见 §3.4）：

| | 谁在用 | bridge 端口 | 内置 MCP 端口 |
| --- | --- | --- | --- |
| 主客户端 | 人类玩家 | 5555（默认）| 5557（默认）|
| **AI 客户端** | AI 角色 | **5556** | **5558** |

---

## 3. 一次性配置（按顺序做）

### 3.1 建 Python 环境

```powershell
Set-Location <REPO>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q      # 应当全绿（本仓库当前 2724 passed, 1 skipped）
```

**验收**：pytest 没有 failed。

> 若 pip 下载慢：配置国内镜像或 HTTP 代理（`pip config set global.index-url …`）。

### 3.2 编译 bridge

```powershell
Set-Location <REPO>\bridge
.\gradlew.bat --no-daemon --console=plain build -x test
```

产物：`<REPO>\bridge\build\libs\baritone-api-bridge-1.0.40.jar`

**验收**：`BUILD SUCCESSFUL`，且该 jar 存在。

> **`-x test` 是故意的**：Java 侧测试依赖真实客户端环境，跳过它们 jar 照样产出。
>
> **若卡在下载 Gradle**（`gradlew` 要先下发行版）：
> ① 配代理；② 或临时把 `bridge\gradle\wrapper\gradle-wrapper.properties` 里的
> `distributionUrl` 换成国内镜像（**改完记得改回来再提交**）；③ 或自己装一份 Gradle 直接用。
> **注意** `distributionSha256Sum` 必须与实际下载的发行版匹配，换源时要一并确认。

### 3.3 把 jar 部署进客户端

mods 目录里最终要有**三个 jar**，其中两个能直接从仓库拿：

| 要装的 | 从哪来 |
| --- | --- |
| **Baritone 本体** | `<REPO>\bridge\libs\baritone-api-fabric-1.15.0-9-gd93f1582.jar` —— **不用去别处找，就是它**（sha256 以 `86d06fce44e8` 开头）。bridge 对它**精确匹配**，换成别的版本会报 `Incompatible mods found` |
| **Fabric API** | 自己去下（要跟 MC 版本匹配，例如 `fabric-api-0.161.0+26.2.jar`）|
| **bridge** | §3.2 编译出来的 `<REPO>\bridge\build\libs\baritone-api-bridge-1.0.40.jar` |

```powershell
$mods = "<MC_GAME_DIR>\versions\<MC_VERSION>\mods"
Copy-Item "<REPO>\bridge\libs\baritone-api-fabric-1.15.0-9-gd93f1582.jar" $mods -Force
Copy-Item "<REPO>\bridge\build\libs\baritone-api-bridge-1.0.40.jar" $mods -Force
# Fabric API 自己放进去

Get-ChildItem $mods -Filter *.jar | Select-Object Name   # 三个都在才算完
```

**验收**：`Get-ChildItem $mods` 里同时有 **bridge jar + Baritone jar + Fabric API jar**。

### 3.4 分配端口（只给 AI 客户端填）

JVM 参数控制端口，**不要改源码默认值再编译**：

```
-Dbaritone.bridge.port=5556 -Dmcp.port=5558
```

- 在启动器里填「**该实例的 JVM 参数**」（HMCL：实例设置 → JVM 参数）
- **只填给 AI 那个实例**；人类玩家那个不填，用默认 5555

**验收**（启动后）：

```powershell
netstat -ano | Select-String ":5555|:5556|:5557|:5558"
```

期望：人类客户端占 **5555/5557**，AI 客户端占 **5556/5558**，互不重叠。

> ⚠️ **这是「工具突然全哑」的头号原因**：两个客户端同时抢 5555，后启动的那个会**静默失败**
> （bridge 起不来，端口没有监听，但游戏本身正常）。

### 3.5 确认 MCP 包装器

**为什么要它**：`mcp_server.py` 里 `BridgeConfig` 默认 `transport_type="websocket"`，
而 CLI 没有覆盖该字段的参数 —— 按官方入口启动会去连 `ws://…:5555`，但那个端口是**裸 TCP**，
必然连不上。包装器只做一件事：把它改成 `"tcp"`。

本仓库自带 `scripts/mcp_stdio_wrapper.py`，**不用手抄**。

**验收**（期望打印出参数列表：`--host` / `--port` / `--mcp-transport` …）：

```powershell
<VENV>\Scripts\python.exe <REPO>\scripts\mcp_stdio_wrapper.py --help
```

### 3.6 写进 DSH 的配置

编辑 `<DSH_HOME>\profiles\<profile>\cordis.patch.yml`，加一段 `insert`：

```yaml
- insert:
    - id: mcp-baritone
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: baritone
        transport: stdio
        command: '<VENV>\Scripts\python.exe'
        args:
          - '<REPO>\scripts\mcp_stdio_wrapper.py'
          - "--host"
          - "localhost"
          - "--port"
          - "5556"          # ← AI 客户端的 bridge 端口
        toolCallTimeoutMs: 120000
```

**要点**：

1. `name` 必须是 DSH **内置**的 `@deepseek-ai/dsh-mcp-client` —— 它不需要装进 profile 的
   `dependencies`，**只写这条 patch 即可**（把它当普通 npm 包装进 dependencies 是错的）。
2. 必须用 **`- insert:`** 包裹。不带 `insert` 的 patch 行如果 id 匹配不到已有条目，
   DSH 会**静默跳过**（只 warn），表现为"配置写了但完全没生效"。
3. `toolCallTimeoutMs` 建议 ≥ 120000：Python 侧有些工具内部会轮询等待。

**然后重启 DSH。**

### 3.7 装 DSH 侧的配套插件

bridge 只负责"看"和"动"，**叫醒 agent** 这件事由 DSH 侧插件做：本仓库的
`<REPO>\plugins\dsh-mc-chat-watch`（盯游戏内聊天 → 把话投进绑定的会话；另含游戏态势与空闲自主）。
少了它就只有"工具面"、没有"陪玩"。

1. 把插件目录拷到 DSH 能长期访问的位置（例如 `E:\dsh-plugins\dsh-mc-chat-watch`）；
2. 在 `<DSH_HOME>\profiles\<profile>\package.json` 里补**两处**：`dependencies` 加
   `"dsh-mc-chat-watch": "link:<插件目录>"`，**并且** `dsh.profile.bundles` 数组里加上
   `dsh-mc-chat-watch`；
3. 重启 DSH。

> ⚠️ **`dependencies` ≠ 装载。** 只在 dependencies 里写了、bundles 里漏了，插件会被完全跳过，
> 日志里连 `[apply]` 都不出现（本项目真踩过）。

**验收**：`<DSH_HOME>\mc-chat-watch\watch.log` 里出现

```
[apply] 聊天监听起来了：127.0.0.1:5556 每 3s 一次；忽略 DeepSeek
[apply] 空闲自主：开；… 开关与节奏都在 DSH 设置 → Minecraft 陪玩 里改
```

---

## 4. 验收清单（逐项打勾）

| # | 检查项 | 命令 / 操作 | 期望 |
| --- | --- | --- | --- |
| 1 | Python 测试 | `<VENV>\Scripts\python.exe -m pytest -q` | 全绿 |
| 2 | bridge jar | `Test-Path <REPO>\bridge\build\libs\baritone-api-bridge-1.0.40.jar` | True |
| 3 | 端口分离 | `netstat -ano \| Select-String ":5556\|:5558"` | 有 LISTENING |
| 4 | MCP 已接入 | 在 DSH 里列出工具 | 出现 `mcp__baritone__*`，约 **99** 个 |
| 5 | 能读游戏状态 | 调 `mcp__baritone__snapshot` | 返回坐标 / 血量 / 维度 |
| 6 | 能说话 | 调 `mcp__baritone__say`（参数 `message`）| 游戏聊天栏出现该句 |
| 7 | 能听玩家说话 | 在游戏里打一句话，再读聊天事件 | 能读到该句（带玩家名）|
| 8 | 会动 | 调 `mcp__baritone__goal_coordinates` | 角色开始寻路 |
| 9 | 能看见画面 | 调 `mcp__baritone__screenshot`，再用图片工具读返回的 `path` | 看到真实的游戏画面 |

**第 5～9 项任一失败，直接跳到 §6。**

---

## 5. 日常使用

### 5.1 说话与听话

- AI 说话：`mcp__baritone__say`（**这是唯一该用的说话工具**）
  - 不要用 `run_command` 说话 —— 那条路会把文本当命令处理
- 听玩家说话：读聊天事件（`get_events`，`type=chat`）

### 5.2 游戏内命令：指定唤醒哪个 DSH 会话

玩家可以在游戏聊天里直接指定「游戏事件投给哪个 DSH 会话」：

```
唤醒会话 <sessionId>
```

- 兼容写法：`!waken <id>`、`waken <id>`
- 清除：`唤醒会话 clear`
- 值存于 `<MC_GAME_DIR>\config\mcbaratone-waken-session.txt`

> **前缀特意用中文**：`!` 会被 Baritone 抢去当命令解析，刷出一屏「未知或不完整的命令」。

> **为什么是聊天文本，不是 `/` 命令**：MC 的 `/` 输入走**服务端命令通道**（客户端把整串交给
> 服务端解析），本地只剩「未知的命令」这类系统消息；而 bridge 是**客户端 mod**，只监听
> `ClientReceiveMessageEvents` 的 GAME / CHAT 频道 —— **拿不到自己发出命令的原始参数**。
> 要真做成 `/` 命令得注册 Fabric 客户端命令（`ClientCommandRegistrationCallback`），
> 代价是重编译 jar + 两个客户端换包 + 重启客户端；本 fork 权衡后保留聊天文本嗅探，
> 换「零编译、改一句话就能用」。**代价如实说清**：没有 tab 补全，手滑打成
> `/唤醒会话 xxx` 不生效（会被服务端回一句「未知的命令」）。

**⚠️ 这条命令只在 DSH 侧有配套投递插件时才有意义。** 该插件不在本仓库（属于 DSH 侧集成），
它做两件事：轮询聊天事件 → 把消息投进指定的**活跃会话**。

**硬限制**：DSH 只能把消息投给**当下开着的**会话窗口；会话关了就投不进去。

### 5.3 长任务的正确用法

`mine_quantity` 这类工具分两种模式：

- **数量 ≤ 5**：工具会盯到挖满再返回（内部轮询上限 90 秒，**小于** `toolCallTimeoutMs`）
- **数量 > 5**：只负责启动，立刻返回，由调用方轮询背包进度
- **超时会自动 `cancel`** —— 这是刻意的，避免残留的 Baritone 任务把角色拖着到处跑

### 5.4 陪玩时读的三本手册

本仓库根目录下有三本**给 agent 读**的操作手册 —— 陪玩那个会话要照着它们做：

| 手册 | 什么时候读 | 讲什么 |
| --- | --- | --- |
| `陪玩会话启动提示.md` | **新开陪玩会话时，第一件事** | 你是谁、环境由哪三段链路组成、会被什么唤醒、**条件反射表**（受伤/聊天/态势/空闲各怎么反应）、七条铁律、工具速查、已知的坑、进游戏先做哪三件侦察 |
| `空闲自主行为手册.md` | 被 `[autonomy]` 叫醒时（消息开头是「（空闲自主时间）」） | 用 **DSH 自己的 goal 机制**（`get_goal` / `create_goal` / `update_goal`）给自己立目标、把目标拆成能判定完成的步骤、一轮只推进一步；另有缺料闭环、红线 |
| `游戏态势应对手册.md` | 收到「（游戏态势）…」消息时 | 低 token 查表：什么情况该动、什么情况只回一个「等」字 |

要点（别踩）：

- **目标用 DSH 的 goal，不要往文件里写** —— 目标的持久化靠 `create_goal` / `update_goal`，
  新窗口 `get_goal` 就能接上；`phase` 是 `active` 时 DSH 会自己续轮推进它（要停就 `pause` / `complete`）。
- 三本手册在**本仓库根**（与 `DSH-READHERE.md` 同级）；工作区里另有副本供人翻阅，
  **改完手动同步两边**。
- 三本手册都**不含具体世界状态/坐标/玩家数据**（公开仓库的发布边界，见 `AGENTS.md`）。
  当前环境的真实状态由会话自己用工具侦察。

---

## 6. 故障排查表

| 症状 | 最可能的原因 | 处置 |
| --- | --- | --- |
| **DSH 里根本没有 `mcp__baritone__*` 工具** | patch 没生效 / wrapper 没起 | ① 确认用了 `- insert:`；② 确认 `command` 指向的 python 路径真实存在；③ 重启 DSH 后看有没有 wrapper 进程（`Get-CimInstance Win32_Process -Filter "Name='python.exe'" \| ? CommandLine -match mcp_stdio`）|
| **工具存在但全部超时/报连接错** | 端口不对 / 两个客户端抢同一端口 | `netstat` 看 5556 是否 LISTENING；确认 AI 客户端带了 `-Dbaritone.bridge.port=5556` |
| **一遇中文就崩或乱码** | 用了未打补丁的上游代码 | 本仓库已修（GB18030 解码）——确认你用的是本分支代码 |
| **收到不到玩家聊天** | 只注册了 GAME 频道 | 本仓库已修（补注册 `ClientReceiveMessages.CHAT`）——确认用本分支代码 |
| **命令返回成功但世界没变** | 客户端预测 ≠ 服务器接受 | **改看世界状态**（读方块/背包）验证；并注意服务端回包有延迟，别读太快 |
| **挖矿"报成功但不动"** | 上游 `mine` handler 是坏的 | 本仓库的工具已改走 Baritone 原生 `#mine <方块>` |
| **`craft` 报 `Recipe requires crafting table`** | 3×3 配方需要打开工作台界面 | 先打开工作台，或只用 2×2 配方 |
| **`get_recipes` 报不可用** | MC 1.21.4 起客户端不再暴露配方表 | 上游限制，无解；`craft` 用内置配方表，不需要先查 |
| **`interact_block` 报 `Target is not visible`** | 薄方块（门/按钮）瞄准擦空 | 换个方向靠近目标再试（本仓库已大幅缓解，但极端角度仍可能失手）|
| **工具报 `no <item> in inventory`** | 背包里**真的没有**那件东西 | 不用自己先挪到快捷栏 —— 工具会自己从背包**任意位置**换手（`equip_best_tool`）；确实缺料就跟玩家要，或自己做一件 |
| **`place_torches` 报 `Torch target out of reach`** | 目标超出交互距离（默认 4.5 格） | 走近再插。⚠️ 老版本这里是**假成功**（够不着也回 `placed:true`、方块却没出现），现已改成距离检查 + 后置校验 |
| **Gradle 下载卡住** | 网络 | 配代理，或临时换镜像（**改完改回来**）|
| **`git push` 报 `Empty reply from server`** | HTTP/2 与代理不兼容 | `git config http.version HTTP/1.1` + `http.postBuffer 524288000` |

### 先分清「工具层」还是「游戏层」

排查上面任何一条之前，先用 `scripts/mc.py` **绕过 MCP 直连 bridge** 跑同一个命令：

```powershell
<VENV>\Scripts\python.exe <REPO>\scripts\mc.py 5556 get_state
<VENV>\Scripts\python.exe <REPO>\scripts\mc.py 5556 get_entities radius=64
<VENV>\Scripts\python.exe <REPO>\scripts\mc.py 5556 --list        # 列出全部 bridge 命令
```

- **mc.py 也失败** → 问题在 **bridge / 游戏侧**（端口、mod、角色状态），与 MCP 工具无关
- **mc.py 成功、工具失败** → 问题在 **MCP 工具层**（参数映射、超时配置）

这条捷径能省掉大量来回猜测。

---

## 7. 红线（别做）

1. **不要改 bridge 源码里的端口默认值再编译** —— 端口一律用 JVM 参数控制。改源码编 jar 会在多客户端场景下静默冲突。
2. **不要把 bridge 暴露到公网** —— 它**没有认证层**，只能跑在回环或可信网络。
3. **不要对有价值的存档跑未过滤的 live 测试** —— `tests/functional/` 里有些用例会传送玩家、发物品、改方块甚至重置世界。按 `tests/functional/README.md` **逐个显式选择测试 ID**。
4. **不要往仓库提交运行态数据** —— 服务器地址、玩家数据、世界地图、检查点、遥测采集、日志、存档状态一律不进版本库（见 `AGENTS.md` 的 publication boundary）。示例请用占位符。
5. **不要靠返回值判断成败** —— 见 §6 第 5 条。这个项目最重要的经验就是「**看世界状态，不看命令返回值**」。

---

## 8. 参考

- `README.md` —— 本 fork 的定位、改了什么、工具速查
- `陪玩会话启动提示.md` —— **给 agent 读**：新开陪玩会话的第一份读物，环境/唤醒源/条件反射表/铁律/工具速查/已知的坑（见 §5.4）
- `空闲自主行为手册.md` —— **给 agent 读**：空闲自主时怎么用 DSH 的 goal 机制立目标、拆步骤、推进（见 §5.4）
- `游戏态势应对手册.md` —— **给 agent 读**：收到游戏态势时按表判断该动不动（见 §5.4）
- `plugins/README.md` —— **DSH 侧配套插件**：是什么、怎么挂进 profile、红线
- `plugins/dsh-mc-chat-watch/README.md` —— 那个插件自己的说明书（设置页怎么用、配置项、为什么不用 DSH 的 settings 服务）
- `AGENTS.md` —— 本仓库的工作约定与发布边界（**动代码前先读**）
- `docs/architecture.md` —— 上游的组件与证据边界
- `tests/functional/README.md` —— live 测试的分类与安全须知
- `THIRD_PARTY_NOTICES.md` —— 第三方组件许可
