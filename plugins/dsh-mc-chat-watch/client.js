/**
 * dsh-mc-chat-watch — 设置页（浏览器半）。
 *
 * 布局照抄 `dsh-thinking-language`：整页 max-width:760px，每行是
 * 「左边文字（标题 + 说明）+ 右边控件」的横向排列。
 * 输入框逻辑照抄 `meow-memory`：`onChange` 只更新本地草稿，**`onBlur` 才真正保存**，
 * 这样不会每敲一个字就发一次请求。
 *
 * 数据不走 DSH 的 settings 服务（本机那个服务没有 installSection/register，
 * `configForms` 拿到的是空壳，`set()` 静默无效），而是走 host 半自建的
 * `/mc-chat-watch-config` 接口（同 `dsh-meme` 的做法）。
 *
 * 打包成 lazy-CJS 工厂产物：`window.__ModuleLoader__.load({ id, factory })`。
 */
window.__ModuleLoader__.load({
  id: 'dsh-mc-chat-watch',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const h = React.createElement

    /**
     * 可选依赖：DSH 的 UI 组件包（`dsh-thinking-language` 就是这么取的）。
     * 有它就用官方的 `Menu` 做下拉（带 portal 弹层、样式与设置页统一），
     * 拿不到就退回自己画的 `McSelect` —— **不用原生 `<select>`**，
     * 因为展开后的 `<option>` 列表由系统渲染、无法套主题，会跟页面格格不入。
     */
    function optionalRequire(...specifiers) {
      for (const specifier of specifiers) {
        try {
          const resolved = require(specifier)
          if (resolved !== undefined && resolved !== null) return resolved
        } catch { /* 换下一个 */ }
      }
      return undefined
    }

    const primitives = optionalRequire('@deepseek-ai/dsh-client-ui-primitives')
    const Menu = primitives && typeof primitives.Menu === 'function' ? primitives.Menu : null

    const NS = 'mc-chat-watch'
    /** Host 半注册的配置读写接口（见 index.js 的 CONFIG_ROUTE）。 */
    const CONFIG_ROUTE = '/mc-chat-watch-config'

    /**
     * 字段定义：分组 → 字段。
     * type: 'bool' 走胶囊下拉；'number' / 'text' 走输入框（失焦保存）。
     */
    const GROUPS = [
      {
        title: '开关',
        fields: [
          { key: 'enabled', label: '总开关', type: 'bool', help: '关掉后完全不轮询、不投递' },
        ],
      },
      {
        title: '账号',
        hint: '游戏内识别"谁在说话"的依据。改完点一下别处即保存。',
        fields: [
          { key: 'playerName', label: '玩家账号名', placeholder: 'xlhl_world', help: '你（人类）在游戏里的名字，用于跟丢判断和主动说话点名' },
          { key: 'aiAccount', label: 'AI 账号名', placeholder: 'DeepSeek', help: 'AI 自己的账号名。用它排除"自己说的话"和"自己这个实体"' },
        ],
      },
      {
        title: '投递目标',
        hint: '游戏消息投给哪个会话。',
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
        title: '态势播报',
        hint: '定期把世界状态交给 AI 判断要不要动。注意，态势播报会根据会话是否处于活动来判断是否发送，即当会话处于活动状态时不会插话',
        fields: [
          { key: 'observe', label: '启用态势播报', type: 'bool', help: '关掉就只在聊天/受伤时叫醒' },
          { key: 'observeSeconds', label: '采样间隔 (秒)', placeholder: '180', type: 'number', help: '多久看一次世界状态' },
          { key: 'observeMode', label: '播报模式', type: 'enum', options: [
            { value: 'changed', label: 'changed' },
            { value: 'always', label: 'always' },
          ], help: 'changed = 只在情况变化时播报；always = 每次都播（更费 token）' },
          { key: 'minWakeSeconds', label: '两次播报最小间隔 (秒)', placeholder: '30', type: 'number' },
          { key: 'maxWakePerHour', label: '每小时上限', placeholder: '40', type: 'number', help: '防话痨与烧钱' },
        ],
      },
      {
        title: '空闲自主',
        hint: '角色闲着时让 AI 自己找事做。',
        fields: [
          { key: 'autonomy', label: '启用空闲自主', type: 'bool' },
          { key: 'autonomyIdleSeconds', label: '空闲判定阈值 (秒)', placeholder: '120', type: 'number', help: '连续闲满这么久才交一次自主时间' },
          { key: 'autonomyMinGapSeconds', label: '两次自主最小间隔 (秒)', placeholder: '300', type: 'number', help: '花费的主闸门，别设太小' },
          { key: 'autonomyTickSeconds', label: '忙闲检查间隔 (秒)', placeholder: '20', type: 'number' },
          { key: 'autonomyMaxPerHour', label: '每小时上限', placeholder: '6', type: 'number' },
        ],
      },
      {
        title: '受伤回应',
        hint: '挨打时要不要立刻叫醒 AI 让它反击或逃命。受伤回应会使用插话机制，即打断 AI 当前正在进行的任务',
        fields: [
          { key: 'hurtResponse', label: '启用受伤回应', type: 'bool', help: '关掉后受伤只当普通事件攒进态势，不再单独叫醒' },
          { key: 'hurtResponseDelaySeconds', label: '响应延迟 (秒)', placeholder: '0', type: 'number', help: '检测到受伤后等几秒再叫醒；0 = 立即。给连续挨打留一个合并窗口' },
          { key: 'hurtWakeGapSeconds', label: '同场战斗节流 (秒)', placeholder: '60', type: 'number', help: '两次受伤响应的间隔，间隔太短会反复插入对话，频繁打断 AI 正在进行的操作' },
          { key: 'retaliateOnPlayerHurt', label: '被玩家打时回击', type: 'bool', help: '关掉后 AI 被玩家打会只躲不打 —— 双人世界里防止它反过来揍你' },
        ],
      },
    ]

    /**
     * 样式。`.mcw-page` / `.mcw-row` / `.mcw-rowText` / `.mcw-title` / `.mcw-desc`
     * 与 `.mcw-select` 全部取自 `dsh-thinking-language` 的 `.dshtl_*`，只改了前缀。
     */
    const CSS = `
      .mcw-page{max-width:760px;padding-top:20px;flex-direction:column;display:flex;gap:28px}
      .mcw-group{flex-direction:column;display:flex;gap:2px}
      .mcw-groupTitle{color:var(--dsw-alias-label-primary);font-size:15px;font-weight:600;line-height:24px;margin-bottom:2px}
      .mcw-groupHint{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:18px;margin-bottom:10px}
      .mcw-row{align-items:center;gap:8px;display:flex;padding:7px 0}
      .mcw-rowText{flex-direction:column;flex:1;gap:4px;min-width:0;padding-right:48px;display:flex}
      .mcw-title{color:var(--dsw-alias-label-primary);font-size:14px;font-weight:400;line-height:22px}
      .mcw-desc{color:var(--dsw-alias-label-tertiary);font-size:12px;font-weight:400;line-height:18px}
      .mcw-select{background:var(--dsw-alias-bg-module-platform);height:36px;font:inherit;color:var(--dsw-alias-label-primary);cursor:pointer;border:none;border-radius:18px;align-items:center;gap:12px;padding:0 14px;font-size:14px;line-height:22px;display:inline-flex;min-width:200px;outline:none;flex:none}
      .mcw-select:hover{background:var(--dsw-alias-interactive-bg-hover)}
      .mcw-select:disabled{opacity:.5;cursor:default}
      .mcw-input{width:280px;flex:none;box-sizing:border-box;background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-primary);border:1px solid transparent;border-radius:10px;height:36px;padding:0 12px;font:inherit;font-size:14px;outline:none;transition:border-color .12s ease}
      .mcw-input:focus{border-color:var(--dsw-alias-brand-primary)}
      .mcw-input.overridden{border-color:var(--dsw-alias-brand-primary)}
      .mcw-input:disabled{opacity:.5;cursor:default}
      .mcw-bar{display:flex;gap:10px;align-items:center;padding-top:6px}
      .mcw-bar .state{font-size:12px;color:var(--dsw-alias-label-secondary)}
      .mcw-bar .state.err{color:var(--dsw-alias-label-error, #d64545)}
      .mcw-bar .state.ok{color:var(--dsw-alias-label-success, #2e9e5b)}
      .mcw-note{font-size:12px;color:var(--dsw-alias-label-tertiary);line-height:18px}
      /* 自建下拉：原生 <option> 列表由系统渲染，套不上主题，所以自己画 */
      .mcw-dd{position:relative;flex:none;min-width:200px}
      .mcw-dd-btn{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-primary);border:none;border-radius:18px;height:36px;padding:0 14px;font:inherit;font-size:14px;line-height:22px;cursor:pointer;outline:none}
      .mcw-dd-btn:hover{background:var(--dsw-alias-interactive-bg-hover)}
      .mcw-dd-btn:disabled{opacity:.5;cursor:default}
      .mcw-dd-btn[data-open="true"]{background:var(--dsw-alias-interactive-bg-hover)}
      .mcw-dd-arrow{flex:none;font-size:10px;opacity:.6;transition:transform .12s ease}
      .mcw-dd-btn[data-open="true"] .mcw-dd-arrow{transform:rotate(180deg)}
      .mcw-dd-menu{position:absolute;right:0;top:calc(100% + 6px);z-index:40;min-width:100%;padding:6px;background:var(--dsw-alias-bg-layer-2, var(--dsw-alias-bg-base));border:1px solid var(--dsw-alias-border-l1);border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.18);display:flex;flex-direction:column;gap:2px}
      .mcw-dd-item{background:transparent;border:none;border-radius:8px;height:32px;padding:0 12px;font:inherit;font-size:14px;line-height:22px;text-align:left;color:var(--dsw-alias-label-primary);cursor:pointer;white-space:nowrap}
      .mcw-dd-item:hover{background:var(--dsw-alias-interactive-bg-hover)}
      .mcw-dd-item[data-active="true"]{color:var(--dsw-alias-brand-primary);font-weight:600}
      /* 保存提示：右下角浮层，1.8 秒后自动消失 */
      .mcw-toast{position:fixed;right:24px;bottom:24px;z-index:9999;padding:10px 16px;border-radius:10px;font-size:13px;line-height:20px;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.2);pointer-events:none}
      .mcw-toast.ok{background:var(--dsw-alias-label-success, #2e9e5b)}
      .mcw-toast.err{background:var(--dsw-alias-label-error, #d64545)}
    `

    /** 把数值型草稿安全转成配置值。 */
    function coerce(field, raw) {
      if (field.type !== 'number') return raw
      if (raw === '') return null
      const n = Number(raw)
      return Number.isFinite(n) ? n : null
    }

    /** 配置值 → 输入框显示的字符串。 */
    function asText(value) {
      return value === undefined || value === null ? '' : String(value)
    }

    /**
     * 通用的胶囊下拉。
     *
     * 只用官方的 `primitives.Menu`（参数照 `dsh-thinking-language` 的用法：
     * `open / onClose / items / selectedId / onSelect / align / portal / anchor`）。
     * **不用原生 `<select>`** —— 它展开后的 `<option>` 列表由系统渲染，套不上主题。
     * 万一 Menu 取不到，就渲染成只读按钮而不是退回原生 select。
     *
     * `options` 是 `[{ value, label }]`，所以开关（开/关）和枚举（changed/always）共用它。
     */
    function MenuSelect(props) {
      const { value, options, onChange, disabled, placeholder } = props
      const [open, setOpen] = React.useState(false)
      const current = options.find((item) => item.value === value)
      // 🔴 Menu 的 items 字段名是 `id` 而不是 `value` —— 传错会让 onSelect 收到
      // undefined，表现就是"开/关两个选项都变成关"（2026-10-22 用户实测）。
      const items = options.map((item) => ({ id: item.value, label: item.label }))

      const anchor = h('button', {
        type: 'button',
        className: 'mcw-dd-btn',
        'data-open': open ? 'true' : 'false',
        disabled,
        onClick: () => setOpen((prev) => !prev),
      },
        h('span', null, current ? current.label : (placeholder || String(value ?? ''))),
        h('span', { className: 'mcw-dd-arrow' }, '▾'))

      if (!Menu) {
        // 组件包不可用：只显示当前值，不给假的下拉
        return h('div', { className: 'mcw-dd' }, anchor)
      }

      return h(Menu, {
        open,
        onClose: () => setOpen(false),
        items,
        selectedId: value,
        onSelect: (id) => { onChange(id); setOpen(false) },
        align: 'end',
        portal: true,
        anchor,
      })
    }

    function McSettingsPage() {
      // cfg=服务端当前生效配置，overrides=settings.json 里被页面改过的键
      const [cfg, setCfg] = React.useState(null)
      const [overrides, setOverrides] = React.useState({})
      // 输入框草稿：只在 blur / 回车时提交（照 meow-memory 的做法）
      const [drafts, setDrafts] = React.useState({})
      const [fatal, setFatal] = React.useState(null)
      const [message, setMessage] = React.useState(null)

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

      /** 写一个键（`null` 表示取消覆盖、回落到 cordis config 的值）。 */
      const commit = React.useCallback(async (key, value) => {
        try {
          const res = await fetch(CONFIG_ROUTE, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ [key]: value }),
          })
          const data = await res.json().catch(() => ({}))
          if (!res.ok || data.ok === false) throw new Error(data.error || `HTTP ${res.status}`)
          setCfg(data.config || {})
          setOverrides(data.overrides || {})
          // 明确反馈：右下角浮出提示，1.8 秒后自动消失
          setMessage({ kind: 'ok', text: '已保存' })
        } catch (error) {
          setMessage({ kind: 'err', text: `保存失败：${String(error && error.message ? error.message : error)}` })
        }
      }, [])

      // 提示自动消失
      React.useEffect(() => {
        if (!message) return undefined
        const timer = setTimeout(() => setMessage(null), 1800)
        return () => clearTimeout(timer)
      }, [message])

      if (fatal) {
        return h('div', { className: 'mcw-page' },
          h('div', { className: 'mcw-note' }, `读不到插件配置：${fatal}`),
          h('div', { className: 'mcw-note' }, 'Host 半可能没加载，或 webServer 服务不可用。'))
      }
      if (cfg === null) {
        return h('div', { className: 'mcw-page' }, h('div', { className: 'mcw-note' }, '正在读取配置…'))
      }

      const renderControl = (field) => {
        const saved = cfg[field.key]
        const overridden = Object.prototype.hasOwnProperty.call(overrides, field.key)

        if (field.type === 'bool') {
          const on = saved === true || saved === 'true'
          return h(MenuSelect, {
            value: on ? 'on' : 'off',
            options: [{ value: 'on', label: '开' }, { value: 'off', label: '关' }],
            onChange: (next) => commit(field.key, next === 'on'),
          })
        }

        // 枚举：与开关同一套下拉，只是选项来自字段定义
        if (field.type === 'enum') {
          const options = Array.isArray(field.options) ? field.options : []
          return h(MenuSelect, {
            value: saved === undefined || saved === null ? '' : String(saved),
            options,
            placeholder: '未设置',
            onChange: (next) => commit(field.key, next),
          })
        }

        // 草稿优先，未编辑过的显示服务端当前值
        const draft = Object.prototype.hasOwnProperty.call(drafts, field.key)
          ? drafts[field.key]
          : asText(saved)
        return h('input', {
          className: overridden ? 'mcw-input overridden' : 'mcw-input',
          type: field.type === 'number' ? 'number' : 'text',
          value: draft,
          placeholder: field.placeholder || '',
          onChange: (e) => setDrafts((prev) => ({ ...prev, [field.key]: e.target.value })),
          onBlur: (e) => {
            const next = coerce(field, e.target.value)
            // 没变就不发请求
            if (next !== null && String(next) === asText(saved)) {
              setDrafts((prev) => { const copy = { ...prev }; delete copy[field.key]; return copy })
              return
            }
            if (next === null && asText(saved) === '') {
              setDrafts((prev) => { const copy = { ...prev }; delete copy[field.key]; return copy })
              return
            }
            commit(field.key, next)
            setDrafts((prev) => { const copy = { ...prev }; delete copy[field.key]; return copy })
          },
          onKeyDown: (e) => { if (e.key === 'Enter') e.target.blur() },
        })
      }

      const groups = GROUPS.map((group) => h('div', { className: 'mcw-group', key: group.title },
        h('div', { className: 'mcw-groupTitle' }, group.title),
        group.hint ? h('div', { className: 'mcw-groupHint' }, group.hint) : null,
        group.fields.map((field) => h('div', { className: 'mcw-row', key: field.key },
          h('div', { className: 'mcw-rowText' },
            h('div', { className: 'mcw-title' }, field.label),
            field.help ? h('div', { className: 'mcw-desc' }, field.help) : null),
          renderControl(field)))))

      return h(React.Fragment, null,
        h('div', { className: 'mcw-page' },
          groups,
          h('div', { className: 'mcw-bar' },
            h('span', { className: 'state' }, '输入框失焦或回车即保存，无需重启 DSH'))),
        // 保存反馈：右下角浮层，1.8 秒后自动消失
        message ? h('div', { className: `mcw-toast ${message.kind}` }, message.text) : null)
    }

    exports.name = 'dsh-mc-chat-watch'

    /**
     * 只向 `settings.section` 槽注册页面。
     *
     * 🔴 `exports.inject` 里**绝不能**出现可选服务 —— cordis 的插件级 inject 语义是
     * "声明的服务不出现就永久 pending"，会把整轮 web boot 拖垮（2026-10-21 白屏事故）。
     * 这里只需要 `slots`，它启动早期必定存在。
     */
    exports.inject = ['slots']

    function apply(ctx) {
      const slots = ctx && ctx.slots
      if (!slots || typeof slots.inject !== 'function') return
      slots.inject('settings.section', () => slots.register(
        { name: 'settings.section', id: NS, order: 30, label: 'Minecraft 陪玩' },
        McSettingsPage))
    }

    exports.apply = apply
    return module.exports
  },
})
