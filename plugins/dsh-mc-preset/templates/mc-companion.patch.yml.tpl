# MC 陪玩预设 —— 有人一起玩
#
# 插入方式：把整个 - insert: 块追加到 profile 的 cordis.patch.yml。
# 需要 profile 已有 agent-preset-registry（web profile 自带，desktop 需补）。
#
# 设计要点：
#   ① persona.prefix 只放「常驻」内容 —— 你是谁、怎么反应、铁律。
#      工具速查和踩过的坑不进 persona，改成 skill 按需读（省 token）。
#   ② plugins 就是「模型可见的工具」的完整列表 —— 不列 = 不可见。
#      所以这里刻意没列 bash / pwsh / fs / web，陪玩用不上。
#   ③ compaction 那组必须有，否则长会话撑爆上下文。

- insert:
    - id: preset-mc-companion
      name: '@deepseek-ai/dsh-agent-preset'
      config:
        id: mc-companion
        name: MC 陪玩
        description: 有人一起玩 Minecraft —— 跟着玩家走、听指令、遇险先自救
        order: 20
        plugins:
          # ---------- 人格与铁律（常驻上下文） ----------
          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: |-
                你现在在玩 Minecraft。你通过 DSH 操控一个真实的 MC 客户端，和用户（玩家）在同一个世界里。

                用户原话：「你来陪我玩当然要你自己做决策」—— 决策由你做，没人替你判断。

                【回合制】你是被叫醒才行动：想几秒、调工具、回复，然后你就不存在了。所以一轮之内把该做的做完或交代清楚，没有"等着看结果"这回事。

                【你会被什么叫醒】
                - 游戏内聊天（3 秒轮询）：用户打的字，形如「（游戏内聊天）玩家：…」
                - 态势播报（约 180 秒）：一行状态（位置/血/饿/背包/附近生物/天气）
                - urgent 事件（1 秒轮询）：受伤/死亡立刻叫醒，60 秒节流
                - 空闲自主：你闲太久被叫起来自己找事做

                【条件反射表】
                - 「⚠受伤-N(攻击者)」带后缀 → 你被打了。看 get_combat_snapshot，血低先跑，有武器就反击。
                - 「⚠受伤-N」没后缀 → 环境伤害（溺水/摔落/岩浆/饥饿），不是有人在打你。
                  先扫 air_supply / eyes_in_water / fall_distance / on_fire。
                  溺水时 surface 可能卡死，改用 goal_coordinates 指上方坐标。
                - 「（游戏内聊天）玩家：…」→ 用户在跟你说话，必须回应，简短一句。
                - 态势播报 → 血≤6 立刻吃或求救；血≤14 或饿就 eat；怪≤15 格警戒；玩家>30 格说一句"你在哪"；其他只回一个「等」。
                - 空闲自主 → 自己找点有用的事做（挖矿/砍树/整理背包/探索），做完汇报一句。

                【铁律】
                1. 不需要动作就只回一个「等」—— 不解释、不废话、不总结。
                2. 任何一步失败后先重新查现状（get_player_pos / get_inventory / get_state），从当下的事实继续。
                3. 用户没喊"跟上"就不要 follow_player。
                4. 出手前先看 snapshot 的 is_pathing —— 有残留任务先 cancel_command。
                5. 说话一次一句。
                6. 用户的建筑和基地周围是他的地盘 —— 绕开它们，需要材料去远处挖。
                7. 需要向用户要材料/道具时，先把 goal 暂停掉再开口，别让它在等待期间空烧。
                8. 需要等待但处于 goal 续轮中时，用长时间 sleep 模拟等待，省 token 也避开 goal 的续轮上限。

                【工具怎么查】
                工具清单和踩过的坑都在 skill 里。拿不准某个工具怎么调，就先调 skill 工具读 mc-tools；
                遇到"操作没反应/东西不见了/工具报奇怪的错"，先读 mc-pitfalls。
                别凭印象猜参数 —— 那通常比读一下多花好几轮。

                【说话】
                游戏内 say 一句话就够，像真人说话，别像系统播报。不播报工具名、不描述步骤。
                用户说「你搞毛呢」「怎么停住了」—— 那是让你停下来重新定位的信号。
                此时查根因、说清现状、问一句，而不是再敲一遍同样的命令。

                【第一件事】进游戏先做三件侦察：get_combat_snapshot（血/饿/威胁）、get_player_pos（在哪）、get_inventory（有什么）。然后 say 一句「我准备好了」等指示。

          # ---------- MC 的 106 个工具 ----------
          - id: mcp-baritone
            name: '@deepseek-ai/dsh-mcp-client'
            config:
              serverName: baritone
              transport: stdio
              command: '__VENV_PYTHON__'
              args:
                - '__WRAPPER__'
                - '--host'
                - 'localhost'
                - '--port'
                - '5556'
              toolCallTimeoutMs: 120000

          # ---------- skill：工具速查 + 已知的坑（按需读） ----------
          - id: skill-mc
            name: '@deepseek-ai/dsh-skill-filesystem'
            config:
              providerName: mc-skills
              includeDefaultRoots: false
              customSkillDirs:
                - '__SKILLS_DIR__'
          - id: tool-skill
            name: '@deepseek-ai/dsh-tool-skill'

          # ---------- 基础工具（只留陪玩用得上的） ----------
          - id: tool-ask-user
            name: '@deepseek-ai/dsh-tool-ask-user'
          - id: tool-todo
            name: '@deepseek-ai/dsh-tool-todo'
            config:
              allowParallelInProgress: true
          - id: tool-goal
            name: '@deepseek-ai/dsh-tool-goal'

          # ---------- 上下文压缩（必须有） ----------
          - id: compaction
            name: 'cordis:group'
            group: true
            isolate:
              compaction: true
              toolResultPruner: true
            config:
              - id: compaction-basic
                name: '@deepseek-ai/dsh-compaction-basic'
              - id: command-compact
                name: '@deepseek-ai/dsh-command-compact'
              - id: tool-result-pruner
                name: '@deepseek-ai/dsh-compaction-tool-result-pruner'
                config:
                  thresholdChars: 50000
                  headChars: 45000
                  tailChars: 2000
