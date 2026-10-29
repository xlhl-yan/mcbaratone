# dsh-mc-preset

把 Minecraft 陪玩做成 **DSH 的 agent preset**（预设模式）—— 新建会话时选一下，人格、工具面、技能全就位，不用再手工贴启动提示文档。

## 两个预设

| preset id | 名称 | 给谁用 |
|---|---|---|
| `mc-companion` | MC 陪玩 | **有人一起玩** —— 跟着走、听指令、不需要动作只回「等」 |
| `mc-solo` | MC 单人游玩 | **一个人玩** —— 没有外援，目标自己定，麻烦自己扛 |

两者的工具面几乎一样，**差别全在 `persona.prefix`**（行为准则）。

## 装

```bash
node plugins/dsh-mc-preset/install.mjs
```

它会：

1. 探测本机的 venv python、`scripts/mcp_stdio_wrapper.py`、本包的 `skills/`
2. 把模板里的占位符替换成实际路径
3. **幂等**合并进 `$DSH_HOME/profiles/web/cordis.patch.yml`（带 `# === MC agent presets BEGIN ===` 标记，重复跑只会替换自己那段）
4. 用 DSH 同一套 YAML schema 校验结构，不合法就中止且不写盘
5. 写之前自动备份

**装完重启 DSH**，新建会话时选「MC 陪玩」或「MC 单人游玩」。

### 常用参数

```bash
# 先看看会做什么，不落盘
node plugins/dsh-mc-preset/install.mjs --dry-run

# 装到桌面端 profile
node plugins/dsh-mc-preset/install.mjs --profile desktop

# 手动指定路径（自动探测失败时）
node plugins/dsh-mc-preset/install.mjs --python D:/some/venv/Scripts/python.exe --wrapper D:/some/mcp_stdio_wrapper.py

# 换 bridge 端口
node plugins/dsh-mc-preset/install.mjs --port 5557

# 卸载
node plugins/dsh-mc-preset/install.mjs --uninstall
```

## 前置条件

| 需要 | 说明 |
|---|---|
| **Python 环境** | 按 `DSH-READHERE.md` §3.1 建好 `<仓库>/.venv`，并能 `python -c "import baritone_client"` |
| **MCP 包装器** | `<仓库>/scripts/mcp_stdio_wrapper.py`（本仓库自带） |
| **DSH ≥ 0.1.7** | 需要 `@deepseek-ai/dsh-agent-preset` 与 `dsh-agent-preset-registry` |
| **profile 已有 registry** | 检查 `cordis.patch.yml` 里有 `id: agent-preset-registry`；没有就照 README 末尾补一段 |

⚠️ **启动顺序**：先开 Minecraft（bridge 端口在听）→ 再开 DSH。反过来 MCP 连不上会退避重试 10 次后放弃，工具消失，只能重启 DSH。

## 装完长什么样

选上预设后，这个会话**只能看到这些工具**：

```
persona          人格与铁律（常驻 system prompt）
mcp-baritone     106 个 MC 工具
skill-mc         本包的 skills/（工具速查 / 熟悉的坑 / 目标灵感）
tool-skill       让 agent 能按需读 skill
tool-ask-user    问用户
tool-todo        任务清单
tool-goal        goal 自主推进
compaction       上下文压缩
```

**没有** bash / 文件 / 网页 / 终端 —— preset 的 `plugins` 就是「模型可见工具」的完整列表，**不列 = 看不见**。陪玩用不上那些，省掉的工具描述既不占 token，也不至于让 agent 分心去写脚本。

## 为什么不把手册塞进 persona

`persona.prefix` 是**每一轮都在上下文里**的。启动提示全文近 10KB，常驻的话每一轮都在吃 token。

所以这里做了切分：

| 内容 | 放哪 | 为什么 |
|---|---|---|
| 你是谁 / 条件反射表 / 铁律 | `persona.prefix`（常驻，1.4~1.7KB） | 每次都要用 |
| 工具速查 / 已知的坑 / 目标灵感 | `skills/*/SKILL.md`（按需读） | 只在查的时候用 |

## 目录

```
plugins/dsh-mc-preset/
├─ install.mjs                    安装/卸载脚本
├─ templates/
│   ├─ mc-companion.patch.yml.tpl 陪玩预设（占位符版）
│   └─ mc-solo.patch.yml.tpl      单人预设
├─ skills/
│   ├─ mc-tools/SKILL.md          106 个工具怎么调
│   ├─ mc-pitfalls/SKILL.md       18 个踩过的坑
│   └─ mc-goals/SKILL.md          目标灵感（单人版会读）
└─ README.md
```

三个占位符：

| 占位符 | 装到哪 |
|---|---|
| `__VENV_PYTHON__` | `<仓库>/.venv/Scripts/python.exe`（Linux/macOS 是 `.venv/bin/python`） |
| `__WRAPPER__` | `<仓库>/scripts/mcp_stdio_wrapper.py` |
| `__SKILLS_DIR__` | `plugins/dsh-mc-preset/skills` 的绝对路径 |

想自己手改模板也行，改完重跑 `install.mjs` 就生效。

## 没选预设的会话看不到 MC 工具

这是**有意为之**：MC 的 106 个工具只在两个 `mc-*` 预设里存在，全局配置里**不再插入** `mcp-baritone`。

⚠️ 副作用：`dsh-mc-chat-watch` 插件还是会叫醒任何绑定会话（聊天/受伤/态势照旧），但**没选预设的会话操作不了游戏**。所以陪玩会话一定要选预设。

## profile 里没有 registry 时补这段

```yaml
- id: agent-preset-registry
  name: '@deepseek-ai/dsh-agent-preset-registry'
  config:
    default: standard
    selectedDefault: standard
```

## 排错

| 现象 | 原因 |
|---|---|
| 新建会话没有「MC 陪玩」选项 | 没重启 DSH / profile 里没有 `agent-preset-registry` |
| 选了预设但没有 MC 工具 | MC 没开（bridge 端口没在听）；或 DSH 启动早于 MC，重启 DSH |
| 工具在但一调就超时 | 端口不对（默认 5556），或 wrapper 没显式走 TCP |
| `install.mjs` 说找不到 python | 没建 venv —— 先按 `DSH-READHERE.md` §3.1 做 |
| DSH 启动报 `patch.insert?.forEach is not a function` | profile 被手工改坏了，用备份 `cordis.patch.yml.bak-*` 还原 |

## 验证装得对不对（不启动 DSH）

```bash
node <dsh>/lib/bin.js --profile web --dump-config | grep -A3 'id: preset-mc'
```

应该能看到 `preset-mc-companion` 和 `preset-mc-solo` 两段，里面各有完整的 `plugins` 列表。**这比启动试错快得多。**
