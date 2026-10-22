# plugins/ —— DSH 侧的配套插件

这里放**跑在 DSH 一侧**、陪玩链路必需的插件源码。它和 `bridge/`（Java 模组，跑在 MC 客户端里）、
`src/`（Python MCP server，跑在 DSH 侧）三段合起来才是完整的一条链：

```
DSH ──► plugins/（本目录） ──TCP 5556──► bridge（Fabric mod，MC 客户端内）
        聊天唤醒 / 态势观察 / 空闲自主
```

## 里面有什么

| 目录 | 是什么 |
| --- | --- |
| `dsh-mc-chat-watch/` | 盯 MC 游戏内聊天 → 把话投进绑定的 DSH 会话叫醒 agent；另含「游戏态势」与「空闲自主」两套叫醒引擎；所有开关在 DSH 设置页「Minecraft 陪玩」里改 |

## 它在 DSH 里怎么挂上

两处都要有（**只写 dependencies 不算装载**，bundle 列表才决定加载）：

1. `<DSH_HOME>/profiles/<profile>/package.json` 的 `dependencies` 里有它（本地 link 或 npm 包名）；
2. 同一个文件的 `dsh.profile.bundles` 数组里有 `dsh-mc-chat-watch`。

> ⚠️ **注意：`dependencies` ≠ 装载。** 这个插件曾经从 `bundles` 列表里掉出去过，表现是
> "装了、也在 dependencies 里，但插件完全没跑，日志里连 `[apply]` 都没有"。

## 和 DSH 实际加载目录的关系：**手动同步，不用 link**

DSH 实际加载的是另一个位置（本机是 `E:\dsh-plugins\dsh-mc-chat-watch` —— profile 的
`dependencies` 里写的就是它）。**本目录是仓库里的副本**，给"换台机器重来一遍"和"别的 DSH
照着做"用；改这里**不会**自动生效。

**改完手动同步**（用户 2026-10-15 定的：不用 link）：

```powershell
# 仓库这份 → DSH 实际加载的那份
Copy-Item <REPO>\plugins\dsh-mc-chat-watch\* "<DSH 实际加载目录>\" -Force
```

> 为什么不用 link 省掉这一步：那份 profile `package.json` 被别的工具重写过一次，**把本地 link
> 插件整个挤掉过**（表现是插件完全不装载）。不值得为省一步去动它。

同步完还要**重启 DSH** 才装载（见下）。

## 改完怎么装载

- 改 `index.js` / `cordis.patch.yml` → **重启 DSH**（Host 半边没有热重载）；
- 自检：看 `<DSH_HOME>/mc-chat-watch/watch.log` 里有没有 `[apply] …` 那几行自检；
- 离线自检（不碰真 MC / 真 DSH）：工作区里的 `工具\测插件命令.mjs`。

## 红线

1. **不要往这里放运行态数据** —— 日志、会话 id、世界坐标、玩家数据一律不进仓库，示例用占位符
   （同 `../AGENTS.md` 的发布边界）。
2. **不要为了插件方便去改 `bridge/` 或 `src/` 的契约** —— 命令名、事件字段这些是三段之间的接口，
   改之前先读 `../DSH-READHERE.md`。
3. **插件的开关全在 DSH 设置页**（设置 → Minecraft 陪玩），游戏里发 `自主关` 之类的文字
   现在就是普通聊天，会正常投给 agent。`唤醒会话` 例外 —— 那是 bridge 侧的命令，仍然有效。
