# QQ 群内 Skill 进化、主动通知与人工发布实施方案

> 文档状态：实施方案草案
>
> 更新时间：2026-09-26
>
> 范围：在已完成的五层记忆 Phase 0～6 之上，为当前 nanobot 的官方 QQ 群机器人增加“自动评审 → 主动推送 → 群内交互确认 → 受控升级/发布”的完整闭环。
>
> 本文不启用 Phase 6、不修改 QQ 配置、不创建 GitHub PR、不发布任何 Skill。它是后续功能分支的实施与验收依据。

## 1. 决策摘要

目标闭环如下：

```text
群内任务与运行 Trace
        ↓
低风险候选筛选、Case 派生、Skill 候选暂存
        ↓
独立回放与自动评审 Gate
        ↓
持久化 Proposal（只读、待确认）
        ↓
官方 QQ 群主动通知
        ↓
管理员群命令审阅、同意或拒绝
        ↓
workspace Skill 原子采用 / 共享 Skill 创建 Draft PR
        ↓
（仅代码或共享 Skill）管理员二次确认后合并与部署
```

核心结论：自动评审可以自动化，正式采用和发布不可自动化。评测通过的唯一含义是 `eligible_for_confirmation`，不是自动切换当前 Skill，也不是自动创建、合并或发布 GitHub PR。

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

### 2.2 当前未接通的部分

1. `Phase6Proposal` 只是内存 DTO，尚未存入数据库，也没有完整状态机。
2. 没有周期调度器把“Trace → 候选 → EvalPack → EvalRun → Proposal”串起来。
3. 没有系统级 QQ 通知器、通知重试记录或目标群配置。
4. 没有 `/evolve` 命令、管理员权限、一次性确认码和幂等确认机制。
5. 没有 workspace Skill 原子采用器，也没有从已确认 Proposal 创建 GitHub Draft PR 的发布适配器。
6. 官方 QQ 当前配置使用 `allowFrom: ["*"]`。这只表示所有成员可向 Agent 发消息，绝不能当作升级审批权限。

### 2.3 已确认的部署事实

- 长期 Gateway 运行在 `nanobot-gateway`，挂载仓库的 `runtime/` 到容器内运行根目录。
- 当前启用的是官方 `qq` 通道，不是 NapCat；配置未显式设置 `phase6`，因此仍使用默认关闭状态。
- 官方 QQ 通道源码有群收发路径，群消息路由依赖 QQ 的 `group_openid` 与出站元数据 `qq_chat_type=group`。
- 当前配置文件不保存唯一目标群标识；上线前必须由管理员明确配置允许感知与允许通知的群。

## 3. 目标范围与非目标

### 3.1 本期目标

1. 对低风险、重复成功的真实任务自动生成候选，并自动运行已定义的评测流程。
2. 在评测通过后，向指定 QQ 群主动发送一条可审阅、带证据编号的 Proposal 通知。
3. 允许指定 QQ 管理员在群内查询、审阅、批准、拒绝和回滚 Proposal。
4. workspace Skill 经过一次批准后以原子方式写入规范 `SKILL.md`，下一轮 Agent 自动读取。
5. builtin/shared/entrypoint/MCP Skill 经过批准后创建 Draft PR；合并与部署需要独立的第二次批准。
6. 对通知、命令、授权判定、版本 CAS、采用、PR、发布和回滚生成结构化审计证据。

### 3.2 明确不做

- 不读取、保存或总结整个群聊历史作为长期记忆。
- 不根据模型自然语言回复、点赞、表情或模糊措辞执行采用/发布。
- 不因评测通过自动修改 `current_revision_id`、覆盖 `SKILL.md`、创建合并提交或部署 Gateway。
- 不让普通群成员拥有审批或回滚权限。
- 不将 QQ AppSecret、GitHub Token、群 openid、管理员 openid 写入 Git、审计正文或群通知。
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
Trace / Case ──→ Phase6 Orchestrator ──→ Proposal Repository │
                       │                    │               │
                       ▼                    ▼               │
                EvalPack / EvalRun       审阅状态机 ─────────┘
                       │                    │
                       ▼                    ▼
                 Gate / 独立回放      Adoption / PR Publisher
                                         │
                      ┌──────────────────┴──────────────────┐
                      ▼                                     ▼
             workspace SKILL.md                      Draft PR + CI
             原子替换、下一轮生效                     二次确认后发布
```

所有自动化写入均在后台受控服务完成。普通 Agent、维护评测角色和 QQ 群成员都不直接拥有文件写入、Git 或网络发布权限。

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
  → approved
      ├─ adopting            → adopted
      └─ creating_pr         → pr_created
                              → publish_approved
                              → publishing → published
  → rejected_by_admin | expired | failed | rolled_back
```

状态不可倒退；重评估、重试或新候选必须创建新的 Proposal。`approved`、`adopted`、`pr_created`、`published` 和 `rolled_back` 都要保存操作人、时间、基线版本、目标版本、原因和关联审计事件。

### 6.2 关键并发与安全规则

- 提案包含 `baseline_revision_id`、`candidate_revision_id`、`candidate_hash` 和 `version_epoch`。
- `approve`/`adopt`/`publish`/`rollback` 使用条件更新（CAS）。当前版本、Proposal 状态或确认码变化时拒绝操作，不做“最后写入者获胜”。
- 确认码只保存哈希，默认 15 分钟有效、单次使用；群通知只展示短码，不展示完整敏感路由信息。
- 相同命令的网络重放返回已完成的幂等结果，绝不重复写文件、重复建 PR 或重复发布。
- Gate 结果、工具 schema digest、fixture hash 或候选 hash 变更时 Proposal 转为 `stale`，需要重新评测。

## 7. QQ 群交互协议

### 7.1 命令

| 命令 | 所有人 | 管理员 | 副作用 |
|---|---:|---:|---|
| `/evolve status` | 是 | 是 | 无 |
| `/evolve list [pending\|recent]` | 是 | 是 | 无 |
| `/evolve review <proposal-id>` | 是 | 是 | 无；只显示脱敏证据摘要 |
| `/evolve approve <proposal-id> <code>` | 否 | 是 | workspace 进入采用或共享 Skill 创建 Draft PR |
| `/evolve reject <proposal-id> <reason>` | 否 | 是 | 终止该 Proposal |
| `/evolve publish <proposal-id> <code>` | 否 | 是 | 仅对已创建 PR 的共享/代码 Skill 触发发布流程 |
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
  pr_publisher.py           # 仅由已批准 Proposal 创建 Draft PR
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

实现一个 Gateway 生命周期管理的系统任务，例如每 2 小时扫描一次，单 workspace lease 串行执行。它直接调用受限 Python 编排器，而不是让普通 Agent Cron 通过自然语言决定是否评测、是否通知或是否发布。

每周期上限：最多 20 个候选、每群每天最多 3 条通知、同一 Skill 同时至多 1 个待确认 Proposal。系统重启后从数据库恢复状态，投递失败使用有限指数退避；超过上限转为 `dead_letter` 并主动产生管理员告警，不静默丢失。

## 9. 采用与发布行为

| Skill 来源 | `approve` 后的动作 | 是否需要重启 | `publish` |
|---|---|---:|---|
| workspace | 校验 hash/CAS 后原子写入 `<workspace>/skills/<name>/SKILL.md`，保留可回滚 revision | 否；下一次 Context 构建生效 | 不需要 |
| builtin/shared | 创建独立分支、中文提交、Draft PR，并附证据链接 | 合并部署后需要 | 二次批准后才允许合并/部署 |
| entrypoint/MCP | 创建 Draft PR 或对应包的受控发布提案 | 视部署方式而定 | 二次批准、CI 与发布检查 |

workspace Skill 采用使用“写临时文件 → fsync → 原子 rename → 记录 revision/CAS → 审计”的顺序。写入失败不得改变 `current_revision_id`。当前正在执行的 Agent turn 保持旧上下文；后续 turn 自动通过 `SkillsLoader` 读取新文件。

共享 Skill 发布继续遵循仓库规则：在专用分支创建中文提交与 Draft PR，经过 CI 后等待 `publish` 的独立管理员确认；任何情况下不得自动合并 `main`。

## 10. 分阶段实施计划

### M0：上线前配置与安全基线

- [ ] 确认目标 QQ group openid、审批管理员 QQ user openid、是否要求 @ 才接收 `/evolve`。
- [ ] 将聊天 `allowFrom` 与审批 `approvalAdminOpenids` 分离；评审 `allowFrom: ["*"]` 是否应收窄。
- [ ] 明确群内通知时段、每天配额、Proposal 有效期、是否同时私聊管理员。
- [ ] 确认 GitHub Draft PR 的目标仓库、默认分支、CI 状态检查与部署责任人。
- [ ] 记录当前 Gateway 构建标识、镜像、运行根目录和当前 Phase 6 关闭状态，作为回滚基线。

完成条件：所有标识和策略通过运行态配置注入，Git 不含真实 openid、token 或 secret。

### M1：Proposal 持久化与状态机

- [ ] 新增迁移和 repository；实现 Proposal、Delivery、Action、Group Scope 的不可变/可变字段边界。
- [ ] 实现状态转移、版本 CAS、过期、确认码哈希、动作幂等与审计。
- [ ] 将现有 `make_proposal()` 接入持久化模型，但保持 Phase 6 默认关闭。
- [ ] 编写单元和并发测试：重复批准、过期批准、旧基线、重复通知、迁移恢复。

完成条件：不依赖 QQ 即可通过 API/测试构造、查询和安全地终结一份 Proposal。

### M2：系统自动评审编排

- [ ] 实现系统级受限周期任务和 workspace lease。
- [ ] 接通 Trace → 低风险选择 → Case → EvalPack → 独立回放 → Gate → Proposal。
- [ ] 实现配额、去重、暂停开关、回归保护和 dead-letter 告警。
- [ ] 先提供影子模式：生成评测和 Proposal，但不通知、不采用。

完成条件：在 fixture Trace 集中可重复生成同一 Gate 与 Proposal，Phase 6 关闭时无任何写入或通知。

### M3：QQ 通知与群内命令

- [ ] 实现群目标路由与 `qq_chat_type=group` 投递；保留投递重试和结果审计。
- [ ] 实现 `/evolve status/list/review/approve/reject`，命令绕过 LLM。
- [ ] 实现群白名单、管理员 openid 校验、确认码校验、限流、脱敏输出和跨群拒绝。
- [ ] 增加官方群收发模拟测试及真实 Gateway 的安全测试群验收。

完成条件：非管理员、错误群、错误码、过期码、重复命令均被拒绝；管理员能在测试群完成“通知—审阅—批准”的闭环。

### M4：workspace Skill 采用与回滚

- [ ] 实现原子采用器、revision 对比、落盘 hash 校验和错误回滚。
- [ ] 实现 `/evolve rollback`，只能恢复已知 revision，必须二次确认码。
- [ ] 验证新 Skill 在下一条独立群消息中生效，当前执行 turn 不受中途替换影响。

完成条件：真实 Gateway 场景中可以对测试 Skill 采用、验证、回滚，且不改变长期 workspace 的无关文件。

### M5：GitHub Draft PR 与受控发布

- [ ] 实现已批准 Proposal 到专用分支、中文提交、Draft PR 的适配器。
- [ ] PR 正文自动附“改动内容、评测证据、验证结果、风险与注意事项”，不泄露聊天内容。
- [ ] 实现 `/evolve publish` 二次确认，串联 CI、PR 状态和既有发布流程。
- [ ] 合并后按仓库规则重建长期 Gateway，并记录构建标识与场景证据。

完成条件：从测试共享 Skill 创建 Draft PR；未执行 `publish` 前没有合并、部署或当前版本切换。

### M6：灰度、观测与正式启用

- [ ] 先影子运行 7 天，观察候选数量、Gate 通过率、通知失败率、管理员拒绝率和误报原因。
- [ ] 再启用“自动评审 + 群通知”，保持采用/发布需要人工确认。
- [ ] workspace 采用稳定后，再开启 Draft PR 创建；发布仍始终二次确认。
- [ ] 每周输出简短运营摘要，超过阈值自动暂停 Phase 6 并通知管理员。

完成条件：连续 7 天无未处理 dead-letter、无越权命令成功、无自动发布、无跨群泄露，且管理员认可通知频率。

## 11. 测试与验收矩阵

| 场景 | 预期结果 | 必须证据 |
|---|---|---|
| Phase 6 关闭 | 不扫描、不写 Proposal、不发 QQ 消息 | 状态、数据库、投递队列为空 |
| 低风险重复成功 Trace | 生成候选并通过评测后得到待通知 Proposal | Trace/Case/EvalRun/Proposal 关联 |
| Gate 失败或证据不足 | Proposal 不可批准、不通知或标记失败原因 | Gate 结果与状态 |
| 通知投递暂时失败 | 重试且不重复发送；超过上限 dead-letter | Delivery 记录与审计 |
| 普通群成员 `/evolve approve` | 拒绝，无状态修改 | 授权拒绝审计 |
| 管理员错误码/过期码 | 拒绝，无状态修改 | Action 审计 |
| 管理员重复确认 | 返回幂等结果，不重复写 Skill/建 PR | 文件 hash/PR 数量/动作日志 |
| workspace 采用 | 下一独立 turn 加载新 Skill | 新旧 hash、Trace、`SkillsLoader` 结果 |
| workspace 回滚 | 恢复已知 revision，下一 turn 生效 | CAS、落盘 hash、审计 |
| shared Skill 批准 | 仅创建 Draft PR | PR 状态，无合并/部署 |
| 发布确认 | CI 成功后按既有流程部署 | Git、PR、Gateway build、真实群场景 Trace |
| 群隔离 | A 群不能查询/批准 B 群 Proposal | 群 scope、命令拒绝、检索审计 |

涉及 QQ、审计、Agent 流程、持久化和用户可见行为的里程碑，除聚焦 `pytest`/`ruff` 外，都必须在长期 Gateway 的全新 WebUI 会话和一个明确授权的 QQ 测试群完成真实场景验收。场景不得使用真实生产 Skill、长期记忆或凭据作为测试对象。

## 12. 运行与回滚

### 12.1 开关

至少提供以下独立开关，避免一个 `enabled` 同时控制扫描、通知、采用和发布：

```text
phase6.enabled                         # 是否可做受控自动评审
phase6.evolution.shadow_mode           # 只评审，不通知/采用
phase6.evolution.notifications_enabled # 是否允许 QQ 主动通知
phase6.evolution.adoption_enabled      # 是否允许 workspace adopt / Draft PR
phase6.evolution.publish_enabled       # 是否允许处理二次发布确认，默认 false
phase6.kill_switch                      # 立即禁止所有自动化写路径
```

`kill_switch` 不撤销已采用版本，但会停止新的扫描、通知、采用、PR 创建和发布。回滚必须由管理员显式命令完成，并使用现有 revision 证据。

### 12.2 监控指标

- 每周期 Trace 扫描数、候选数、Gate 通过率、证据不足率；
- 每群通知数、投递失败/重试/dead-letter 数；
- 管理员批准、拒绝、过期、重复命令和越权尝试数；
- workspace 采用成功率与回滚次数；
- PR 创建、CI 失败、发布失败和自动暂停次数；
- 群感知数据的脱敏拒绝数与跨群隔离拒绝数。

告警阈值建议：连续 3 个回归、任一越权成功、任一跨群数据暴露、连续 3 次通知 dead-letter，均立即暂停自动化并向管理员通知。

## 13. 实施前待确认项

1. 目标 QQ 群是否同时允许普通对话，还是仅用于运维通知与 `/evolve`？
2. 群内 `approve` 是否足够，还是对 `publish`/`rollback` 强制要求管理员 C2C 私聊二次确认？建议后者。
3. 管理员数量、群 openid、管理员 openid 和通知时间窗是什么？
4. workspace Skill 的哪些目录允许自动采用？默认建议仅 `runtime/workspace/skills/` 下的显式白名单。
5. shared/builtin Skill 的 GitHub 仓库、CI 状态检查和部署责任人是什么？
6. 影子运行期是否接受每天一份汇总通知，还是只在出现通过 Proposal 时通知？

在这些项目确认前，可以完成 M1、M2 的纯本地实现与测试，但不能安全开启 M3 以后的群通知和采用权限。

## 14. 最终 Definition of Done

以下条件全部满足才可称为“QQ 群内受控 Skill 进化上线”：

1. Phase 6 默认关闭，所有新增自动化都有独立 feature flag 和立即 kill switch。
2. 自动评测通过只会产生可追溯、可过期的 Proposal，不会自动升级或发布。
3. 指定群能收到一次、脱敏、可审阅的通知；投递可重试、可查、可去重。
4. 群命令是确定性路由，管理员、群、确认码、Proposal 状态和基线版本均经过校验。
5. 普通成员、错误群、重放请求、过期确认和版本冲突无法造成状态改变。
6. workspace Skill 能原子采用、下一轮生效、可审计、可回滚，且通常不需要重启 Gateway。
7. shared/代码 Skill 只创建 Draft PR；合并和部署需要独立 `publish` 确认与既有 CI/发布验收。
8. 自动化暂停、失败恢复、通知 dead-letter、回滚和跨群隔离均有单元、集成和真实 Gateway 场景证据。
