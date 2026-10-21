/**
 * dsh-mc-chat-watch — 设置页（浏览器半）。
 *
 * 与 Host 半（index.js）用同一个命名空间 'mc-chat-watch' 配对：
 * Host 注册 `ctx.settings.installSection(...)`，这里向 `settings.section` 槽注册
 * 一张独立的一级设置页，表单读写 `ctx.settingsScope.bind({ namespace })`。
 *
 * 打包成加载器要的 lazy-CJS 工厂产物：`window.__ModuleLoader__.load({ id, factory })`。
 * id 必须与 loader entry 名一致，否则 ModuleLoader 会报 "loaded without registering"。
 *
 * 配置改完**立即生效**（Host 半的 onChange 会把新值同步回运行期），不用重启 DSH。
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
      .mcw-row{display:flex;flex-direction:column;gap:4px}
      .mcw-row label{font-size:12px;color:var(--dsw-alias-label-secondary)}
      .mcw-row .help{font-size:11px;color:var(--dsw-alias-label-secondary);opacity:.75;line-height:1.45}
      .mcw-row input[type=text],.mcw-row input[type=number]{background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:7px 10px;font-size:13px;outline:none;transition:border-color .12s ease}
      .mcw-row input:focus{border-color:var(--dsw-alias-brand-primary)}
      .mcw-row input.overridden{border-color:var(--dsw-alias-brand-primary)}
      .mcw-inline{display:flex;gap:8px;align-items:center}
      .mcw-inline button{background:var(--dsw-alias-bg-layer-2);color:var(--dsw-alias-label-primary);border:1px solid var(--dsw-alias-border-l1);border-radius:8px;padding:5px 10px;font-size:12px;cursor:pointer}
      .mcw-inline button:hover{border-color:var(--dsw-alias-brand-primary)}
      .mcw-inline button:disabled{opacity:.45;cursor:default}
      .mcw-flag{font-size:11px;color:var(--dsw-alias-brand-primary);white-space:nowrap}
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
      const display = isBool
        ? (raw ? '开' : '关')
        : (raw === undefined || raw === null ? '' : String(raw))

      return h('div', { className: 'mcw-row' },
        h('label', { htmlFor: `mcw-${field.key}` }, field.label),
        h('div', { className: 'mcw-inline' },
          isBool
            ? h('button', {
                id: `mcw-${field.key}`,
                disabled: busy,
                onClick: () => onSet(field.key, !raw),
              }, display)
            : h('input', {
                id: `mcw-${field.key}`,
                type: field.type === 'number' ? 'number' : 'text',
                className: overridden ? 'overridden' : '',
                value: display,
                placeholder: field.placeholder || '',
                disabled: busy,
                onChange: (e) => onSet(field.key, field.type === 'number' ? Number(e.target.value) : e.target.value),
              }),
          overridden
            ? h(React.Fragment, null,
                h('span', { className: 'mcw-flag' }, '已覆盖'),
                h('button', { disabled: busy, onClick: () => onUnset(field.key) }, '恢复默认'))
            : null),
        field.help ? h('span', { className: 'help' }, field.help) : null)
    }

    function McSettingsPage(props) {
      // form 是设置作用域（两代 harness 名字不同、接口一致），由 mountPage 注入
      const form = props && props.form ? props.form : null

      /** 读快照。0.2.x 叫 getSnapshot()，个别形态只有 get()。 */
      const readSnapshot = () => {
        try {
          if (!form) return null
          if (typeof form.getSnapshot === 'function') return form.getSnapshot()
          if (typeof form.get === 'function') return form.get()
          return null
        } catch { return null }
      }

      const [snapshot, setSnapshot] = React.useState(readSnapshot)
      const [busy, setBusy] = React.useState(false)
      const [message, setMessage] = React.useState(null)

      // 订阅作用域变化：别处（另一个窗口/另一个用户）改了也能同步过来
      React.useEffect(() => {
        if (!form || typeof form.subscribe !== 'function') return undefined
        try {
          return form.subscribe(() => setSnapshot(readSnapshot()))
        } catch { return undefined }
      }, [form])

      const reload = React.useCallback(() => {
        setSnapshot(readSnapshot())
      }, [form])

      const apply = React.useCallback(async (fn, label) => {
        if (!form) return
        setBusy(true)
        setMessage(null)
        try {
          await fn()
          reload()
          setMessage({ kind: 'ok', text: `${label}已保存` })
        } catch (error) {
          setMessage({ kind: 'err', text: `${label}保存失败：${String(error && error.message ? error.message : error)}` })
        } finally {
          setBusy(false)
        }
      }, [form, reload])

      const onSet = (key, value) => apply(() => form.set(key, value), '配置')
      const onUnset = (key) => apply(() => form.unset(key), '配置')

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

      // 诊断条：排查"拿不到表单"时唯一的线索来源（打开设置页就能看见）
      const diagBar = (props.diagnostics && props.diagnostics.length)
        ? h('details', { className: 'mcw-diag' },
            h('summary', null, `诊断（${props.diagnostics.length} 条，点开看）`),
            props.diagnostics.map((line, i) => h('div', { key: i, className: 'mcw-diagline' }, line)))
        : null

      if (!form) {
        return h('div', { className: 'mcw-panel' },
          h('p', { className: 'hint' },
            '这个 DSH 版本没有把设置表单接口交给插件（configForms / settingsScope 都不可用），页面暂时只能读不能改。',
            '可以先改 cordis.patch.yml 里的 config，或在游戏里用「态势 开/关」「自主 开/关」。'),
          diagBar)
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
     * 已经挂过一次页面了吗。
     *
     * 两代 harness 的服务名不同，两个分支都装、谁先被服务就用谁；一旦装好，
     * 另一支必须让位，否则同一页会被注册两次。
     */
    let pageMounted = false
    /** 诊断记录：显示在页面顶部，便于在没有控制台的情况下看清拿到了什么。 */
    let diagnostics = []

    function note(text) {
      diagnostics.push(`${new Date().toLocaleTimeString()} ${text}`)
      if (diagnostics.length > 12) diagnostics.shift()
      // 同时打到控制台：这样不打开设置页、只看页面加载后的 console 就能确认
      // apply 拿到了哪个 transport、form 有没有（截图工具会把它当"控制台错误"报出来）。
      // 定位完可以删掉这一行。
      try { console.error(`[mc-chat-watch] ${text}`) } catch { /* 忽略 */ }
    }

    /**
     * 把页面挂进 settings.section 槽。
     *
     * `form` 是设置作用域对象（两代名字不同，接口一致）：
     *   getSnapshot() / subscribe(fn) / set(field, value) / unset(field)
     *
     * ⚠️ 即使拿不到 form 也照样注册页面 —— 否则页面上什么都看不到，排查时只能猜。
     * 页面会在顶部把诊断写出来（拿到了哪个服务、form 有没有）。
     */
    function mountPage(ctx, form, source) {
      if (pageMounted) return
      const slots = ctx && ctx.slots
      if (!slots || typeof slots.inject !== 'function') {
        note('ctx.slots 不可用，无法注册页面')
        return
      }
      pageMounted = true
      note(form ? `已挂载页面（来源 ${source}）` : `页面已挂载但拿不到 form（来源 ${source}）`)
      slots.inject('settings.section', () => slots.register(
        { name: 'settings.section', id: NS, order: 30, label: 'Minecraft 陪玩' },
        (props) => h(McSettingsPage, Object.assign({}, props, { form, diagnostics }))))
    }

    /**
     * 装两套 transport 分支。
     *
     * ⚠️ 这里的 `ctx.inject([...], cb)` 与 `exports.inject` 是**两回事**：
     *   - `exports.inject` 是**插件级**依赖，不满足 → 整个插件永久 pending → web boot 挂；
     *   - `apply` 内部的 `ctx.inject` 是**子作用域**依赖，不满足 → 只是这个回调不执行。
     * 所以可选服务一律用后者（2026-10-21 踩过：把 settingsScope 写进 exports.inject，
     * Web GUI 直接白屏 "web boot: 1 entry did not activate"）。
     *
     * 服务名随 harness 世代变化（dsh 0.2.x = `configForms`，0.1.x = `settingsScope`），
     * 一个 harness 只会提供其中一种，所以两套都装上、先到的赢。
     */
    function apply(ctx) {
      if (!ctx || typeof ctx.inject !== 'function') {
        note('ctx.inject 不可用')
        return
      }

      // dsh 0.2.x：configForms 按命名空间取表单
      ctx.inject(['configForms', 'locale'], (pageCtx) => {
        try {
          const forms = pageCtx.configForms || (typeof pageCtx.get === 'function' ? pageCtx.get('configForms') : null)
          if (!forms) { note('configForms 分支：服务为空'); return }
          const form = typeof forms.get === 'function' ? forms.get(NS) : (forms[NS] || forms)
          note(`configForms 可用（${typeof forms.get === 'function' ? 'get()' : '对象'}），namespace ${NS} → ${form ? '有表单' : '无表单'}`)
          mountPage(ctx, form, 'configForms')
        } catch (error) {
          note(`configForms 分支异常：${error && error.message ? error.message : error}`)
        }
      })

      // dsh 0.1.x：settingsScope 需要 bind 出命名空间作用域
      ctx.inject(['settingsScope', 'locale'], (pageCtx) => {
        try {
          const svc = pageCtx.settingsScope || (typeof pageCtx.get === 'function' ? pageCtx.get('settingsScope') : null)
          if (!svc) { note('settingsScope 分支：服务为空'); return }
          const form = typeof svc.bind === 'function' ? svc.bind({ namespace: NS }) : svc
          note(`settingsScope 可用（${typeof svc.bind === 'function' ? 'bind()' : '直接对象'}）→ ${form ? '有作用域' : '无作用域'}`)
          mountPage(ctx, form, 'settingsScope')
        } catch (error) {
          note(`settingsScope 分支异常：${error && error.message ? error.message : error}`)
        }
      })
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
