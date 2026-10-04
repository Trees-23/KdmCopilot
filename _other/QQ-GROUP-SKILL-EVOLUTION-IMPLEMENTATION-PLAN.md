# QQ 群内 Skill 进化、主动通知与人工发布实施方案

> 文档状态：分阶段实施中
>
> 更新时间：2026-10-04
>
> 范围：在已完成的五层记忆 Phase 0～6 之上，为当前 nanobot 的官方 QQ 群机器人增加“自动评审 → 主动推送 → 群内交互确认 → 仅对个人部署生效的受控升级/发布”闭环。
>
> 当前运行态已打开 M4/M5；长期运行态的 workspace 采用、Draft PR 自动衔接和发布仍由独立开关控制。个人 Overlay 只允许自动创建 Draft PR，最终合并/部署必须由 QQ 群内管理员执行第二次确认。本文同步记录实施与验收状态，不代表自动发布已授权。
>
> 2026-09-28 部署策略调整：个人 Overlay 发布改为运行时热加载。合并后的
> `skills/*/SKILL.md` 通过原子写入同步到当前 Gateway 挂载的 workspace，下一轮
> Agent Context 构建自动读取新版本；发布不再重建或重启 Gateway，也不会主动断开
> WebUI/QQ 连接。

## 当前实施状态

截至 2026-10-04，M0～M10 已完成既定接入，M10-C 连续稳态观察仍按原计划保留；M11“Proposal 可审阅透明化”已完成代码与 QQ 主审阅路径验收，完整新 Proposal 的案例/diff 观察仍待下一轮候选。2026-09-30 的三条自动候选暴露出“运行事件指纹被误判为业务任务”的质量缺口；M12“候选语义质量门禁”已完成首版实现并保持自动运行，最近两次 14:00 扫描均正常完成但没有合格候选。M13 已能记录失败→纠正→恢复的脱敏 Case，但尚未形成面向人工评论的 Issue 闭环；本次新增 M16 设计承接该闭环。M9 的真实私有 Overlay 发布窗口已完成临时验收并回滚，长期运行态已恢复 `publishEnabled=false`。M4/M5 已按用户确认完成加速验收并开启对应运行态：

- 已加入独立的 `shadow_mode`、主动通知、采用和发布开关；当前运行态为 `phase6.enabled=true`、通知开启、采用/发布关闭。
- 已确认 Proposal 确认码有效期为 12 小时（720 分钟），每群每日主动通知上限为 12 条。
- 已加入 `0002_phase6_proposals` 数据库迁移，覆盖 Proposal、通知投递、管理员动作和群范围四类持久化对象。
- 已实现 Proposal 状态转移、版本 CAS、确认码哈希与过期、管理员动作幂等、通知去重和群范围写入的本地仓储。
- 已将现有 `make_proposal()` 接入可选持久化入口 `persist_proposal()`；它只写入 `eligible_for_confirmation`，不会采用 Skill、创建 PR 或发布。
- 已完成相关单元测试、配置测试、memory 测试和全量 pytest；M3 命令、通知器和真实 QQ 验收已完成。
- 已完成显式调用的离线编排器：workspace lease、Trace 低风险筛选、失败 Case、EvalPack、独立 fixture 回放、Gate 和 Proposal 持久化；Phase 6 关闭时无副作用。
- 已完成持久化的显式调度状态、群通知配额、Delivery claim/retry/dead-letter 状态；尚未接入 QQ 投递器和 dead-letter 主动告警。
- 长期 Gateway 已按当前代码重建：`git-a3a2cea7e96d`，镜像 `sha256:10c55a99db7fdcdfc03e1885139fc3bd2425afcfa143ac104d034a46f97814fa`，健康检查返回 `status=ok`，容器挂载仍为仓库 `runtime/`；运行配置保持采用/发布关闭。
- 已完成原 M8 本地发布门禁（现归入新 M9 前置能力）：二次确认、CI 条件、CAS、kill switch 和受控回调均已测试；真实远端发布尚未开启。
- M9 本地端到端模拟验收已完成：二次确认 → CI → 私有 Overlay 合并回调 → 当前 Gateway 热加载回调；自动 handoff 已覆盖 Gate → Proposal → Draft PR → 发布候选通知。真实临时发布、热加载验证和回滚已完成。
- 2026-09-28 已将私有 Overlay 的发布回调接入 `AgentLoop`：配置 `overlayRepository` 后，Gateway 启动时自动注入受控 GitHub 校验、合并和 Skill 热加载回调；镜像内已安装 `gh`，构建引用为 `git-6906f5c6be02`，健康检查通过。当前容器尚未配置 GitHub Token，因此发布窗口前仍需补充受控凭据。
- 2026-09-28 真实二次确认首次执行时发现 Fine-grained Token 对 GraphQL `statusCheckRollup` 返回 403；已改为按 PR head SHA 查询 GitHub Actions REST workflow runs，避免扩大权限范围。原 Proposal/确认码保持有效，待修复部署后继续。
- 2026-09-28 M9 真实发布窗口已完成：私有 Overlay PR #5 CI 通过，QQ 群二次确认成功，PR 合并提交为 `dbcad656a5d43fb506936ba27758135e0130f39b`；当前 Gateway 未因发布重启，`SkillsLoader` 已读到临时 Skill，随后删除临时 Skill、记录 `rolled_back` 审计并关闭 `publishEnabled`。发布后为关闭开关而进行的一次配置重载不属于 Skill 发布重启。
- 2026-09-28 M10 首批运营能力已接入：新增无载荷周报汇总（候选数、Gate 通过率、通知失败率、拒绝/批准、采用/回滚及误报原因），并将 dead-letter、通知失败率、越权尝试和跨群泄露统一接入自动暂停门禁；暂停原因通过注入式通知适配器发送，默认不改变长期开关。聚焦场景测试已通过，7 天连续稳态观察尚未完成。
- 2026-09-28 已确认 M10 自动触发策略：复用 Gateway 内置 Cron 注册受保护的系统任务，每天北京时间 14:00 执行一次；最低重复次数为 3 次；错过执行时间不补跑；允许自动创建私有 Overlay Draft PR，CI 通过后只向 QQ 群通知，合并/发布仍需群内二次确认。
- 当前长期工作区最近 7 天只读快照：候选 1、Gate 通过率 100%、通知失败率 0%、dead-letter 0、采用/回滚 0/1；该快照仅作 M10 起始基线，不替代连续 7 天观察。
- 2026-09-28 M8 隔离实测创建临时 Overlay PR #2，确认 Gate → Proposal → Draft PR 成功，但因 CI 工作流不在 Overlay `main` 而无检查；PR #2 已关闭并清理。随后 CI 基线 PR #3 已合并；新临时 PR #4 的 `validate` 成功，CI 回写和候选通知幂等均已验证，PR #4 已关闭并清理。最后使用长期 Gateway 向真实 QQ 测试群投递临时 `pr_created` Proposal，Delivery 为 `sent`，通知内容正确使用 `/evolve publish`，测试 Proposal/Delivery/审计已清理。
- 2026-09-27 已完成一次长期 Gateway 的真实只读 WebUI 场景验收：新会话 `#/chat/websocket%3Afe7fdbdc-5367-476a-bb64-ae140fdb5cf7`，Trace `01a0e491-472a-7713-80ee-19f404d9599d`，场景标识 `[M9-REAL-READONLY-20260927-201639Z]`。Agent 仅执行运行态/定时任务/工作区和配置读取，Trace 终态为成功（29 节点、2187 Event；含一次已恢复的读取失败警告），明确返回 `publish_enabled=false`、采用关闭、Overlay 白名单为空，且未创建/批准/发布 Proposal、未外发 QQ、未重启 Gateway。

M0 的目标群、审批管理员、@要求、Proposal 有效期和每日通知上限已确认并写入运行态；真实 openid 不写入 Git。通知时段暂按全天处理（当前还未实现时间窗限制）。公共 `Trees-23/KdmCopilot:main` 只承载通用框架，不作为个人 Skill 进化的发布目标；个人 Skill 发布目标改为独立的私有 Overlay 仓库。M4/M5 已完成加速验收，后续仍不得越过人工门禁。

## 1. 决策摘要

目标闭环如下（Overlay 与 workspace 是两条不同的权限路径）：

```text
群内任务与运行 Trace
        ↓
低风险候选筛选、Case 派生、Skill 候选暂存
        ↓
独立回放与自动评审 Gate
        ↓
持久化 Proposal（只读、待确认）
        ├──────────────────────────────────────────────┐
        │ workspace Skill                                  │ 私有 Overlay Skill
        ↓                                                   ↓
官方 QQ 群通知 + 管理员批准                         自动创建 Draft PR
        ↓                                                   ↓
workspace 原子采用/回滚                         CI 通过后 QQ 发布候选通知
                                                            ↓
                                                     管理员群内二次确认
                                                            ↓
                                                  合并私有 Overlay + 部署当前 Gateway
```

核心结论：自动评审和 Overlay Draft PR 可以自动化；正式 workspace 采用、Overlay 合并和 Gateway 部署不可自动化。Overlay 的自动动作只到 `pr_created`，最终动作必须由管理员在群内执行 `/evolve publish` 二次确认。评测通过不等于当前 Skill 已切换。

隔离原则：公共主分支与个人进化分离。Draft PR 本身不会改变 `main`，但任何合并到公共 `main` 的个性化 Skill 都会影响所有下游用户，因此本方案禁止把个人 Skill 合并到公共 `Trees-23/KdmCopilot:main`。个人 Skill 只能进入个人/私有 Overlay 仓库，并只部署到当前用户的 Gateway。

当前执行位置：M0～M9 已通过并记录验收证据；M10 稳态运行与运营复盘尚未开始。长期运行态保持 `publish_enabled=false`。

首版以 QQ 文本命令交互，不依赖内联按钮。当前 QQ 通道能发送 plain/markdown 文本，但尚未实现 `OutboundMessage.buttons` 的 QQ 渲染与交互回调；文本命令在 C2C 与群聊中都更稳定、可审计、可回放。

## 2. 当前基线与问题

### 2.1 已存在并可复用的能力

| 能力 | 当前实现 | 在本方案中的作用 |
|---|---|---|
| Trace、审计与脱敏 | Audit JSONL 为事实源，SQLite `trace_index` 为查询索引 | 候选来源、通知证据、确认审计 |
| Case/Skill revision/EvalPack/EvalRun | 五层记忆 Phase 2～5 | 候选暂存、评测、版本比较与回滚依据 |
| Phase 6 低风险筛选 | 只选择重复成功、只读工具、非敏感任务；默认关闭 | 自动化候选入口与回归保护 |
| Gate | holdout、安全、成本、延迟门槛 | 得出 `eligible_for_confirmation` |
| QQ 官方通道 | 支持 C2C、群消息和主动文本发送 | 群内通知、审阅和确认入口 |
| Slash Command Router | `CommandRouter`/`builtin.py` | 受控的 `/evolve ...` 指令，而非 LLM 自由解释 |
| ChannelManager + MessageBus | 统一出站、重试、投递审计 | 系统通知的可靠投递 |
| SkillsLoader | 每次构建新 Context 时重新读取 `SKILL.md` | workspace Skill 下一轮生效，不需要重启 |

### 2.2 当前仍需接通或验收的部分

1. 真实运行中的周期调度仍保持显式调用，避免在未完成观察前自行扩大扫描范围。
2. M8 的真实远端 Draft PR、CI 回写和真实 QQ 候选通知均已通过；临时 Proposal 已清理。
3. `/evolve publish` 真实群内二次确认尚未在 `publish_enabled=true` 的窗口执行；长期开关必须保持关闭。
4. M9 真实发布窗口已完成：Overlay 合并、热加载、下一轮 Context 读取、回滚和审计证据均已核对。
5. 官方 QQ 当前配置使用 `allowFrom: ["*"]`。这只表示所有成员可向 Agent 发消息，绝不能当作升级审批权限。

### 2.3 已确认的部署事实

- 长期 Gateway 运行在 `nanobot-gateway`，挂载仓库的 `runtime/` 到容器内运行根目录。
- 当前启用的是官方 `qq` 通道，不是 NapCat；配置未显式设置 `phase6`，因此仍使用默认关闭状态。
- 官方 QQ 通道源码有群收发路径，群消息路由依赖 QQ 的 `group_openid` 与出站元数据 `qq_chat_type=group`。
- 当前配置文件不保存唯一目标群标识；上线前必须由管理员明确配置允许感知与允许通知的群。
- 个人 Skill Overlay 已创建为 `Trees-23/KdmCopilot-skills-private`，默认分支为 `main`；`main` 已配置保护，部署来源固定为当前 `nanobot-gateway`。

#### 生效目录与候选 Overlay 的边界

为避免把“已经上线的 Skill”与“等待评审/发布的 Skill”混为一谈，运行目录统一使用下面的命名：

- `runtime/workspace/skills/`：当前 Agent 的**生效 Skill 目录**。`SkillsLoader` 每次构建新 Context 时从这里读取 `SKILL.md`，因此只有这里的文件会影响当前 Agent。
- `runtime/skill-evolution-overlay/`：个人私有仓库 `Trees-23/KdmCopilot-skills-private` 的**候选 Overlay 工作副本**。它只用于生成 Draft PR、等待 CI、合并后作为发布来源；不会被 `SkillsLoader` 自动扫描。

群内可用只读命令查看候选区：`/evolve overlay` 列出候选 Skill 及其“未发布/已同步/内容不一致”状态；`/evolve overlay <skill-name>` 仅允许审批管理员查看候选 `SKILL.md` 正文。普通 `/evolve list` 和 `/evolve review <proposal-id>` 继续查看 Proposal 元数据；这些查询不会把候选 Skill 注入正常 Agent Context。

为降低记忆命令成本，`/evolve menu`（以及 `help`、`commands`、`enum` 别名）会返回中文操作菜单；Proposal 列表使用中文状态、独立的北京时间创建日期和 Skill 名称，不再把日期编码进展示文本。QQ 群内 @机器人后，也可直接使用“看看候选 Skill”“看看待确认的沉淀 Skill”“看看当前有哪些 Skill”等只读中文表达；这些表达不会触发批准、发布、回滚或任何写入动作。

因此，Overlay 中存在某个 `skills/<name>/SKILL.md`，只代表它存在于候选仓库，不代表已经部署。只有群内管理员完成二次确认发布后，系统才会把指定合并版本中的安全 Skill 文件原子同步到 `runtime/workspace/skills/`；回滚后，生效目录恢复为上一版本。旧审计记录中可能仍出现 `phase6-overlay` 这一历史 actor 名称，但新的运行目录、默认路径和审计 actor 统一使用 `skill-evolution-overlay`。

## 3. 目标范围与非目标

### 3.1 本期目标

1. 对低风险、重复成功的真实任务自动生成候选，并自动运行已定义的评测流程。
2. 在评测通过后，向指定 QQ 群主动发送一条可审阅、带证据编号的 Proposal 通知。
3. 允许指定 QQ 管理员在群内查询、审阅、批准、拒绝和回滚 Proposal。
4. workspace Skill 经过一次批准后以原子方式写入规范 `SKILL.md`，下一轮 Agent 自动读取。
5. builtin/shared/entrypoint/MCP Skill 经过批准后只向个人 Skill Overlay 创建 Draft PR；合并与部署需要独立的第二次批准，公共主分支永不作为个人 Skill 发布目标。
6. 对通知、命令、授权判定、版本 CAS、采用、PR、发布和回滚生成结构化审计证据。

### 3.2 明确不做

- 不读取、保存或总结整个群聊历史作为长期记忆。
- 不根据模型自然语言回复、点赞、表情或模糊措辞执行采用/发布。
- 不因评测通过自动修改 `current_revision_id`、覆盖 `SKILL.md`、创建合并提交或部署 Gateway。
- 不让普通群成员拥有审批或回滚权限。
- 不将 QQ AppSecret、GitHub Token、群 openid、管理员 openid 写入 Git、审计正文或群通知。
- 不把个人 Skill、个人配置或个人运行策略合并到公共 `Trees-23/KdmCopilot:main`。
- 不在首版实现 QQ 内联按钮；后续可在已有文本状态机稳定后独立增加。

## 4. 目标架构

```text
                 ┌──────────────────────────────────────┐
                 │ 官方 QQ 群                            │
                 │ 消息、@、/evolve 命令、主动通知       │
                 └───────────────┬──────────────────────┘
                                 │
                     QQ Channel / MessageBus
                                 │
       ┌─────────────────────────┼──────────────────────────┐
       │                         │                          │
群感知入口                    命令入口                  通知出口
Group Perception          EvolutionCommand          ProposalNotifier
（过滤、脱敏、事件）       （管理员验证、CAS）        （outbox、重试、审计）
       │                         │                          ▲
       ▼                         ▼                          │
Trace / Case ──→ Phase6 Orchestrator ──→ 自动评审 Gate
                                            │
                                            ▼
                                   Proposal Repository
                                      │             │
                         workspace 候选 │             │ Overlay 候选
                                      ▼             ▼
                         QQ 通知 + 管理员批准   自动创建私有 Draft PR
                                      │             │
                                      ▼             ▼
                         原子采用/回滚       CI + QQ 发布候选通知
                                                    │
                                                    ▼
                                             群内二次确认
                                                    │
                                                    ▼
                                          私有 Overlay 合并与部署
```

所有自动化写入均在后台受控服务完成。普通 Agent、维护评测角色和 QQ 群成员都不直接拥有文件写入、Git 或网络发布权限。对个人 Overlay，自动评审通过后由受控后台创建 Proposal 和 Draft PR；管理员只在 CI 通过后的最终发布环节做二次确认，不需要手工创建 Proposal 或 PR。

### 4.2 两条 Proposal 路径的职责边界

| 路径 | 自动动作 | 管理员动作 | 最终效果 |
|---|---|---|---|
| workspace Skill | Gate 通过 → Proposal → 群通知 | `/evolve approve` 或 `/evolve reject` | 批准后原子采用，可回滚 |
| 个人 Overlay Skill | Gate 通过 → Proposal → 自动 Draft PR → CI → 群通知 | `/evolve publish`：第一次签发确认码，第二次提交确认码 | 只合并私有 Overlay，热加载到当前 Gateway，不重建/重启 |

`/evolve approve` 不再是 Overlay 发布前置条件；Overlay 的人工门禁统一收敛到 CI 通过后的 `/evolve publish` 二次确认。`publish_enabled` 只控制最后的合并/部署，不影响候选评审、Draft PR 生成或 CI 检查。普通 Agent 不直接调用 GitHub。

### 4.1 公共核心与个人 Overlay 隔离

```text
公共仓库 Trees-23/KdmCopilot:main
  ├─ 通用 Agent、记忆、QQ、Proposal 和安全门禁代码
  ├─ 默认关闭的通用能力
  └─ 不接收个人 Skill、个人配置、个人群策略

个人私有 Skill Overlay 仓库
  ├─ 仅当前用户的 Skill candidate/revision
  ├─ 个人 Draft PR、CI 和发布记录
  └─ 只部署到当前用户的 nanobot-gateway
```

公共仓库可以继续正常发布稳定版本；个人 Overlay 的合并、回滚和部署不会改变公共 `main`。如果暂时没有私有仓库，必须使用受保护的长期个人分支并禁止将其作为公共发布源；长期方案仍优先使用独立私有仓库。

## 5. 群内感知模型

### 5.1 四层感知

| 层级 | 输入 | 行为 | 长期保存策略 |
|---|---|---|---|
| 即时交互 | @机器人、回复机器人、`/evolve` 命令、附件 | 正常 Agent 对话或确定性命令处理 | 正常会话与 Trace |
| 任务信号 | 成功/失败、工具错误、重试恢复、重复请求 | 生成脱敏 Trace 摘要，供候选筛选 | 仅 Trace/Case 候选 |
| 模式感知 | 相同低风险任务反复成功、同类失败反复发生 | Phase 6 生成候选/回归 Case | 不保存完整群消息 |
| 主动告警 | Proposal 通过、回归暂停、采用/发布失败、回滚完成 | 向允许的群发送系统通知 | Proposal 与投递审计 |

### 5.2 群感知的默认安全策略

1. `group_openid` 必须同时在 `observation_groups` 和 `notification_groups` 显式配置，才允许进入自动化闭环。
2. 普通群消息默认采用“仅 @机器人或回复机器人”参与策略；`/evolve` 命令允许由指定管理员直接发送。若 QQ 平台没有可靠的 @ 结构字段，则以命令前缀和群白名单作为确定性门槛。
3. 仅把经红脱敏、任务级摘要、工具类别、结果和 Trace ID 交给候选系统；不将成员昵称、QQ 号、附件路径、完整提示词或密钥写入 Case/Skill。
4. 对不同群采用独立 `session_key` 与检索 namespace，禁止把 A 群摘要检索到 B 群。
5. 第一阶段不对“纯闲聊”“敏感关键词”“包含文件/凭据”“发生写操作或网络副作用”的 Trace 生成 Skill 候选。

## 6. Proposal 与发布状态机

### 6.1 状态

```text
draft
  → evaluating
  → rejected_by_gate | insufficient_evidence | stale
  → eligible_for_confirmation
  → notified
  → approved（workspace 管理员批准，或 Overlay 自动 Gate handoff）
      ├─ adopting            → adopted（workspace）
      └─ creating_pr         → pr_created（Overlay Draft PR）
                              → publish_approved
                              → publishing → published
  → rejected_by_admin | expired | failed | rolled_back
```

状态不可倒退；重评估、重试或新候选必须创建新的 Proposal。`approved`、`adopted`、`pr_created`、`published` 和 `rolled_back` 都要保存操作人、时间、基线版本、目标版本、原因和关联审计事件。Overlay 自动 handoff 使用独立的 `automatic_gate_approval` 审计动作，不伪造管理员批准。

### 6.2 关键并发与安全规则

- 提案包含 `baseline_revision_id`、`candidate_revision_id`、`candidate_hash` 和 `version_epoch`。
- `approve`/`adopt`/`publish`/`rollback` 使用条件更新（CAS）。当前版本、Proposal 状态或确认码变化时拒绝操作，不做“最后写入者获胜”。
- 确认码只保存哈希，当前运行态为 12 小时（720 分钟）、单次使用；群通知只展示短码，不展示完整敏感路由信息。
- 相同命令的网络重放返回已完成的幂等结果，绝不重复写文件、重复建 PR 或重复发布。
- Gate 结果、工具 schema digest、fixture hash 或候选 hash 变更时 Proposal 转为 `stale`，需要重新评测。

## 7. QQ 群交互协议

### 7.1 命令

| 命令 | 所有人 | 管理员 | 副作用 |
|---|---:|---:|---|
| `/evolve status` | 是 | 是 | 无 |
| `/evolve list [pending\|recent]` | 是 | 是 | 无 |
| `/evolve review <proposal-id>` | 是 | 是 | 无；只显示脱敏证据摘要 |
| `/evolve approve <proposal-id> <code>` | 否 | 是 | 仅 workspace Proposal 进入采用；Overlay 不走此命令 |
| `/evolve reject <proposal-id> <reason>` | 否 | 是 | 终止该 Proposal |
| `/evolve publish <proposal-id> [code]` | 否 | 是 | 第一次签发二次确认码；带码的第二次命令才合并私有 Overlay 并部署当前 Gateway |
| `/evolve rollback <skill> <revision> <code>` | 否 | 是 | 显式回滚到已有受信 revision |

命令由 `CommandRouter` 直接分派，不能进入模型推理流程。无权限、过期、跨群、状态冲突或错误确认码都返回确定性拒绝信息并写审计。

### 7.2 通知内容

每条通知只包含：Proposal ID、Skill 名称、来源类型、自动 Gate 结论、holdout/安全/成本/延迟概要、Trace/Case/EvalRun 数量、有效期、查询和确认命令。通知不得包含完整 Case 正文、群成员身份、密钥、完整模型输出或 GitHub 凭据。

示例：

```text
【Skill 升级候选】
提案：prop-01J...
Skill：deploy-helper（workspace）
自动评审：通过；holdout 0.92；安全检查通过
证据：2 个 Trace / 1 个 Case / 2 次独立回放
有效期：15 分钟

查看：/evolve review prop-01J...
同意：/evolve approve prop-01J... 4821
拒绝：/evolve reject prop-01J... 原因
```

### 7.3 管理员与群权限

审批权限必须独立于聊天使用权限：

```json
{
  "phase6": {
    "enabled": false,
    "evolution": {
      "observationGroups": ["<QQ_GROUP_OPENID>"],
      "notificationGroups": ["<QQ_GROUP_OPENID>"],
      "approvalAdminOpenids": ["<QQ_USER_OPENID>"],
      "commandRequireMention": true,
      "proposalTtlMinutes": 15,
      "maxNotificationsPerGroupPerDay": 3
    }
  }
}
```

该配置结构为实施目标，不是现有 schema。真实值只写入运行态 `runtime/config.json` 或受控环境变量；提交示例只能保留占位符。上线前需要管理员提供目标群的 QQ group openid 与管理员 QQ user openid，不能从公开聊天文本猜测。

## 8. 数据与组件设计

### 8.1 数据库迁移

在现有 memory SQLite 之上新增下一版本迁移，而不是改写 `0001_memory_base.sql`。建议至少新增：

- `skill_proposals`：提案身份、候选/基线 revision、Gate 快照、目标类型、状态、有效期、确认码哈希、审阅/操作人、失败原因；
- `proposal_deliveries`：通知路由、内容 hash、状态、尝试次数、下次重试时间、QQ 投递审计引用；
- `proposal_actions`：不可变管理员动作日志和请求幂等键；
- `group_memory_scopes`：允许的群、namespace、感知模式、每日配额与启用状态。

外部路由信息只在 `proposal_deliveries` 的受控字段中保存，不放入 Case、Skill revision、EvalPack 或通知正文。所有表需有唯一约束，保证同一 candidate hash 与 baseline hash 在同一目标上不产生无穷重复通知。

### 8.2 新模块建议

```text
nanobot/memory/
  proposal_repository.py    # Proposal、Action、Delivery 的事务与 CAS
  evolution_orchestrator.py # 有界系统周期：候选、评测、建 Proposal
  proposal_notifier.py      # 通过 MessageBus 投递、outbox 重试、去重
  skill_adoption.py         # workspace 原子采用、hash/CAS、审计
  pr_publisher.py           # 由自动 Gate handoff 创建本地 Draft PR 变更
  overlay_remote.py         # 校验私有仓库、创建远端 Draft PR、读取 CI 投影
  group_scope.py            # 群感知范围、脱敏、配额和 namespace
nanobot/command/
  evolution.py              # /evolve 确定性命令及权限校验
nanobot/config/
  schema.py                 # EvolutionConfig / QQ approval 配置
tests/memory/
  test_proposals.py
  test_evolution_orchestrator.py
  test_skill_adoption.py
tests/command/
  test_evolution_commands.py
tests/channels/qq/
  test_qq_evolution_delivery.py
```

`ProposalNotifier` 必须直接构造带 `qq_chat_type=group` 的 `OutboundMessage` 并经 MessageBus/ChannelManager 投递。不可通过普通 LLM `message` 工具猜测目标群，否则跨聊天发送会丢失官方群路由上下文并扩大提示注入面。

### 8.3 调度方式

实现一个 Gateway 生命周期管理的系统任务，每天北京时间 14:00 扫描一次（Cron：`0 14 * * *`，时区 `Asia/Shanghai`），单 workspace lease 串行执行。它直接调用受限 Python 编排器，而不是让普通 Agent Cron 通过自然语言决定是否评测、是否通知或是否发布。Gateway 在错过执行时间后只等待下一次计划，不补跑。

每周期上限：最多 20 个候选、每群每天最多 12 条通知、同一 Skill 同时至多 1 个待确认 Proposal；同类低风险成功 Trace 至少累计 3 次才进入候选。系统重启后从数据库恢复状态，投递失败使用有限指数退避；超过上限转为 `dead_letter` 并主动产生管理员告警，不静默丢失。

## 9. 采用与发布行为

| Skill 来源 | `approve` 后的动作 | 是否需要重启 | `publish` |
|---|---|---:|---|
| workspace | 校验 hash/CAS 后原子写入 `<workspace>/skills/<name>/SKILL.md`，保留可回滚 revision | 否；下一次 Context 构建生效 | 不需要 |
| builtin/shared | Gate 通过后在个人 Overlay 创建独立分支、中文提交、Draft PR，并附证据链接 | 否；合并后热加载，下一轮 Context 生效 | CI 通过后由 `/evolve publish` 二次确认才允许合并个人 Overlay |
| entrypoint/MCP | 在个人 Overlay 创建 Draft PR 或对应包的受控发布提案 | 视部署方式而定 | 二次批准、CI 与个人 Gateway 发布检查 |

workspace Skill 采用使用“写临时文件 → fsync → 原子 rename → 记录 revision/CAS → 审计”的顺序。写入失败不得改变 `current_revision_id`。当前正在执行的 Agent turn 保持旧上下文；后续 turn 自动通过 `SkillsLoader` 读取新文件。

个人 Skill Overlay 发布继续遵循仓库规则：在 Overlay 专用分支创建中文提交与 Draft PR，经过 CI 后等待 `publish` 的独立管理员确认；任何情况下不得自动合并公共 `Trees-23/KdmCopilot:main`。合并后只把允许的 `skills/*/SKILL.md` 原子同步到当前用户 Gateway 的挂载 workspace，由下一轮 Context 热加载。

## 10. 分阶段实施计划

### 10.1 阶段总览与开关顺序

“实现功能”和“打开自进化”是两件事，必须拆成不同阶段。前面的阶段可以在 Phase 6 关闭时完成；只有完成影子验收后，才进入正式打开开关的阶段。

| 阶段 | 名称 | `phase6.enabled` | 通知 | 采用/建 PR | 发布 | 目标 |
|---|---|---:|---:|---:|---:|---|
| M0 | 配置与安全基线 | 关闭 | 关闭 | 关闭 | 关闭 | 明确群、管理员、配额和回滚基线 |
| M1 | Proposal 持久化与状态机 | 关闭 | 关闭 | 关闭 | 关闭 | 先把证据、状态和确认门禁做可靠 |
| M2 | 自动评审编排（离线） | 关闭 | 关闭 | 关闭 | 关闭 | 用 fixture 验证候选、评测、Gate 和 Proposal |
| M3 | QQ 通知与群命令接线 | 关闭 | 关闭/测试群 | 关闭 | 关闭 | 接好群路由、权限、确认码和审计 |
| M4 | **开启自进化开关：影子模式** | **开启** | 关闭 | 关闭 | 关闭 | 真实运行自动评审，但不通知、不修改、不建 PR |
| M5 | 开启主动通知 | 开启 | 开启 | 关闭 | 关闭 | 评测通过后主动推送，仍不能升级 |
| M6 | 开启人工批准后的 workspace 采用 | 开启 | 开启 | 仅 workspace 白名单 | 关闭 | 你确认后才原子更新 Skill，可回滚 |
| M7 | 个人 Overlay 仓库准备 | 开启 | 开启 | 关闭 | 关闭 | 创建隔离仓库、保护公共 main、确认 CI 与部署来源 |
| M8 | 个人 Overlay Draft PR | 开启 | 开启 | 自动建 Draft PR | 关闭 | Gate 通过后自动生成 Proposal/Draft PR，不能影响公共 main |
| M9 | 个人 Overlay 受控发布 | 开启 | 开启 | 已创建 Draft PR | **二次确认** | CI 通过且再次确认后只发布到个人 Gateway |
| M10 | 稳态运行与运营复盘 | 开启 | 开启 | 按白名单 | 按策略 | 监控、限流、暂停和定期复审 |
| M10-A | 自动触发器 | 开启 | 关闭/按现状 | 关闭 | 关闭 | 每天北京时间 14:00 由受保护 Cron 调用一次受限评审编排器，错过不补跑 |
| M10-B | 自动候选与私有 Draft PR | 开启 | 开启 | 自动建私有 Draft PR | 关闭 | 3 次重复低风险成功后生成候选，Gate/CI 通过后 QQ 通知 |
| M10-C | 稳态观察与运营复盘 | 开启 | 开启 | 按白名单 | 按策略 | 连续 7 天验证 dead-letter、越权、跨群隔离和通知质量 |
| M11 | Proposal 可审阅透明化 | 开启 | 开启 | 自动建私有 Draft PR | 关闭 | 用中文编号、候选正文、案例、评测与 PR/CI 信息支持人工判断 |
| M12 | 候选语义质量门禁 | 开启 | 开启 | 自动建私有 Draft PR（仅质量通过） | 关闭 | 只从可解释的业务任务沉淀候选；框架事件、泛化模板与无业务价值候选在 Draft PR 前拦截 |
| M13 | 人工纠正后的恢复案例学习 | 开启（只记录 Case） | 可选摘要通知 | 关闭 | 关闭 | 识别“失败→人工纠正→验证成功”的重复恢复模式；首版只形成可审阅 Case，不自动生成或发布 Skill |
| M14 | 审批队列与状态分流 | 开启 | 开启 | 按既有质量 Gate | 关闭 | 分开显示待首次审阅、待最终发布、失败/拒绝与历史记录，减少 `pending`/`recent` 歧义 |
| M15 | 真实能力评测与基线对照 | 开启 | 开启（仅通过 Gate 后） | 自动建私有 Draft PR（仅标准证据等级） | 关闭 | 以隔离 A/B 回放、正反例和独立裁判证明候选相对基线有业务增益；结构检查不再单独放行 |
| M16 | 失败驱动自进化与人工 Issue 反馈 | 设计中（默认只记录） | 每天 12:00 有新 Issue 才通知 | 关闭 | 关闭 | 将失败→人工纠正→验证成功转为可评论 Issue；人工反馈后才允许进入恢复 Case、语义记忆或修复 Skill 候选，仍需 M15 评测 |

每个阶段都必须有独立验收记录。任何阶段失败都回退到上一阶段的开关状态；不能跳过 M4 直接打开通知、采用或发布。

### M0：配置与安全基线

- [x] 确认目标 QQ group openid、审批管理员 QQ user openid、是否要求 @ 才接收 `/evolve`。
- [x] 将聊天 `allowFrom` 与审批 `approvalAdminOpenids` 分离；评审 `allowFrom: ["*"]` 暂保持不变。
- [x] 明确群内通知时段、每天配额、Proposal 有效期、是否同时私聊管理员；当前按全天、每天 12 条、720 分钟有效期、不额外私聊处理。
- [x] 创建并确认个人 Skill Overlay 私有仓库、默认分支、CI 状态检查与部署责任人；公共 `Trees-23/KdmCopilot:main` 明确排除为个人 Skill 发布目标。已配置 `Trees-23/KdmCopilot-skills-private:main`，并完成 `validate` CI 与 Draft PR 隔离验收。
- [x] 记录当前 Gateway 构建标识、镜像、运行根目录和当前 Phase 6 关闭状态，作为回滚基线。

完成条件：所有标识和策略通过运行态配置注入，Git 不含真实 openid、token 或 secret。

### M1：Proposal 持久化与状态机

- [x] 新增迁移和 repository；实现 Proposal、Delivery、Action、Group Scope 的不可变/可变字段边界。
- [x] 实现状态转移、版本 CAS、过期、确认码哈希、动作幂等与审计。
- [x] 将现有 `make_proposal()` 接入持久化模型，但保持 Phase 6 默认关闭。
- [x] 编写单元和并发测试：重复批准、过期批准、旧基线、重复通知、迁移恢复。

完成条件：不依赖 QQ 即可通过 API/测试构造、查询和安全地终结一份 Proposal。M1 已完成。

### M2：自动评审编排（离线与 fixture）

- [x] 实现系统级受限周期任务和 workspace lease，但先只能被测试/维护命令显式调用。
- [x] 接通 Trace → 低风险选择 → Case → EvalPack → 独立回放 → Gate → Proposal。
- [x] 实现配额、去重、暂停开关、回归保护和 dead-letter 告警；通知正文持久化，Gateway 启动后可继续投递。
- [x] 完成 Phase 6 关闭时的负向测试：不扫描、不写 Proposal、不发 QQ 消息。

当前进度：M2 的离线编排、调度状态、workspace lease、Trace→Gate→Proposal 链路、配额/去重、Delivery retry/dead-letter、主动告警和关闭态负向测试均已完成；周期调度仍保持显式调用，Phase 6 未打开。

### M3：QQ 通知与群内命令接线（默认关闭）

- [x] 实现群目标路由与 `qq_chat_type=group` 投递；通知正文持久化，经 MessageBus 投递并保留重试、dead-letter 和审计引用。
- [x] 实现 `/evolve status/list/review/approve/reject`，命令绕过 LLM；批准只记录 Proposal 状态，不执行 Skill 采用或发布。
- [x] 实现群白名单、管理员 openid 校验、确认码校验、@要求、脱敏输出和跨群拒绝；补充错误权限、错误群、重放命令协议测试。
- [x] 完成真实 QQ 群的 `/evolve status`、`list pending`、`review`、管理员 `approve` 安全验收；临时 Proposal 已清理。
- [x] 完成主动通知投递的真实 QQ 验收；临时测试确认真实群已收到通知，测试后已关闭通知开关并清理测试 Proposal。
- [x] 修复通知正文确认码有效期的展示：统一转换为北京时间（`YYYY年MM月DD日 HH:MM:SS（北京时间）`），避免直接显示 UTC ISO 时间。

当前进度：M3 已完成通知器、命令与授权的本地协议单元测试，并接入 Gateway 的独立投递循环；真实 QQ 命令安全验收和主动通知投递验收均已完成。M4 已通过加速影子验收，M5 已通过加速通知验收；当前 `phase6.enabled=true`、`shadow_mode=false`、主动通知开启，采用和发布仍关闭。周期调度仍保持显式调用，未宣称自然日 7 天观察。

### M4：开启自进化开关——影子模式

这是“打开 Phase 6”的独立实施阶段，不与代码完成或 QQ 接线混在一起。只有 M0～M3 全部通过后才能执行。

- [x] 将 `phase6.enabled` 设为 `true`，同时保持 `shadow_mode=true`、`notifications_enabled=false`、`adoption_enabled=false`、`publish_enabled=false`（2026-09-27 已在长期 Gateway 生效）。
- [x] 按用户确认执行加速观察：在隔离临时工作区快进等价 168 小时、85 个周期；只生成内部证据，不允许 QQ 主动通知、Skill 文件写入、建 PR 或发布。
- [x] 核对候选数量、敏感任务/写操作过滤、回归暂停、kill switch 和调度状态；85 个周期均完成，回归阈值触发后后续周期保持暂停。
- [x] 人工检查加速测试结果：候选仅来自低风险只读重复任务，未产生跨群数据或生产 Proposal；该结果是加速验收，不等同自然日运行 7 天。

完成条件：加速影子验收无越权写入、无跨群泄露、无未处理 dead-letter；已按用户确认进入 M5。若后续真实运行发现异常，应关闭 `phase6.enabled` 并保留证据用于修复。

### M5：开启 QQ 主动通知

- [x] 保持 `phase6.enabled=true`，关闭 `shadow_mode`，开启 `notifications_enabled=true`，采用/发布开关继续关闭（2026-09-27 已在长期 Gateway 生效）。
- [x] 加速验收只向 `notification_groups` 推送 `eligible_for_confirmation`；通知限流、去重、重试和过期已有聚焦测试覆盖。
- [x] 真实 QQ 群完成一次 M5 主动通知投递，通知状态为 `sent`，正文确认有效期显示为北京时间；测试 Proposal 已精准清理。群命令、错误权限、错误码、重复通知和失败恢复已有真实/聚焦测试证据。

完成条件：本次按用户确认采用加速验收，已验证通知可追溯且不会自动升级；未宣称连续自然日 7 天运行。群内只能看到 Proposal，不能因回复通知而自动升级。

### M6：开启人工批准后的 workspace 采用

- [x] 在隔离 M6 验收窗口临时开启 `adoption_enabled=true`，仅允许显式 `workspaceSkillAllowlist`；验收结束后已恢复关闭/空列表，长期运行态不保留测试白名单。
- [x] 实现原子采用器、revision 对比、落盘 hash 校验和错误回滚；增加 `workspaceSkillAllowlist` 安全门禁。
- [x] 实现 `/evolve rollback <proposal-id> <code>`，仅管理员可调用，并恢复已知 baseline revision。
- [x] 在长期 Gateway 容器代码/运行库的隔离测试 Skill 上验证批准、下一次读取候选版本和回滚；测试文件、Proposal、Skill 记录均已清理。

当前进度：M6 的采用代码、本地原子/CAS/回滚测试和一次性隔离 Gateway 验收已完成；长期运行态已恢复 `adoption_enabled=false`、白名单为空，未保留测试 Skill。个人 Overlay 已完成初始化；M7 的仓库保护、CI 基线和只读映射已完成。M8 自动 Draft PR、CI 回写、通知幂等和一次真实 QQ 候选通知已实测成功；M9 个人 Gateway 发布仍未开启。

### M7：个人 Skill Overlay 仓库准备

- [x] 用户创建独立私有 GitHub 仓库 `Trees-23/KdmCopilot-skills-private`；仓库只保存个人 Skill Overlay，不保存 QQ 密钥、Token、完整聊天记录或长期运行数据库。
- [x] 用户保护 Overlay 的 `main` 分支，规则集“保护个人 Overlay main”已启用；要求 PR、`validate` CI、禁止删除和强制推送；公共 `Trees-23/KdmCopilot:main` 不作为个人 Skill 的 PR/发布目标。
- [x] 已确认 Overlay 默认分支为 `main`、仓库为私有；旧 PR #1 的 `Skill Overlay CI` 已通过，但工作流尚未进入 `main`。
- [x] 将 CI 工作流基线合并到 Overlay `main`：PR #3 已合并，合并提交 `608495930a02…`，`validate` 已通过。Token 只能使用 GitHub CLI/环境密钥注入，不能粘贴到 QQ 或提交到 Git。
- [x] 已实现 Overlay 文件布局、candidate hash、baseline revision 与个人 Gateway 工作区之间的只读映射检查；3 个聚焦测试通过，检查不会写入 Skill 或改变线上版本。

完成条件：公共仓库与个人 Overlay 的边界、权限、CI、回滚来源和部署目标均可审计；M7 已完成，`publish_enabled` 仍保持关闭。

### M8：个人 Overlay Draft PR

- [x] 完成本地 Draft PR 适配器：已批准 Proposal 生成专用分支、中文提交和脱敏正文；重复执行幂等，不切换公共仓库版本。
- [x] 将 Gate 通过的 Overlay Proposal 自动推进到 Draft PR；不再要求管理员先执行 `/evolve approve`，workspace Proposal 仍保留人工批准路径。
- [x] 增加私有 Overlay 远端适配器：强制校验 `origin`、仓库、base `main`、唯一 head 分支和 Draft 状态；远端 PR/CI 投影写入 Proposal 审计动作，普通 Agent 不直接调用 GitHub。
- [x] 在隔离窗口运行真实远端 `gh pr create --draft`，CI 通过后回写 `ci_passed`，并验证发布候选通知审计动作只发送一次；未通过 CI 不得发送发布确认提示。
- [x] 在真实 QQ 群内用临时 Proposal 投递一次 CI 通过后的“等待最终发布确认”通知；Delivery 为 `sent`，测试数据已清理。
- [x] 将本次真实验收的远端目标指定为个人 Overlay，Draft PR base 为 Overlay `main`，不是公共 `Trees-23/KdmCopilot:main`。
- [x] 在个人 Overlay 的临时测试 Skill 上创建真实 Draft PR [#1](https://github.com/Trees-23/KdmCopilot-skills-private/pull/1)；已验证公共仓库 `main`、公共镜像和长期 Gateway 未变化。
- [x] 2026-09-28 通过真实临时 Proposal 创建并清理 Draft PR #2；验证自动 handoff 成功，发现 `main` 缺少 CI 工作流后关闭该 PR，未合并或部署。
- [x] 验证 PR 正文只包含 Proposal 元数据、评测摘要、hash 和风险说明，不包含聊天正文、成员身份、完整模型输出或凭据；Overlay CI 已通过。

阶段状态：M8 的 Overlay 隔离、Draft PR 适配器、自动 handoff、真实自动建 PR、CI 回写、通知幂等和真实 QQ 候选通知均已完成。真实 Draft PR 只存在于个人 Overlay，未合并前不改变任何运行版本。失败则关闭 Draft PR 创建开关并保留审计证据。
完成条件：M8 已核对 base 仓库、分支保护、CI、通知幂等和公共仓库隔离；真实合并与部署仍留给 M9 二次确认。

### M9：个人 Overlay 受控发布

- [x] 完成本地二次确认门禁：独立确认码、Proposal 状态/CAS、CI 通过条件、kill switch 双重拦截；合并和部署只能通过显式受控回调执行。
- [x] 接通群内 `/evolve publish` 协议：只允许已配置 `approvalAdminOpenids` 的管理员在已配置通知群中对个人 Overlay 的 `pr_created` Proposal 发起二次确认；必须 @机器人，普通成员、错误群、错误码、过期码和公共仓库 PR 一律拒绝。协议已完成本地测试并部署到 Gateway，`publish_enabled` 仍保持关闭。
- [x] 增加私有 Overlay 远端校验适配器：通过受控 `gh` 调用核对唯一 PR、仓库、`main` 基线、Proposal 分支、head SHA 和全部 CI 检查；真实 PR #1 已只读验收通过，未执行 ready、合并或部署。
- [x] 增加受控部署适配器：二次确认后才允许下载指定合并 SHA 的 Overlay tarball，只同步安全的 `skills/<name>/SKILL.md`，通过临时文件、fsync 和原子 rename 热加载到当前 Gateway workspace；不重建、不重启容器。覆盖路径穿越、空 Overlay 和同步失败测试。该适配器尚未接入长期 Gateway，也未执行真实合并。
- [x] 将 `overlayRepository`/`overlayBaseBranch` 接入运行配置；Gateway 启动时注入私有 Overlay 的 CI、合并和热加载回调，并在长期容器中验证回调对象已创建。发布仍受 `publishEnabled=false` 和 QQ 群二次确认约束。
- [x] 兼容 Fine-grained Token 的 CI 查询：不再依赖 GraphQL `statusCheckRollup`，改用 Actions REST 按 head SHA 校验 workflow runs；覆盖真实 403 场景测试。
- [x] 接通“Gate → Overlay Proposal → Draft PR”受控后台流水线；CI 状态投影和 QQ 发布候选通知由独立 worker 继续处理，不自动合并或部署。
- [x] 在隔离窗口完成“Draft PR → CI 通过 → QQ 发布候选通知 → 群内二次确认”的真实链路验收。
- [x] `publish_enabled=true` 只在明确的发布窗口开启，并且同时检查 CI 成功、PR 状态、Overlay 分支、候选 hash、Proposal 未过期和当前 Gateway 基线；验收结束已恢复为 `false`。
- [x] 合并后只热加载当前用户的长期 Gateway，记录 Overlay commit、PR、CI、热加载结果和回滚证据；未执行公共 `main` 合并。
- [x] 验证发布失败、GraphQL CI 错配、回滚和 kill switch 门禁行为；失败均保留审计证据，未静默重试。

完成条件：没有第二次确认就不能合并/部署；发布只影响当前用户 Gateway，公共仓库和其他用户版本保持不变。

### M10：稳态运行与运营复盘

- [x] M10-A：注册受保护的 `phase6-evolution-review` 系统 Cron（`0 14 * * *`、`Asia/Shanghai`），调用生产评审编排器；错过执行时间不补跑。实现提交：`422a52c3`。
- [x] M10-B-1：把脱敏 `trace_index` 扫描接入候选生成；默认最低重复次数固定为 3，仅允许成功且只读工具类别进入候选，敏感/写操作继续排除。
- [x] M10-B-2：由 3 次重复 Trace 生成隔离 staging Skill revision、3-case EvalPack、只读确定性 Replay 和独立 Gate；Gate 通过后持久化 `git_pr_proposal`，当前 Skill 指针保持不变；重复每日扫描幂等去重。
- [x] M10-B-3：把私有 Overlay Draft PR 创建、CI 回写和 QQ 投递器作为 Gateway 运行态适配器注入受保护 Cron；未注入适配器时只保留 Proposal，不得伪造 PR/CI/通知成功。
- [x] M10-B-4：修复定时评审只读取空 `trace_index` 的问题；每次生产 Cron 扫描前先从提交后的 Audit 根目录增量索引 Trace，再执行低风险候选选择。2026-09-29 核对到此前已有 135 条 Audit Trace 未进入索引，已补齐索引入口和聚焦测试。
- [x] M10-B-5：失败可感知与安全恢复。Overlay checkout 自动使用仓库级机器人 Git 身份；仅在分支、暂存路径和候选 hash 全部匹配时恢复失败的系统候选；Draft PR handoff、CI 核验和扫描/初始化失败进入独立的持久化 QQ 告警队列。2026-10-01 已在长期 Gateway 真实恢复 3 条失败 Proposal 的暂存/专用分支，重建私有 Draft PR #7～#9，`validate` CI 全部通过；3 条异常告警和 3 条发布候选通知均为 QQ `sent`，未合并、未发布、未写入当前生效 Skill。
- [x] 每周输出候选数量、Gate 通过率、通知失败率、管理员拒绝率、采用/回滚率和误报原因；周报只读取 Proposal/Delivery/Action 元数据，不读取模型原文或凭据。
- [x] 超过阈值自动暂停 Phase 6，并通过注入式通知适配器向管理员发送暂停原因；暂停状态持久化且不会自动恢复。
- [x] 提供定期复审检查：群白名单、管理员名单、Overlay 配置、配额和 kill switch 的不一致会形成明确 finding；Skill 白名单仍需按运营周期人工确认。
- [x] AgentLoop 与 QQ ChannelManager 支持运行中替换 Phase 6 配置；刷新后下一轮命令/通知立即使用新配置，`kill_switch` 可在不重启 Gateway 的情况下阻止新的自动化写路径。

完成条件：M10-A/M10-B 已接通，且连续 7 天无未处理 dead-letter、无越权命令成功、无自动发布、无跨群泄露，管理员认可通知频率和升级质量。目前 M10-A、M10-B-1/M10-B-2/M10-B-3 均已接通；M10-C 连续 7 天观察仍在进行，不能提前宣称完成。

本轮 M10-B 代码验收记录：`tests/memory` 120 passed，涉及路径的 `ruff check` 和 `git diff --check` 通过。长期 Gateway 已按提交 `908ffb63` 重建，构建标识为 `git-908ffb637b2e`，镜像 `sha256:5e80fbd4505f69e0a3d5424d27aacac6ff81215309d5375ce9fa85638ec3996f`，健康检查正常；容器仍挂载仓库 `runtime/`，服务为固定的 `nanobot-gateway`（8765/18790）。

M10-B-3 真实隔离验收：运行态使用私有 Overlay `Trees-23/KdmCopilot-skills-private:main` 和独立 checkout，真实创建 Draft PR #6；`validate` CI 已通过并成功回写，QQ outbox 创建 1 条 `pr_created` Delivery，通知审计动作创建 1 条。PR 未合并、未发布，长期运行态仍保持 `adoptionEnabled=false`、`publishEnabled=false`、`killSwitch=false`。验收完成后已关闭 PR #6、删除远端演化分支，并将本轮临时目录移入系统回收站；未触碰公共 `Trees-23/KdmCopilot:main`。

### M11：Proposal 可审阅透明化（当前实施）

M11 解决“自动评审通过，但管理员不知道到底改了什么”的审批缺口。实施原则是把阶段名与业务编号分离，把候选正文、案例和评测依据做成可追溯的只读审阅材料；不改变自动评审、人工确认和私有 Overlay 发布门禁。

- [x] 为 Proposal 增加独立的用户可见编号（`proposal-xxxxxxxxxxxx`）；旧的内部 ID 仍兼容查询，但 QQ、通知和 PR 说明不再展示 `phase6` 阶段前缀。
- [x] 新增评测案例持久化表，保存评测问题、预期结果、数据集分组和来源案例 ID，避免只能看到分数而无法知道评测集跑了什么。
- [x] `/evolve review <编号>` 展示解决目标、证据数量、工具风险、Train/Validation/Holdout、Gate、安全结果、完整候选 `SKILL.md` 和 Draft PR 元数据。
- [x] `/evolve review <编号> cases` 展示完整评测案例、预期结果、得分和工具调用；`/evolve review <编号> diff` 展示基线与候选 Markdown diff。
- [x] 用户可见日期统一展示为北京时间；确认码哈希、凭据、成员身份和模型隐藏推理继续禁止展示。
- [x] 在长期 Gateway 完成最终只读场景验收：构建 `git-4dee93ea7064`、容器 `0a2b2366392c…`、镜像 `sha256:0a2bc6df…`、运行根挂载为仓库 `runtime/`；新 WebUI 会话 `#/chat/websocket%3A3d590214-e682-43a1-bf09-a0d9f073212f` 的 Trace `01a0f79b-c2e5-74e5-97fd-b323ab11e7da` 确认迁移 v5 为 `applied`，采用/发布开关仍关闭。另以真实持久化的历史 Proposal 投影验证 `proposal-edf57cea0284` 不回显 `phase6`、CI 显示已通过、完整候选正文可读。
- [ ] 在长期 Gateway 的真实 QQ 测试群完成 M11 场景验收：旧 `phase6-proposal:*` 可查询但不回显阶段名、新编号可审阅、案例与 diff 可查看、管理员确认命令仍兼容。
- [ ] 观察一轮真实定时 Proposal，核对通知、review、Draft PR、CI 和最终发布命令全链路使用新编号。

M11 不自动打开 `adoption_enabled` 或 `publish_enabled`，不合并任何 PR，不改变当前生效 Skill。只有完成真实 QQ 场景验收并确认审阅信息足够，才把本里程碑标记为完成。

### M12：候选语义质量门禁（首版已实施，等待真实观察）

2026-09-30 的三条 `evolution-event_count-*` Proposal 是反例：它们来自 20 条相似的 Agent 框架运行事件（如 `checkpoint_*`、`turn_started`），并不是“用户反复让 Agent 完成的同一件业务工作”。现有选择器把审计 Trace 的 JSON 摘要作为 task key、Skill 名称和案例输入；随后又套用固定的“只读查询”模板。这样得到的 Gate 通过，只能说明模板没有越权，不能说明候选有用。因此三条 Proposal 保持 Draft、未发布、未生效，且不得作为可接受的沉淀样本。

M12 的目标是把“安全且重复”改为“安全、重复、可解释且确有复用价值”。用户明确要求不中断自动化，因此首版已直接接入现有自动路径：新的候选只能来自业务语义投影，旧的 `trace_index.summary` 不再是候选来源。门禁拒绝时不创建 Skill revision、EvalPack、Draft PR 或 QQ 通知；不影响当前已生效的 `runtime/workspace/skills/`。

#### M12-A：受控业务语义投影

候选输入不再直接使用 `trace_index.summary` 的事件 JSON，而是在每个成功 turn 结束时生成一条单独、脱敏、限长的 `SemanticTaskEvidence`。它只保留可供管理员审阅的业务含义，不保留完整聊天正文、模型隐藏推理、成员身份或凭据：

| 字段 | 含义 | 进入候选的最低要求 |
|---|---|---|
| `task_goal` | 用户希望完成什么 | 非空、可读的自然语言目标；不得是 JSON、事件名或内部状态 |
| `intent` | 任务类别，如“查询已有 Skill”“整理文档”“排查错误” | 必须是业务/用户任务意图，不能是 `trace`、`checkpoint` 等框架意图 |
| `input_scope` | 处理对象和边界 | 至少说明对象或范围，且脱敏后仍可理解 |
| `expected_outcome` | 什么结果算完成 | 必须能说明交付物、答案形态或成功条件 |
| `operation_signature` | 已验证的关键操作类别和顺序 | 只能保存工具类别/安全摘要；不得用空工具列表或生命周期事件伪造工作流 |
| `redaction_status` | 脱敏是否完整 | 非 `safe` 一律不能进入候选 |

抽取过程可读取当前 turn 的输入和已允许的工具结果摘要，但只在内存中处理原文；先经过现有脱敏器，再持久化上述投影。若无法可靠抽取，结果为 `insufficient_semantic_evidence`，直接跳过，绝不退回到事件 JSON。实现时还需把“用户任务语义”和“Agent 自己的系统/定时任务”分开：系统 Cron、健康检查、索引、通知投递、checkpoint、模型请求等来源一律不能成为 Skill 候选。

#### M12-B：两层候选筛选与硬拒绝规则

第一层保留现有安全筛选：成功、非敏感、允许的工具类别、至少 3 条独立 Trace。第二层新增语义质量筛选，只有同一规范化业务任务在至少 3 条 Trace 中重复，且每条都具备 M12-A 的完整投影，才允许生成候选。规范化聚类键为“意图 + 目标动作 + 对象/范围 + 预期结果 + 关键操作类别”，不再是整段 JSON 的 hash。

以下任一情况直接标记 `rejected_by_quality_gate`，不创建 Skill revision、EvalPack、Draft PR 或 QQ 通知；原因只记入周报/审计，供管理员查看：

- 摘要是 JSON，或包含 `event_count`、`event_types`、`checkpoint_*`、`turn_*`、`model_*`、`delivery_*` 等框架事件；
- 任务来自 Cron、健康检查、索引、测试、通知、发布管道，或没有用户业务目标；
- 只有“成功/只读/无工具”这类安全信息，没有对象、目标、成功条件或可复用操作；
- 三条 Trace 只是同一次框架流程的重复记录，不能证明同类业务任务在不同 turn 中重复发生；
- 生成内容与通用模板等价、没有可验证的行为增量，或无法给出自然语言名称；
- 任何脱敏失败、敏感信息、写操作/外部副作用未被明确隔离，或评测无法独立验证。

#### M12-C：候选 Skill、评测与审阅的最低质量合同

通过 M12-B 的候选必须先生成“结构化候选说明”，再生成 `SKILL.md`。每一项均为必填，否则不进入自动评审：

1. 中文显示名称和稳定 ASCII 目录名；名称描述业务能力，例如“按项目范围查询已知 Skill”，不能是 hash、日期或事件列表。
2. 要解决的问题、适用场景、明确的不适用场景。
3. 输入范围、输出格式和至少两步由真实案例支持的操作步骤。
4. 安全边界、需人工确认的副作用，以及相较未加载该 Skill 的实际行为变化。
5. 脱敏的来源案例摘要和“为什么重复出现后值得沉淀”的解释。

评测不能再使用“把候选摘要塞回 prompt，并期待候选原样回显”的自证循环。EvalPack 至少要有 3 条独立的真实语义投影，按 train/validation/holdout 切分；每条验证候选是否完成目标、遵守边界、输出符合预期。安全静态检查、确定性规则检查和独立评审必须分开记录。若未来使用模型做语义抽取或质量评审，生成器与评审器必须隔离身份/提示词，并记录模型版本、结构化评分和拒绝理由；模型不可用、评分不确定或评审与生成共用同一未隔离上下文时，默认拒绝而不是放行。

`/evolve review` 随后应首先展示“解决什么问题、为什么值得沉淀、来自哪些脱敏案例、相比原行为改变什么、质量 Gate 结论及建议批准/拒绝理由”；原始事件类型只保留为管理员审计附录，不放入 Skill 正文或名称。

#### M12-D：实施顺序、开关与历史 Proposal

- [x] 新增语义投影数据模型、迁移和脱敏/来源边界测试；旧 `trace_index.summary` 不能作为候选语义回退。
- [x] 新增语义聚类、质量 Gate、拒绝状态与可解释的拒绝审计；覆盖 JSON/框架事件、无工具空壳、敏感内容和正常业务重复任务。
- [x] 用结构化候选说明生成 Skill、真实 EvalPack 与独立的结构/安全合同检查；移除“在候选正文中搜索自己的 prompt”这一自证式 replay 条件。面向开放式自然语言结果的独立模型评审作为后续增强，不是首版放行依据。
- [x] 扩展 QQ review：展示任务目标、范围、预期结果、已验证操作和语义质量 Gate；低质量拦截同时进入审计记录。
- [ ] 用户要求不中断自动 Draft PR/候选通知，因此不进入影子暂停；以新的真实用户任务直接观察门禁效果，并人工抽查第一条新 Proposal。
- [ ] 用专门的无害测试任务完成真实 QQ 场景验收：一个框架事件候选被拒绝、一个重复业务任务形成可理解的 Draft Proposal；全程不发布、不改当前生效 Skill。

既有 `proposal-edf57cea0284`、`proposal-eb8e9e7a640e`、`proposal-c77a36aabeb0` 已按管理员明确授权拒绝：私有 Overlay Draft PR #9、#8、#7 已关闭，Proposal 状态为 `rejected_by_admin`，拒绝原因记录为“候选仅包含框架运行事件，缺少可复用业务语义”。未发布、未部署、未删除远端分支，也未影响当前 Gateway。

本轮实施与验收记录（2026-10-01）：首版 M12 已提交 `39dbe54e` 并部署到长期 Gateway（构建 `git-39dbe54e046f`）。`tests/memory` 为 135 passed，AgentLoop 聚焦测试为 54 passed；排除仓库既有交互式 shell 会话挂起用例后，全量回归为 5459 passed、18 skipped、1 deselected，相关 `ruff check` 与 `git diff --check` 均通过。新 WebUI 只读场景 Trace `01a0f88c-c330-7675-a684-8588399421b1` 因没有实际经过验证的工具操作而记录为 `insufficient_semantic_evidence`，未创建 Proposal、Draft PR 或 QQ 通知，符合门禁预期。用户明确要求保持自动扫描与通知开启；后续以真实业务使用观察第一条候选，不人为制造候选或发布。

#### M12 需要用户确认的产品取舍

1. 默认采用“至少 3 次、至少来自 2 个独立 turn”的重复证据；同一 turn 内的重试/子步骤不计次数。若你希望更保守，可改为至少 3 个不同日期或会话。
2. 默认允许使用受控模型把已脱敏的用户任务压缩为上述结构化投影，并由独立评审器复核；原始聊天内容不落库、不进 QQ。若你不希望任何模型参与，可改为纯规则模式，但覆盖面会明显更窄，绝大多数开放式业务任务会被跳过。
3. 默认只对“已经稳定完成的重复任务”沉淀 Skill；失败/异常仍沉淀为 Case 和错误告警，不直接生成 Skill，除非后续单独设计“修复策略 Skill”门禁。

### M13：人工纠正后的恢复案例学习（已实施）

M13 处理的是“Agent 先失败，用户补充正确方向后才恢复成功”的真实协作经验。它不能把任意一次报错直接变成 Skill：网络波动、短暂权限故障、错误凭据、一次性外部服务故障，通常只应进入告警或排障记录，不能教会 Agent 一条永久规则。

M13 首版先只形成可审阅的“恢复 Case”，不创建 Skill revision、EvalPack、Draft PR，也不改变当前生效 Skill。只有经过观察和人工确认后，才讨论是否把高质量的重复恢复 Case 交给后续“修复策略 Skill”门禁。

#### M13-A：恢复链路与最小证据

- [x] 新增 `RecoveryEpisode` 持久化模型，安全关联同一会话/任务范围内的失败 Trace、人工纠正 turn 和后续成功 Trace；原始聊天正文、成员身份、凭据和隐藏推理不得复制进入该模型。
- [x] 仅接受用户业务任务引发的明确失败（如允许工具错误、结果校验失败、范围理解错误）；Cron、测试、通知、发布流水线、框架异常和敏感任务一律排除。
- [x] 记录脱敏后的失败类别、可见症状、纠正方向摘要、恢复操作类别、最终验证结果和不适用边界；无法可靠关联时标记 `insufficient_recovery_evidence`，不推断原因。
- [x] 保持失败告警链路独立：M13 Case 采集失败不得影响原有 QQ 异常通知、重试、dead-letter 或正常 Agent 回复。

#### M13-B：重复恢复 Case 的质量门禁

- [x] 至少 3 个独立恢复 Episode，且来自至少 2 个独立会话；同一 turn 的重试、同一次外部故障连锁和人工测试不计入重复次数。
- [x] 三个 Episode 必须具有相同的失败类别、业务目标/范围和可复用纠正方向，并都以实际验证成功结束；只记录“用户说了什么”但无法核对恢复结果的 Episode 不得入选。
- [x] 明确拒绝：凭据/权限绕过、一次性网络波动、破坏性命令、写入或外部副作用无法安全隔离、纠正方向含敏感内容、以及仅凭模型猜测得出的“恢复规律”。
- [x] 通过后只生成“恢复 Case 审阅卡”：触发症状、失败类别、纠正动作、验证证据、适用/不适用范围和建议结论；默认不发高频 QQ 推送，可在每日汇总中提示有新 Case。

#### M13-C：验收与后续边界

- [ ] 用无害隔离场景验证“失败→用户纠正→成功”关联、脱敏、跨会话去重和排除一次性故障；不写入长期 Skill、不触发发布。
- [ ] 在 QQ 中提供只读查看入口及中文自然语言查询，例如“看看最近的恢复案例”“这类报错有没有被总结过”。
- [ ] 观察至少一周的真实 Case；只有人工确认 Case 表达足够清楚、误关联率可接受时，才另立里程碑设计“修复策略 Skill”候选与评测，不得借 M13 自动打开发布权限。

### M14：审批队列与状态分流（已实施）

M14 解决当前 `/evolve list pending` 与 `/evolve list recent` 容易混淆的问题。`recent` 是历史 Proposal 记录，不等于“最近已经生效的 Skill”；而 Overlay Draft PR 已创建后进入 `pr_created`，虽然仍需要管理员决定是否最终发布，却不适合继续混在“待首次确认”里。

#### M14-A：面向人的状态模型与命令

- [x] 保留 `/evolve list pending` 作为兼容入口，但改为“所有等待管理员动作”的总览，并明确每项的下一步动作。
- [x] 新增 `/evolve list review`：仅列出等待首次审阅/拒绝/批准的 Proposal（`eligible_for_confirmation`、`notified`）。
- [x] 新增 `/evolve list publish`：仅列出已建私有 Draft PR、CI 已通过且等待最终群内发布确认的 Proposal（`pr_created`）；若发布开关关闭，明确显示“可审阅，当前不可发布”。
- [x] 新增 `/evolve list rejected` 与 `/evolve list failed`：分别展示管理员拒绝/质量拒绝和系统失败/告警项；`/evolve list recent` 保持为所有近期 Proposal 的历史视图。
- [x] QQ 菜单、中文自然语言查询和状态文案同步更新，例如“有哪些待我发布的 Skill”“最近拒绝了什么沉淀”“查看失败的进化任务”。

#### M14-B：审阅信息与安全边界

- [ ] 每项输出统一展示：Proposal 编号、中文状态、创建日期（北京时间）、Skill 名称、质量 Gate、PR/CI 摘要、当前是否已生效、下一步可执行命令。
- [ ] `review`、`cases`、`diff` 继续为只读；拒绝、批准、发布仍严格校验群范围、管理员身份、状态机、确认码和幂等键。
- [ ] 列表默认只显示当前群范围内的 Proposal；不得因“最近”查询泄露其他群、成员身份、凭据、原始聊天正文或隐藏推理。

#### M14-C：验收

- [ ] 构造隔离记录覆盖待审阅、待发布、已拒绝、质量拒绝、失败、已发布/已回滚；验证各列表互斥规则、`pending` 总览、北京时间和下一步指令。
- [ ] 在真实 QQ 测试群验证命令与中文自然语言查询；普通成员查询范围、管理员变更权限和跨群隔离不回归。
- [ ] 不为 M14 改变 `adoption_enabled`、`publish_enabled` 或公共仓库边界。

### M15：真实能力评测与基线对照（已实施，待长期 Gateway 场景验收）

M15 解决当前首版 EvalPack 的边界：现有评测能证明候选 Markdown 结构完整、未出现已知危险操作、并满足确定性合同，但不能充分证明“加载这个 Skill 后，真实 Agent 在业务任务上比不加载时更好”。M15 将结构/安全检查保留为必要条件，同时增加隔离的真实 A/B 回放、正反例、反回归检查与独立裁判。它不训练模型、不修改用户记忆，也不自动打开采用或发布。

#### M15-A：评测数据集与防泄漏规则

- [x] 每个 Case 只由脱敏的 `SemanticTaskEvidence` 或已批准的恢复 Case 派生；保留来源 Trace/Case ID、脱敏版本、数据集 hash 和生成时间，不保存完整聊天正文、成员身份、凭据或隐藏推理。
- [x] Case 分为四类：适用正例（应使用候选）、同义/范围变体（同一能力的合理不同表达）、不适用反例（相似但范围/目标不同，必须不套用）、安全反例（敏感、写入、外部副作用或越界请求，必须拒绝/转人工）。每类都要有可核对的预期结果和工具边界。
- [ ] 先稳定划分数据，再生成候选：训练集仅用于形成候选 Skill；验证集仅用于有限次数的候选修订；Holdout 在最终评测前不得提供给生成器或修订过程。禁止把候选 Skill、评测期望或同一完整 Trace 同时用于生成与 Holdout 判分。
- [ ] 采用双证据等级：3～9 个独立真实证据为 `limited`，只保留内部质量记录/周期摘要，不创建 Draft PR；至少 10 个独立真实证据、至少 3 种合理表述或范围，并覆盖正例与反例，才为 `standard`，允许进入自动 Draft PR Gate。模型生成的变体必须标记为合成评测数据，不能替代真实重复证据。
- [ ] 训练/验证/Holdout 默认按稳定 hash 切分；`standard` 数据集至少保留 20% 的真实证据为 Holdout。对不足 10 条的 `limited` 数据集可保留 1 条 Holdout 仅做诊断，不得据此宣称能力已验证。

#### M15-B：隔离 A/B 回放与独立评审

- [x] 在临时、无网络副作用的评测工作区中，对同一份已封存 Case 分别运行“基线 Agent（不加载候选 Skill）”和“候选 Agent（只额外加载候选 Skill）”；统一使用 `glm-5.3-flash`、`high` 推理强度、相同的只读工具白名单和零温度设置。候选 Skill 只写入候选侧临时工作区，评测结束必定清理。
- [x] 在 40 次/日总调用预算下，每候选固定 20 次：两个正例（含范围变体）各做 3 次基线/候选回放，另各做一次不适用与安全反例；每类由独立裁判调用一次。每日最多 2 个候选，预算不足时不启动半套评测。
- [x] 候选执行器与裁判使用独立的新 AgentLoop、独立 session 与独立提示词；裁判只接收脱敏 Case 和可见回答，不接收候选生成上下文。裁判 JSON 不可解析、评测异常、多次正例回放不一致、危险工具或安全判断失败时默认拒绝。
- [ ] 验证集最多允许两次有记录的候选修订；达到上限后必须封存版本并在未见 Holdout 上一次性评测，禁止按 Holdout 结果反复调参。

#### M15-C：放行 Gate 与用户可见证据

只有同时满足以下条件，候选才能创建 Proposal/Draft PR；其中任一失败都只留下质量审计，不通知发布候选：

| 维度 | 默认放行线 | 失败处理 |
|---|---|---|
| 证据等级 | `standard`；至少 10 个独立真实证据，覆盖正例与反例 | 保留为 `limited`，不建 PR |
| 业务完成率 | Holdout 候选均值 ≥ 85%，且不低于基线；适用正例相对基线至少有 10 个百分点可解释改善，或新增了基线缺失的强制约束 | `quality_regression` 或 `no_demonstrable_gain` |
| 范围与格式 | Holdout ≥ 90% 遵守对象范围、输出结构和预期交付物 | `contract_violation` |
| 不适用反例 | 错误套用率 ≤ 5%，应拒绝/转人工的场景正确率 ≥ 95% | `overgeneralization` |
| 安全 | 高风险工具、敏感回显、越界访问均为 0 | `security_violation`，立即停止 |
| 性能 | 中位成本增幅 ≤ 15%，中位延迟增幅 ≤ 20%，且不得以明显降低完成率换取性能 | `performance_regression` |
| 稳定性 | 三次回放的关键结论一致；独立裁判无高不确定性 | `unstable_evaluation` |

- [x] `/evolve review <编号>` 增加“基线 vs 候选”摘要：真实证据数、模型/推理强度、调用数、完成分、改善值、范围拒绝、安全、成本、延迟、稳定性与最终 Gate 原因；`diff` 保持查看 Skill 内容差异。
- [ ] PR 正文与 QQ 通知只展示脱敏聚合结论和可读改善理由；完整逐例内容仅向当前群审批管理员提供只读查询，不展示原始聊天或模型隐藏推理。

#### M15-D：实施与验收

- [x] 运行模式为可配置的 `disabled` / `shadow` / `enforced`；当前运行配置为 `enforced`，即 A/B 不通过、预算耗尽、模型/裁判异常均不会创建 Proposal、Draft PR 或候选 QQ 通知。
- [x] 用隔离 fixture 验证：候选对适用任务有可测改善、对相似反例不误套用、对敏感/写入请求安全拒绝、基线/候选环境完全一致、重复回放可复核，并覆盖两个候选正好耗尽 40 次日预算的边界。
- [ ] 在长期 Gateway 和 QQ 测试群用无害测试 Proposal 验收新的评测摘要、`cases` A/B 展示、质量拒绝和通知抑制；不发布、不合并公共仓库、不改长期生效 Skill。
- [ ] 记录评测成本上限、超时/裁判故障的告警与 dead-letter 处理；评测基础设施异常只阻止该候选，不得影响普通 QQ 对话或把失败误标为候选通过。

#### M15 需要用户确认的产品取舍

1. 本方案默认“至少 10 个独立真实证据”才允许自动创建 Draft PR；3～9 条只留内部质量记录。若希望更快看到候选，可降为 6 条，但误报风险更高。
2. 本方案默认允许以隔离模型生成脱敏的同义/反例测试变体，并要求独立裁判复核；若完全不用模型，则只使用真实证据与规则反例，评测覆盖会更窄但成本更低。
3. 当前已确认预算为：`glm-5.3-flash`、`high` 推理强度、每日最多 2 个候选、最多 40 次模型调用；预算耗尽时 QQ 发送系统异常告警，绝不跳过 Gate 放行。

### M16：失败驱动自进化与人工 Issue 反馈（设计阶段）

M16 将失败场景纳入自进化范围，但与“重复成功任务→Skill”保持独立通道。失败本身不能直接生成 Skill；只有失败后出现可关联的人工纠正、实际验证成功，并得到管理员明确反馈，才允许进入后续恢复 Case、语义记忆或修复 Skill 候选。M16 默认不改变当前生效 Skill、不自动采用、不自动发布。

#### M16-A：失败汇总与 Issue 卡

- [ ] 复用 M13 的 `RecoveryEpisode`，只接收用户业务任务引发的明确失败；排除 Cron、Dream、通知/发布流水线、框架异常、测试记录、敏感任务和无法关联的单次外部故障。
- [ ] 每天北京时间 12:00 由受保护 Cron 执行一次失败汇总；错过不补跑。没有新增 Issue 时不发送 QQ 消息，避免空转打扰；严重重复失败仍可走即时异常告警。
- [ ] 将“失败 Trace→人工纠正→恢复 Trace”整理为脱敏 Issue 卡，字段至少包括：Issue 编号、发生日期、任务目标、可见症状、失败类别、已尝试操作、人工纠正方向、恢复验证、适用/不适用范围和当前建议动作。
- [ ] Issue 卡不得包含原始聊天正文、凭据、成员身份、完整工具参数、隐藏推理或跨群数据；保留来源 Trace/Recovery Case ID 和审计链接，供当前群管理员只读追溯。

#### M16-B：QQ 主动通知与人工评论

- [ ] 新 Issue 只在当天首次汇总时主动通知目标 QQ 群；沿用每日 12 条通知上限、投递幂等、重试和 dead-letter 告警。通知正文只展示摘要和下一步命令。
- [ ] 提供确定性命令：
  - `/evolve recovery list pending`：查看待评论 Issue；
  - `/evolve recovery list all`：查看当前群可见的全部 Issue；
  - `/evolve recovery review <issue-id>`：查看完整脱敏 Issue 卡；
  - `/evolve recovery accept <issue-id> <动作>`：接受建议并指定“记录案例/写入语义记忆/生成修复 Skill 候选”；
  - `/evolve recovery reject <issue-id> <原因>`：拒绝沉淀；
  - `/evolve recovery note <issue-id> <补充>`：补充正确方向或边界，不改变状态，等待再次确认。
- [ ] 支持等价中文自然语言查询，但所有状态变更最终归一到确定性动作、管理员身份、群范围和幂等键校验；普通成员只能查看允许范围，不能接受、拒绝或推动 Skill 候选。
- [ ] 人工动作状态至少包括：`待评论`、`仅记录 Case`、`写入语义记忆`、`生成修复候选`、`已拒绝`、`证据不足`和`已过期`；每次动作记录操作者、北京时间、理由和来源 Issue。

#### M16-C：人工反馈后的沉淀分流

- [ ] “仅记录 Case”：保留恢复 Case 和人工结论，不产生 Skill 或 PR。
- [ ] “写入语义记忆”：只写入脱敏、可复用的规则和适用边界；不得复制完整对话，不得改变当前 Skill。
- [ ] “生成修复 Skill 候选”：必须至少有 3 个独立恢复 Episode、至少来自 2 个独立会话、失败类别/业务目标/纠正方向一致，并且每个 Episode 都实际验证成功；同一次重试、同一次外部故障连锁和人工测试不计数。
- [ ] 修复候选必须进入 M15 的隔离 A/B、正反例、安全和性能 Gate；A/B 未证明改善时只保留 Case/记忆，不创建 Proposal、Draft PR 或发布通知。
- [ ] 修复已有 Skill 时生成独立候选 revision，保留基线、差异、来源 Issue 和回滚点；不得覆盖当前生效版本，不得绕过私有 Overlay 和群内二次确认。

#### M16-D：验收与开关

- [ ] 增加独立的 `recovery_review_enabled`、`recovery_notifications_enabled` 和 `recovery_skill_candidate_enabled` 开关；默认只记录 Recovery Case，后两项默认关闭。
- [ ] 用无害隔离场景验证：失败→人工纠正→成功关联、12:00 汇总、QQ 通知、管理员评论、拒绝/仅记录/生成候选三种分流、跨会话去重、脱敏和通知失败恢复。
- [ ] 观察至少一周真实失败 Case 后，再决定是否打开 `recovery_skill_candidate_enabled`；不得因为 M16 开启而自动打开 `adoption_enabled` 或 `publish_enabled`。
- [ ] 任何 Issue 汇总、评论解析或候选生成异常都必须留下审计并向 QQ 告警；不得静默丢弃失败场景，也不得将异常误标记为人工已接受。

#### M16 的产品原则

1. 失败是进化信号，但不是 Skill 证据；人工纠正和实际恢复结果才是最小闭环。
2. 12:00 是失败 Issue 汇总时间，14:00 仍是成功任务候选扫描时间；两条通道互不覆盖。
3. 人工评论是方向信号，不是直接上线授权；任何 Skill 变化仍必须经过 M15 Gate、私有 Draft PR 和既有群内发布确认。

## 11. 测试与验收矩阵

| 场景 | 预期结果 | 必须证据 |
|---|---|---|
| Phase 6 关闭 | 不扫描、不写 Proposal、不发 QQ 消息 | 状态、数据库、投递队列为空 |
| 低风险重复成功 Trace | 生成候选并通过评测后得到待通知 Proposal | Trace/Case/EvalRun/Proposal 关联 |
| 重复的失败→人工纠正→成功 | 只形成脱敏恢复 Case；未满足 M13 门禁时不生成 Skill/PR | 失败、纠正、成功 Trace 关联；恢复类别、验证和排除理由 |
| M16 12:00 失败汇总无新增 Issue | 定时任务成功结束但不发送空通知 | Cron 执行记录、汇总游标、无新增 Delivery |
| M16 新增失败 Issue | QQ 主动发送一条脱敏 Issue 摘要，可用 `recovery review/list` 查看 | Issue、Delivery、群范围和通知幂等记录 |
| M16 管理员评论“仅记录/记忆/修复候选” | 按分流状态落库；只有“修复候选”继续进入 M15，不直接改生效 Skill | 管理员动作、理由、状态转移、审计 |
| M16 失败证据不足或偶发外部故障 | 标记 `证据不足`/`仅记录 Case`，不生成 Skill、Proposal 或 PR | 排除原因、来源 Episode、零新增 Proposal |
| M16 非管理员评论或跨群 Issue | 拒绝状态变更，不泄露 Issue 内容 | 权限拒绝、群 scope 和审计 |
| 框架事件或 JSON 指纹重复 | 被语义质量门禁拒绝；不生成 Skill、EvalPack、Draft PR 或 QQ 通知 | `rejected_by_quality_gate` 原因、零新增 Proposal/PR/Delivery |
| 重复的可解释业务任务 | 影子期仅产生结构化候选说明和质量结论；恢复开关后才可进入 Proposal | 脱敏语义投影、跨 turn 证据、质量评分、人工抽查记录 |
| M15 真实能力评测 | 在相同隔离环境中候选相对基线有可复核改善，且反例不误套用、安全/性能不退化 | 数据集分割 hash、三次 A/B 回放、独立裁判、聚合指标与拒绝理由 |
| 泛化只读模板 | 因无具体行为增量被拒绝，不能靠安全 Gate 单独通过 | 候选内容检查、质量 Gate 理由、无 Draft PR |
| Gate 失败或证据不足 | Proposal 不可批准、不通知或标记失败原因 | Gate 结果与状态 |
| 通知投递暂时失败 | 重试且不重复发送；超过上限 dead-letter | Delivery 记录与审计 |
| 普通群成员 `/evolve approve` | 拒绝，无状态修改 | 授权拒绝审计 |
| 管理员错误码/过期码 | 拒绝，无状态修改 | Action 审计 |
| 管理员重复确认 | 返回幂等结果，不重复写 Skill/建 PR | 文件 hash/PR 数量/动作日志 |
| 审批队列查询 | `review`、`publish`、`rejected`、`failed` 和 `recent` 按状态正确分流；`pending` 给出待动作总览 | 列表内容、北京时间、下一步命令、群范围审计 |
| workspace 采用 | 下一独立 turn 加载新 Skill | 新旧 hash、Trace、`SkillsLoader` 结果 |
| workspace 回滚 | 恢复已知 revision，下一 turn 生效 | CAS、落盘 hash、审计 |
| shared Skill 批准 | 仅创建 Draft PR | PR 状态，无合并/部署 |
| 发布确认 | CI 成功后按既有流程部署 | Git、PR、Gateway build、真实群场景 Trace |
| Overlay 提交/扫描失败 | 不创建 PR、不改生效 Skill；QQ 收到脱敏中文异常告警；投递失败可重试且可审计 | Proposal、`phase6_alert_deliveries`、群消息、Overlay 状态 |
| 遗留系统候选 | 只恢复已验证的系统生成暂存文件，拒绝处理任意用户改动；下一次扫描可安全重试 | 分支/路径/hash 比对、恢复审计、后续 Draft PR 或异常告警 |
| 群隔离 | A 群不能查询/批准 B 群 Proposal | 群 scope、命令拒绝、检索审计 |

涉及 QQ、审计、Agent 流程、持久化和用户可见行为的里程碑，除聚焦 `pytest`/`ruff` 外，都必须在长期 Gateway 的全新 WebUI 会话和一个明确授权的 QQ 测试群完成真实场景验收。场景不得使用真实生产 Skill、长期记忆或凭据作为测试对象。

## 12. 运行与回滚

### 12.1 开关

至少提供以下独立开关，避免一个 `enabled` 同时控制扫描、通知、采用和发布：

```text
phase6.enabled                         # 是否可做受控自动评审
phase6.evolution.shadow_mode           # 只评审，不通知/采用
phase6.evolution.notifications_enabled # 是否允许 QQ 主动通知
phase6.evolution.adoption_enabled      # 是否允许 workspace Skill 原子采用
phase6.evolution.draftPrEnabled        # 是否允许 Overlay 自动创建 Draft PR
phase6.evolution.publish_enabled       # 是否允许处理二次发布确认，默认 false
phase6.evolution.overlayRepository     # 私有 Overlay 仓库 owner/name；为空则不注入发布回调
phase6.evolution.overlayBaseBranch     # Overlay 基线分支，默认 main
phase6.kill_switch                      # 立即禁止所有自动化写路径
```

开关的实际启用顺序必须遵循 M4～M9：先只开 `phase6.enabled` 做影子评审，再开通知，再开人工批准后的 workspace 采用，再准备个人 Overlay、创建个人 Draft PR，最后才允许二次确认后的个人 Gateway 发布。单独打开 `phase6.enabled` 不会授予 Agent 自己修改正式 Skill、合并公共 PR 或部署的权限。

当前运行态为：`phase6.enabled=true`、`shadow_mode=false`、`notifications_enabled=true`、`adoption_enabled=false`、`draftPrEnabled=true`、`publish_enabled=false`。自动候选在通过语义质量、安全 Gate 与评测后可创建私有 Overlay Draft PR；发布仍需独立二次确认。

当 `overlayRepository` 已配置时，Gateway 会在启动时注入受控的 GitHub Overlay
校验、合并和热加载回调；未配置时 `/evolve publish` 会安全地返回“发布回调尚未配置”，
不会尝试 Git 或文件写入。GitHub 凭据只从受控环境映射给 `gh`，不进入 Proposal 或日志。

`kill_switch` 不撤销已采用版本，但会停止新的扫描、通知、采用、PR 创建和发布。回滚必须由管理员显式命令完成，并使用现有 revision 证据。

### 12.2 Skill 热加载与连接稳定性

个人 Overlay 发布不再调用 `scripts/rebuild_gateway_for_scenario.sh`，也不执行
`docker compose restart`。发布适配器只下载已合并 commit 的安全路径
`skills/*/SKILL.md`，逐个执行“临时文件 → fsync → 原子 rename”。当前正在执行的
Agent turn 保持已经构建好的旧 Context；下一轮 turn 的 `SkillsLoader` 会重新扫描并
读取新文件，因此无需重启 Gateway，WebUI、QQ 和已有 WebSocket 连接保持不变。

如果未来修改的是 Python 代码、依赖、Docker 镜像或 Skill 加载器本身，仍然需要按
代码部署流程重建 Gateway；“热加载”只覆盖个人 Overlay 的 Markdown Skill 内容。

个人 Overlay 的 Git/GitHub 子进程可从忽略的本地 `.env` 读取
`NANOBOT_GITHUB_PROXY_URL`，仅用于访问 GitHub；Token 仍只经受控环境变量临时传给
子进程，不进入 Git URL、提交、Proposal 或 QQ。若 WSL 网络重建导致 Windows 主机地址变化，
Draft PR/CI 核验会留下可重试审计并向 QQ 发送异常告警；更新该本地代理地址后，下一次受保护
扫描会安全重试，不能静默卡住。

### 12.3 Gateway 重启与连接恢复说明

2026-09-27 对长期 Gateway 做了真实 WebUI 观测：`docker compose restart nanobot-gateway` 会发送 SIGTERM，Gateway 依次停止 QQ、WebSocket 和 Agent，再重新初始化 MCP、QQ 与 WebSocket；这段时间 `8765` 没有可用连接，已有 TCP/WebSocket 连接必然断开。日志显示这是正常的容器生命周期行为，不是 Phase 6 或 Skill 采用导致的断连。

WebUI 的 `NanobotClient` 已有指数退避和 token 刷新重连；真实观测中状态依次为“重连中→连接中→已连接”，约 9.8 秒恢复。直接使用 WebSocket 的 VSCode/脚本客户端如果没有同等重连逻辑，会表现为永久断开，需要客户端自行重连或刷新连接。重启后应等待健康检查通过，再继续发送消息；不要把短暂的重连窗口误判为数据丢失。

2026-09-27 M9 重建证据：代理 `172.22.208.1:7890` 恢复后，宿主机可访问 Docker Registry；BuildKit 首次拉取基础镜像令牌仍超时，随后单独拉取 `node:24-bookworm-slim` 成功，重新执行 `scripts/rebuild_gateway_for_scenario.sh` 完成构建。最终 health 为 `http://127.0.0.1:18790/health`，构建引用为 `git-d605b959d5e6`，长期 Compose 服务为 `nanobot-gateway`，未启动临时 Gateway、未更换端口、未使用损坏旧镜像。

### 12.3 监控指标

- 每周期 Trace 扫描数、候选数、Gate 通过率、证据不足率；
- 每群通知数、投递失败/重试/dead-letter 数；
- 管理员批准、拒绝、过期、重复命令和越权尝试数；
- workspace 采用成功率与回滚次数；
- PR 创建、CI 失败、发布失败和自动暂停次数；
- 群感知数据的脱敏拒绝数与跨群隔离拒绝数。

告警阈值建议：连续 3 个回归、任一越权成功、任一跨群数据暴露、连续 3 次通知 dead-letter，均立即暂停自动化并向管理员通知。

## 13. 重新制定后的实施顺序与用户配合清单

### 13.1 当前已确认的外部条件

以下条件已经由用户完成，不再作为阻塞项：

1. 私有仓库为 `Trees-23/KdmCopilot-skills-private`，默认分支为 `main`。
2. Overlay `main` 已配置保护；CI 基线 PR #3 已合并并通过检查。公共 `Trees-23/KdmCopilot:main` 永远不是个人 Skill 发布目标。
3. Overlay 只服务当前 `nanobot-gateway`；合并后只热加载当前 Gateway，不发布公共镜像，也不重启容器。
4. GitHub 凭据只通过 `gh` 登录态或受控环境注入，不进入 QQ、日志、Proposal 正文或 Git 提交。

### 13.2 接下来按顺序推进

1. 用临时测试 Skill 在私有 Overlay 创建一条新的 Draft PR，验证仓库、base、分支保护和 CI 状态回写。（已完成）
2. 在真实 QQ 群内投递一次 CI 通过后的发布候选通知。（已完成）
3. 配置 `GITHUB_PERSONAL_ACCESS_TOKEN` 并让 Gateway 获得私有 Overlay 访问权限。（已完成）
4. 创建临时测试 Skill/Draft PR 并等待 CI；向真实 QQ 群投递发布候选通知。（已完成）
5. 在群内完成 `/evolve publish` 两次确认、私有 PR 合并和热加载。（已完成）
6. 验证下一轮 Context 读取、临时 Skill 回滚和审计记录。（已完成）
7. 关闭发布开关并清理临时 Proposal/Skill/分支。（已完成）
8. 完成 M16 设计审阅，先接入 12:00 失败 Issue 汇总与只读 QQ 通知；默认保持 `recovery_skill_candidate_enabled=false`，观察真实 Case 后再决定是否开放修复 Skill 候选。（设计中）

### 13.3 在你配合前明确不做的事情

- 不向 `Trees-23/KdmCopilot:main` 创建个人 Skill PR。
- 不把个人聊天、群策略、私有配置或运行数据库提交到任何 Git 仓库。
- 不打开 `publish_enabled`，不合并、不部署、不重建公共版本。
- 不把“群里有人看到确认码”当作授权；真正授权依据是发送者 openid、群白名单、@要求、Proposal 状态/CAS 和一次性确认码。
- 不因为创建 Draft PR 就改变当前 Gateway 的 Skill；只有 M9 二次确认发布后才会更新你的 Gateway。

### 13.4 固定的人工门禁

`/evolve publish` 二次确认固定在 QQ 群内完成：仅 `approvalAdminOpenids` 中的管理员有效，必须 @机器人并使用一次性确认码；不改为 C2C 私聊。CI 只负责评测和安全检查，不代表允许合并；`publish_enabled` 只在隔离发布窗口临时开启。

## 14. 最终 Definition of Done

以下条件全部满足才可称为“QQ 群内受控 Skill 进化上线”：

1. M0～M3 已完成并验收；M4 作为独立变更打开 `phase6.enabled` 进入影子模式，且所有新增自动化都有独立 feature flag 和立即 kill switch。
2. 自动评测通过只会产生可追溯、可过期的 Proposal，不会自动升级或发布。
3. 指定群能收到一次、脱敏、可审阅的通知；投递可重试、可查、可去重。
4. 群命令是确定性路由，管理员、群、确认码、Proposal 状态和基线版本均经过校验。
5. 普通成员、错误群、重放请求、过期确认和版本冲突无法造成状态改变。
6. workspace Skill 能原子采用、下一轮生效、可审计、可回滚，且通常不需要重启 Gateway。
7. 个人 Skill 只在私有 Overlay 创建 Draft PR；合并和部署需要独立 `publish` 确认与 CI 验收，发布后热加载当前 Gateway，公共 `Trees-23/KdmCopilot:main` 不发生个人 Skill 变更。
8. 自动化暂停、失败恢复、通知 dead-letter、回滚和跨群隔离均有单元、集成和真实 Gateway 场景证据。
9. 自动候选必须有脱敏、可读的业务任务语义与可验证的行为增量；框架事件、空壳模板和仅因“重复成功”形成的候选不得创建 Draft PR 或通知管理员。
