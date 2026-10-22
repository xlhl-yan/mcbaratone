# dsh-mc-chat-watch

DSH 插件：盯住 Minecraft（AI 角色所在那个客户端）的游戏内聊天，一有新消息就把 agent 叫醒；
另附「游戏态势」与「空闲自主」两套叫醒引擎。**事件驱动**，不是定时开口，闲着的时候几乎不花 token。

- Host 半边：`index.js`（只注册配置读写接口 + 四套引擎）
- 浏览器半边：`client.js`（在 DSH 设置里贡献「Minecraft 陪玩」页）
- 配置：`cordis.patch.yml`（bundle patch）为主，**DSH 设置页改动落在 `<DSH_HOME>/mc-chat-watch/settings.json`**
- 日志 / 状态：`<DSH_HOME>/mc-chat-watch/`（默认 `~/.dsh/mc-chat-watch/`）

---

## 一、所有开关都在 DSH 设置页

**打开方式**：DSH → 设置 → 左侧「Minecraft 陪玩」。

七组配置，改完即时生效、不用重启 DSH：

| 组 | 能改什么 |
| --- | --- |
| 账号 | 玩家账号名、AI 账号名（用于识别"谁在说话"、排除自己） |
| 投递目标 | 固定会话 ID、设备前缀 |
| 连接 | bridge 主机 / 端口 / 单次超时 |
| 节奏 | 聊天轮询间隔、单轮最多投递 |
| 开关 | 插件总闸 |
| 态势播报 | 启用开关、采样间隔、播报模式（`changed` / `always`）、两次最小间隔、每小时上限 |
| 空闲自主 | 启用开关、空闲阈值、两次最小间隔、忙闲检查间隔、每小时上限 |

**保存规则**：输入框**失焦或按回车**才提交（不会边打字边存）；开关是点一下即生效。
保存成功后右下角会浮出「已保存」，1.8 秒后自动消失。

**优先级**：`settings.json`（设置页写的） > `cordis.patch.yml` > 代码默认值。
页面里被改过的项会显示「已覆盖」标记；想回落到配置文件的值，把那一项清空即可。

> **曾经有一套游戏内聊天命令**（`自主关`、`态势 间隔 300` 之类），
> 有了设置页之后已经移除 —— 现在游戏里发这些话就是普通聊天，会正常投给 agent。

### 为什么不由 DSH 的 settings 服务承载？

本机实测：`ctx.get('settings')` 返回的是**一份设置文档对象**（`documentPath` / `describe` /
`mutate` 等），既没有 `installSection` 也没有 `register`。走那条路时页面能挂载、`set()` 也能调，
但写完 `value` / `user` 里永远是 `undefined`，且**不报错**。

所以改成和 `dsh-meme` 一样的做法：**插件自己注册 HTTP 接口**。

- `GET  /mc-chat-watch-config` → `{ ok, config, overrides }`
- `POST /mc-chat-watch-config` → 收一个 patch 对象；值为 `null` 表示删掉该覆盖
- 只接受 loopback 请求（这条路没有认证层）

---

## 二、配置项（`cordis.patch.yml`）

设置页能改的项在配置文件里同样有效，只是优先级更低：

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `enabled` | `true` | 插件总闸（聊天转发） |
| `host` / `port` | `127.0.0.1` / `5556` | AI 客户端 bridge 的地址（端口靠 JVM 参数 `-Dbaritone.bridge.port` 控制） |
| `tickSeconds` | `3` | 聊天轮询间隔 |
| `playerName` / `aiAccount` | `<玩家名>` / `<AI 账号>` | 双玩家识别 |
| `ignoreSenders` | `[]` | 这些发送者的话不投递；留空则自动包含 `aiAccount` |
| `maxPerTick` | `10` | 单轮最多投几条，防刷屏 |
| `sessionId` | 空 | 游戏消息投给谁；留空 = **拒绝投递**（不猜） |
| `observe` / `observeSeconds` / `observeMode` | `true` / `180` / `changed` | 态势观察引擎 |
| `autonomy` / `autonomyTickSeconds` / `autonomyIdleSeconds` / `autonomyMinGapSeconds` / `autonomyMaxPerHour` | `true` / `20` / `120` / `300` / `6` | 空闲自主引擎 |
| `proactive` | `false` | 老的规则版主动说话（已被 observe 取代，默认关） |

---

## 三、四套引擎在干什么

| 引擎 | 干什么 | 闸门 |
| --- | --- | --- |
| 聊天转发 | 玩家说话 → 投进绑定的会话，叫醒 agent | 每 3s 轮询；≤10 条/tick |
| 态势观察 `observe` | 每 180s 采一次游戏态势，**变了才**交给 agent | 最短 30s 一次、上限 40 次/小时；agent 忙时**只攒最新一条**，不排队 |
| 空闲自主 `autonomy` | 角色闲着（不寻路 + agent 不忙）满 120s 就交一次「自主时间」，goal 由 agent 自己定 | 间隔 ≥300s、上限 6 次/小时 |
| 主动说话 `proactive` | 规则版模板播报（血少 / 天黑 / 饿） | 默认关；和 observe 会打架（同一件事说两遍） |

判断表在工作区：`游戏态势应对手册.md`、`空闲自主行为手册.md`。

---

## 四、投递会话怎么定（防串台）

优先级：**游戏内 `唤醒会话 <id>` > 设置页/配置 `sessionId` > 不投**。

`唤醒会话` 这条由 **bridge（Java 侧）** 实现，不是本插件 —— 它把 sessionId 写进
`.minecraft-ai/versions/<版本>/config/mcbaratone-waken-session.txt`，插件只读那个结果。

没有明确绑定就**直接丢消息并记日志** —— 绝不"猜一个最近活跃的会话"，猜错会把游戏消息
送进别的窗口。原话：「找不到就不发，降级处理容易串台」。

同理，采到的态势数据不完整（位置全 0 / 血 0 / 饿 NaN，bridge 在玩家未加载或刚重生时会这样）
就**整轮跳过**，不交给 agent。

---

## 五、日志与自检

`<DSH_HOME>/mc-chat-watch/watch.log`，每次装载会打自检行：

```
[apply] 聊天监听起来了：127.0.0.1:5556 每 3s 一次；忽略 <AI 账号>
[apply] 态势观察：开；… 每 180s 采一次（设置页可改），…
[apply] 空闲自主：开；… 间隔>=300s，上限 6 次/小时；开关与节奏都在 DSH 设置 → Minecraft 陪玩 里改
[apply] 忙闲探测：agents=yes handle=no status=n/a
[apply] 设置接口已注册: /mc-chat-watch-config
```

常见行：

| 日志 | 含义 |
| --- | --- |
| `[fire] （游戏内聊天）xxx → 投递=已进会话 …` | 正常，话已经进 DSH 会话 |
| `… → 失败(会话 session-xxx… 不在活跃列表（窗口没开？）)` | 目标窗口没开，**消息丢弃**（不是串台） |
| `[tick] bridge 不可达：…` | MC 没开 / 端口不对，静默等下一轮 |
| `[observe] agent 正忙，暂存态势（累计 N 次）` | 背压生效，忙完只会补投最新一条 |
| `拿不到 webServer，设置页将只能读不能写` | webServer 服务没起来（插件自身照常工作） |

**改 `index.js` / `client.js` 后必须重启 DSH** —— Host 半边和浏览器半边都没有热重载。
**改设置页里的值不用重启**，它落在 `settings.json`，引擎每轮都按当前值跑。

---

## 六、离线自检

`调研AI陪玩MC\工具\测插件客户端加载.mjs`：用 `vm` 模拟 DSH 的模块加载器和 React，
把 `client.js` 真跑一遍 —— 验证 `exports.inject` 只含 `slots`（写错会让整个 web boot 白屏）、
页面注册进 `settings.section`、布尔项走 `primitives.Menu` 而不是原生 `<option>`、保存提示存在。

```powershell
node "D:\DeepSeekHarnessDesktop\WorkSpace\调研AI陪玩MC\工具\测插件客户端加载.mjs"
```

---

*最后更新：2026-10-22（开关统一迁到 DSH 设置页，移除游戏内命令）*
