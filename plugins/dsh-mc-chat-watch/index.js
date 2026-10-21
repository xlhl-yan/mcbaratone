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
import { readdirSync, statSync } from 'node:fs';
import { appendFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
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
  /** 轮询间隔（秒）。事件驱动，所以可以短；3s 对聊天够用。 */
  tickSeconds: 3,
  /** 玩家账号名。用于「跟丢判断」和主动说话时点名。 */
  playerName: 'xlhl_world',
  /**
   * AI 自己的账号名。
   *
   * 两处用它：① 假装不是自己说的话（`ignoreSenders` 留空时自动取它）；
   * ② 从 `get_entities` 的玩家列表里排除自己这个实体 —— 以前这里写死 'DeepSeek'，
   * 换账号就得改源码。
   */
  aiAccount: 'DeepSeek',
  /**
   * 这些发送者的消息不投递（AI 自己说的别把自己吵醒）。
   * **留空 = 自动用 [aiAccount]**，通常不用手填。
   */
  ignoreSenders: [],
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
};

export function resolveConfig(input = {}) {
  const cfg = { ...DEFAULTS, ...input };
  cfg.port = Number(cfg.port) || DEFAULTS.port;
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
  // 留空 = 只忽略 AI 自己说的（这也是历史默认行为）
  if (!Array.isArray(cfg.ignoreSenders) || cfg.ignoreSenders.length === 0) {
    cfg.ignoreSenders = [cfg.aiAccount];
  }
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
      buf += new TextDecoder('gbk').decode(chunk);
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

/** 从 bridge 事件里抽出「谁说了什么」。 */
export function parseChatEvent(event, ignoreSenders) {
  const data = event?.data || {};
  const sender = typeof data.sender === 'string' ? data.sender : '';
  const raw = typeof data.message === 'string' ? data.message : '';
  if (sender && ignoreSenders.includes(sender)) return null;
  // bridge 给的 message 是**整行**（形如 `<xlhl_world> 你好`）。
  // 有独立 sender 字段时剥掉前缀，没有就原样留着（宁可多带一点也别丢内容）。
  let text = raw;
  if (sender) {
    const prefix = `<${sender}> `;
    if (raw.startsWith(prefix)) text = raw.slice(prefix.length);
  }
  return { sender, raw, text };
}

/**
 * 游戏内控制命令词表。
 *
 * 前缀特意用中文，不用 `!` —— `!` 会被 Baritone 抢去当命令解析，刷一屏
 * 「未知或不完整的命令」（与 bridge 侧「唤醒会话」同一个坑）。
 * 整串匹配（^…$），所以正常说话里的「自主」「态势」不会误触发。
 */
const CONTROL_PATTERNS = [
  {
    key: 'autonomy',
    re: /^(?:空闲)?自主(?:任务)?\s*(?:开关)?\s*(开|打开|开启|启用|关|关闭|停|停用|禁用|状态|on|off|status)?$/i,
  },
  {
    key: 'observe',
    re: /^(?:游戏)?态势(?:提醒|播报)?\s*(?:开关)?\s*(开|打开|开启|启用|关|关闭|停|停用|禁用|状态|on|off|status)?$/i,
  },
];

/**
 * 「间隔」类命令：游戏里直接改运行节奏，不用回来动 YAML。
 *
 *   自主 间隔 600      → 两次「自主时间」至少隔 600 秒（花费的主闸门）
 *   自主 空闲 60       → 角色闲满 60 秒就交（低于这个值不会提前叫）
 *   态势 间隔 300      → 每 300 秒采一次态势
 *   不带数字 = 查询当前值（`自主 间隔`）
 *
 * 值写进 <stateDir>/tuning.json，**tick 时现读**，所以改完立即生效、不用重启 DSH；
 * 删掉文件或某个键就回到 cordis config 里的默认值。
 */
const TUNE_PATTERNS = [
  {
    key: 'autonomy',
    field: 'autonomyMinGapSeconds',
    label: '空闲自主间隔',
    re: /^(?:空闲)?自主(?:任务)?\s*(?:间隔|周期|频率)\s*(\d+)?\s*(?:秒|s)?$/i,
  },
  {
    key: 'autonomy',
    field: 'autonomyIdleSeconds',
    label: '空闲判定阈值',
    re: /^(?:空闲)?自主(?:任务)?\s*(?:空闲|闲置)\s*(\d+)?\s*(?:秒|s)?$/i,
  },
  {
    key: 'observe',
    field: 'observeSeconds',
    label: '态势采样间隔',
    re: /^(?:游戏)?态势(?:提醒|播报)?\s*(?:间隔|周期|频率)\s*(\d+)?\s*(?:秒|s)?$/i,
  },
];

/** 命令里的动作词 → 'on' / 'off' / 'status'（没给动作词 = 查询）。 */
function parseAction(word) {
  const w = String(word || '').toLowerCase();
  if (w === '') return 'status';
  if (['关', '关闭', '停', '停用', '禁用', 'off'].includes(w)) return 'off';
  if (['开', '打开', '开启', '启用', 'on'].includes(w)) return 'on';
  return 'status';
}

/**
 * 「这句话是不是控制命令？」是就返回命令对象，不是返回 null。
 *
 * - 开分类：`{ kind: 'switch', key, action }`，key：autonomy | observe，action：on | off | status
 * - 间隔类：`{ kind: 'tune', key, field, label, seconds }`，seconds 为 null 表示查询
 */
export function parseControlCommand(text) {
  const t = String(text || '').trim();
  // 先试「间隔」：`自主 间隔 600` 不含开关动作词，两者不会互相误吃。
  for (const { key, field, label, re } of TUNE_PATTERNS) {
    const m = re.exec(t);
    if (m) {
      return {
        kind: 'tune',
        key,
        field,
        label,
        seconds: m[1] === undefined ? null : Number(m[1]),
      };
    }
  }
  for (const { key, re } of CONTROL_PATTERNS) {
    const m = re.exec(t);
    if (m) return { kind: 'switch', key, action: parseAction(m[1]) };
  }
  return null;
}

/** 设置页与 Host 半的配对键：两半必须用同一个字符串。 */
export const SETTINGS_NS = 'mc-chat-watch';

export function apply(ctx, input = {}) {
  const cfg = resolveConfig(input);

  // ── 设置页（Host 半）───────────────────────────────────────────────
  // 只注册命名空间与"配置变了"的回调；表单由 ./client.js 那一半画。
  //
  // 🔴 绝对不要用 ctx.inject(['settings'], ...)：cordis 的 inject 语义是"声明的服务
  // 一直不出现就永久 pending"，而 pending 的插件会让整个 web boot 失败 —— 2026-10-21
  // 实测白屏："web boot: 1 entry did not activate"。这里全部走软获取：拿不到就跳过，
  // 插件本体照常工作，只是没有设置页。
  try {
    const settingsSvc = typeof ctx.get === 'function' ? ctx.get('settings') : ctx.settings;
    if (settingsSvc && typeof settingsSvc.installSection === 'function') {
      settingsSvc.installSection(ctx, SETTINGS_NS, null, cfg, {
        // 设置页写入后，把新值同步回运行期 —— cfg 是本地对象，直接改它
        setSource: (get) => {
          try {
            const next = typeof get === 'function' ? get() : null;
            if (next && typeof next === 'object') Object.assign(cfg, resolveConfig(next));
          } catch {
            /* 取不到就继续用旧的 */
          }
        },
        // 改完立刻重建后续要用到的派生值（账号名等），不用重启 DSH
        onChange: () => {
          try {
            Object.assign(cfg, resolveConfig(cfg));
          } catch {
            /* 同上 */
          }
          // 🧹 设置页已成为"态势/自主"的唯一真源：把这几个**运行时覆盖文件**清掉。
          // 它们优先级高于 config（游戏内命令写进来的），留着会让页面上的改动不生效。
          // 代价是"以前用游戏内命令调过的值"会被重置成 config 里的值 —— 这正是把
          // 所有开关集中到设置页的目的。
          try {
            const { dir: stateDir } = statePaths(cfg);
            for (const name of ['observe-switch.txt', 'autonomy-switch.txt', 'tuning.json']) {
              const target = join(stateDir, name);
              if (existsSync(target)) rmSync(target, { force: true });
            }
          } catch {
            /* 删不掉就下次 tick 现读时仍按文件走，不影响主流程 */
          }
        },
      });
    }
  } catch {
    /* 设置服务不可用：功能照旧，只是没有页面 */
  }

  const { dir } = statePaths(cfg);
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    /* 建不出来就只跳过日志 */
  }
  const logPath = join(dir, 'watch.log');
  const sessionsRoot = cfg.sessionsRoot || join(homeDir(), 'sessions');

  const say = (line) => {
    try {
      appendFileSync(logPath, `${new Date().toISOString()} ${line}\r\n`);
    } catch {
      /* 日志写不了也不能炸 */
    }
  };

  // ── 游戏内开关（「自主 关」「态势 开」「自主 状态」）────────────────────
  // 为什么走游戏内聊天，而不是 DSH 插件页的配置界面：
  //   DSH 的插件页**不会**从 cordis config 自动生成表单 —— 要由插件自己带
  //   浏览器半边（browser half）注册 plugins.bundle.config / plugins.row.config
  //   插槽来画控件（见 dsh-client-modules 的「配置插槽」文档）。本插件是纯 Host
  //   插件，所以那一页永远是空的（2026-10-15 用户截图确认）。
  //   于是把开关放到用户能触达的地方 —— MC 聊天栏，一句命令就切，不用改 YAML、
  //   不用重启 DSH。命令由本插件拦截，不会投给 agent。
  //
  // 状态存在 <stateDir>/<key>-switch.txt：文件在 → 听文件的；没有文件 → 跟随
  // cordis config 里的默认值。删掉文件即恢复配置默认。
  const switchPaths = {
    autonomy: join(dir, 'autonomy-switch.txt'),
    observe: join(dir, 'observe-switch.txt'),
  };
  /** 运行时开关的兜底默认值（惰性读 A/O —— 调用时它们一定已经初始化）。 */
  const switchDefaultOf = (key) => (key === 'autonomy' ? A.enabled : O.enabled);

  const readSwitch = (key) => {
    let raw;
    try {
      raw = readFileSync(switchPaths[key], 'utf8').trim().toLowerCase();
    } catch {
      return null; // 没有文件
    }
    if (['off', '0', 'false', '关', '关闭'].includes(raw)) return false;
    if (['on', '1', 'true', '开', '打开'].includes(raw)) return true;
    return null; // 内容不认 → 当没设过
  };

  const writeSwitch = (key, on) => {
    try {
      mkdirSync(dir, { recursive: true });
      writeFileSync(switchPaths[key], on ? 'on' : 'off');
      return true;
    } catch (error) {
      say(`[cmd] 写开关失败：${String((error && error.message) || error)}`);
      return false;
    }
  };

  /** 当前到底开没开：开关文件优先，其次看配置。 */
  const switchOn = (key) => {
    const v = readSwitch(key);
    return v === null ? switchDefaultOf(key) : v;
  };

  // ── 运行时参数（游戏内「自主 间隔 600」「态势 间隔 300」）──────────────
  // 与开关同一套思路：命令写 <stateDir>/tuning.json，tick 时**现读**，
  // 所以改完立即生效、不用重启 DSH。删掉文件或某个键 → 回到 cordis config 默认值。
  const tuningPath = join(dir, 'tuning.json');

  const readTuning = () => {
    try {
      const obj = JSON.parse(readFileSync(tuningPath, 'utf8'));
      return obj && typeof obj === 'object' ? obj : {};
    } catch {
      return {}; // 没文件 / 内容坏了：一律当「没设过」
    }
  };

  const writeTuning = (field, seconds) => {
    try {
      mkdirSync(dir, { recursive: true });
      const obj = readTuning();
      obj[field] = seconds;
      writeFileSync(tuningPath, JSON.stringify(obj, null, 2));
      return true;
    } catch (error) {
      say(`[cmd] 写参数失败：${String((error && error.message) || error)}`);
      return false;
    }
  };

  /** 运行时值优先，没有就用调用方给的配置默认值。 */
  const tuneValue = (field, fallback) => {
    const v = Number(readTuning()[field]);
    return Number.isFinite(v) && v > 0 ? v : fallback;
  };

  /** 在游戏里回一句话（以 AI 角色身份，会出现在聊天栏）。 */
  const replyToGame = async (text) => {
    try {
      await bridgeCall(cfg, 'chat', { message: text });
    } catch {
      /* MC 没开就算了，日志里已经记了 */
    }
  };

  /** 某个可调参数的配置默认值（惰性读 A/O —— 调用时它们一定已经初始化）。 */
  const tuneFallback = (field) => {
    if (field === 'autonomyMinGapSeconds') return A.minGapSeconds;
    if (field === 'autonomyIdleSeconds') return A.idleSeconds;
    if (field === 'observeSeconds') return O.seconds;
    return 0;
  };

  const handleControlCommand = async (cmd, parsed) => {
    // ── 间隔类：改运行节奏 ──────────────────────────────────────────────
    if (cmd.kind === 'tune') {
      const current = tuneValue(cmd.field, tuneFallback(cmd.field));
      if (cmd.seconds === null || !Number.isFinite(cmd.seconds)) {
        say(`[cmd] ${parsed.sender} 查询「${cmd.label}」→ ${current} 秒`);
        await replyToGame(`[${cmd.label}] 现在是 ${current} 秒`);
        return;
      }
      // 10 秒 ~ 24 小时：防止手滑打出 0 秒这种疯跑值。
      const seconds = Math.max(10, Math.min(24 * 3600, Math.round(cmd.seconds)));
      if (!writeTuning(cmd.field, seconds)) {
        await replyToGame(`[${cmd.label}] 写入失败，看插件日志`);
        return;
      }
      say(`[cmd] ${parsed.sender} 把「${cmd.label}」改成 ${seconds} 秒（${tuningPath}）`);
      await replyToGame(`[${cmd.label}] 已改成 ${seconds} 秒`);
      return;
    }

    // ── 开关类 ────────────────────────────────────────────────────────
    const label = cmd.key === 'autonomy' ? '空闲自主' : '态势提醒';
    if (cmd.action === 'status') {
      const raw = readSwitch(cmd.key);
      const shown = raw === null ? switchDefaultOf(cmd.key) : raw;
      say(`[cmd] ${parsed.sender} 查询「${label}」→ ${shown ? '开' : '关'}${raw === null ? '（配置默认）' : '（游戏内开关）'}`);
      await replyToGame(`[${label}] 现在是「${shown ? '开' : '关'}」`);
      return;
    }
    const on = cmd.action === 'on';
    if (!writeSwitch(cmd.key, on)) {
      await replyToGame(`[${label}] 切换失败，看插件日志`);
      return;
    }
    say(`[cmd] ${parsed.sender} 把「${label}」改成 ${on ? '开' : '关'}（${switchPaths[cmd.key]}）`);
    await replyToGame(`[${label}] 已${on ? '开启' : '关闭'}`);
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
    const r = deliverToSession(ctx, sessionsRoot, text, effectiveSessionId);
    say(`[proactive] 唤醒 agent：${r.ok ? '已进会话' : `失败(${r.why})`}`);
    return r;
  };

  const TEMPLATES = {
    lowHealth: ['我快没血了！', '撑不住了，得躲一下…', '救命，血好少'],
    nightfall: ['天黑了，小心怪物', '晚上了，别乱跑啊', '天暗了，要回屋吗'],
    hungry: ['我饿了，得找点吃的', '肚子咕咕叫…'],
  };

  // 目标会话的优先级：游戏里 !waken 设的 > 配置文件 sessionId > 自动挑最近活跃的。
  // 玩家能在游戏里随时改，不用回来动配置文件。
  let effectiveSessionId = String(cfg.sessionId || '').trim();
  let sessionSyncedAt = 0;
  const syncSessionFromGame = async (force = false) => {
    const now = Date.now();
    if (!force && now - sessionSyncedAt < 10000) return;
    sessionSyncedAt = now;
    try {
      const r = await bridgeCall(cfg, 'get_waken_session', {});
      const fromGame = String((r && r.data && r.data.session_id) || '').trim();
      if (fromGame && fromGame !== effectiveSessionId) {
        effectiveSessionId = fromGame;
        say(`[session] 游戏侧指定了唤醒会话：${fromGame}`);
      }
    } catch {
      /* bridge 不在就沿用上一次的值 */
    }
  };

  const tick = async () => {
    if (!cfg.enabled) return;
    if (busy) return; // 上一轮还没回来就跳过，别堆积
    busy = true;
    try {
      // 顺手同步目标会话（游戏里可能刚被别人敲 !waken 改过）
      await syncSessionFromGame();
      const afterSeq = cursor === null ? 0 : cursor;
      const result = await bridgeCall(cfg, 'get_events', {
        after_seq: afterSeq,
        type: 'chat',
        limit: 50,
      });
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

      let delivered = 0;
      for (const event of events) {
        const parsed = parseChatEvent(event, cfg.ignoreSenders);
        if (!parsed || !parsed.text) continue;
        lastHumanChatAt = Date.now(); // 有人说话 → 闲聊计时重来
        // 游戏内控制命令：只当开关用，不投给 agent、不占投递配额。
        const control = parseControlCommand(parsed.text);
        if (control) {
          await handleControlCommand(control, parsed);
          continue;
        }
        if (delivered >= cfg.maxPerTick) break;
        const line = `（游戏内聊天）${parsed.sender || '某人'}：${parsed.text}`;
        const sent = deliverToSession(ctx, sessionsRoot, line, effectiveSessionId);
        delivered += 1;
        say(
          `[fire] ${line} → 投递=${sent.ok ? `已进会话（${sent.mode}）${String(sent.sessionId).slice(0, 20)}…` : `失败(${sent.why})`}`,
        );
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
      say(`[tick] bridge 不可达：${String((error && error.message) || error)}`);
    } finally {
      busy = false;
    }
  };

  say(
    `[apply] 聊天监听起来了：${cfg.host}:${cfg.port} 每 ${cfg.tickSeconds}s 一次；忽略 ${cfg.ignoreSenders.join('/')}`,
  );
  const stop = ctx.interval(tick, cfg.tickSeconds * 1000);

  // ── 主动说话引擎主循环 ──────────────────────────────────────────────
  let proBusy = false;
  const proactiveTick = async () => {
    if (!P.enabled || proBusy) return;
    proBusy = true;
    try {
      const st = await bridgeCall(cfg, 'get_state', {});
      const s = (st && st.data) || {};
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
  const O = {
    enabled: input.observe === true,
    seconds: Math.max(3, Number(input.observeSeconds) || 5),
    // changed = 态势真的变了才叫醒（站着不动时不烧钱）
    // always  = 每轮都交（响应最全，代价最大）
    mode: input.observeMode === 'always' ? 'always' : 'changed',
    minWakeSeconds: Math.max(3, Number(input.minWakeSeconds) || 10),
    maxWakePerHour: Math.max(1, Number(input.maxWakePerHour) || 120),
  };

  let lastSnapshot = '';
  let lastWakeAt = 0;
  let lastUrgentWakeAt = 0;
  // urgent 事件的唤醒节流（毫秒）——**一场战斗只叫醒 agent 一次**。
  // 起因（2026-10-16 用户原话「第二次的间隔太短了，连续多次插入会话导致你的响应反而变慢了」）：
  // 原来 8 秒。被连击时每 8 秒就 steer 一条"🔴 受伤"进会话，**把 agent 正在进行的回复反复打断**，
  // 结果是"叫得越勤、反应反而越慢"。僵尸连咬 6 下一场也就 10 秒左右 —— 60 秒足够覆盖一整场
  // 战斗，打完 agent 还醒着；60 秒后还在挨打才会再叫第二次。
  const URGENT_GAP_MS = 60000;
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
  // urgent 类型（死亡/受伤/遭攻击）**每一轮都查**；其余（天气/维度/重生）轮着查。
  // 起因（2026-10-16 用户原话「这个时间延迟有点太长了吧」）：原来单类型轮询，6 个类型
  // 轮一圈最坏要等 18 秒才轮到 damage —— 挨打要秒级反应，不能排队。
  const URGENT_TYPES = NOTABLE.filter(([, , u]) => u === true).map(([t]) => String(t).toLowerCase());
  const REST_TYPES = NOTABLE.filter(([, , u]) => u !== true).map(([t]) => String(t).toLowerCase());

  const notableTick = async () => {
    if (!O.enabled) return;
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
        // 🔴 2026-10-16 实测：bridge 的响应**没有 data 包装**（直接 { events, latest_seq, ... }）。
        // 这里原来只认 r.data.events，于是 events 永远是空数组 —— notableTick 自上线起就一直
        // 在空转：死亡/受伤/遭攻击一条都没唤醒过 agent（用户原话「怎么没见你攻击呢」）。
        // 照 tick() 的写法两种都认。
        const data = (r && (r.data || r)) || {};
        const events = Array.isArray(data.events) ? data.events : [];
        if (events.length) say(`[notable] ${type} ×${events.length}`);
        for (const ev of events) {
          const line = render(ev.data || {});
          // urgent（死亡/受伤/遭攻击）：**立刻叫醒**，不攒批 —— 攒着的代价就是"醒来时人已经死了"。
          // 8 秒节流：被连击时不至于刷爆 token / 消息。
          if (urgent && Date.now() - lastUrgentWakeAt >= URGENT_GAP_MS) {
            lastUrgentWakeAt = Date.now();
            say(`[urgent] ${line} → 已立刻交 agent`);
            wakeAgent(
              `🔴 ${line} —— 立刻用 get_combat_snapshot 看附近威胁：` +
                `血低就先拉开距离保命，有武器就 attack_nearest 反击。别站着挨打。`
            );
          } else {
            pendingNotable.push(line);
          }
        }
        notableCursors.set(key, Number(data.latest_seq ?? cursor));
      }
      while (pendingNotable.length > 8) pendingNotable.shift();
    } catch {
      /* bridge 不在就下一轮再说 */
    }
  };

  if (O.enabled) {
    // 1 秒一轮：urgent 类型（受伤/遭攻击/死亡）每一轮都查，所以"挨打 → 检测到"≤1 秒。
    // 起因（2026-10-16 用户原话「延迟还是有点高」）：3 秒一轮仍嫌钝。首轮要快、后续慢点没事
    // —— 后续由 URGENT_GAP_MS（60 秒）兜住：一场战斗只会叫醒 agent 一次，压低轮询不会刷爆会话。
    const stopNotable = ctx.interval(notableTick, 1000);
    ctx.effect(() => () => {
      if (typeof stopNotable === 'function') stopNotable();
    });
  }

  /** 把当前局面压成一行紧凑文本（越短越省 token）。 */
  const snapshotGame = async () => {
    const st = await bridgeCall(cfg, 'get_state', {});
    const s = (st && st.data) || {};
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
      const items = ((inv && inv.data && inv.data.inventory) || [])
        .filter((i) => i.count > 0)
        .map((i) => `${String(i.id || '').replace('minecraft:', '')}x${i.count}`);
      bits.push(`背包[${items.join(' ') || '空'}]`);
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
    if (!switchOn('observe') || obsBusy) return;
    const startedAt = Date.now();
    if (startedAt - lastObserveAt < tuneValue('observeSeconds', O.seconds) * 1000) return;
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
      if (isAgentRunning(ctx, effectiveSessionId)) {
        pendingObserve = { snap, count: (pendingObserve ? pendingObserve.count : 0) + 1 };
        lastWakeAt = now; // 占住节奏位，免得 agent 一闲下来就被补投一串
        say(`[observe] agent 正忙，暂存态势（累计 ${pendingObserve.count} 次）`);
        return;
      }

      const carried = pendingObserve;
      pendingObserve = null;
      lastWakeAt = now;
      observeLog.push(now);

      const r = deliverToSession(
        ctx,
        sessionsRoot,
        `（游戏态势）${snap}\n` +
          (carried ? `（我忙的时候态势变了 ${carried.count} 次，上面这条是最新的）\n` : '') +
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
        effectiveSessionId,
      );
      say(`[observe] ${snap} → ${r.ok ? '已交 agent' : `失败(${r.why})`}`);
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
      `[apply] 态势观察：配置=${O.enabled ? '开' : '关'}，当前=${switchOn('observe') ? '开' : '关'}；` +
        `每 ${tuneValue('observeSeconds', O.seconds)}s 采一次（游戏内「态势 间隔 <秒>」可改），` +
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
  const A = {
    enabled: input.autonomy === true,
    tickSeconds: Math.max(10, Number(input.autonomyTickSeconds) || 20),
    idleSeconds: Math.max(30, Number(input.autonomyIdleSeconds) || 120),
    minGapSeconds: Math.max(60, Number(input.autonomyMinGapSeconds) || 300),
    maxPerHour: Math.max(1, Number(input.autonomyMaxPerHour) || 6),
  };

  let lastBusyAt = Date.now();
  let lastAutonomyAt = 0;
  const autonomyLog = [];
  let autoBusy = false;

  const autonomyTick = async () => {
    // 配置只是默认值，游戏内「自主 关/开」随时覆盖（用户要的就是"操作时能随手关掉"）。
    if (!switchOn('autonomy') || autoBusy) return;
    autoBusy = true;
    try {
      // 空闲判定走 get_state 的 is_pathing（bridge 直接给的布尔），
      // 不用 snapshotGame() —— 那个是给人看的一行文本摘要，拿不到结构化字段。
      let s;
      try {
        const st = await bridgeCall(cfg, 'get_state', {});
        s = (st && st.data) || {};
      } catch {
        return; // bridge 不在 / MC 没开，静默跳过
      }
      const pos = s.position;
      if (!pos || !Number.isFinite(Number(pos.x)) || Number(s.health) <= 0) return;

      const now = Date.now();
      if (Number(s.is_pathing)) {
        lastBusyAt = now;
        return;
      }
      // 角色静止 ≠ 会话空闲：agent 可能正在算配方、查背包、等熔炉烧完。
      // 它正忙着就别叫 —— 它本来就在干活，再叫只会多堆一个待处理轮次。
      if (isAgentRunning(ctx, effectiveSessionId)) {
        lastBusyAt = now;
        return;
      }
      const idleFor = now - lastBusyAt;
      // 两个节奏都由游戏内命令控制（现读，立即生效）：
      //   「自主 空闲 <秒>」= 闲满多久才交；「自主 间隔 <秒>」= 两次之间至少隔多久。
      if (idleFor < tuneValue('autonomyIdleSeconds', A.idleSeconds) * 1000) return;

      while (autonomyLog.length && autonomyLog[0] < now - 3600_000) autonomyLog.shift();
      if (autonomyLog.length >= A.maxPerHour) return;
      if (now - lastAutonomyAt < tuneValue('autonomyMinGapSeconds', A.minGapSeconds) * 1000) return;
      lastAutonomyAt = now;
      lastBusyAt = now; // 交出去就重新计时，别连环叫
      autonomyLog.push(now);

      let snap = '';
      try {
        snap = await snapshotGame();
      } catch {
        snap = `位置(${Math.round(Number(pos.x))},${Math.round(Number(pos.y))},${Math.round(Number(pos.z))})`;
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
        effectiveSessionId,
      );
      say(`[autonomy] 空闲 ${Math.round(idleFor / 1000)}s → ${r.ok ? '已交 agent' : `失败(${r.why})`}`);
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
      `[apply] 空闲自主：配置=${A.enabled ? '开' : '关'}，当前=${switchOn('autonomy') ? '开' : '关'}；` +
        `每 ${A.tickSeconds}s 看一次，闲 ${tuneValue('autonomyIdleSeconds', A.idleSeconds)}s 交一次，` +
        `间隔>=${tuneValue('autonomyMinGapSeconds', A.minGapSeconds)}s，上限 ${A.maxPerHour} 次/小时；` +
        `开关「自主 关/开/状态」，节奏「自主 空闲 <秒>」「自主 间隔 <秒>」`,
    );
  }

  // 自检：确认真的能读到 agent.status。读不到时 isAgentRunning 一律返回 false
  // （退回"照常投递"的旧行为），那种情况必须在日志里看见，而不是悄悄失效。
  try {
    const agents = ctx.get('agents');
    const id = String(effectiveSessionId || '').trim();
    const handle = agents && typeof agents.get === 'function' ? agents.get(id) : undefined;
    say(
      `[apply] 忙闲探测：agents=${agents ? 'yes' : 'no'} handle=${handle ? 'yes' : 'no'} ` +
        `status=${handle ? String(handle.status ?? handle.agent?.status) : 'n/a'}`,
    );
  } catch (error) {
    say(`[apply] 忙闲探测失败：${String((error && error.message) || error)}`);
  }

  ctx.effect(() => () => {
    if (typeof stop === 'function') stop();
    say('[stop] 聊天监听停了');
  });
}
