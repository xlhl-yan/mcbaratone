/**
 * `dsh-mc-chat-watch` · Host 半边
 *
 * 盯住 Minecraft 里 AI 角色所在客户端的聊天，一有新消息就把话**投进当前会话**，
 * 把 agent 叫醒 —— 这样用户不用切到 DSH 窗口说话，AI 也能"听见"。
 *
 * 依据（照 dsh-selfwake 真机验过的那套）：
 *   ctx.get('agents') → agents.get(sessionId) → handle.agent.steer(message)（插入式；退回 followup 排队）
 *
 * 与 selfwake 的区别：selfwake 是"到点主动开口"（时间驱动）；
 * 本插件是"有人说话才醒"（事件驱动），所以轮询间隔可以很短而几乎不费 token。
 */

import { createConnection } from 'node:net';
import { execFile } from 'node:child_process';
import { readdirSync, statSync } from 'node:fs';
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { homedir } from 'node:os';

export const name = 'mc-chat-watch';
/** 只声明真正需要的服务；可选的用 ctx.get() 取，取不到就降级。 */
export const inject = ['timer', 'agents'];

const DEFAULTS = {
  enabled: true,
  host: '127.0.0.1',
  /** AI 客户端 bridge 的端口（用 JVM 参数 -Dbaritone.bridge.port 控制） */
  port: 5556,
  /**
   * 事件推送 WS 的端口（bridge 那侧用 -Dbaritone.ws.port 控制，默认 5559）。
   *
   * damage / chat 走这条长连接，不再轮询；连不上时自动回退到轮询，
   * 所以填错了也只是回到旧行为，不会失聪。
   */
  wsPort: 5559,
  /** 轮询间隔（秒）。事件驱动，所以可以短；3s 对聊天够用。 */
  tickSeconds: 3,
  /** 玩家账号名。用于「跟丢判断」和主动说话时点名。 */
  playerName: 'xlhl_world',
  /**
   * AI 自己的账号名。
   *
   * 两处用它：① **永远不投递自己说的话**（防自激：自己说一句 → 被投递 → agent 又被
   * 叫醒 → 又说一句，会转圈）；② 从 `get_entities` 的玩家列表里排除自己这个实体。
   */
  aiAccount: 'DeepSeek',
  /**
   * 只关注这些发送者说的话（白名单）。
   *
   * **留空 = 谁说话都投递**；填了就只放行名单里的人，其余（包括没有发送者的系统消息）
   * 一律丢弃。适合"只想听某个队友指挥"或"服里太吵只认一个人"的场景。
   *
   * ⚠️ AI 自己说的话无论名单怎么填都不会投递 —— 那是防自激的硬约束，不是配置项。
   */
  includeSenders: [],
  /** 单次 tick 最多投递几条，防刷屏。 */
  maxPerTick: 10,
  /** 找「最近有写入的会话」的根目录。留空 = <DSH_HOME>/sessions */
  sessionsRoot: '',
  /**
   * 固定把游戏消息投给哪个会话。留空 = 自动挑「最近有写入的那个」（会猜错）。
   * 填 session-xxxxxxxx-... 后就是零推测，游戏消息再也不会串到别的窗口。
   */
  sessionId: '',
  /** 状态与日志目录。留空 = <DSH_HOME>/mc-chat-watch */
  stateDir: '',
  /** 日志里标注的设备名。 */
  device: '【PC】',
  /** 单次 TCP 超时（毫秒）。 */
  timeoutMs: 4000,
  /**
   * 受伤回应：挨打时是否立刻叫醒 agent 让它反击/逃命。
   * 关掉后受伤只当普通事件攒进态势，不再单独唤醒。
   */
  hurtResponse: true,
  /**
   * 受伤后延迟几秒才叫醒（0 = 立即）。
   *
   * 为什么留这个：检测是 1 秒一轮、立刻唤醒，但"立刻"有时反而帮倒忙 ——
   * 被连击时 agent 刚开口就被打断。给一个可调的缓冲，自己权衡"反应快"与"别打断"。
   */
  hurtResponseDelaySeconds: 0,
  /**
   * 被**玩家**打时是否回击。
   *
   * 默认 true（保持原有行为：谁打我都还手）。在双人世界里把它关掉，
   * AI 就只在挨 NPC 打时反击，被玩家误伤时不会反过来打你。
   */
  retaliateOnPlayerHurt: true,
  /** 同一场战斗的唤醒节流（秒）—— 一场战斗只叫醒一次，避免反复打断。 */
  hurtWakeGapSeconds: 60,
};

/**
 * 环境伤害类型 → 中文名 + 精确处置。
 *
 * 键来自 bridge 的 `DamageSource.getMsgId()`（原版 MsgId，不带 `minecraft:`）。
 * 让 AI 直接知道"你溺水了 / 你在岩浆里"，比它自己去翻 air_supply / eyes_in_water
 * 猜一遍快得多，也省一轮工具调用 —— 这几秒常常就是生死线。
 */
const HAZARD_TYPES = {
  drown: ['溺水', '立刻上浮换气：往水面游，或挖开头顶那块方块'],
  lava: ['掉进岩浆', '立刻退离岩浆（别在原地打转）；有抗火药水就喝，没有就往最近的岸上跑'],
  on_fire: ['身上着火', '跳进水里或跑进雨里；没有水源就跑开远离火源'],
  in_fire: ['站在火焰里', '立刻退出火焰方块'],
  hot_floor: ['踩到岩浆块', '立刻离开岩浆块上方'],
  in_wall: ['卡在方块里窒息', '立刻往后退或往上，挖开卡住你的那块方块'],
  cramming: ['被 mob 挤住', '立刻走开，别卡在怪堆里'],
  starve: ['饿到掉血', '立刻吃东西'],
  fall: ['摔落', '先确认高度、别再往边上走；血低先吃东西回血'],
  fly_into_wall: ['鞘翅撞墙', '停止滑翔，先落地'],
  cactus: ['碰到仙人掌', '立刻离开仙人掌旁边'],
  sweet_berry_bush: ['走进甜浆果丛', '立刻走出浆果丛'],
  freeze: ['冰冻', '立刻离开细雪/冰面，靠近热源'],
  out_of_world: ['掉出世界', '基本已经救不回来了，准备重生'],
  generic: ['不明原因伤害', '看 get_state 的 health / last_damage_source 再判断'],
};

/** 把 `damage_type` 翻成「中文名 + 处置」。认不出来的类型原样展示。 */
function describeHazard(damageType) {
  const key = String(damageType || '').replace(/^minecraft:/i, '').trim().toLowerCase();
  if (!key) return null;
  const hit = HAZARD_TYPES[key];
  if (hit) return { key, zh: hit[0], fix: hit[1] };
  return { key, zh: `${key}（未收录的环境伤害）`, fix: '看 get_state 的 health / on_fire / air_supply 判断' };
}

/**
 * 把"逗号/空格/顿号分隔的字符串"或数组统一成去重后的字符串数组。
 *
 * 设置页的输入框只能给字符串，而配置里要的是数组 —— 归一化放在这里做一次，
 * 免得每个用它的地方各自猜格式。
 */
export function normalizeSenders(value) {
  const parts = Array.isArray(value)
    ? value
    : String(value == null ? '' : value).split(/[,，、;；\s]+/);
  const out = [];
  for (const part of parts) {
    const name = String(part == null ? '' : part).trim();
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

export function resolveConfig(input = {}) {
  const cfg = { ...DEFAULTS, ...input };
  cfg.port = Number(cfg.port) || DEFAULTS.port;
  cfg.wsPort = Number(cfg.wsPort) || DEFAULTS.wsPort;
  cfg.tickSeconds = Math.max(1, Number(cfg.tickSeconds) || DEFAULTS.tickSeconds);
  cfg.maxPerTick = Math.max(1, Number(cfg.maxPerTick) || DEFAULTS.maxPerTick);
  cfg.timeoutMs = Math.max(500, Number(cfg.timeoutMs) || DEFAULTS.timeoutMs);
  // 账号名。兼容早期的 watchPlayer 键（那时叫这个，老配置还能用）。
  // 注意不能写成 `cfg.playerName || input.watchPlayer`：cfg 已经把 DEFAULTS 摊进来了，
  // cfg.playerName 永远有值，旧键会被永远短路掉。
  const rawPlayer = input.playerName !== undefined
    ? input.playerName
    : (input.watchPlayer !== undefined ? input.watchPlayer : DEFAULTS.playerName);
  cfg.playerName = String(rawPlayer).trim() || DEFAULTS.playerName;
  cfg.aiAccount = String(cfg.aiAccount || DEFAULTS.aiAccount).trim() || DEFAULTS.aiAccount;
  // 仅关注名单：留空 = 全放行。字符串也认（设置页输入框给的就是字符串）。
  cfg.includeSenders = normalizeSenders(cfg.includeSenders);
  // 旧键由"忽略谁"改成"只关注谁"，语义是反的 —— 绝不能自动迁移，
  // 直接从配置里摘掉，免得它一直挂在设置文件里让人误以为还在生效。
  delete cfg.ignoreSenders;
  return cfg;
}

export function homeDir() {
  return process.env.DSH_HOME || join(homedir(), '.dsh');
}

function statePaths(cfg) {
  const dir = cfg.stateDir || join(homeDir(), 'mc-chat-watch');
  return { dir };
}

/**
 * 把非 ASCII 字符转成 `\uXXXX` 转义。
 *
 * 为什么必须这么做：bridge 的 Java 端按 JVM 默认编码（中文 Windows = GB18030）
 * 读 socket，而 Node 的 JSON.stringify 会**原样输出**中文字符 → UTF-8 字节被当
 * GB18030 解 → 游戏里显示乱码（实测低血量播报变成一串问号）。
 * Python 侧没这问题，是因为 json.dumps() 默认 ensure_ascii=True 已经替它转义了。
 * 转义后整包都是 ASCII，Java 的 JSON 解析器再还原回中文，两边都不会错。
 */
function asciiEscape(text) {
  return text.replace(/[^\x00-\x7F]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

/**
 * 跟 bridge 说一句话，拿回一条 JSON。
 * bridge 是按 JVM 默认编码（中文 Windows 上是 GB18030）写字的，
 * 所以解码先试 gb18030 再退 utf-8 —— 与 Python 侧 decode_bridge_bytes 同一套策略。
 */
export function bridgeCall(cfg, command, params = {}) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const sock = createConnection({ host: cfg.host, port: cfg.port }, () => {
      const payload = JSON.stringify({
        id: `mcw-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        request_seq: 1,
        command,
        params,
      });
      sock.write(asciiEscape(payload) + '\n');
    });
    let buf = '';
    // 🔴 2026-10-22 用户报「你的游戏内回复变成乱码了」→ 排查后发现**是插件这边解错**：
    //
    // · 原来写的是 `new TextDecoder('gbk').decode(chunk)`。bridge 历史上确实用 GBK
    //   （Java 的 `InputStreamReader` 没指定字符集，走中文 Windows 的平台默认），
    //   但 2026-10-22 已把 bridge 两侧钉死成 UTF-8（`ConnectionHandler` 的
    //   InputStreamReader / OutputStreamWriter 都显式传 StandardCharsets.UTF_8）。
    //   于是 UTF-8 字节被插件当 GBK 解 → 「编码修好了」变成「缂栫爜淇ソ浜」。
    //
    // · 第二个坑：`decode(chunk)` 默认 `{stream:false}`，把每个 TCP 分片当独立文本解。
    //   一个汉字 3 字节，一旦跨分片就两边都解坏 —— 必须开 `{stream:true}` 让解码器
    //   自己缓存半个字符。实例复用，别每次 new。
    const decoder = new TextDecoder('utf-8');
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      try {
        sock.destroy();
      } catch {
        /* ignore */
      }
      fn(arg);
    };
    sock.on('data', (chunk) => {
      buf += decoder.decode(chunk, { stream: true });
      const nl = buf.indexOf('\n');
      if (nl < 0) return;
      const line = buf.slice(0, nl).trim();
      if (!line) return;
      try {
        done(resolve, JSON.parse(line));
      } catch (error) {
        done(reject, error);
      }
    });
    sock.on('error', (error) => done(reject, error));
    sock.setTimeout(cfg.timeoutMs, () => done(reject, new Error('bridge 响应超时')));
  });
}

/** 找最近有写入的会话目录。 */
export function findLatestSessionId(sessionsRoot) {
  let best = '';
  let bestMs = -1;
  const walk = (dir, depth) => {
    if (depth > 4) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const full = join(dir, entry.name);
      if (entry.name.startsWith('session-')) {
        // 看目录内【文件】的 mtime —— 目录自身的 mtime 在改文件内容时不会更新，
        // 只看它会把消息投进几十分钟前的旧会话（踩过：串台）。
        let ms = 0;
        try {
          for (const f of readdirSync(full)) {
            try {
              const s = statSync(join(full, f));
              if (s.mtimeMs > ms) ms = s.mtimeMs;
            } catch {
              /* 单个文件读不到就跳过 */
            }
          }
        } catch {
          /* 目录读不到就跳过 */
        }
        if (ms > bestMs) {
          bestMs = ms;
          best = entry.name;
        }
      } else {
        walk(full, depth + 1);
      }
    }
  };
  walk(sessionsRoot, 0);
  return best;
}

/** 把一条文本投进当下活着的那个会话，把 agent 叫醒。 */
export function deliverToSession(ctx, sessionsRoot, text, pinnedSessionId = '') {
  const agents = ctx.get('agents');
  if (agents === undefined || typeof agents.get !== 'function') {
    return { ok: false, why: '拿不到 agents 服务' };
  }
  // 配了 pinnedSessionId 就认它（零推测）；没配才去猜「最近写入的那个」。
  // 必须有明确绑定的会话，否则**拒绝投递**。
  // 以前这里会退化成「找最近有写入的那个会话」，猜错就把游戏消息送进了别的窗口
  // （串台，2026-10-13 踩过）。宁可这条消息丢掉并记日志，也不要投错地方。
  const sessionId = String(pinnedSessionId || '').trim();
  if (sessionId === '') {
    return { ok: false, why: '未绑定会话（sessionId 为空），拒绝投递以免串台' };
  }
  let handle;
  try {
    handle = agents.get(sessionId);
  } catch (error) {
    return { ok: false, why: 'agents.get 抛错：' + String((error && error.message) || error) };
  }
  if (handle === undefined || handle === null) {
    return { ok: false, why: `会话 ${sessionId.slice(0, 20)}… 不在活跃列表（窗口没开？）` };
  }
  const agent = handle.agent ?? handle;
  // 插入式投递：`steer()` 把消息插到**当前轮的下一个 step**，而 `followup()` 是排到**下一轮**。
  // 排队语义下 agent 正跑长任务时，游戏里说的话要等它整轮做完才看得到 —— 时效性太差
  // （用户 2026-10-16 反馈）。两者都会唤醒（wakeup=true），所以 agent 闲着时一样好用。
  // 万一某个版本的句柄只有 followup，就退回排队，别把消息丢了。
  const mode =
    typeof agent.steer === 'function' ? 'steer'
      : typeof agent.followup === 'function' ? 'followup'
        : '';
  if (mode === '') return { ok: false, why: '这个句柄既没有 steer 也没有 followup 方法' };
  const message = {
    id: `mc-chat-${Date.now()}`,
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'user' },
  };
  try {
    agent[mode](message);
  } catch (error) {
    return { ok: false, why: `${mode} 抛错：` + String((error && error.message) || error) };
  }
  return { ok: true, sessionId, mode };
}

/**
 * 这个会话的 agent 现在是不是正在跑？（抓取不到就返回 false）
 *
 * 为什么要问：agent 正在跑长任务时**还有没有必要投** —— 即使改成插入式（`steer`），
 * 插进去的态势它也得回一句「等」，白烧 token 也刷屏；而"忙时攒着、只留最新一条"
 * 就能省掉这些无意义轮次（用户 2026-10-15 反馈）。
 * ⚠️ 这条背压**只用在态势/自主上，聊天不走它** —— 玩家说的话必须立刻插进去。
 *
 * `agent.status` 只有两个值，见 dsh-agent-loop 的 `get status()`：
 *   phase.kind 是 idle/maintenance → 'idle'，其余 → 'running'。
 * 这也是 DSH 自己的用法（dsh-agent 里 `lookup(sessionId)?.status === 'running'`）。
 *
 * 拿不到状态时返回 false —— 宁可多投一条，也不要因为探测失败把消息吞了。
 */
export function isAgentRunning(ctx, sessionId) {
  const id = String(sessionId || '').trim();
  if (id === '') return false;
  try {
    const agents = ctx.get('agents');
    if (agents === undefined || typeof agents.get !== 'function') return false;
    const handle = agents.get(id);
    if (handle === undefined || handle === null) return false;
    // handle 可能是 AgentHandle（status 挂在 agent 上），也可能自己带 status。
    return (handle.status ?? handle.agent?.status) === 'running';
  } catch {
    return false;
  }
}

/**
 * 从 bridge 事件里抽出「谁说了什么」。
 *
 * @param event          bridge 的 chat 事件（`{data:{sender,message}}`）
 * @param includeSenders 仅关注名单；**空数组 = 谁说话都放行**
 * @param selfName       AI 自己的账号名 —— 它说的话永远丢弃（见下方说明）
 * @returns {{sender:string,raw:string,text:string}|null} null = 这条不该投递
 */
export function parseChatEvent(event, includeSenders, selfName) {
  const data = event?.data || {};
  const sender = typeof data.sender === 'string' ? data.sender : '';
  const raw = typeof data.message === 'string' ? data.message : '';

  // ① AI 自己的话永远丢弃。这是**防自激的硬约束**，不开放配置：
  //    自己说一句 → 被投递 → agent 又被叫醒 → 又说一句，会无限转圈烧 token。
  //    不能靠默认值兜底 —— 用户手滑把名单填成自己就会转圈，所以在代码里挡住。
  if (sender && selfName && sender === selfName) return null;

  // ② 仅关注名单：留空 = 全放行；填了就只放行名单里的人。
  //    注意 sender 为空（系统消息/命令回显）且名单非空时也要挡掉 ——
  //    既然用户说了"只听这几个人"，那就别让无名消息混进来。
  const list = Array.isArray(includeSenders) ? includeSenders : [];
  if (list.length > 0 && (!sender || !list.includes(sender))) return null;

  // bridge 给的 message 是**整行**（形如 `<xlhl_world> 你好`）。
  // 有独立 sender 字段时剥掉前缀，没有就原样留着（宁可多带一点也别丢内容）。
  let text = raw;
  if (sender) {
    const prefix = `<${sender}> `;
    if (raw.startsWith(prefix)) text = raw.slice(prefix.length);
  }
  return { sender, raw, text };
}


/** 设置页与 Host 半的配对键：两半必须用同一个字符串。 */
export const SETTINGS_NS = 'mc-chat-watch';

/** 设置页读写走插件自己的 HTTP 前缀（不依赖 DSH 的 settings 服务，见 apply 里的说明）。 */
export const CONFIG_ROUTE = '/mc-chat-watch-config';

/**
 * 设置页的读写后端。
 *
 * 🔴 为什么不用 DSH 的 settings 服务：本机实测 `ctx.get('settings')` 返回的是**一份设置文档**
 * 对象（有 documentPath/describe/mutate 等），既没有 `installSection` 也没有 `register`，
 * 所以 createClient 侧 `configForms.get(NS)` 拿到的是**没有字段的空壳** —— 能挂载、能调
 * `set()`、但 `value`/`user` 里永远是 undefined（2026-10-22 用户点开关复现）。
 *
 * 于是改成和 `dsh-meme` 一样的做法：**插件自己开 HTTP 接口 + 自己落一份 settings.json**。
 * 优先级：settings.json（页面改的） > cordis config > 代码默认值。
 */
function settingsFilePath(cfg) {
  const { dir } = statePaths(cfg);
  return join(dir, 'settings.json');
}

function readSettingsFile(cfg) {
  try {
    const raw = readFileSync(settingsFilePath(cfg), 'utf8');
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function writeSettingsFile(cfg, patch) {
  const { dir } = statePaths(cfg);
  try { mkdirSync(dir, { recursive: true }); } catch { /* 已存在 */ }
  const current = readSettingsFile(cfg);
  const next = { ...current };
  for (const [key, value] of Object.entries(patch || {})) {
    // `__` 开头的是内部指令键（`__action` 之类），不是配置项 —— 绝不能落盘，
    // 否则它会一直躺在 settings.json 里被当成真配置读回来。
    if (key.startsWith('__')) continue;
    if (value === null || value === undefined) delete next[key];
    else next[key] = value;
  }
  writeFileSync(settingsFilePath(cfg), `${JSON.stringify(next, null, 2)}\n`, 'utf8');
  return next;
}

/** 只允许 loopback 访问（bridge 与设置接口都没有认证层）。 */
function denyNonLoopback(req, res) {
  try {
    const addr = req.socket?.remoteAddress || '';
    const ok = addr === '127.0.0.1' || addr === '::1' || addr === '::ffff:127.0.0.1' || addr === '';
    if (!ok) {
      res.statusCode = 403;
      res.end('loopback only');
      return true;
    }
  } catch { /* 判断不了就放行 */ }
  return false;
}

function sendJson(res, status, payload) {
  try {
    const body = JSON.stringify(payload);
    res.statusCode = status;
    res.setHeader('content-type', 'application/json; charset=utf-8');
    res.setHeader('cache-control', 'no-store');
    res.end(body);
  } catch {
    try { res.end('{}'); } catch { /* 忽略 */ }
  }
}

export function apply(ctx, input = {}) {
  // 优先级：settings.json（页面改的） > cordis config > 代码默认值。
  //
  // 🔴 2026-10-22 踩的坑：这里原本只写 `resolveConfig(input)`，于是**启动时根本不读
  // settings.json** —— 页面存的值只在当次 POST 里生效，一重启 DSH 就退回 cordis.yml
  // 里硬编码的旧值（用户实测"配置页改的 session-id 没生效"，根因就是这句）。
  // 现在两步走：先按 cordis 配置算出状态目录 → 再把 settings.json 叠上去。
  const bootCfg = resolveConfig(input);
  const bootSaved = readSettingsFile(bootCfg);
  const cfg = Object.keys(bootSaved).length
    ? resolveConfig({ ...input, ...bootSaved })
    : bootCfg;

  // ── 连接状态 + 手动重连（设置页「连接」组用）──────────────────────
  //
  // 用户要的：状态放到页面上，断了能自己点重连，不用重启 DSH。
  // 状态只反映「最近一次请求是不是通的」——它是观测值，不额外探测，
  // 所以页面看到的是真实结果，不是 ping 出来的假绿。
  const link = {
    ok: false,
    lastOkAt: 0,
    lastErrorAt: 0,
    lastError: '',
    lastReconnectAt: 0,
    reconnects: 0,
  };
  const noteLinkUp = () => {
    link.ok = true;
    link.lastOkAt = Date.now();
  };
  const noteLinkDown = (error) => {
    link.ok = false;
    link.lastErrorAt = Date.now();
    link.lastError = String((error && error.message) || error || 'unknown');
  };
  /** 由 apply 末尾赋上（游标等状态定义在后面，这里只留引用）。 */
  let resetCursorState = null;

  // ── MCP 连接（5556 那条链路的另一半）────────────────────────────────
  //
  // 别和上面的 `link` 混了：
  //   · `link`   = **插件自己**直连 bridge 5556 的结果；
  //   · `mcp`    = **DSH 的 MCP 客户端 → python wrapper → bridge 5556** 那条链路。
  // 两者共用同一个 bridge 端口，但 wrapper 崩了插件照样通 —— 所以要分开显示。
  //
  // 这里**只做状态展示，不提供重连**（2026-10-22 用户拍板）。原因：
  //   · wrapper 活着、只是 5556 断了 → 下一个**只读**命令（get_state 等）会自己
  //     `_reconnect`，轮不到人点；
  //   · wrapper 已被 DSH 放弃（`reconnect.maxAttempts=10` 用完，进程直接没了）
  //     → 没有进程可杀，唯一解法是重启 DSH。
  // 所以"重启 MCP"按钮只在极窄的中间态有用，却会制造"点一下就好"的假安全感。
  //
  // 探测手段只有一个可靠的：**找 python 进程里命令行含 mcp_stdio_wrapper 的那些**。
  // 用 `execFile` 异步跑（不要 execFileSync：那会阻塞事件循环、拖慢整个 HTTP 响应），
  // 而且只在缓存过期时刷新，GET 永远拿缓存值、不等待。
  const mcpProbe = {
    pids: [],
    checkedAt: 0,
    error: '',
  };
  const MCP_PROBE_TTL_MS = 15_000;
  let mcpProbing = false;

  const probeMcp = () => {
    if (mcpProbing) return;
    if (Date.now() - mcpProbe.checkedAt < MCP_PROBE_TTL_MS) return;
    mcpProbing = true;
    try {
      execFile(
        'powershell',
        [
          '-NoProfile',
          '-NonInteractive',
          '-Command',
          "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | " +
            "Where-Object { $_.CommandLine -match 'mcp_stdio_wrapper' } | " +
            'Select-Object -ExpandProperty ProcessId',
        ],
        { timeout: 10_000, windowsHide: true },
        (error, stdout) => {
          mcpProbing = false;
          mcpProbe.checkedAt = Date.now();
          if (error) {
            mcpProbe.pids = [];
            mcpProbe.error = String(error.message || error).slice(0, 120);
            return;
          }
          mcpProbe.error = '';
          mcpProbe.pids = String(stdout || '')
            .split(/\s+/)
            .map((s) => Number(s))
            .filter((n) => Number.isInteger(n) && n > 0);
        },
      );
    } catch (error) {
      mcpProbing = false;
      mcpProbe.checkedAt = Date.now();
      mcpProbe.error = String((error && error.message) || error).slice(0, 120);
    }
  };

  // ── 设置页的读写接口（HTTP，不依赖 DSH 的 settings 服务）─────────────────
  //
  // 为什么不用 DSH 的 settings 机制：本机实测（2026-10-22）ctx.get('settings') 拿到的
  // **是一份设置文档对象**（有 documentPath/describe/mutate），既没有 installSection
  // 也没有 register。于是 client 侧 configForms.get(NS) 拿到的是**没有字段的空壳**：
  // 页面能挂载、form.set() 也能调，但写完 value/user 里永远是 undefined
  // （用户点开关复现，控制台三行日志确认）。
  //
  // 所以改用和 dsh-meme 一样的做法：**插件自己开 HTTP 接口 + 自己落 settings.json**。
  // 优先级：settings.json（页面改的） > cordis config > 代码默认值。
  // ⚠️ webServer 也是**异步挂载**的：apply 早期 `ctx.get('webServer')` 会拿到 null
  // （2026-10-22 实测：日志打出"没有 webServer 服务"）。用 apply 内的 `ctx.inject`
  // —— 子作用域依赖，服务不来只是这个回调不执行，插件照常存活。
  ctx.inject(['webServer'], (webCtx) => {
    const webServer = (() => {
      try {
        if (webCtx && webCtx.webServer) return webCtx.webServer
        if (webCtx && typeof webCtx.get === 'function') return webCtx.get('webServer')
        return null
      } catch { return null }
    })()

    if (!webServer || typeof webServer.register !== 'function') {
      console.log('[mc-chat-watch] 拿不到 webServer，设置页将只能读不能写')
      return
    }

    if (true) {
    try {
      ctx.effect(() => webServer.register({
        kind: 'prefix',
        path: CONFIG_ROUTE,
        handler(req, res) {
          if (denyNonLoopback(req, res)) return
          try {
            const method = String(req.method || 'GET').toUpperCase()
            if (method === 'GET') {
              // 顺手刷新一次 MCP 探测（异步、不阻塞本次响应；命中缓存则什么都不做）
              probeMcp()
              sendJson(res, 200, {
                ok: true,
                config: cfg,
                overrides: readSettingsFile(cfg),
                link,
                mcp: {
                  running: mcpProbe.pids.length > 0,
                  pids: mcpProbe.pids,
                  checkedAt: mcpProbe.checkedAt,
                  error: mcpProbe.error,
                },
              })
              return
            }
            if (method !== 'POST') {
              sendJson(res, 405, { ok: false, error: 'only GET / POST' })
              return
            }
            let body = ''
            req.on('data', (chunk) => {
              body += chunk
              if (body.length > 65536) { try { req.destroy() } catch { /* 忽略 */ } }
            })
            req.on('end', async () => {
              try {
                const patch = JSON.parse(body || '{}')

                // 手动重连：清掉游标与节流状态，然后真连一次把结果带回页面。
                // 不清游标的话，MC 重启后 event_seq 从 0 重数，旧游标会把新事件全挡在门外
                // （2026-10-21 踩过：聊天全哑、受伤事件正常，极难辨认）。
                if (patch && patch.__action === 'reconnect') {
                  link.reconnects += 1;
                  link.lastReconnectAt = Date.now()
                  try {
                    if (typeof resetCursorState === 'function') resetCursorState()
                  } catch { /* 状态还没就绪就只做探测 */ }
                  try {
                    await bridgeCall(cfg, 'get_version', {})
                    noteLinkUp()
                  } catch (error) {
                    noteLinkDown(error)
                  }
                  sendJson(res, 200, {
                    ok: true,
                    config: cfg,
                    overrides: readSettingsFile(cfg),
                    link,
                  })
                  return
                }

                // MCP 那条链路**故意没有重连动作**：能自愈的（只读命令）会自己重连，
                // 救不回来的（wrapper 已被 DSH 放弃）只能重启 DSH。页面只展示状态。

                const saved = writeSettingsFile(cfg, patch)
                // 立即生效：**从原始 input 重建**（不能 `{...cfg, ...saved}` —— cfg 已被上一次
                // 写入改过，删掉的键会残留在里面，表现为"取消覆盖后值不恢复默认"）。
                Object.assign(cfg, resolveConfig({ ...input, ...saved }))
                // 🧹 页面成为"态势/自主"的唯一真源：清掉游戏内命令写的运行时覆盖文件
                try {
                  const { dir: stateDir } = statePaths(cfg)
                  for (const name of ['observe-switch.txt', 'autonomy-switch.txt', 'tuning.json']) {
                    const path = join(stateDir, name)
                    if (existsSync(path)) rmSync(path, { force: true })
                  }
                } catch { /* 删不掉就下次 tick 现读时仍按文件走 */ }
                sendJson(res, 200, {
                  ok: true,
                  config: cfg,
                  overrides: saved,
                  link,
                })
              } catch (error) {
                sendJson(res, 400, { ok: false, error: '写入失败: ' + String(error && error.message ? error.message : error) })
              }
            })
            req.on('error', () => { /* 客户端断了，不处理 */ })
            return
          } catch (error) {
            sendJson(res, 500, { ok: false, error: String(error && error.message ? error.message : error) })
          }
        },
      }), 'mc-chat-watch: settings route')
      console.log('[mc-chat-watch] 设置接口已注册: ' + CONFIG_ROUTE)
    } catch (error) {
      console.log('[mc-chat-watch] 设置接口注册失败: ' + String(error && error.message ? error.message : error))
    }
    }
  })

  const { dir } = statePaths(cfg);
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    /* 建不出来就只跳过日志 */
  }
  const logPath = join(dir, 'watch.log');
  const sessionsRoot = cfg.sessionsRoot || join(homeDir(), 'sessions');

  // ── 日志轮转（2026-10-22）────────────────────────────────────────────
  //
  // 原来只 append 不轮转，跑一天就 10.5 MB —— 大头是每次 observe 都把整包背包铺出来，
  // 一行 500+ 字符。三处一起改：
  //   C. **大小上限**：超过 8 MB 把 watch.log 轮转成 watch.prev.log（只留一份）；
  //   A. **bridge 长时间连不上**（MC 关了）：同样视为"这一轮结束"，轮转一次；
  //   D. 背包只打关键项（见 formatInventory）。
  // 留 .prev 而不是直接删：日志是排障的唯一线索，刚才"session 投不出去"就是靠它查出来的。
  const LOG_MAX_BYTES = 8 * 1024 * 1024;
  let logBytes = -1;              // -1 = 还没统计过，首次写入时现算
  let bridgeDownSince = 0;        // bridge 从什么时候开始连不上（0 = 正常）
  let bridgeDownLogged = false;   // 这轮断开是否已经报过 —— tick 每 3 秒一次，
                                  // 用时间窗判断会连报 2~3 条，改成布尔标志只报一次

  const rotateLog = (why) => {
    try {
      renameSync(logPath, `${logPath}.prev`);
      logBytes = 0;
      appendFileSync(logPath, `${new Date().toISOString()} [log] 已轮转（${why}），上一轮见 watch.log.prev\r\n`);
    } catch {
      /* 轮转失败就算了，不能因为日志炸掉插件 */
    }
  };

  /** bridge 连续异常超过 90 秒 → MC 大概已经关了，轮转一次并清空状态。 */
  const noteBridgeDown = () => {
    const now = Date.now();
    if (!bridgeDownSince) {
      bridgeDownSince = now;
      return;
    }
    if (now - bridgeDownSince > 90_000) {
      bridgeDownSince = 0;
      bridgeDownLogged = false;
      rotateLog('bridge 断开超过 90 秒（MC 客户端可能已关闭）');
    }
  };
  const noteBridgeUp = () => {
    bridgeDownSince = 0;
    bridgeDownLogged = false;
  };
  /** 断开时只在第一次记一条日志（tick 每 3 秒跑一次，用时间窗判断会连报好几条）。 */
  const shouldLogBridgeDown = () => {
    if (bridgeDownLogged) return false;
    bridgeDownLogged = true;
    return true;
  };

  const say = (line) => {
    try {
      const text = `${new Date().toISOString()} ${line}\r\n`;
      if (logBytes < 0) {
        try { logBytes = statSync(logPath).size; } catch { logBytes = 0; }
      }
      logBytes += Buffer.byteLength(text, 'utf8');
      if (logBytes > LOG_MAX_BYTES) {
        rotateLog(`超过 ${Math.round(LOG_MAX_BYTES / 1024 / 1024)}MB`);
      }
      appendFileSync(logPath, text);
    } catch {
      /* 日志写不了也不能炸 */
    }
  };


  /** null = 还没定起点（首轮先取 latest_seq，不拉历史消息）。 */
  let cursor = null;
  let busy = false;

  // ── 主动说话引擎的状态 ──────────────────────────────────────────────
  // 混合模式：急事（血少/天黑/饿）直接用模板播报，零 token 零延迟；
  // 能聊的（挖到宝贝/跟丢/久无对话）才唤醒 agent 组织语言。
  const P = {
    enabled: input.proactive !== false,
    seconds: Math.max(2, Number(input.proactiveSeconds) || 6),
    maxPerHour: Math.max(1, Number(input.maxPerHour) || 8),
    secondsPerHour: 3600_000,
    lowHealth: Number(input.lowHealthThreshold) || 6,
    lostDistance: Number(input.lostDistance) || 30,
    playerName: cfg.playerName,
    cooldowns: {
      lowHealth: 120_000,
      nightfall: 600_000,
      hungry: 300_000,
      valuable: 300_000,
      lostPlayer: 180_000,
      idleChat: 900_000,
    },
    valuableItems: Array.isArray(input.valuableItems) && input.valuableItems.length
      ? input.valuableItems
      : ['diamond', 'emerald', 'iron_ingot', 'gold_ingot', 'netherite_scrap', 'ancient_debris'],
  };

  const firedAt = {};        // 各类上次触发时刻
  const firedLog = [];       // 最近触发的时刻，用来算每小时上限
  let lastInventory = null;  // 上次背包计数快照
  let lastNight = null;      // 上次是否夜晚
  let lastHumanChatAt = Date.now(); // 玩家上次说话

  const canFire = (kind) => {
    const now = Date.now();
    while (firedLog.length && firedLog[0] < now - P.secondsPerHour) firedLog.shift();
    if (firedLog.length >= P.maxPerHour) return false;
    if (firedAt[kind] && now - firedAt[kind] < P.cooldowns[kind]) return false;
    return true;
  };
  const markFired = (kind) => {
    const now = Date.now();
    firedAt[kind] = now;
    firedLog.push(now);
  };
  const speakTemplate = async (kind, lines) => {
    const text = lines[Math.floor(Math.random() * lines.length)];
    try {
      await bridgeCall(cfg, 'chat', { message: text });
      markFired(kind);
      say(`[proactive] ${kind} → 模板播报「${text}」`);
      return true;
    } catch (error) {
      say(`[proactive] ${kind} 播报失败：${String((error && error.message) || error)}`);
      return false;
    }
  };
  const wakeAgent = (situation) => {
    const text =
      `（游戏内事件，需要你主动开口）${situation}\n` +
      `请用 mcp__baritone__say 工具在游戏里说一句自然、简短的话（别解释、别播报工具名）。`;
    const r = deliverToSession(ctx, sessionsRoot, text, currentSessionId());
    say(`[proactive] 唤醒 agent：${r.ok ? '已进会话' : `失败(${r.why})`}`);
    return r;
  };

  const TEMPLATES = {
    lowHealth: ['我快没血了！', '撑不住了，得躲一下…', '救命，血好少'],
    nightfall: ['天黑了，小心怪物', '晚上了，别乱跑啊', '天暗了，要回屋吗'],
    hungry: ['我饿了，得找点吃的', '肚子咕咕叫…'],
  };

  // 目标会话来源：只认配置 / 设置页里的 sessionId。
  // 曾经还有一条"游戏里 !waken 改会话、插件每 10 秒问一次 bridge"的路径
  // （syncSessionFromGame → get_waken_session），2026-10-22 用户拍板：
  // **唤醒会话已经统一搬到插件设置页，那条路毫无意义** —— 每 10 秒一次
  // bridge 往返、返回的永远是同一个值，纯浪费。已删除。
  //
  // ⚠️ 必须是**函数**而不是 const 快照：设置页改 sessionId 时只更新 cfg，
  // 早先写成 `const effectiveSessionId = ...` 会让它永远停在 apply 那一刻的值
  // （2026-10-22 用户实测"配置页改的 session-id 没生效"）。每次调用现读。
  const currentSessionId = () => String(cfg.sessionId || '').trim();

  /**
   * 总开关。
   *
   * 🔴 2026-10-22 用户报「我在设置页把总开关关掉了，为什么还是有提示」：
   * 原来只有 `tick`（聊天监听）检查 `cfg.enabled`，而 proactive / notable / observe /
   * autonomy 四个引擎各自只看自己的小开关 —— 于是「总开关」实际上只管聊天，
   * 态势播报和空闲自主照常在跑。总开关的语义必须是**一键全停**。
   */
  const masterOn = () => boolOf(cfg.enabled, true);

  // ── WS 事件通道（2026-10-22）────────────────────────────────────────
  //
  // 把 damage 和 chat 从轮询换成 bridge 的事件推送（`ws://host:5559`）：
  // 挨打从"最多 1 秒发现"变成"几乎立刻"，同时省掉一天十万次 get_events。
  //
  // ⚠️ 两个关键约束：
  // 1. **WS 事件里没有 seq**（`broadcastEvent` 只发 type/timestamp/data），所以无法
  //    和轮询结果去重 —— 只能二选一：WS 连通时暂停对应轮询、断开时恢复。
  // 2. **必须自己重连**。WS 断了不会有任何报错，表现只是"AI 完全不响应"，
  //    这比轮询失败更难发现。所以这里带指数退避重连，并且断开时立刻回退轮询。
  const WS_EVENTS = ['damage', 'chat'];
  const WS_RECONNECT_MIN_MS = 1000;
  const WS_RECONNECT_MAX_MS = 30_000;
  const wsLink = {
    state: 'off', // off | connecting | open | closed
    socket: null,
    timer: null,
    retries: 0,
    lastError: '',
    lastMsgAt: 0,
    connectedAt: 0,
  };
  const wsUp = () => wsLink.state === 'open';

  /**
   * 投递一条聊天事件 —— **WS 推送和轮询共用这一条路径**。
   * @param {{sender?:string,message?:string}} chatData bridge chat 事件的 data
   * @returns {boolean} 是否真的投递了一条
   */
  const deliverChat = (chatData) => {
    const parsed = parseChatEvent({ data: chatData }, cfg.includeSenders, cfg.aiAccount);
    if (!parsed || !parsed.text) return false;
    lastHumanChatAt = Date.now(); // 有人说话 → 闲聊计时重来
    const line = `（游戏内聊天）${parsed.sender || '某人'}：${parsed.text}`;
    const sent = deliverToSession(ctx, sessionsRoot, line, currentSessionId());
    say(
      `[fire] ${line} → 投递=${sent.ok ? `已进会话（${sent.mode}）${String(sent.sessionId).slice(0, 20)}…` : `失败(${sent.why})`}`,
    );
    return true;
  };

  const tick = async () => {
    if (!masterOn()) return;
    if (busy) return; // 上一轮还没回来就跳过，别堆积
    busy = true;
    try {
      const afterSeq = cursor === null ? 0 : cursor;
      const result = await bridgeCall(cfg, 'get_events', {
        after_seq: afterSeq,
        type: 'chat',
        limit: 50,
      });
      // 连上了 → 复位断线状态，下一次断开才会重新报一条（见 shouldLogBridgeDown）
      noteBridgeUp();
      noteLinkUp();
      const latest = Number(result?.data?.latest_seq ?? result?.latest_seq ?? 0);

      if (cursor === null) {
        cursor = latest;
        say(`[init] 起点 latest_seq=${cursor}（不拉历史）`);
        return;
      }

      const events = Array.isArray(result?.data?.events)
        ? result.data.events
        : Array.isArray(result?.events)
          ? result.events
          : [];

      // WS 正在推送聊天 → 轮询只推进游标，避免同一条消息被投递两次
      // （WS 事件里没有 seq，无法去重，所以只能二选一）。
      if (wsUp()) {
        cursor = latest;
        return;
      }

      let delivered = 0;
      for (const event of events) {
        if (delivered >= cfg.maxPerTick) break;
        if (deliverChat(event.data || {})) delivered += 1;
      }

      // 🔴 2026-10-16 实测：**bridge（MC 客户端）重启后 event_seq 会从头开始计数**，
      //    而插件的 cursor 还停在旧的大数值上 —— 此后 `latest > cursor` 永远为假，
      //    游标就此卡死，玩家在游戏里说的话再也读不到（当晚用户原话：
      //    「我发现我在聊天框输入文字他过不来了」）。证据：`[init] 起点 latest_seq=28694`
      //    之后 bridge 的 latest_seq 只有 3507，用户那条消息是 3503，全被这个游标挡在外面。
      //    判据：latest 比 cursor 小 → 序号回绕，把游标拉到新序号（并留一行日志，便于事后追溯）。
      if (latest < cursor) {
        say(`[tick] 序号回绕（cursor=${cursor} → latest=${latest}），重置游标`);
        cursor = latest;
      } else if (latest > cursor) {
        cursor = latest;
      }
    } catch (error) {
      // bridge 没起、MC 没开，都会走到这 —— 静默跳过，等下一轮。
      noteBridgeDown();
      noteLinkDown(error);
      // tick 每 3 秒一次，用时间窗判断会连报 2~3 条；改成布尔标志，只在
      // 从"正常"转"断开"的那一刻打一条，恢复时（noteBridgeUp）复位。
      if (shouldLogBridgeDown()) {
        say(`[tick] bridge 不可达：${String((error && error.message) || error)}（之后静默，恢复时会再报）`);
      }
    } finally {
      busy = false;
    }
  };

  say(
    `[apply] 聊天监听起来了：${cfg.host}:${cfg.port} 每 ${cfg.tickSeconds}s 一次；` +
      `只关注 ${cfg.includeSenders.length ? cfg.includeSenders.join('/') : '所有人'}（AI 自己说的始终忽略）`,
  );
  const stop = ctx.interval(tick, cfg.tickSeconds * 1000);

  // ── 主动说话引擎主循环 ──────────────────────────────────────────────
  let proBusy = false;
  const proactiveTick = async () => {
    if (!masterOn() || !P.enabled || proBusy) return;
    proBusy = true;
    try {
      const s = await getStateCached();
      const health = Number(s.health ?? 20);
      const food = Number(s.food_level ?? 20);
      const pos = s.position || {};

      // ① 血少 → 模板，立刻说
      if (health > 0 && health <= P.lowHealth && canFire('lowHealth')) {
        await speakTemplate('lowHealth', TEMPLATES.lowHealth);
      }

      // ② 天黑 —— 只在白天→夜晚那一次转换上触发，不是整夜循环
      const wt = Number(s.world_time ?? 0);
      const isNight = wt % 24000 >= 13000 && wt % 24000 < 23000;
      if (lastNight !== null && isNight && !lastNight && canFire('nightfall')) {
        await speakTemplate('nightfall', TEMPLATES.nightfall);
      }
      lastNight = isNight;

      // ③ 饿 → 模板
      if (food >= 0 && food <= 6 && canFire('hungry')) {
        await speakTemplate('hungry', TEMPLATES.hungry);
      }

      // ④ 背包新增贵重物 → 唤醒 agent 组织语言
      try {
        const inv = await bridgeCall(cfg, 'get_inventory', {});
        const counts = {};
        for (const it of (inv && inv.data && inv.data.inventory) || []) {
          if (!it.count) continue;
          const id = String(it.id || '').replace('minecraft:', '');
          counts[id] = (counts[id] || 0) + it.count;
        }
        if (lastInventory) {
          for (const id of P.valuableItems) {
            const gained = (counts[id] || 0) - (lastInventory[id] || 0);
            if (gained > 0 && canFire('valuable')) {
              const zh =
                {
                  diamond: '钻石',
                  emerald: '绿宝石',
                  iron_ingot: '铁锭',
                  gold_ingot: '金锭',
                  netherite_scrap: '下界合金碎片',
                  ancient_debris: '远古残骸',
                }[id] || id;
              markFired('valuable');
              wakeAgent(`你刚挖到 ${zh} x${gained}。`);
              break;
            }
          }
        }
        lastInventory = counts;
      } catch (error) {
        say(`[proactive] 查背包失败：${String((error && error.message) || error)}`);
      }

      // ⑤ 跟丢 → 唤醒 agent
      if (canFire('lostPlayer')) {
        try {
          const ents = await bridgeCall(cfg, 'get_entities', { radius: 64 });
          const list = (ents && ents.data && ents.data.entities) || [];
          // 实测结构：{ type:"minecraft:player", name, distance, position:{x,y,z} }
          //   · name 是显示名，不一定是账号名
          //   · bridge 已经给了 distance，优先用
          //   · 坐标在 position 子对象里 —— 第一版我读 e.x/e.z 得到 undefined（当 0 用），
          //     于是人贴脸上也算出四五十格，一直误报跟丢。
          const other = list.find((e) => {
            const t = String(e.type || e.entity_type || '');
            if (!t.includes('player')) return false;
            return String(e.name || '') !== cfg.aiAccount; // 排除自己
          });
          if (other) {
            let dist = Number(other.distance);
            if (!Number.isFinite(dist)) {
              const o = other.position || {};
              const dx = Number(o.x ?? 0) - Number(pos.x ?? 0);
              const dz = Number(o.z ?? 0) - Number(pos.z ?? 0);
              dist = Math.hypot(dx, dz);
            }
            if (dist > P.lostDistance) {
              markFired('lostPlayer');
              wakeAgent(
                `玩家 ${String(other.name || P.playerName)} 离你约 ${Math.round(dist)} 格，你可能跟丢了。`,
              );
            }
          }
        } catch {
          /* get_entities 不可用就跳过这一条 */
        }
      }

      // ⑥ 久无对话 → 唤醒 agent 闲聊
      if (Date.now() - lastHumanChatAt > P.cooldowns.idleChat && canFire('idleChat')) {
        markFired('idleChat');
        wakeAgent('已经很久没人说话了。');
      }
    } catch (error) {
      say(`[proactive] 出错：${String((error && error.message) || error)}`);
    } finally {
      proBusy = false;
    }
  };

  if (P.enabled) {
    const stopPro = ctx.interval(proactiveTick, P.seconds * 1000);
    ctx.effect(() => () => {
      if (typeof stopPro === 'function') stopPro();
    });
    say(
      `[apply] 主动说话引擎开了：每 ${P.seconds}s 查一次，上限 ${P.maxPerHour} 次/小时；` +
        `血<=${P.lowHealth}/天黑/饿 → 模板播报；挖到宝贝/跟丢/久无话 → 唤醒 agent`,
    );
  } else {
    say('[apply] 主动说话引擎关了（proactive: false）');
  }

  // ── 观察模式：把游戏态势交给 agent，由它自己判断要不要动 ──────────
  // 和 proactive 的分工：proactive 是「按写死的规则替你决定要不要开口」；
  // 这里是把态势原样交出去，由 agent 判断。代价是每交一次就跑一轮 LLM。
  // ⚠️ 全部字段都是 **getter**，每次访问现读 `cfg` —— 否则设置页改了也不会生效。
  // 这里踩过：原来写 `input.observe === true`（读的是 apply 时的 cordis config），
  // 于是页面上改「态势播报」开关完全没反应（2026-10-22 用户实测"开不回去"）。
  const boolOf = (value, fallback) => (value === undefined ? fallback : value === true || value === 'true');
  const O = {
    get enabled() { return boolOf(cfg.observe, true); },
    get seconds() { return Math.max(3, Number(cfg.observeSeconds) || 180); },
    // changed = 态势真的变了才叫醒（站着不动时不烧钱）
    // always  = 每轮都交（响应最全，代价最大）
    get mode() { return cfg.observeMode === 'always' ? 'always' : 'changed'; },
    get minWakeSeconds() { return Math.max(3, Number(cfg.minWakeSeconds) || 30); },
    get maxWakePerHour() { return Math.max(1, Number(cfg.maxWakePerHour) || 40); },
  };

  let lastSnapshot = '';
  let lastWakeAt = 0;
  let lastUrgentWakeAt = 0;
  // urgent 事件的唤醒节流（毫秒）——**一场战斗只叫醒 agent 一次**。
  // 起因（2026-10-16 用户原话「第二次的间隔太短了，连续多次插入会话导致你的响应反而变慢了」）：
  // 原来 8 秒。被连击时每 8 秒就 steer 一条"🔴 受伤"进会话，**把 agent 正在进行的回复反复打断**，
  // 结果是"叫得越勤、反应反而越慢"。僵尸连咬 6 下一场也就 10 秒左右 —— 60 秒足够覆盖一整场
  // 战斗，打完 agent 还醒着；60 秒后还在挨打才会再叫第二次。
  const observeLog = [];
  let obsBusy = false;

  // ── 值得一提的低频事件（死亡/受伤/遭攻击/天气/维度）───────────────
  // 为什么不让观察周期自己去拉：事件缓冲 TTL 只有 300 秒、上限 2048 条，
  // 高频事件滚动很快，180 秒才拉一次容易漏。这里 **1 秒一轮**，且 urgent 类型
  // **每一轮都查**，既稳稳落在 TTL 内，又是秒级反应。
  //
  // ⚠ 必须走游标模式（带 after_seq）。bridge 的 get_events 不带游标时走
  //   pollEvents()，那里有 iterator.remove()，会把事件从缓冲区**抽走** ——
  //   连聊天事件都会被抢掉。带游标才是纯读。
  // 第三列 = urgent：**立刻唤醒 agent**，不塞进 pendingNotable 等态势周期攒批。
  // 起因（2026-10-16，用户发现「怎么没见你攻击呢」）：AI 下矿连死 5 次，因为"挨打"只当
  // 普通事件攒着发，等态势播报把它叫醒时人已经死了 —— 请求-响应模式下没人叫它，它根本
  // 不知道自己正在被打，自然不会还手。
  //
  // ⚠️ urgent **只留 DAMAGE 一个**（用户原话：「没必要检测攻击和死亡，单检测一个被攻击就行」）：
  //    · ENTITY_ATTACK 的语义是**"我出手打人"**（`if (player == Minecraft.getInstance().player)`），
  //      自己做了什么自己清楚、不需要被叫醒；原来还把文案写成「⚔遭攻击」，方向是反的、纯噪音。
  //    · DEATH 是"被伤害"的终点 —— 前面那几条 DAMAGE 早就报过了，再唤醒一次没有新增信息。
  const NOTABLE = [
    ['DEATH', (d) => `☠死了(${Math.round(Number(d.x))},${Math.round(Number(d.y))},${Math.round(Number(d.z))})`],
    ['RESPAWN', () => '↻已重生'],
    [
      'DAMAGE',
      (d) =>
        `⚠受伤-${Math.round(Number(d.amount) || 0)}` +
        (d.attacker_type ? `(${String(d.attacker_type).replace('minecraft:', '')})` : ''),
      true,
    ],
    ['ENTITY_ATTACK', (d) => `⚔出手攻击了${String(d.entity_type || '目标').replace('minecraft:', '')}`],
    ['WEATHER_CHANGE', (d) => `天气:${d.weather_type || '?'}`],
    ['DIMENSION_CHANGE', () => '⇒换维度'],
  ];
  const notableCursors = new Map();
  let notableSeeded = false;
  let notableIdx = 0;
  const pendingNotable = [];
  /**
   * 往待播队列里塞一条，重复内容不重复塞。
   *
   * 2026-10-22 实测踩到：持续下雨时 bridge 一直发 `weather_change`，每条都被 render 成
   * `天气:rain` 塞进队列，最后态势行里出现「天气:rain 天气:rain 天气:rain ×7」。
   * 同类还有"悬浮/维度"这种状态型事件 —— 状态没变就没必要重复播。
   */
  const pushPending = (line) => {
    if (pendingNotable.includes(line)) return;
    pendingNotable.push(line);
    while (pendingNotable.length > 6) pendingNotable.shift();
  };
  // urgent 类型（死亡/受伤/遭攻击）**每一轮都查**；其余（天气/维度/重生）轮着查。
  // 起因（2026-10-16 用户原话「这个时间延迟有点太长了吧」）：原来单类型轮询，6 个类型
  // 轮一圈最坏要等 18 秒才轮到 damage —— 挨打要秒级反应，不能排队。
  const URGENT_TYPES = NOTABLE.filter(([, , u]) => u === true).map(([t]) => String(t).toLowerCase());
  const REST_TYPES = NOTABLE.filter(([, , u]) => u !== true).map(([t]) => String(t).toLowerCase());

  /**
   * 处理一条 DAMAGE 事件 —— **轮询和 WS 推送共用这一条路径**。
   *
   * 抽出来的原因（2026-10-22 把 damage 换成 WS 推送时）：两种来源必须走完全相同的
   * 判断链（受伤总开关 → 攻击者分流 → 节流 → 延迟），否则会出现"推送时一套规则、
   * 断线回退轮询时另一套"的双标准 —— 这种不一致最难查。
   */
  const handleDamageEvent = (evData) => {
    const line = `⚠受伤-${Math.round(Number(evData.amount) || 0)}` +
      (evData.attacker_type ? `(${String(evData.attacker_type).replace('minecraft:', '')})` : '');

    // ① 受伤回应总开关：关掉就只当普通事件攒着，不再单独叫醒
    if (!boolOf(cfg.hurtResponse, true)) {
      pushPending(line);
      return;
    }

    // ② 攻击者是谁 → 决定要不要还手
    //
    // DAMAGE 事件**自带 attacker_name**（bridge 的 addDamageEntity 会填，
    // 玩家是账号名、生物是显示名），所以这里不用再回查 get_entities。
    //
    // 分流规则（2026-10-22 用户定）：
    //   · 攻击者 == 配置的 playerName  → 看 retaliateOnPlayerHurt 开关
    //   · 攻击者是**别的玩家**或怪物   → **一律回击**（不用问）
    //   · **根本没有攻击者**（attacker_type 为空）→ **环境伤害**：
    //     溺水 / 摔落 / 岩浆 / 饥饿 / 窒息都没有"谁打的我"，
    //     这时提"反击"是纯误导 —— 附近可能连个生物都没有，AI 却会去找怪挥刀，
    //     白白错过上浮、吃东西、后退的黄金时间。这一路把全部精力放在脱困上。
    const attackerType = String(evData.attacker_type || '').toLowerCase();
    const hasAttacker = attackerType !== '';
    const byPlayer = attackerType.includes('player');
    const attackerName = String(evData.attacker_name || '');
    const isKnownPlayer = byPlayer && attackerName !== '' && attackerName === cfg.playerName;

    let advice;
    if (!hasAttacker) {
      // 环境伤害：bridge 的 damage_type 已经告诉我们具体是哪种，直接照方抓药。
      const hazard = describeHazard(evData.damage_type);
      advice = hazard
        ? `这是**${hazard.zh}** —— ${hazard.fix}。当前是环境伤害，需要尽快脱困。`
        : `这是**环境伤害**（没有攻击者），需要尽快脱困。`
          + `看 air_supply / eyes_in_water / fall_distance / on_fire / health，`
          + `该上浮上浮、该吃就吃、该退就退。`;
    } else if (isKnownPlayer && !boolOf(cfg.retaliateOnPlayerHurt, true)) {
      advice = `打你的是 ${attackerName || cfg.playerName}（配置里的玩家）—— `
        + `按配置这一轮走"脱离"路线：拉开距离、跑开、或者躲到安全处。`;
    } else {
      const who = byPlayer ? (attackerName || '玩家') : '';
      advice = `血低就先拉开距离保命，有武器就 attack_nearest 反击。`
        + (byPlayer ? `（打你的是${who}；针对玩家用 attack_nearest(attack_players=true) ）` : '');
    }

    // ③ 一场战斗的唤醒节流（秒）：连着挨打只叫一次，避免反复打断回复
    const gapMs = Math.max(0, Number(cfg.hurtWakeGapSeconds) || 0) * 1000;
    if (Date.now() - lastUrgentWakeAt < gapMs) {
      pushPending(line);
      return;
    }
    lastUrgentWakeAt = Date.now();

    // ④ 响应延迟（秒）：0 = 立即；>0 则等一下再叫，给连击留出合并窗口
    const delayMs = Math.max(0, Number(cfg.hurtResponseDelaySeconds) || 0) * 1000;
    // 环境伤害已经知道是哪一种了，不用再让它去"排查"
    const look = hasAttacker ? '立刻用 get_combat_snapshot 看附近威胁' : '立刻照下面的处置做';
    const fire = () => {
      say(`[urgent] ${line} → 已交 agent${delayMs ? `（延迟 ${delayMs / 1000}s）` : '（立即）'}`);
      wakeAgent(`🔴 ${line} —— ${look}：${advice}`);
    };
    if (delayMs > 0) {
      const timer = setTimeout(fire, delayMs);
      if (typeof timer === 'object' && timer && typeof timer.unref === 'function') timer.unref();
    } else {
      fire();
    }
  };

  const notableTick = async () => {
    if (!masterOn() || !O.enabled) return;
    try {
      // 首轮只探测"当前末尾"，避免把开启之前的陈年事件倒出来
      if (!notableSeeded) {
        const probe = await bridgeCall(cfg, 'get_events', { after_seq: 0, limit: 1 });
        // bridge 的响应是无包装的（直接 { latest_seq, events, ... }），但历史上也见过带 data
        // 包装的形式 —— 两种都认，别再想当然。
        const latest = Number((probe && (probe.data?.latest_seq ?? probe.latest_seq)) ?? 0);
        // ⚠️ 游标的键统一用**小写**（原因见 notableTick 里的说明）
        for (const [t] of NOTABLE) notableCursors.set(String(t).toLowerCase(), latest);
        notableSeeded = true;
        return;
      }
      // 每 3 秒一轮：urgent 类型全查一遍（秒级反应），非 urgent 才轮着来。
      const batch = [...URGENT_TYPES];
      if (REST_TYPES.length) {
        batch.push(REST_TYPES[notableIdx % REST_TYPES.length]);
        notableIdx += 1;
      }
      for (const key of batch) {
        const entry = NOTABLE.find(([t]) => String(t).toLowerCase() === key);
        if (!entry) continue;
        const [type, render, urgent] = entry;
        const cursor = notableCursors.get(key) ?? 0;
        // 🔴 2026-10-16 实测：EventManager.getEventsAfter 用的是 `type.equals(event.getType())`
        //    —— **区分大小写**，而 bridge 的事件名一律小写（publishEvent 里 name().toLowerCase()）。
        //    表里的键写成 'DAMAGE'/'DEATH' 就永远匹配不上，notableTick 一条事件都读不到。
        //    实测对照：type=damage ✅ 有事件、type=DAMAGE ❌ 空。所以这里一律转小写。
        const r = await bridgeCall(cfg, 'get_events', { after_seq: cursor, type: key, limit: 5 });
        // WS 在推送 damage 时，这里只推进游标、不重复处理（原因见 WS 段的说明）。
        // 断开后 WS 停止推送，这一支自然失效、轮询接管。
        if (wsUp() && key === 'damage') {
          const d0 = (r && (r.data || r)) || {};
          notableCursors.set(key, Number(d0.latest_seq ?? cursor));
          continue;
        }
        // 🔴 2026-10-16 实测：bridge 的响应**没有 data 包装**（直接 { events, latest_seq, ... }）。
        // 这里原来只认 r.data.events，于是 events 永远是空数组 —— notableTick 自上线起就一直
        // 在空转：死亡/受伤/遭攻击一条都没唤醒过 agent（用户原话「怎么没见你攻击呢」）。
        // 照 tick() 的写法两种都认。
        const data = (r && (r.data || r)) || {};
        const events = Array.isArray(data.events) ? data.events : [];
        if (events.length) {
          // 有事件 → 进入"战斗窗口"，接下来 30 秒按 1 秒一轮盯紧
          markCombat();
          say(`[notable] ${type} ×${events.length}`);
        }
        for (const ev of events) {
          const evData = ev.data || {};
          // damage 走共用路径（WS 推送时也调它）；其余类型一律攒进待播队列
          // —— 表里第三列 urgent 只有 DAMAGE 一个，所以这里可以直接分流。
          if (key === 'damage') {
            handleDamageEvent(evData);
          } else {
            pushPending(render(evData));
          }
        }
        notableCursors.set(key, Number(data.latest_seq ?? cursor));
      }
    } catch {
      /* bridge 不在就下一轮再说 */
    }
  };

  // ── notableTick 的自适应节奏（2026-10-22 优化）────────────────────
  //
  // 原来无条件 1 秒一轮 → 一天 86,400 次 get_events，绝大多数是空转。
  // 但又不能简单降频：僵尸连咬 6 下才 10 秒，3 秒延迟就够挨两下了。
  //
  // 折中：**平时 5 秒，检测到事件的 30 秒内切回 1 秒**（"战斗窗口"）。
  //   · 挨打时永远 1 秒内发现 —— 响应不变慢；
  //   · 平时请求量降到约 1/5；
  //   · interval 本身仍是 1 秒粒度，只是回调内部按 due 跳过，不产生 bridge 请求。
  const NOTABLE_IDLE_MS = 5000;
  const NOTABLE_COMBAT_MS = 1000;
  const COMBAT_WINDOW_MS = 30_000;
  let combatUntil = 0;
  let lastNotableRun = 0;
  /** 任何"有料"的检测都算进战斗窗口，接下来 30 秒盯紧一点。 */
  const markCombat = () => { combatUntil = Date.now() + COMBAT_WINDOW_MS; };

  {
    // 起因（2026-10-16 用户原话「延迟还是有点高」）：3 秒一轮仍嫌钝，所以战斗窗口内保 1 秒。
    // 一场战斗只会叫醒 agent 一次（hurtWakeGapSeconds 兜住），压低平时轮询不会漏事件。
    const stopNotable = ctx.interval(() => {
      const now = Date.now();
      const due = now < combatUntil ? NOTABLE_COMBAT_MS : NOTABLE_IDLE_MS;
      if (now - lastNotableRun < due) return;
      lastNotableRun = now;
      notableTick();
    }, 1000);
    ctx.effect(() => () => {
      if (typeof stopNotable === 'function') stopNotable();
    });
  }

  // ── get_state 短缓存（2026-10-22 优化）──────────────────────────────
  //
  // observe / autonomy / proactive 三个引擎各自会拉一次 get_state，内容几乎相同。
  // 这里给 1.5 秒的共享窗口：同一时刻三个引擎都在跑时只发一次请求。
  // TTL 故意做得比所有引擎的周期都短 —— 不影响任何"现读"语义，只吃掉重复。
  const STATE_CACHE_TTL_MS = 1500;
  let stateCacheAt = 0;
  let stateCacheValue = null;
  const getStateCached = async () => {
    const now = Date.now();
    if (stateCacheValue && now - stateCacheAt < STATE_CACHE_TTL_MS) return stateCacheValue;
    const st = await bridgeCall(cfg, 'get_state', {});
    stateCacheValue = (st && st.data) || {};
    stateCacheAt = now;
    return stateCacheValue;
  };

  /**
   * 背包压成一行：只列关键物资，其余归并计数。
   *
   * 原来是把 30+ 项全铺出来（`... cobblestonex64 andesitex15 cobblestonex64 gravelx1 ...`），
   * 一行 500+ 字符，日志跟着涨到 10MB。真正要看的只有武器/食物/火把/少量方块，
   * 其余对判断毫无价值。
   */
  const formatInventory = (inventory) => {
    const items = (inventory || []).filter((i) => Number(i.count) > 0);
    if (!items.length) return '空';
    const KEY = [
      /_sword$/, /_axe$/, /_pickaxe$/, /_shovel$/, /_hoe$/,
      /^bow$/, /^arrow$/, /^shield$/, /helmet$/, /chestplate$/, /leggings$/, /boots$/,
      /^torch$/, /bread$/, /beef$/, /porkchop$/, /chicken$/, /mutton$/, /cod$/, /salmon$/,
      /^apple$/, /^carrot$/, /^potato$/, /melon_slice$/,
      /bucket$/, /crafting_table$/, /^furnace$/,
    ];
    const norm = (id) => String(id || '').replace('minecraft:', '');
    const key = [];
    const rest = new Map();
    for (const i of items) {
      const name = norm(i.id);
      const count = Number(i.count) || 0;
      if (KEY.some((re) => re.test(name))) key.push(`${name}x${count}`);
      else rest.set(name, (rest.get(name) || 0) + count);
    }
    // 剩下的按数量取前三（圆石/泥土这类量大但没信息量的，也只留符号）
    const top = [...rest.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
    const parts = [...key, ...top.map(([n, c]) => `${n}x${c}`)];
    const more = rest.size - top.length;
    if (more > 0) parts.push(`+${more}种`);
    return parts.join(' ') || '空';
  };

  /** 把当前局面压成一行紧凑文本（越短越省 token）。 */
  const snapshotGame = async () => {
    const s = await getStateCached();
    // 数据完整性校验。bridge 在玩家未加载 / 刚重生 / 连接抖动的瞬间会返回一堆
    // 默认值——位置(0,0,0)、血0、饿 NaN。照单全收发出去只会误导判断（用户原话：
    // 「找不到就不发，降级处理容易串台」），所以这里直接抛错，让本轮整轮跳过。
    const pos = s.position;
    const health = Number(s.health);
    const food = Number(s.food_level);
    const posOk =
      pos &&
      Number.isFinite(Number(pos.x)) &&
      Number.isFinite(Number(pos.y)) &&
      Number.isFinite(Number(pos.z)) &&
      !(Number(pos.x) === 0 && Number(pos.y) === 0 && Number(pos.z) === 0);
    if (!posOk || !Number.isFinite(health) || !Number.isFinite(food)) {
      throw new Error(
        `get_state 数据不完整，跳过本轮（health=${s.health} food=${s.food_level} pos=${JSON.stringify(pos)}）`,
      );
    }
    const wt = Number(s.world_time ?? 0) % 24000;
    const bits = [
      `位置(${Math.round(Number(pos.x))},${Math.round(Number(pos.y))},${Math.round(Number(pos.z))})`,
      `血${Math.round(health)}/20`,
      `饿${food}`,
      wt >= 13000 && wt < 23000 ? '夜晚' : '白天',
      Number(s.is_pathing) ? '寻路中' : '待命',
    ];
    try {
      const inv = await bridgeCall(cfg, 'get_inventory', {});
      bits.push(`背包[${formatInventory(((inv && inv.data && inv.data.inventory) || []))}]`);
    } catch {
      /* 查不到就略过 */
    }
    try {
      const ents = await bridgeCall(cfg, 'get_entities', { radius: 32 });
      const list = (ents && ents.data && ents.data.entities) || [];
      const players = list.filter(
        (e) => String(e.type || '').includes('player') && String(e.name || '') !== cfg.aiAccount,
      );
      // 只报「会动、可能有威胁」的活物，把鱼/蝙蝠这类噪音滤掉
      const NOISE = ['cod', 'salmon', 'tropical_fish', 'bat', 'item', 'experience_orb'];
      const mobs = list.filter((e) => {
        const t = String(e.type || '');
        if (!e.is_living || t.includes('player')) return false;
        return !NOISE.some((n) => t.includes(n));
      });
      if (players.length) {
        bits.push(
          `玩家:${players.map((p) => `${p.name}(${Math.round(Number(p.distance) || 0)}格)`).join(',')}`,
        );
      }
      if (mobs.length) {
        bits.push(
          `活物:${mobs
            .slice(0, 4)
            .map((m) => `${String(m.type || '').replace('minecraft:', '')}(${Math.round(Number(m.distance) || 0)}格)`)
            .join(',')}`,
        );
      }
    } catch {
      /* 略 */
    }
    if (pendingNotable.length) {
      bits.push(pendingNotable.splice(0).join(' '));
    }
    return bits.join(' ');
  };

  // 会话正忙时把态势攒在这里，只留最新一条（见 isAgentRunning 的注释）。
  let pendingObserve = null;

  // 采样间隔交给运行时参数（「态势 间隔 <秒>」）：interval 用固定粒度轮询，
  // 到点了才真的采一次 —— 这样改间隔立即生效，不必重建 interval。
  const OBSERVE_POLL_MS = 5000;
  let lastObserveAt = 0;

  const observeTick = async () => {
    // 配置只是默认值，游戏内「态势 关/开」随时覆盖（关着时这里只读一个小文件就返回）。
    if (!masterOn() || !O.enabled || obsBusy) return;
    const startedAt = Date.now();
    if (startedAt - lastObserveAt < O.seconds * 1000) return;
    lastObserveAt = startedAt;
    obsBusy = true;
    try {
      let snap;
      try {
        snap = await snapshotGame();
      } catch {
        return; // MC 没开 / bridge 断了，静默跳过
      }
      const changed = snap !== lastSnapshot;
      lastSnapshot = snap;
      if (O.mode === 'changed' && !changed) return;

      const now = Date.now();
      while (observeLog.length && observeLog[0] < now - 3600_000) observeLog.shift();
      if (observeLog.length >= O.maxWakePerHour) return;
      if (now - lastWakeAt < O.minWakeSeconds * 1000) return;

      // 会话正忙：不排队、不唤醒，只把最新态势攒着（覆盖旧的）。
      // 忙的时候投递不解决问题，只会在它跑完之后堆出一串「等」。
      if (isAgentRunning(ctx, currentSessionId())) {
        pendingObserve = { snap, count: (pendingObserve ? pendingObserve.count : 0) + 1 };
        lastWakeAt = now; // 占住节奏位，免得 agent 一闲下来就被补投一串
        say(`[observe] agent 正忙，暂存态势（累计 ${pendingObserve.count} 次）`);
        return;
      }

      // 用户要求（2026-10-22）：「态势只保留最新的一版，不用把历史几次一并汇总」
      // —— 忙的时候攒下的旧态势直接丢掉，绝不拼进消息里。
      pendingObserve = null;
      lastWakeAt = now;
      observeLog.push(now);

      const r = deliverToSession(
        ctx,
        sessionsRoot,
        `（游戏态势）${snap}\n` +
          `需要操作就直接调工具去做（说话 mcp__baritone__say；血/饿低就吃东西 mcp__baritone__eat；` +
          `跟随 mcp__baritone__follow_player；挖矿 mcp__baritone__mine_quantity）；` +
          `不需要操作就只回一个「等」字，别多说。\n` +
          // 这几条**故意贴在消息里**，而不是只写进手册/skill：手册要它主动去读，
          // 而这些是"每轮都得照做、忘了就出事"的硬约束（2026-10-16 与用户定的 A/B 方案）。
          `铁律：① 不需要动作就只回「等」，别解释；② 任何一步失败后**先重新查一次现状**` +
          `（get_inventory / get_state / get_player_pos），绝不沿用旧假设继续往下跑；` +
          `③ 用户没喊「跟上」就别 follow_player；④ 出手前先看 snapshot 的 is_pathing，` +
          `有残留任务先 cancel_command；⑤ 说话一次一句。\n` +
          `（判断表与工具速查：skill mc-situation，或工作区 游戏态势应对手册.md）`,
        currentSessionId(),
      );
      say(`[observe] ${snap} → ${r.ok ? '已交 agent' : `失败(${r.why})`}`);
    } catch (error) {
      // 同 autonomyTick：定时器回调必须自己兜住一切。这里原本只有 finally，
      // 任何意外异常都会冒到 process 层把 DSH 干掉（2026-10-22 用户实遇）。
      say(`[observe] 本轮出错（已忽略）：${String((error && error.message) || error)}`);
    } finally {
      obsBusy = false;
    }
  };

  {
    // interval 常驻：运行时的「态势 关/开」要能重新打开它，所以注册与否不能只看配置。
    // 5 秒粒度轮询：真正采不采由 observeTick 里的间隔判断决定（「态势 间隔」立刻生效）。
    const stopObs = ctx.interval(observeTick, OBSERVE_POLL_MS);
    ctx.effect(() => () => {
      if (typeof stopObs === 'function') stopObs();
    });
    say(
      `[apply] 态势观察：${O.enabled ? '开' : '关'}；` +
        `每 ${O.seconds}s 采一次（游戏内「态势 间隔 <秒>」可改），` +
        `mode=${O.mode}，最短 ${O.minWakeSeconds}s 交一次，` +
        `上限 ${O.maxWakePerHour} 次/小时；开关用「态势 关/开/状态」`,
    );
  }

  // ── 空闲自主：角色闲下来就交一次「自主时间」，goal 由 agent 自己定 ──
  // 和 observe 的分工：observe 盯的是**世界**（态势变了才叫醒）；
  // 这里盯的是**角色自己** —— 只要它闲着（没在寻路）超过 idleSeconds 就交一次，
  // goal 完全交给 agent 决定（用户原话：「goal可以是任何事情，只要当前处于
  // 空闲状态就可以，完全由 AI 自行决定」）。
  //
  // 时序上刻意保守：交出去之后立刻把 lastBusyAt 拉回 now，避免 agent 还在
  // 干活（比如等 mine_quantity 挖完）时被连环叫醒。
  // 同 O：全部 getter，每次访问现读 cfg，否则设置页改了不生效。
  const A = {
    get enabled() { return boolOf(cfg.autonomy, true); },
    get tickSeconds() { return Math.max(10, Number(cfg.autonomyTickSeconds) || 20); },
    get idleSeconds() { return Math.max(30, Number(cfg.autonomyIdleSeconds) || 120); },
    get minGapSeconds() { return Math.max(60, Number(cfg.autonomyMinGapSeconds) || 300); },
    get maxPerHour() { return Math.max(1, Number(cfg.autonomyMaxPerHour) || 6); },
  };

  let lastBusyAt = Date.now();
  let lastAutonomyAt = 0;
  const autonomyLog = [];
  let autoBusy = false;

  const autonomyTick = async () => {
    if (!masterOn() || !A.enabled || autoBusy) return;
    autoBusy = true;
    try {
      // 空闲判定只关心「在不在寻路 / 活着没 / 血多少」—— 用轻量的 is_idle，
      // 不再拉整个 get_state（那个会序列化位置/背包/导航/伤害来源，每 20 秒一次纯浪费）。
      // 老 jar 没有这个命令时报错 → 回退到 get_state，保证不因升级不同步而停摆。
      let busyState = null;
      try {
        const r = await bridgeCall(cfg, 'is_idle', {});
        busyState = (r && r.data) || {};
      } catch {
        busyState = null;
      }
      let pathing = false;
      let dead = false;
      if (busyState) {
        pathing = busyState.pathing === true;
        dead = busyState.dead === true || Number(busyState.health) <= 0;
      } else {
        // 兜底：老 bridge 没有 is_idle
        let s;
        try {
          const st = await bridgeCall(cfg, 'get_state', {});
          s = (st && st.data) || {};
        } catch {
          return; // bridge 不在 / MC 没开，静默跳过
        }
        const pos = s.position;
        if (!pos || !Number.isFinite(Number(pos.x)) || Number(s.health) <= 0) return;
        pathing = Number(s.is_pathing) === 1 || s.is_pathing === true;
        dead = false;
      }
      if (dead) return;

      const now = Date.now();
      if (pathing) {
        lastBusyAt = now;
        return;
      }
      // 角色静止 ≠ 会话空闲：agent 可能正在算配方、查背包、等熔炉烧完。
      // 它正忙着就别叫 —— 它本来就在干活，再叫只会多堆一个待处理轮次。
      if (isAgentRunning(ctx, currentSessionId())) {
        lastBusyAt = now;
        return;
      }
      const idleFor = now - lastBusyAt;
      // 两个节奏都由游戏内命令控制（现读，立即生效）：
      //   「自主 空闲 <秒>」= 闲满多久才交；「自主 间隔 <秒>」= 两次之间至少隔多久。
      if (idleFor < A.idleSeconds * 1000) return;

      while (autonomyLog.length && autonomyLog[0] < now - 3600_000) autonomyLog.shift();
      if (autonomyLog.length >= A.maxPerHour) return;
      if (now - lastAutonomyAt < A.minGapSeconds * 1000) return;
      lastAutonomyAt = now;
      lastBusyAt = now; // 交出去就重新计时，别连环叫
      autonomyLog.push(now);

      let snap = '';
      try {
        snap = await snapshotGame();
      } catch {
        // 🔴 这里原来写的是 `位置(${pos.x},…)`，但 `pos` 只在上面「老 jar 兜底」分支里
        // 才有定义（2026-10-22 改 is_idle 时挪进去的）。走 is_idle 正常路径时 pos 不存在，
        // 于是 MC 一关、snapshotGame 抛错 → 这个 catch 自己也抛 ReferenceError →
        // 没有外层 catch → DSH 进程直接退出。兜底文案不许再依赖任何分支内变量。
        snap = '（当前局面暂时读不到，bridge 可能刚断开）';
      }
      const r = deliverToSession(
        ctx,
        sessionsRoot,
        `（空闲自主时间）${snap}\n` +
          '现在角色闲着。先 `get_goal`：有在跑的目标就推进**一步**；没有就自己 `create_goal` 立一个，' +
          'objective 里写清目标 + 3~6 个可验证的步骤。\n' +
          '做法：直接调工具（查背包 mcp__baritone__get_inventory；挖矿 mcp__baritone__mine_quantity；' +
          '合成 mcp__baritone__craft；熔炼 mcp__baritone__smelt_items；种地 mcp__baritone__farm；' +
          '找玩家 mcp__baritone__get_entities；说话 mcp__baritone__say）。\n' +
          '缺料就先向玩家要，要不到再自己去挖/烧。推进一步后先 get_goal 拿 revision，' +
          '再 `update_goal(action="edit", objective=…)` 更新进度；做完了 `action="complete"`。' +
          '这一轮什么都不想做，就只回一个「等」字。\n' +
          // 同态势那条：硬约束贴在消息里，别指望它去翻手册（A/B 方案，2026-10-16）。
          '铁律：① 一轮只推进一步，做完汇报一句；② 不拆玩家造的东西、不在基地附近乱挖；' +
          '③ 任何一步失败后先重新确认现状，别沿用旧假设；④ 动手前先清残留任务' +
          '（snapshot 看 is_pathing → cancel_command）；⑤ 说话一次一句。\n' +
          '（判断表与起步示例：skill mc-autonomy，或工作区 空闲自主行为手册.md）',
        currentSessionId(),
      );
      say(`[autonomy] 空闲 ${Math.round(idleFor / 1000)}s → ${r.ok ? '已交 agent' : `失败(${r.why})`}`);
    } catch (error) {
      // 🔴 2026-10-22 用户报「dsh 直接中断运行」：这里原本**只有 finally、没有 catch**，
      // 任何未预料的异常（当时是一个 `pos is not defined`）都会冒到 process 层，
      // 把整个 DSH 干掉。定时器回调必须自己兜住一切 —— 插件出错的代价只能是
      // 「这一轮不干活」，绝不能是「宿主退出」。
      say(`[autonomy] 本轮出错（已忽略）：${String((error && error.message) || error)}`);
    } finally {
      autoBusy = false;
    }
  };

  {
    // interval 常驻：运行时的「自主 开」要能把它重新打开，所以注册与否不能只看配置。
    const stopAuto = ctx.interval(autonomyTick, A.tickSeconds * 1000);
    ctx.effect(() => () => {
      if (typeof stopAuto === 'function') stopAuto();
    });
    say(
      `[apply] 空闲自主：${A.enabled ? '开' : '关'}；` +
        `每 ${A.tickSeconds}s 看一次，闲 ${A.idleSeconds}s 交一次，` +
        `间隔>=${A.minGapSeconds}s，上限 ${A.maxPerHour} 次/小时；` +
        `开关与节奏都在 DSH 设置 → Minecraft 陪玩 里改`,
    );
  }

  // 自检：确认真的能读到 agent.status。读不到时 isAgentRunning 一律返回 false
  // （退回"照常投递"的旧行为），那种情况必须在日志里看见，而不是悄悄失效。
  try {
    const agents = ctx.get('agents');
    const id = String(currentSessionId() || '').trim();
    const handle = agents && typeof agents.get === 'function' ? agents.get(id) : undefined;
    say(
      `[apply] 忙闲探测：agents=${agents ? 'yes' : 'no'} handle=${handle ? 'yes' : 'no'} ` +
        `status=${handle ? String(handle.status ?? handle.agent?.status) : 'n/a'}`,
    );
  } catch (error) {
    say(`[apply] 忙闲探测失败：${String((error && error.message) || error)}`);
  }

  /**
   * 手动重连时要清掉的状态。
   *
   * 为什么必须清：bridge（MC 客户端）重启后 `event_seq` 会从 0 重新计数，而这里的游标
   * 是单向推进的 —— 不清就会"拿着一个大数去等小序号"，事件永远读不到。2026-10-21 踩过
   * 一次：受伤事件看着正常（它每轮无条件覆盖游标），只有聊天全哑，极难辨认。
   * 同理还有战斗窗口与断线计时器，不清会让重连后立刻又进"静默"状态。
   */
  resetCursorState = () => {
    try {
      cursor = null;
      notableSeeded = false;
      notableCursors.clear();
      lastUrgentWakeAt = 0;
      combatUntil = 0;
      bridgeDownSince = 0;
      bridgeDownLogged = false;
      lastObserveAt = 0;
      // WS 也一并重连：MC 重启后 WS 长连接通常已经半死，不主动断开会一直挂着不恢复。
      if (wsLink.timer) { try { clearTimeout(wsLink.timer); } catch { /* 忽略 */ } }
      wsLink.timer = null;
      wsClose();
      wsLink.state = 'off';
      wsLink.retries = 0;
      wsConnect();
    } catch {
      /* 个别状态还没就绪也无妨，重连本身已经做了探测 */
    }
  };

  // ── WS 客户端：收 damage / chat 推送 ────────────────────────────────
  const wsUrl = () => `ws://${cfg.host || '127.0.0.1'}:${Number(cfg.wsPort) || 5559}`;

  const wsScheduleReconnect = (why) => {
    if (wsLink.timer) return;
    const delay = Math.min(
      WS_RECONNECT_MAX_MS,
      WS_RECONNECT_MIN_MS * Math.max(1, 2 ** Math.min(wsLink.retries, 5)),
    );
    wsLink.retries += 1;
    wsLink.timer = setTimeout(() => {
      wsLink.timer = null;
      wsConnect();
    }, delay);
    if (typeof wsLink.timer === 'object' && wsLink.timer && typeof wsLink.timer.unref === 'function') {
      wsLink.timer.unref();
    }
    say(`[ws] ${why} → ${Math.round(delay / 1000)}s 后重连（第 ${wsLink.retries} 次）；期间回退到轮询`);
  };

  const wsClose = () => {
    const socket = wsLink.socket;
    wsLink.socket = null;
    if (socket) {
      try { socket.onclose = null; socket.onerror = null; socket.onmessage = null; socket.close(); } catch { /* 忽略 */ }
    }
  };

  const wsConnect = () => {
    // ⚠️ 这里**不看总开关**：连接本身是"观测通道"，连着才能把状态显示出来。
    // 真正处理事件时才看总开关（见 onmessage）—— 关掉总开关后 WS 仍然连着，
    // 但收到的事件一律丢弃。这样"WS 通不通"永远可见，而不是一关总开关就瞎。
    if (wsLink.state === 'connecting' || wsLink.state === 'open') return;
    if (typeof WebSocket !== 'function') {
      wsLink.state = 'off';
      say('[ws] 当前 Node 没有内置 WebSocket —— 只走轮询');
      return;
    }
    const url = wsUrl();
    wsLink.state = 'connecting';
    try {
      const socket = new WebSocket(url);
      wsLink.socket = socket;

      socket.onopen = () => {
        wsLink.state = 'open';
        wsLink.retries = 0;
        wsLink.connectedAt = Date.now();
        wsLink.lastError = '';
        try {
          socket.send(JSON.stringify({ type: 'subscribe', events: WS_EVENTS }));
        } catch { /* 订阅失败下一次重连再说 */ }
        say(`[ws] 已连接 ${url}，订阅 ${WS_EVENTS.join('/')}（这两个类型改由推送驱动）`);
      };

      socket.onmessage = (event) => {
        wsLink.lastMsgAt = Date.now();
        const raw = typeof event.data === 'string' ? event.data : '';
        if (!raw) return;
        let msg;
        try { msg = JSON.parse(raw); } catch { return; }
        if (!msg || msg.type !== 'event') return;   // subscribed / pong 之类
        // 只订了 damage/chat，这两个都很小；真收到压缩包说明订阅面变了，直接跳过。
        if (msg.compressed) return;
        const kind = String(msg.event_type || '').toLowerCase();
        const data = msg.data || {};
        // 总开关关掉 → 事件照收（保活、可见），但一条都不处理
        if (!masterOn()) return;
        try {
          if (kind === 'damage') handleDamageEvent(data);
          else if (kind === 'chat') deliverChat(data);
        } catch (error) {
          // 和定时器同理：一条推送处理失败不能冒到外面去
          say(`[ws] 处理 ${kind} 出错（已忽略）：${String((error && error.message) || error)}`);
        }
      };

      socket.onerror = () => {
        wsLink.lastError = 'socket error';
        // 具体原因通常只有 close 才拿得到，这里不重复记日志
      };

      socket.onclose = () => {
        const wasOpen = wsLink.state === 'open';
        wsLink.state = 'closed';
        wsLink.socket = null;
        if (wasOpen) say('[ws] 连接断开，自动重连并回退轮询');
        wsScheduleReconnect('连接关闭');
      };
    } catch (error) {
      wsLink.state = 'closed';
      wsLink.lastError = String((error && error.message) || error);
      wsScheduleReconnect(`建立连接失败：${wsLink.lastError}`);
    }
  };

  wsConnect();

  ctx.effect(() => () => {
    if (wsLink.timer) { try { clearTimeout(wsLink.timer); } catch { /* 忽略 */ } }
    wsClose();
    wsLink.state = 'off';
    say('[ws] 连接已关闭');
  });

  ctx.effect(() => () => {
    if (typeof stop === 'function') stop();
    say('[stop] 聊天监听停了');
  });
}
