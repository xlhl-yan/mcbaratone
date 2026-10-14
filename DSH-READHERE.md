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
| **Baritone（Fabric 版）** | 见 `bridge/libs/README.md` | **bridge 是 Baritone 的桥，没有它必崩** |
| **Fabric API** | 与 MC 版本匹配 | 事件 API 依赖 |

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
.\.venv\Scripts\python.exe -m pytest -q      # 应当全绿（本仓库当前 2721 passed, 1 skipped）
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

把 jar 复制到**目标客户端**的 mods 目录：

```powershell
Copy-Item "<REPO>\bridge\build\libs\baritone-api-bridge-1.0.40.jar" `
          "<MC_GAME_DIR>\versions\<MC_VERSION>\mods\" -Force
```

**验收**：mods 目录里同时有 **bridge jar + Baritone jar + Fabric API jar**。

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

---

## 4. 验收清单（逐项打勾）

| # | 检查项 | 命令 / 操作 | 期望 |
| --- | --- | --- | --- |
| 1 | Python 测试 | `<VENV>\Scripts\python.exe -m pytest -q` | 全绿 |
| 2 | bridge jar | `Test-Path <REPO>\bridge\build\libs\baritone-api-bridge-1.0.40.jar` | True |
| 3 | 端口分离 | `netstat -ano \| Select-String ":5556\|:5558"` | 有 LISTENING |
| 4 | MCP 已接入 | 在 DSH 里列出工具 | 出现 `mcp__baritone__*`，约 **96** 个 |
| 5 | 能读游戏状态 | 调 `mcp__baritone__snapshot` | 返回坐标 / 血量 / 维度 |
| 6 | 能说话 | 调 `mcp__baritone__say`（参数 `message`）| 游戏聊天栏出现该句 |
| 7 | 能听玩家说话 | 在游戏里打一句话，再读聊天事件 | 能读到该句（带玩家名）|
| 8 | 会动 | 调 `mcp__baritone__goal_coordinates` | 角色开始寻路 |

**第 5～8 项任一失败，直接跳到 §6。**

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

**⚠️ 这条命令只在 DSH 侧有配套投递插件时才有意义。** 该插件不在本仓库（属于 DSH 侧集成），
它做两件事：轮询聊天事件 → 把消息投进指定的**活跃会话**。

**硬限制**：DSH 只能把消息投给**当下开着的**会话窗口；会话关了就投不进去。

### 5.3 长任务的正确用法

`mine_quantity` 这类工具分两种模式：

- **数量 ≤ 5**：工具会盯到挖满再返回（内部轮询上限 90 秒，**小于** `toolCallTimeoutMs`）
- **数量 > 5**：只负责启动，立刻返回，由调用方轮询背包进度
- **超时会自动 `cancel`** —— 这是刻意的，避免残留的 Baritone 任务把角色拖着到处跑

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
- `AGENTS.md` —— 本仓库的工作约定与发布边界（**动代码前先读**）
- `docs/architecture.md` —— 上游的组件与证据边界
- `tests/functional/README.md` —— live 测试的分类与安全须知
- `THIRD_PARTY_NOTICES.md` —— 第三方组件许可
