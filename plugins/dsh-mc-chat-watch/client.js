/**
 * dsh-mc-chat-watch — 设置页（浏览器半）。
 *
 * 与 Host 半（index.js）用同一个命名空间 'mc-chat-watch' 配对，但**配置数据不走 DSH 的
 * settings 服务** —— 本机实测那个服务没有 installSection/register，`configForms` 拿到的是
 * 没有字段的空壳，`set()` 静默无效（2026-10-22）。
 *
 * 实际做法：Host 半用 `webServer.register` 开一个 `/mc-chat-watch-config` 接口，
 * 这里 `fetch` 读写它（同 `dsh-meme` 的做法）。配置落在
 * `$DSH_HOME/mc-chat-watch/settings.json`，优先级高于 cordis config。
 *
 * 打包成加载器要的 lazy-CJS 工厂产物：`window.__ModuleLoader__.load({ id, factory })`。
 * id 必须与 loader entry 名一致，否则 ModuleLoader 会报 "loaded without registering"。
 */
window.__ModuleLoader__.load({
  id: 'dsh-mc-chat-watch',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const h = React.createElement

    const NS = 'mc-chat-watch'
    /** Host 半注册的配置读写接口（见 index.js 的 CONFIG_ROUTE）。 */
    const CONFIG_ROUTE = '/mc-chat-watch-config'

    /** 表单字段定义：分组 → 字段。defaultValue 只在 Host 没给出值时用于占位显示。 */
    const GROUPS = [
      {
        title: '账号',
        hint: '两个客户端的账号名。改完立即生效，不用重启。',
        fields: [
          { key: 'playerName', label: '玩家账号名', placeholder: 'xlhl_world', help: '你（人类）在游戏里的名字，用于跟丢判断和主动说话点名' },
          { key: 'aiAccount', label: 'AI 账号名', placeholder: 'DeepSeek', help: 'AI 自己的账号名。用它排除"自己说的话"和"自己这个实体"。留空 ignoreSenders 自动跟随' },
        ],
      },
      {
        title: '投递目标',
        hint: '游戏消息投给哪个会话。留空 = 自动挑最近活跃的（会串台，不推荐）。',
        fields: [
          { key: 'sessionId', label: '固定会话 ID', placeholder: 'session-xxxxxxxx-...', help: '填了就零推测；换窗口后要改这里' },
          { key: 'device', label: '设备前缀', placeholder: '【PC】', help: '投递消息前面加的设备标识' },
        ],
      },
      {
        title: '连接',
        fields: [
          { key: 'host', label: 'bridge 主机', placeholder: '127.0.0.1' },
          { key: 'port', label: 'bridge 端口', placeholder: '5556', type: 'number', help: 'AI 客户端的 bridge 端口（人那侧默认 5555）' },
          { key: 'timeoutMs', label: '单次超时 (ms)', placeholder: '4000', type: 'number' },
        ],
      },
      {
        title: '节奏',
        hint: '数值越小越实时，也越耗资源。',
        fields: [
          { key: 'tickSeconds', label: '轮询间隔 (秒)', placeholder: '3', type: 'number', help: '聊天与事件多久看一次' },
          { key: 'maxPerTick', label: '单轮最多投递', placeholder: '10', type: 'number', help: '防刷屏' },
        ],
      },
      {
        title: '开关',
        fields: [
          { key: 'enabled', label: '总开关', type: 'bool', help: '关掉后完全不轮询、不投递' },
        ],
      },
      // ── 下面两组对应以前只能"在游戏里打字"才能改的状态 ──────────────
      // 游戏内命令原本写进 <DSH_HOME>/mc-chat-watch/{*-switch.txt,tuning.json}，
      // 那是**运行时覆盖**、优先级高于这里。所以 Host 半在收到设置页改动时会
      // 清掉这些覆盖文件（见 index.js 的 onChange），否则在这里改完不生效。
      {
        title: '态势播报',
        hint: '对应游戏内「态势 开/关」「态势 间隔 N 秒」。定期把世界状态交给 AI 判断要不要动。',
        fields: [
          { key: 'observe', label: '启用态势播报', type: 'bool', help: '关掉就只在聊天/受伤时叫醒' },
          { key: 'observeSeconds', label: '采样间隔 (秒)', placeholder: '180', type: 'number', help: '多久看一次世界状态' },
          { key: 'observeMode', label: '播报模式', placeholder: 'changed', help: 'changed = 只在情况变化时播报；always = 每次都播（更费 token）' },
          { key: 'minWakeSeconds', label: '两次播报最小间隔 (秒)', placeholder: '30', type: 'number' },
          { key: 'maxWakePerHour', label: '每小时上限', placeholder: '40', type: 'number', help: '防话痨与烧钱' },
        ],
      },
      {
        title: '空闲自主',
        hint: '对应游戏内「自主 开/关」「自主 间隔 N 秒」「自主 空闲 N 秒」。角色闲着时让 AI 自己找事做。',
        fields: [
          { key: 'autonomy', label: '启用空闲自主', type: 'bool' },
          { key: 'autonomyIdleSeconds', label: '空闲判定阈值 (秒)', placeholder: '120', type: 'number', help: '连续闲满这么久才交一次自主时间' },
          { key: 'autonomyMinGapSeconds', label: '两次自主最小间隔 (秒)', placeholder: '300', type: 'number', help: '花费的主闸门，别设太小' },
          { key: 'autonomyTickSeconds', label: '忙闲检查间隔 (秒)', placeholder: '20', type: 'number' },
          { key: 'autonomyMaxPerHour', label: '每小时上限', placeholder: '6', type: 'number' },
        ],
      },
    ]

    const CSS = `
      .mcw-panel{display:flex;flex-direction:column;gap:18px;padding:4px 0;font-size:13px;color:var(--dsw-alias-label-primary)}
      .mcw-group{display:flex;flex-direction:column;gap:10px;padding:14px 16px;border:1px solid var(--dsw-alias-border-l1);border-radius:10px;background:var(--dsw-alias-bg-layer-1)}
      .mcw-group h3{margin:0;font-size:13px;font-weight:600;color:var(--dsw-alias-label-primary)}
      .mcw-group .hint{margin:0 0 2px;font-size:12px;color:var(--dsw-alias-label-secondary);line-height:1.5}
      .mcw-row{display:flex;flex-direction:column;gap:5px}
      .mcw-row label{font-size:12px;color:var(--dsw-alias-label-secondary)}
      .mcw-row .help{font-size:11px;color:var(--dsw-alias-label-secondary);opacity:.75;line-height:1.45}
      .mcw-field{width:100%}
      .mcw-row input[type=text],.mcw-row input[type=number]{width:100%;box-sizing:border-box;background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:8px 11px;font-size:13px;outline:none;transition:border-color .12s ease}
      .mcw-row input:focus{border-color:var(--dsw-alias-brand-primary)}
      .mcw-row input.overridden{border-color:var(--dsw-alias-brand-primary)}
      .mcw-inline{display:flex;gap:8px;align-items:center;width:100%}
      .mcw-inline button{background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:5px 10px;font-size:12px;cursor:pointer;white-space:nowrap}
      .mcw-inline button:hover{border-color:var(--dsw-alias-brand-primary)}
      .mcw-inline button:disabled{opacity:.45;cursor:default}
      .mcw-flag{font-size:11px;color:var(--dsw-alias-brand-primary);white-space:nowrap}
      /* 下拉选择器：照抄 dsh-thinking-language 的 .dshtl_selector（胶囊形、无边框） */
      .mcw-select{background:var(--dsw-alias-bg-module-platform);height:36px;font:inherit;color:var(--dsw-alias-label-primary);cursor:pointer;border:none;border-radius:18px;align-items:center;gap:12px;padding:0 14px;font-size:14px;line-height:22px;display:inline-flex;min-width:96px;outline:none}
      .mcw-select:hover{background:var(--dsw-alias-interactive-bg-hover, var(--dsw-alias-bg-layer-2))}
      .mcw-select:disabled{opacity:.5;cursor:default}
      .mcw-switch{display:none}
      .mcw-bar{display:flex;gap:10px;align-items:center;padding-top:4px}
      .mcw-bar button{background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer}
      .mcw-bar button:hover{border-color:var(--dsw-alias-brand-primary)}
      .mcw-bar button:disabled{opacity:.5;cursor:default}
      .mcw-bar .state{font-size:12px;color:var(--dsw-alias-label-secondary)}
      .mcw-bar .state.err{color:var(--dsw-alias-label-error, #d64545)}
      .mcw-bar .state.ok{color:var(--dsw-alias-label-success, #2e9e5b)}
    `

    /** 把 scope 快照里的 user 层摊平成 { key: value }，用于判断"这个字段被改过"。 */
    function userLayer(snapshot) {
      const user = snapshot && snapshot.user
      return user && typeof user === 'object' ? user : {}
    }

    /** 取当前生效值（value 层）。 */
    function valueLayer(snapshot) {
      const value = snapshot && snapshot.value
      return value && typeof value === 'object' ? value : {}
    }

    function FieldRow(props) {
      const { field, snapshot, onSet, onUnset, busy } = props
      const values = valueLayer(snapshot)
      const user = userLayer(snapshot)
      const overridden = Object.prototype.hasOwnProperty.call(user, field.key)
      const raw = values[field.key]
      const isBool = field.type === 'bool'
      const checked = raw === true || raw === 'true'
      const display = raw === undefined || raw === null ? '' : String(raw)

      // 布尔项用原生 <select> 二选一，照抄 dsh-thinking-language 的 FallbackMenu：
      // 官方"思考语言"页就是这种胶囊下拉。比 switch 可靠 —— 下拉的 onChange 不会被
      // label 转发二次触发，也不需要额外维护 checked 状态。
      const control = isBool
        ? h('select', {
            id: `mcw-${field.key}`,
            className: 'mcw-select',
            value: checked ? 'on' : 'off',
            disabled: busy,
            onChange: (e) => onSet(field.key, e.target.value === 'on'),
          },
          h('option', { value: 'on' }, '开'),
          h('option', { value: 'off' }, '关'))
        : h('input', {
            id: `mcw-${field.key}`,
            type: field.type === 'number' ? 'number' : 'text',
            className: overridden ? 'overridden' : '',
            value: display,
            placeholder: field.placeholder || '',
            disabled: busy,
            onChange: (e) => onSet(field.key, field.type === 'number' ? Number(e.target.value) : e.target.value),
          })

      // 布尔项的下拉本身就是控件，外层 label 只做文字（用 htmlFor 指过去是安全的，
      // select 不会像 checkbox 那样被 label 二次转发 change）
      return h('div', { className: 'mcw-row' },
        h('label', { htmlFor: `mcw-${field.key}` }, field.label),
        h('div', { className: 'mcw-inline' },
          control,
          overridden
            ? h(React.Fragment, null,
                h('span', { className: 'mcw-flag' }, '已覆盖'),
                h('button', { disabled: busy, onClick: () => onUnset(field.key) }, '恢复默认'))
            : null),
        field.help ? h('span', { className: 'help' }, field.help) : null)
    }

    function McSettingsPage(props) {
      // form 是设置作用域（两代 harness 名字不同、接口一致），由 mountPage 注入
      // 配置数据走插件自己的 HTTP 接口（host 半注册），**不依赖 DSH 的 settings 服务**
      // —— 本机那个服务没有 installSection/register，configForms 拿到的表单是空壳，
      // set() 静默无效（2026-10-22 实测）。
      const [cfg, setCfg] = React.useState(null)
      const [overrides, setOverrides] = React.useState({})
      const [busy, setBusy] = React.useState(false)
      const [message, setMessage] = React.useState(null)
      const [fatal, setFatal] = React.useState(null)

      const load = React.useCallback(async () => {
        try {
          const res = await fetch(CONFIG_ROUTE, { headers: { accept: 'application/json' } })
          if (!res.ok) throw new Error(`HTTP ${res.status}`)
          const data = await res.json()
          setCfg(data.config || {})
          setOverrides(data.overrides || {})
          setFatal(null)
        } catch (error) {
          setFatal(String(error && error.message ? error.message : error))
        }
      }, [])

      React.useEffect(() => { load() }, [load])

      // 拼成 FieldRow 认识的 { value, user } 形状：value=当前生效配置，user=页面改过的键
      const snapshot = { value: cfg || {}, user: overrides || {} }

      const apply = React.useCallback(async (patch, label) => {
        setBusy(true)
        setMessage(null)
        try {
          const res = await fetch(CONFIG_ROUTE, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(patch),
          })
          const data = await res.json().catch(() => ({}))
          if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`)
          setCfg(data.config || {})
          setOverrides(data.overrides || {})
          setMessage({ kind: 'ok', text: `${label}已保存` })
        } catch (error) {
          setMessage({ kind: 'err', text: `${label}保存失败：${String(error && error.message ? error.message : error)}` })
        } finally {
          setBusy(false)
        }
      }, [])

      const onSet = (key, value) => apply({ [key]: value }, '配置')
      const onUnset = (key) => apply({ [key]: null }, '配置')

      // 样式只注入一次
      React.useEffect(() => {
        const id = 'mcw-settings-style'
        if (document.getElementById(id)) return undefined
        const style = document.createElement('style')
        style.id = id
        style.textContent = CSS
        document.head.appendChild(style)
        return () => { try { style.remove() } catch { /* 忽略 */ } }
      }, [])

      // 读不到配置（host 半的设置接口没起来）时给出明确原因，而不是空表单
      if (fatal) {
        return h('div', { className: 'mcw-panel' },
          h('p', { className: 'hint' },
            `读不到插件配置：${fatal}`),
          h('p', { className: 'hint' },
            'Host 半可能没加载，或 webServer 服务不可用。可以先改 cordis.patch.yml 里的 config，',
            '或在游戏里用「态势 开/关」「自主 开/关」。'))
      }

      // 首次加载中
      if (cfg === null) {
        return h('div', { className: 'mcw-panel' },
          h('p', { className: 'hint' }, '正在读取配置…'))
      }

      return h('div', { className: 'mcw-panel' },
        GROUPS.map((group) => h('div', { className: 'mcw-group', key: group.title },
          h('h3', null, group.title),
          group.hint ? h('p', { className: 'hint' }, group.hint) : null,
          group.fields.map((field) => h(FieldRow, {
            key: field.key,
            field,
            snapshot,
            busy,
            onSet,
            onUnset,
          })))),
        h('div', { className: 'mcw-bar' },
          h('button', { disabled: busy, onClick: reload }, '刷新'),
          message ? h('span', { className: `state ${message.kind}` }, message.text) : null,
          h('span', { className: 'state' }, '改动立即生效，无需重启 DSH')))
    }

    /**
     * 页面挂进 settings.section 槽。
     *
     * 数据不走 DSH 的 settings 服务（本机那个服务没有 installSection/register，
     * configForms.get() 返回的是**没有字段的空壳**），而是走插件自己的 HTTP 接口
     * `<CONFIG_ROUTE>`（host 半注册，读 `$DSH_HOME/mc-chat-watch/settings.json`）。
     */
    function apply(ctx) {
      const slots = ctx && ctx.slots
      if (!slots || typeof slots.inject !== 'function') return
      slots.inject('settings.section', () => slots.register(
        { name: 'settings.section', id: NS, order: 30, label: 'Minecraft 陪玩' },
        McSettingsPage))
    }

    exports.apply = apply
    // 🔴 inject 只放"没有它这个插件就没意义"、且**启动早期一定存在**的服务。
    // 声明了 inject 的服务若一直不出现，cordis 会让插件**永久 pending**，那一轮
    // web boot 就整片挂掉 —— 2026-10-21 实测：把 settingsScope 写进 inject 后
    // Web GUI 直接白屏，报 "web boot: 1 entry did not activate /
    // dsh-mc-chat-watch: pending (waiting for service: settingsScope)"。
    // 所以 settingsScope 改为运行时软获取（见 resolveScope）。
    exports.inject = ['slots']
    exports.name = 'dsh-mc-chat-watch'
    return module.exports
  },
})
