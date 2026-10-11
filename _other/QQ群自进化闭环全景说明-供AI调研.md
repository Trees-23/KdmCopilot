# QQ 群 Skill 自进化闭环全景说明

> 用途：供后续 AI 分析、调研和修改 nanobot QQ 群自进化能力。
>
> 口径：本文描述当前仓库代码、长期 Gateway、workspace 运行库和已完成的真实验收。文中“候选”“Proposal”“Case”都不等于正式 Skill。
>
> 当前核对时间：2026-10-12（Asia/Shanghai）

## 0. 先看结论

当前系统有两条业务路线：

1. **成功任务路线**：用户任务正常完成，系统从成功证据中判断是否值得形成一份可复用的候选操作规范，再经过质量检查、对照评测、安全评测和采用门禁，最终才可能成为 workspace 正式 Skill。
2. **失败任务路线**：任务失败后形成 Issue。管理员可以拒绝并归档，也可以提交修复方向；只有 `revise` 才允许继续形成候选，并与成功路线共用后续评测、采用和回滚流程。

当前已经完成的清理动作：

- 11 条低质量成功路线候选已关闭，保留审计记录，不再继续评测。
- 2 条长期卡住的候选评测已人工终止，状态改为失败，原因标记为人工质量拒绝。
- 之前成功采用的 `evolution-task-8f308f24d1` 已回退到 baseline。
- 当前运行库中没有候选处于 `evaluating`，也没有评测处于 `running`。
- 当前正式生效目录中的候选 Skill 已恢复为 baseline；没有一条由本轮成功/失败候选产生的增量 Skill 正在生效。

当前仍需处理的核心问题不是“能不能生成 Skill”，而是：

- 候选生成太早，成功一次或重复几次就可能产生候选草稿；
- “做成功过”被误当成“值得沉淀”；
- 同一能力类别没有先合并再生成一个候选，导致重复候选；
- 候选状态和评测状态收尾不完整；
- 评测按顺序串行执行，单个外部模型连接异常可能阻塞后续候选。

后续改造目标应是：**先按能力类别聚合成功证据，确认长期有用，再为一类能力生成一个候选；候选必须独立、有超时、有终态、有去重。**

## 1. 业务边界和名词

### 1.1 两条路线不是两套系统

两条路线的后半段共用同一个候选、评测、Proposal、workspace 采用和回滚机制。区别只在来源：

| 路线 | 来源 | 谁可以决定继续 | 不能做什么 |
| --- | --- | --- | --- |
| 成功任务路线 | 正常完成且有可验证工具结果的任务 | 系统先聚合、再自动送评测 | 不能把一次成功直接当成正式 Skill |
| 失败任务路线 | 工具失败、任务失败、失败恢复 Episode | 管理员选择 `reject` 或 `revise` | 普通用户不能直接把失败记录变成 Skill |

### 1.2 资产的业务含义

后文统一使用下面的含义：

- **证据（Evidence）**：任务发生过什么的脱敏记录，包括目标、范围、顺序步骤、工具类别、风险和验证结果。证据不是 Skill。
- **Case**：可归档、可回放或可用于评测的案例。Case 不是 Skill，也不自动修改当前 Skill。
- **Issue**：失败任务经过聚合后的待处理问题卡片。Issue 不是 Skill。
- **候选（Candidate）**：根据成功证据或管理员 `revise` 方向生成的隔离 Skill 草稿。候选不会自动进入 Agent Context。
- **评测（Evaluation）**：对 baseline 和 candidate 进行对照检查的运行记录。评测通过也不等于已经写入 Skill，仍需 Proposal/采用门禁。
- **Proposal**：说明某个候选是否可以采用、目标是什么、baseline/candidate hash 是什么的审批对象。Proposal 不是 Skill。
- **正式 workspace Skill**：写入 `runtime/workspace/skills/<skill-name>/SKILL.md`，并由下一轮 Agent Context 读取的当前生效文件。只有这个目录里的文件会直接影响当前 Agent。
- **Overlay Draft PR**：个人私有 Overlay 的候选发布物。它只能是私有 Draft PR，不能自动合并、发布或修改公共 `main`。

判断某项是不是正式 Skill 的唯一直接依据：

```text
runtime/workspace/skills/<skill-name>/SKILL.md
```

数据库里的 candidate、Proposal、Case、评测记录、Overlay 文件都不能替代这个判断。

## 2. 成功任务路线

### 2.1 完整链路

```text
用户在 WebUI/QQ 完成任务
  ↓
Trace、工具事件、脱敏任务摘要
  ↓
任务目标包 + 有序步骤证据
  ↓
按能力类别聚合重复成功任务
  ↓
业务价值和安全边界检查
  ↓
生成一个新 Skill candidate，或已有 Skill 的 revision candidate
  ↓
候选隔离保存，当前正式 Skill 不变
  ↓
对照检查：baseline vs candidate
  + 正常案例
  + 范围变体
  + 不适用反例
  + 安全反例
  + 失败恢复
  + 性能和稳定性
  ↓
质量门禁通过
  ↓
生成 workspace Proposal
  ↓
只允许 allowlist 中的 workspace Skill 原子采用
  ↓
临时文件 → fsync → 原子 rename → revision CAS → 采用审计
  ↓
下一轮 Agent Context 从 workspace/skills 读取新 Skill
  ↓
如发现问题，使用一次性确认码回滚 baseline
```

### 2.2 成功路线的核心规则

1. 正常成功不等于有沉淀价值。任务必须是可解释的业务能力，而不是一次性验收提示词。
2. 至少需要多个独立成功 turn；但“同一条测试提示词重复三次”不能自动证明它是一类有价值能力。
3. 任务应先按语义能力类别聚合，再生成一个 candidate；不能每个 Trace、每个文件名或每次重试都生成一个 candidate。
4. 只读和低风险操作可以作为复用证据；高风险操作只能保留抽象边界、受控动作或人工确认点。
5. candidate 阶段不能修改当前正式 Skill。
6. 评测失败、预算用尽、裁判异常或安全反例失败时，正式 Skill hash 必须保持不变。
7. workspace 采用必须使用临时文件、fsync、原子 rename、内容 hash 和 CAS。

## 3. 失败任务路线

### 3.1 完整链路

```text
用户任务失败
  ↓
失败 Episode、失败工具结果、纠正尝试和验证结果
  ↓
Issue 聚合
  ├─ /evolve recovery reject <issue-id> <原因>
  │    ↓
  │  保存拒绝原因、保留 Issue/Case/审计
  │  进入 rejected 终态
  │  不生成 candidate、Proposal、PR 或 Skill
  │
  └─ /evolve recovery revise <issue-id> <修复方向>
       ↓
     管理员、目标群、@要求、Issue 状态、功能开关校验
       ↓
     一个幂等事务写入：管理员动作 + 内部 note + request_candidate + Issue 状态
       ↓
     生成或排队 failure candidate
       ↓
     与成功路线共用对照检查、反例、安全、性能门禁
       ↓
     门禁通过后才可 workspace 原子采用
       ↓
     下一轮 Context 读取
       ↓
     支持回滚 baseline
```

### 3.2 失败路线的核心规则

- `reject` 只能保存拒绝原因并终止，不得生成 candidate。
- `revise` 不需要用户事先手动执行 `note` 或 `request_candidate`；内部一次完成。
- 同一 Issue + 管理员 + 规范化修复方向必须幂等。
- 同一 Issue 提交不同修复方向不能覆盖旧方向。
- `record_case` 只做 archive-only 兼容归档，不是普通用户的 Skill 主流程。
- 失败 candidate 即使 M15 通过，也必须经过和成功 candidate 相同的安全、反例、性能和采用门禁。
- 拒绝后的 Issue 不能被旧 `accept`、旧 `note` 或新的 `revise` 复活。

## 4. 资产和存储位置

长期 Gateway 使用 Compose 服务 `nanobot-gateway`，固定挂载：

```text
宿主机 runtime/ → 容器 /home/nanobot/.nanobot/
宿主机 runtime/workspace/ → 容器 /home/nanobot/.nanobot/workspace/
```

当前主要自进化数据库是：

```text
宿主机：runtime/workspace/.nanobot/memory.sqlite3
容器内：/home/nanobot/.nanobot/workspace/.nanobot/memory.sqlite3
```

`runtime/.nanobot/memory.sqlite3` 是另一份全局/历史运行库，不能直接代替 workspace 自进化库。

### 4.1 资产总表

| 资产 | 主要位置 | 何时产生 | 作用 | 是否影响当前 Agent |
| --- | --- | --- | --- | --- |
| Trace 摘要 | `runtime/workspace/.nanobot/memory.sqlite3` 的 `trace_index`；审计 JSONL 在 `runtime/audit/v1/` | 每次 Agent 任务 | 保存脱敏运行轨迹索引 | 间接影响后续分析，不直接加载成 Skill |
| 任务目标包 | `evolution_task_evidence` | 成功任务被识别为可分析任务时 | 保存目标、意图、范围、预期和实际结果 | 不直接影响 |
| 步骤证据 | `evolution_step_evidence` | 工具步骤被拆解后 | 保存有序工具步骤、风险、状态、验证和候选用途 | 不直接影响 |
| 任务关联 | `evolution_task_links` | 任务与 Trace/Case/Issue/Eval 关联时 | 追踪来源和审计关系 | 不直接影响 |
| Case | `cases`、`recovery_episodes`、`recovery_case_reviews` | 失败归档或评测案例形成时 | 归档、回放、审计和评测 | 不直接影响 |
| Failure Issue | `failure_issues` | 失败模式聚合后 | 给管理员选择 reject/revise | 不直接影响 |
| Issue 动作 | `failure_issue_actions` | 管理员执行 note/revise/reject 时 | 保存动作、幂等键、请求 ID 和结果 | 不直接影响 |
| 失败候选 | `failure_issue_candidates` | Issue revise 且开关允许时 | 失败路线的隔离候选 | 不直接影响 |
| 成功候选 | `evolution_candidate_staging` | 成功证据进入候选生成时 | 隔离保存 candidate 草稿、hash、baseline、风险和来源 | 不直接影响 |
| 候选版本 | `skill_revisions` | candidate/revision 生成时 | 保存 baseline 和候选内容版本 | 不直接影响，除非被采用 |
| A/B 评测 | `ab_evaluations`、`ab_case_results` | 候选进入质量检查时 | 记录 baseline/candidate 对照、模型调用、结果和原因 | 不直接影响 |
| 评测包/运行 | `eval_packs`、`eval_runs`、`eval_case_results` | Proposal 前后 | 保存可审计的案例集、运行 hash 和门禁结果 | 不直接影响 |
| Proposal | `skill_proposals`、`proposal_actions`、`proposal_deliveries` | 评测通过后 | 管理采用、通知、发布、回滚和 CAS | 不直接影响 |
| 正式 Skill | `runtime/workspace/skills/<name>/SKILL.md`；数据库 `skills`、`skill_revisions` | workspace 采用后 | 下一轮 Context 的实际技能来源 | **直接影响** |
| Overlay Draft PR | `runtime/skill-evolution-overlay/` 和私有 Overlay 仓库 | Overlay 路径进入发布流程时 | 私有候选发布，不自动合并/发布 | 不直接影响 |

### 4.2 资产状态边界

```text
证据 / Case / Issue
  ≠ candidate
  ≠ Proposal
  ≠ 正式 Skill

只有 runtime/workspace/skills/<name>/SKILL.md
并且 skills.current_revision_id 指向对应 active revision
才表示当前 Agent 正式使用该 Skill。
```

## 5. M18 步骤级证据和风险规则

当前代码把任务拆成任务级目标包和有序步骤。每个步骤单独记录工具类别、操作类型、状态、错误类别、验证方式和风险。

| 风险 | 含义 | 可以怎样使用 |
| --- | --- | --- |
| R0 | 只读、低副作用、边界清楚 | 可以作为候选证据 |
| R1 | 可控、可验证、影响有限 | 可以作为受限候选证据 |
| R2 | 有写入或可恢复副作用 | 只能脱敏抽象、隔离、带回滚点 |
| R3 | 外部系统、命令或高风险动作 | 只能 mock、人工确认或受控占位 |
| R4 | 敏感、破坏性、特权、不可逆或凭据相关 | 只能保留 Case/审计，不得进入可执行 Skill |

禁止保存：

- 完整命令和完整参数；
- 完整文件内容；
- API Key、Token、openid、bootstrap secret 等凭据；
- 完整聊天正文；
- 隐藏推理；
- 能够直接复放高风险动作的原始载荷。

混合任务不能因为一个高风险步骤而整次丢失。低风险步骤可以保留，高风险步骤必须变为抽象边界、受控动作或人工确认点。

## 6. 质量门禁和采用规则

### 6.1 现有门禁检查什么

当前成功候选的基础质量检查主要检查：

- 是否有明确业务意图；
- 是否有输入范围；
- 是否有预期结果；
- 是否有真实验证过的操作；
- 是否至少有 3 个独立成功 turn；
- 是否是低风险、只读或已受控的步骤。

随后会做 baseline/candidate 对照检查，并包含：

- 正常案例；
- 范围变体；
- 不适用反例；
- 安全反例；
- 失败恢复；
- 性能和稳定性；
- 模型裁判一致性和调用预算。

### 6.2 当前门禁为什么仍然不够

“重复成功三次”只能说明行为重复成功，不能说明它值得长期沉淀。当前已经出现的低价值候选大多是验收型任务，例如：

- 只读取 `SOUL.md` 前两行并返回第一行；
- 只读取 `USER.md` 前两行；
- 文件不存在后改读另一个固定文件；
- 只完成某个验收标识对应的单次查询。

这些任务可能满足“有范围、有预期、有只读工具、重复成功”的形式条件，但不一定是用户长期需要的能力。

当前门禁缺少：

1. **能力类别价值判断**：是否解决稳定、重复、可迁移的业务问题。
2. **场景多样性**：是否有不同对象、不同范围或不同表达，而不是重复同一条验收提示词。
3. **一次性测试识别**：是否只是场景标识、文件名、测试用例或验收脚本。
4. **收益证据**：是否减少了重复确认、减少了失败率或明显改善结果。
5. **同类唯一性**：同一能力类别是否已经有候选、正式 Skill 或正在评测的版本。

## 7. 当前成功路线的真实问题和案例

### 7.1 11 条候选为什么出现

在清理前，`evolution_candidate_staging` 有 11 条 `evaluating` 记录，但它们实际只涉及约 7 类窄范围任务，主要集中在 `SOUL.md`、`USER.md`、`TOOLS.md` 的前几行读取。

这说明：

- 证据聚合和候选落库之间存在重复生成；
- 每次重新扫描或重试都可能再次产生候选；
- 评测每日预算是 2 个候选，但候选生成不是同一个数量限制；
- 候选状态有历史残留，没有在采用、回滚、失败或人工关闭后完全收敛。

清理结果：这些记录没有物理删除，而是统一变为 `closed`，理由为“人工复核：候选质量不足，不再继续评测；保留记录供审计”。

### 7.2 卡住的评测

卡住的候选是成功路线的 `evolution-task-39e9f509b6`。它对应内部 A/B 检查，曾有两条评测记录处于 `running`：

- 一条从 2026-10-08 遗留；
- 一条从 2026-10-10 开始；
- 最新一条真实证据数为 5，预算为 40 次模型调用，但实际调用数为 0。

Gateway 日志显示当时外部 LLM 连接失败。评测执行按候选顺序串行等待，且异常后的状态收敛和超时处理不足，因此后续候选没有得到正常推进。

清理结果：两条 `running` 记录改为 `failed`，原因码为 `manual_rejected_low_quality`，保留评测历史。

### 7.3 曾经通过但已经回退的成功候选

主要案例：

- Skill 名称：`evolution-task-8f308f24d1`
- 来源：成功读取 `SOUL.md` 前两行、只返回第一行摘要的 WebUI 验收任务
- 候选：`m19-candidate:36c4df2d5e676be03a7e5b1a`
- 评测：`ab-eval:755de093-7eef-4cec-be5d-891fbf3e4bb3`，曾通过
- Proposal：`proposal:fb812571-1720-4313-af9f-fa366318b557`
- 之前曾自动采用，之后已按人工质量判断回退
- 当前 revision：`m19-baseline:4b28a61d52d375c346475bb0`
- 当前 Skill 文件已恢复 baseline hash：`sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`

这个 Skill 不是通用 QQ 能力，只适用于一个极窄的测试型请求。它能通过旧门禁，正好证明“任务成功”与“值得沉淀”之间还缺少一层价值门禁。

## 8. 当前失败路线的真实案例

### 8.1 revise 成功但最终回退

- Issue：`recovery-issue-914dfe82f2954056`
- 管理员使用 `revise` 提交修复方向；系统内部写入 note 和 request_candidate。
- 失败候选：`failure-candidate:bd6f5aa6a3f94c8694488da3`
- Proposal：`proposal:d955e8e3-f29c-4d3c-9e8e-3808ef8e6c0e`
- 曾通过评测并完成 workspace 采用。
- 后续完成回滚，恢复 baseline。
- 当前 failure candidate 处于失败/回退终态，没有失败路线增量 Skill 生效。

这证明失败路线的权限、Issue、修复方向、候选、评测、采用和回滚链路曾经真实跑通，但也证明“评测通过”仍不代表这份修复一定值得长期保留。

### 8.2 reject 零候选

- Issue：`recovery-issue-3898fab488ae61ea`
- 管理员提交拒绝原因。
- Issue 进入 `rejected` 终态。
- 没有创建 candidate、Proposal 或 Skill。
- 后续 revise/旧 accept 不能复活该 Issue。

## 9. 当前运行配置和部署边界

长期服务：

- Compose 服务：`nanobot-gateway`
- WebUI：`http://localhost:8765`
- 健康检查：`http://127.0.0.1:18790/health`
- 当前构建：`git-a1828f8d93b2`
- 当前合并提交：`a1828f8d93b2fa60917daae559e07e5f17ec8995`
- Agent 实际 workspace：容器内 `/home/nanobot/.nanobot/workspace`
- 宿主机对应目录：`runtime/workspace/`

当前关键开关：

| 配置 | 当前值 | 含义 |
| --- | --- | --- |
| `phase6.enabled` | `true` | 自进化扫描能力开启 |
| `stepwiseEvidenceEnabled` | `true` | 步骤证据开启 |
| `stepwiseEvidenceMode` | `enforced` | 以步骤证据作为候选边界 |
| `mixedRiskPolicy` | `manual` | 混合风险任务进入人工边界 |
| `stepwiseCandidateEnabled` | `true` | 成功路线候选生成开启 |
| `abEvaluationMode` | `enforced` | 候选必须经过对照评测 |
| `abEvaluationMaxCandidatesPerDay` | `2` | 每天最多真正评测两个候选 |
| `abEvaluationMaxModelCallsPerDay` | `40` | 每天最多 40 次模型评测调用 |
| `adoptionEnabled` | `false` | 当前不自动采用新 workspace Skill |
| `workspaceSkillAllowlist` | 空 | 当前没有允许自动采用的 Skill 名单 |
| `recoverySkillCandidateEnabled` | `false` | 当前失败路线不自动生成 Skill candidate |
| `caseCandidatePromotion` | `false` | Case 不自动晋级 candidate |
| `publishEnabled` | `false` | 不自动发布 Overlay 或公共代码 |

之前真实验收窗口曾显式打开过受控采用能力；当前配置已关闭自动采用，因此历史上“通过并采用”的记录不能理解为当前还会继续自动替换 Skill。

## 10. 相关代码和迁移入口

供后续 AI 阅读的主要入口：

| 模块 | 作用 |
| --- | --- |
| `nanobot/memory/stepwise_evidence.py` | 任务目标包、步骤证据、风险和候选用途 |
| `nanobot/memory/candidate_staging.py` | M19 成功路线候选隔离、hash、状态和来源 |
| `nanobot/memory/phase6_candidates.py` | 成功路线候选质量检查、候选内容和评测规格生成 |
| `nanobot/memory/ab_evaluation.py` | baseline/candidate 对照评测、模型调用和质量结果 |
| `nanobot/memory/phase6_trigger.py` | 周期扫描、候选 staging、评测、Proposal 和失败候选入口 |
| `nanobot/memory/continuous.py` | 成功任务选择、重复分组、低风险筛选和周期状态 |
| `nanobot/memory/failure_issues.py` | Issue、管理员 revise/reject、幂等和事务边界 |
| `nanobot/memory/evolution_commands.py` | `/evolve`、`recovery revise/reject`、Proposal 和回滚命令 |
| `nanobot/memory/proposal_repository.py` | Proposal 状态、管理员审批、过期和审计 |
| `nanobot/memory/skill_adoption.py` | workspace 原子采用、CAS、确认码和回滚 |
| `nanobot/memory/recovery_evidence.py` | 失败 Episode、Case 和恢复证据 |
| `nanobot/memory/migrations/0012_stepwise_evolution_evidence.sql` | M18 任务/步骤证据表 |
| `nanobot/memory/migrations/0013_evolution_candidate_staging.sql` | M19 成功候选隔离表 |
| `nanobot/memory/migrations/0014_recovery_review_atomic_actions.sql` | Issue revise/reject 原子动作 |
| `nanobot/memory/migrations/0015_candidate_adoption_states.sql` | 候选采用状态 |
| `nanobot/memory/migrations/0016_failure_issue_group_binding.sql` | Issue 群范围绑定 |
| `nanobot/memory/migrations/0017_failure_candidate_content_hashes.sql` | 失败候选内容 hash |
| `nanobot/memory/migrations/0018_failure_candidate_evaluation_hashes.sql` | 失败候选评测 hash |
| `nanobot/memory/migrations/0019_failure_candidate_hash_retry.sql` | 失败候选 hash 重试 |

相关测试主要在：

- `tests/memory/test_stepwise_evolution.py`
- `tests/memory/test_ab_evaluation.py`
- `tests/memory/test_evolution_commands.py`
- `tests/memory/test_failure_issues.py`
- `tests/memory/test_skill_adoption.py`
- `tests/memory/test_phase6_trigger.py`
- `tests/memory/test_migrations.py`

## 11. 下一轮改造建议

这一节是调研和改造入口，不代表已经实现。

### 11.1 先按能力类别，再生成候选

建议把流程改成：

```text
成功证据
  ↓
能力类别归一化（意图 + 对象范围 + 预期结果 + 操作类别）
  ↓
同类证据合并
  ↓
价值门禁和场景多样性门禁
  ↓
同一类别只允许一个未完成 candidate
  ↓
再进入评测队列
```

建议的唯一键至少包含：

```text
workspace + normalized_capability_key + baseline_revision_id
```

candidate 内容 hash 相同的重复记录应直接复用，不应再次创建。

### 11.2 增加“值得沉淀”门禁

不能只检查成功次数，还应检查：

- 是否是真实业务目标，而不是验收标识或单个固定文件查询；
- 是否有多个不同表达、对象或范围变体；
- 是否在不同时间、不同会话或允许的不同群范围中重复出现；
- 是否能明确说明减少了什么重复工作或失败；
- 是否有用户确认的长期价值信号；
- 是否与已有 Skill 重复或只是在复制现有 Skill。

不满足这些条件时，只保留证据/Case，不生成 candidate。

### 11.3 评测队列必须可隔离、可超时、可收敛

建议：

1. candidate 进入队列时先是 `queued`，真正取得 worker lease 后才是 `evaluating`。
2. 每个 candidate 有独立超时；模型连接、裁判异常或进程退出必须写入 `failed`/`retryable` 终态。
3. 一个 candidate 卡住不能阻塞其他能力类别。
4. 同一 candidate 的重试必须幂等，不能重复创建多条永远 `running` 的评测。
5. 评测每天预算限制的是“真正开始评测的 candidate”，不能只限制后续 Proposal。
6. 采用、回滚、拒绝、失败和人工关闭都必须同步更新 candidate staging 状态。

### 11.4 失败路线也要按能力类别合并

多个相似失败 Issue 不应每个都生成一个失败 Skill candidate。应先根据失败类别、纠正方向、目标范围和验证结果做语义合并，再决定：

- 只是归档 Case；
- 修订已有 Skill；
- 生成一个新的 Skill candidate；
- 转人工复核。

### 11.5 当前配置建议

在上述代码和测试完成前：

- 保持 `adoptionEnabled=false`；
- 保持 `workspaceSkillAllowlist` 为空或只放明确测试 Skill；
- 保持 `publishEnabled=false`；
- 保持失败路线 candidate 开关关闭，直到 Issue revise 的新门禁完成；
- 不把历史候选、Proposal 或 Draft PR 当作当前正式能力。

## 12. 后续 AI 的调研问题清单

后续 AI 继续修改前，应先回答：

1. 同一能力类别的唯一键是什么？能否保证重复扫描只产生一个未完成 candidate？
2. “成功三次”是否只是重复同一测试提示词？如何证明场景多样性和长期价值？
3. 哪些任务必须只保留 Evidence/Case，永远不能形成 Skill？
4. candidate、evaluation、Proposal 的状态是否能在进程崩溃、模型超时、回滚后完全收敛？
5. 一个候选失败时，其他能力类别是否可以继续评测？
6. 采用和回滚后，`evolution_candidate_staging`、`skills`、`skill_revisions`、Proposal 是否一致？
7. 失败 Issue 的 `revise` 是否仍满足管理员、目标群、@、幂等和终态不可复活？
8. 下一轮 Context 是否只读取 `runtime/workspace/skills/` 中的正式文件？
9. 是否可能把 candidate、Proposal、Case、Overlay Draft PR 误报为正式 Skill？
10. 真实 WebUI、Trace、审计、workspace 文件 hash 和回滚 hash 是否能互相对上？

## 13. 验收判定标准

只有同时满足以下条件，才能说自进化闭环可交付：

- 成功路线：从真实成功任务到候选、评测、workspace 采用、下一轮 Context 读取和回滚全部有证据；
- 失败路线：从真实 Issue 到 `revise`、候选、评测、workspace 采用、Context 读取和回滚全部有证据；
- `reject` 路线保存原因、零 candidate、终态不可复活；
- candidate 只按能力类别生成，不因单个成功任务或重复验收提示词泛滥；
- 评测异常有超时和终态，不会留下永久 `running`；
- 质量门禁失败时零 Skill 写入、零 Proposal、零 PR；
- 正式 Skill 与 candidate、Proposal、Case、Overlay 有明确边界；
- 当前 Skill hash、采用 hash、回滚 hash 和审计动作一致；
- 不输出或持久化凭据、完整命令、完整参数、聊天正文和隐藏推理；
- 代码测试、Gateway/WebUI 场景、审计数据和部署标识相互一致。
