# nanobot 五层记忆体系实施计划

> 文档状态：计划草案
>
> 更新时间：2026-09-24
>
> 适用范围：nanobot 当前 Python Agent、Audit/Trace、Skill、工具执行链，以及 `nanobot-llm-wiki`、Hermes Agent Self-Evolution、SkillOpt-Sleep 的集成规划。

## 1. 文档目标

本计划用于把当前 nanobot 已有的会话、MemoryStore、Dream、Audit/Trace、Skill 和 ToolRegistry 组织成可演进的五层记忆体系：

1. 当前上下文记忆；
2. Trace 过程记忆；
3. 知识图谱记忆；
4. Skill 与案例记忆；
5. 遗忘、降权、归档和删除机制。

计划不要求一次性重写 AgentLoop，也不把所有内容塞进一个数据库。核心原则是：会话保存工作记忆，Trace 保存过程证据，Wiki 保存结构化知识，Skill/Case 保存可复用能力，Retention 控制生命周期。

## 2. 当前基线

### 2.1 nanobot 已有能力

- `SessionManager` 将会话保存为 JSONL，支持缓存、原子保存、损坏恢复、fork、删除和 `last_consolidated`。
- `ContextBuilder` 将身份、bootstrap 文件、长期记忆、近期历史和 Skill 摘要组装为系统上下文。
- `ContextGovernor` 负责 token 预算、工具结果裁剪、孤立 tool result 清理和非法工具调用修复。
- `MemoryStore` 管理 `SOUL.md`、`USER.md`、`memory/MEMORY.md`、`memory/history.jsonl` 和 Dream cursor。
- Dream 读取未处理历史，结合当前记忆文件，通过受限工具更新长期文件；对于 Skill 只生成候选版本，不直接覆盖正式 Skill，并以真实 Git diff 判断是否产生有效修改。
- `SkillsLoader` 支持 builtin/workspace skill、frontmatter、`always`、依赖检查、disabled skill 和渐进式加载。
- `ToolRegistry` 提供工具注册、稳定 schema 排序、参数校验、结构化错误和工具定义缓存。
- Audit/Trace 提供 `trace_id`、`turn_id`、`run_id`、父子运行关系、工具调用事件、失败恢复、索引、查询、完整性校验和 WebUI 展示。

### 2.2 当前缺口

- Trace 尚未系统化派生为可检索的知识、案例和 Skill 证据。
- 记忆检索主要依赖文件、工具和 prompt 注入，缺少统一的 intent 路由和 memory scope 选择。
- Wiki 是独立项目，尚未成为 nanobot 核心记忆协议的一部分。
- Skill 版本、案例、Trace、评测结果之间缺少统一关联模型。
- Dream 有文件级遗忘和归档规则，但尚未覆盖 Trace、Wiki、SQLite 检索索引和案例库。
- 尚无 Hermes/SkillOpt 风格的“采集—回放—评测—暂存—审核—发布”闭环。

## 3. 目标架构

```text
用户输入
  ↓
意图路由层
  ├─ 当前会话/活动目标
  ├─ Trace 查询
  ├─ Wiki 知识检索
  ├─ Skill/Case 检索
  └─ 写入、忘记、进化策略
  ↓
ContextBuilder（按需注入、token 预算、冲突检测）
  ↓
AgentRunner + ToolRegistry
  ↓
Audit/Trace（过程证据）
  ↓
异步派生器
  ├─ Trace 摘要
  ├─ 知识候选
  ├─ 案例候选
  └─ Skill 优化评测集
  ↓
Wiki / Case Store / Skill Staging
```

### 3.1 五层边界

| 层 | 职责 | 典型数据 | 默认注入策略 |
|---|---|---|---|
| 当前上下文 | 支撑当前 turn 的最小工作集 | 最近消息、活动目标、工具结果、压缩摘要 | 直接注入，严格 token 预算 |
| Trace 过程记忆 | 保存发生了什么、为什么发生、是否恢复 | 模型调用、工具调用、重试、失败、父子 run | 不直接注入，按 trace 查询 |
| 知识图谱 | 保存稳定事实、实体、决策和关系 | 项目、用户偏好、架构决策、依赖关系 | 先检索，再注入摘要 |
| Skill/Case | 保存可复用步骤和已验证经验 | SKILL.md、成功案例、失败模式、工具调用模板 | 摘要常驻，案例按需加载 |
| Retention | 控制生命周期和权限 | 置信度、时效、访问次数、归档、删除状态 | 影响检索和写入，不直接作为事实 |

## 4. 目标数据模型

### 4.1 统一记忆记录

```json
{
  "memory_id": "mem_01...",
  "memory_type": "fact|decision|trace_summary|case|skill",
  "namespace": "user|project|workspace|agent",
  "content": "...",
  "summary": "...",
  "source_refs": {
    "session_keys": [],
    "trace_ids": [],
    "history_cursors": []
  },
  "entities": [],
  "relations": [],
  "confidence": 0.0,
  "authority": 0.0,
  "salience": 0.0,
  "created_at": "...",
  "updated_at": "...",
  "last_accessed_at": "...",
  "access_count": 0,
  "decay_score": 0.0,
  "status": "candidate|active|stale|archived|deleted",
  "sensitivity": "public|private|secret",
  "source_actor": "user|main_agent|maintenance_agent|system",
  "supersedes": [],
  "revision_id": "mem_01...:v1",
  "previous_revision_id": null,
  "last_skill_review_at": null,
  "activity_epoch": 0,
  "version": 1
}
```

### 4.2 Skill 与案例

Skill 定义继续使用 `skills/<name>/SKILL.md`，增加版本和内容 hash；案例作为 Wiki 的 `case` 页面存储，不直接污染 SKILL.md：

```json
{
  "case_id": "case_01...",
  "skill_id": "github-code-review",
  "skill_version": "1.3.0",
  "intent": "review pull request",
  "task_signature": {"language": "python", "risk": "security"},
  "input_summary": "...",
  "preconditions": ["working_tree_clean"],
  "steps": [{"tool": "read_file", "purpose": "..."}],
  "outcome": {"status": "success", "quality_score": 0.88},
  "failure_patterns": [],
  "trace_ids": ["trace_..."],
  "confidence": 0.86,
  "user_confirmed": true,
  "status": "candidate|active|archived|rejected",
  "version": 1,
  "revision_id": "case_01...:v1",
  "previous_revision_id": null,
  "created_at": "...",
  "last_used_at": "...",
  "use_count": 4
}
```

准入条件：Trace 完整、结果可验证、无秘密、无越权、通过评测或用户确认，并且与已有案例去重。

### 4.3 Skill 专属评测包（EvalPack）

每个 Skill 使用自己的评测包，不要求所有 Skill 共用同一套题目。评测包由维护 Agent 根据 Skill 定义、成功/失败 Case 和脱敏 Trace 自动生成草案；后台服务校验、封存和执行，用户不需要手工编写全部评测题。

```json
{
  "eval_pack_id": "eval_repo_code_review_v1",
  "skill_id": "repo-code-review",
  "skill_version": "candidate-3",
  "dataset_hash": "sha256:...",
  "sources": {"case_ids": [], "trace_ids": [], "synthetic": true},
  "train": [{"task_input": "...", "rubric": ["..."]}],
  "validation": [{"task_input": "...", "rubric": ["..."]}],
  "holdout": [{"task_input": "...", "rubric": ["..."]}],
  "fixture_refs": ["workspace-snapshot-..."],
  "allowed_tools": ["read_file", "grep", "list_dir"],
  "forbidden_actions": ["write_file", "external_side_effect"],
  "baseline_ref": "skill:repo-code-review@1.0.0",
  "baseline_tool_schema_hash": "sha256:...",
  "baseline_model": "...",
  "baseline_config_hash": "sha256:...",
  "split_seed": 20260925,
  "status": "draft|reviewed|sealed|evaluating|passed|failed|insufficient_evidence"
}
```

评测题可以来自四类来源：Skill 读取后由强模型合成的题目、真实 Session/Trace 挖掘出的成功和失败任务、失败 Trace 自动转成的回归题，以及可自动验证的固定 fixture（例如带已知问题的测试仓库）。题目要求描述任务和评分 rubric，不要求固定措辞答案。

首版沿用 Hermes 的实用默认值：总计约 20 道题，按 10 train / 5 validation / 5 holdout 划分。与 Hermes 不同，nanobot 增加最低质量门槛：至少 10 道有效题、至少 3 道 holdout、至少覆盖 3 类任务、至少包含 1 道失败或边界题；每道题必须可执行、rubric 可判断、通过去重和敏感信息检查。若题目不足或无法验证，不能伪装成合格 EvalPack。

评测包封存后，candidate 不得修改其 holdout 内容、fixture、答案或评分标准。生成题目的 Agent、审核题目/评分标准的 Agent、运行任务的评测 Agent、最终评分器和发布主体必须逻辑分离；不能由同一个 Agent 自己出题、自己判分并批准发布。评测集不足时，状态为 `eval_pack_missing` 或 `insufficient_evidence`，candidate 只能停留 staging。

题目生成后使用固定 `split_seed` 或稳定哈希划分数据集，并为每个 EvalPack 保存版本和 `dataset_hash`。holdout 只对后台评测服务可见，candidate 生成和 GEPA 优化只能读取 train/validation；EvalPack 更新、补入真实任务或失败回归题时必须生成新版本并重新封存。

## 5. 存储分层、事实源与修改方式

五层记忆不应全部写入同一个 Markdown 文件，也不应全部写入同一个数据库。推荐采用“原始记录不可变、派生内容可版本化、索引可重建、Markdown 保留可读性”的结构。

### 5.1 存储总览

| 内容 | 推荐事实源 | SQLite 的作用 | 是否直接进入上下文 |
|---|---|---|---|
| 原始会话 | `workspace/sessions/*.jsonl` | 会话列表和元数据索引 | 只取最近尾部 |
| 原始 Trace | `runtime/audit/v1` append-only JSONL segments | `state/audit-index.sqlite` 查询索引，可重建 | 默认不进入 |
| Trace 摘要 | Wiki `trace_summary` 页面 | FTS、来源、状态、时间索引 | 按需摘要注入 |
| 稳定事实/决策 | `SOUL.md`、`USER.md`、`memory/MEMORY.md`、Wiki 页面 Markdown | Wiki `wiki.db`、FTS5、关系表 | 按意图检索 |
| 案例 | Wiki `page_type=case` 的 Markdown 页面 | Wiki `wiki.db` 的结构化字段、FTS5、关系索引 | 检索摘要后注入 |
| Wiki/Case revision | `memory/wiki/revisions/<page_id>/<version>.md` 不可变快照 | revision 索引、父版本、变更原因、来源和审计 ID | 不直接注入 |
| Skill 正文 | `skills/<name>/SKILL.md` | Skill manifest、版本、hash、评测索引 | 摘要常驻，正文按需 |
| Skill 候选 | `memory/skill-staging/<skill>/<version>/` | 候选状态和评测索引 | 不直接注入 |

### 5.2 为什么原始 Trace 不用 Markdown

Trace 是高频、追加式、结构化和需要完整性校验的数据。它更适合当前已有的 Audit 设计：

- append-only JSONL segment 作为事实源；
- 事件带 `trace_id`、`turn_id`、`run_id`、`tool_call_id`；
- segment 可 fsync、校验、封存和轮换；
- SQLite 只做查询索引，不是唯一事实源；
- 原始 payload 可单独设置更短保留期。

如果把完整 Trace 写成 Markdown，会出现查询慢、并发写入困难、结构字段不稳定和难以验证完整性等问题。

### 5.3 为什么 Skill 和案例仍保留 Markdown

Skill 和案例需要人审、diff、Git 版本和跨 Agent 迁移，因此 Markdown 作为规范源更合适：

- Skill：`SKILL.md` 是执行规范；
- Case：一页一个案例，frontmatter 保存结构字段，正文保存摘要和关键步骤；
- Wiki 页面：人类可读、可编辑、可归档；
- SQLite：只保存派生索引、FTS、关系、状态和统计。

这形成：

```text
Markdown = 可读、可审查、可版本化的规范源
SQLite   = 快速检索和关系索引
JSONL    = 高吞吐、不可变的原始事件源
```

### 5.4 案例的推荐落点

首版不另建一套独立案例数据库，直接复用 `nanobot-llm-wiki`：

- 页面目录：`memory/wiki/pages/`；
- 页面类型：`case`、`anti_pattern`、`trace_summary`；
- 页面关系：`derived_from`、`validated_by`、`similar_to`、`implements`；
- `source_cursors` 或 frontmatter 保存 history cursor；
- 自定义字段保存 `skill_id`、`skill_version`、`trace_ids`、`confidence`、`outcome`。

只有当案例规模、写入并发或评测查询证明 Wiki schema 不足时，才增加独立 `cases.db`；不要在第一阶段同时维护两个案例事实源。

### 5.5 相似度召回：SQLite-only 方案

“不用向量数据库”不等于“不能做相似度召回”。首版使用 SQLite 内置和派生索引完成三级召回：

1. **SQLite FTS5 初召回**：标题、摘要、正文、标签、别名、任务关键词。
2. **结构化过滤和加权**：`page_type`、`skill_id`、语言、风险、namespace、时间、置信度、成功状态。
3. **轻量重排**：关键词重叠、实体重叠、Skill/项目匹配、时间衰减、访问次数、图关系邻近度。

推荐评分：

```text
score =
  0.35 * fts_score
+ 0.20 * entity_overlap
+ 0.15 * task_signature_match
+ 0.10 * skill_match
+ 0.10 * authority_and_confidence
+ 0.05 * graph_proximity
+ 0.05 * recency_and_usage
```

如果未来实测表明纯 lexical 召回不足，先调优分词、同义词、字段权重、任务签名和图关系；仍不足时，才单独评估 SQLite 内向量扩展。该评估不改变 Markdown/JSONL 事实源，也不属于首版范围。

### 5.6 数据如何修改

不同对象采用不同修改语义：

- **会话**：由 `SessionManager` 原子保存；压缩只改变 live suffix，历史归档另写 `history.jsonl`。
- **Trace**：不修改旧事件；重试、恢复和更正追加新事件；索引可重建。
- **Wiki/案例**：修改 active 页面前，先在 `memory/wiki/revisions/<page_id>/<version>.md` 保存不可变 Markdown 快照；页面记录单调递增的 `version`、`revision_id`、`previous_revision_id`、变更原因、来源和 `supersedes`。`expected_version` 防止后台任务覆盖较新的页面。普通“忘记”默认转为修正、合并、降权或 archive，不直接硬删除。
- **Skill**：不由运行中的 Agent 直接覆盖正式版本；candidate 与正式版本均保存 version 和 content hash。workspace adopt 永不覆盖旧 `SKILL.md`，而是生成新版本目录并切换 current 指针；builtin/shared Skill 经过评测后通过 Git PR 发布，可切回上一个已验证版本或提交。
- **Skill manifest/SQLite 索引**：由文件扫描或变更钩子重建，索引损坏不影响 Markdown 和 Skill 正文。

### 5.7 写入流水线

```text
会话消息
  → Session JSONL
  → Agent 执行
  → Audit Trace JSONL
  → 异步摘要/脱敏/去重
  → Trace summary / Case candidate
  → SQLite FTS 与关系索引
  → 人工确认或低风险自动采用
  → Wiki/Case active
  → 多案例归纳成 Skill candidate
  → held-out 评测
  → Git PR 或 workspace adopt
```

关键原则：**先保存原始证据，再生成派生记忆；派生记忆可以修改和淘汰，但不能反向覆盖原始 Trace。**

#### 5.7.1 低风险自动写入白名单

“低风险自动写入”不表示任何记忆对象都可直接生效。首版采用如下白名单：

- 可自动进入 active：有完整 Trace 来源、脱敏通过、工具执行结果可验证且 `confidence >= 0.85` 的 `trace_summary`；
- 可自动写入 candidate：Case、事实修正建议、关系建议、Skill candidate；
- `wiki_link` 仅在来源和目标都属于当前 workspace、关系类型在允许枚举、且不涉及用户偏好或正式决策时可作为低风险操作；
- 必须取得用户明确确认：用户偏好、正式决策、正式 Skill、外部写操作、Git PR、永久删除；
- 永不自动：覆盖 active 页面、`wiki_unlink`、`wiki_forget(archive=false)`、正式发布或切换 Skill current 指针。

Case candidate 不是“已验证的正式经验”；只有准入、去重和需要时的人工确认完成后，才可提升为 active。

### 5.8 后台维护服务与受限维护 Agent

记忆维护和 Skill 整合不使用当前用户任务里的普通 `spawn` 子 Agent。普通子 Agent 服务于当前 turn，绑定用户交付和工作区上下文；记忆治理属于后台、低优先级、需要串行和可恢复的系统维护任务。

推荐组成：

- **后台维护服务**：计时、去重、排队、workspace 级互斥锁、状态保存、索引重建、权限校验和审计。
- **受限维护 Agent**：仅在需要语义判断时由后台服务启动，类似 Dream 的 ephemeral Agent；只读 Trace/Wiki/Skill manifest，只能写 Wiki candidate、Case 和 `memory/skill-staging/`，不能覆盖正式 Skill、执行 shell 或调用外部副作用工具。

维护不是“每条消息各创建一次 45 分钟后的任务”，而是**每个会话一条可反复延期的防抖维护状态**。每条新消息只更新同一条状态的 `last_activity_at`、`last_message_cursor` 和 `due_at=last_activity_at+45m`，不会单独触发总结。

```text
连续会话 A、B、C
  → 同一 session_maintenance_state 持续更新 due_at
  → C 后连续 45 分钟无新消息
  → 单 worker 对 (last_review_cursor, last_message_cursor] 的整批消息/Trace 做一次 review
  → 记录 review cursor 与状态
  → 下一条新消息才开启新的 activity epoch
```

持久状态至少包含：`session_key`（主键）、`activity_epoch`、`last_activity_at`、`last_message_cursor`、`last_review_cursor`、`due_at`、`status=active|pending|running|completed`、`lease_until`。Gateway 重启后由 SQLite 中的 pending 状态恢复到期检查；worker 取任务时再次核对 cursor/epoch，旧任务标为 `obsolete` 而不执行。

每个 workspace 同一时刻最多运行一个维护任务。维护 Agent 启动后先固定本次处理的 `snapshot_cursor`；若分析期间收到新消息，当前任务只处理到该快照，新消息留给下一次 idle 批次。新用户消息会使尚未开始的任务延期；已经进入语义分析的任务可完成只读分析或安全写入 staging，但不能改动正式资产。

维护 Agent 只允许输出下列动作：

```text
no_op                    没有值得维护的内容
merge_memory             合并重复事实或案例
correct_memory           修正事实并保留 supersedes 关系
archive_memory           降权并归档过时内容
create_case              创建 Wiki Case
update_case              更新或合并 Wiki Case
propose_new_skill        新建 Skill candidate
propose_skill_merge      修改已有 Skill 的 candidate
propose_skill_reference  建立或修改 Skill references candidate
```

首版不允许维护 Agent 自动拆分、自动合并或自动下线正式 Skill；这些只能作为 candidate 交由评测和人工确认。

### 5.9 Wiki、Trace 与 Skill 的工具契约

`nanobot-llm-wiki` 已有可通过 entry point 或 MCP 接入的基础工具。首版应复用其检索、读取和关系能力，而不是为 Case 再造一套平行工具；Case 只是带有约定元数据的 Wiki 页面。

| 现有 Wiki 工具 | 首版用途 | 使用边界 |
|---|---|---|
| `wiki_search`、`wiki_read` | 查询事实、决策、Case、Trace 摘要 | 可供主 Agent 和维护 Agent 只读调用 |
| `wiki_link` | 建立页面关系 | 满足 5.7.1 白名单时可低风险写入；维护 Agent 通过候选提案间接使用 |
| `wiki_unlink` | 移除页面关系 | `restricted`；仅用户明确确认后由主 Agent 执行，维护 Agent 不直接调用 |
| `wiki_status`、`wiki_doctor` | 检查索引、页面和存储健康 | 诊断工具，不进入正常推理上下文 |
| `wiki_import` | 受控导入历史知识源 | 仅后台迁移任务调用，不暴露给日常 Agent |
| `wiki_upsert` | 页面底层创建与更新 | 由用户显式写入或后台服务校验后的提案落盘；维护 Agent 不直接调用 |
| `wiki_forget` | archive 或永久删除 | 永久删除仅限用户明确请求、秘密、权限违规或合规删除；后台维护不可调用 |

MCP 侧已有同义的 `knowledge_search`、`knowledge_read`、`knowledge_upsert`、`knowledge_link`、`knowledge_unlink`、`knowledge_forget`、`knowledge_status`。NanoBot 插件接入优先采用 `wiki_*` 命名；如经 MCP 接入，只保留一层适配，避免同一能力以两套名称同时进入模型上下文。

#### 5.9.1 页面元数据必须可往返保存

当前 Wiki 页模型和 Markdown frontmatter 仅稳定处理少量已知字段。若直接把 `trace_ids`、`skill_id` 等新字段手工写进 frontmatter，后续 `wiki_upsert` 重写页面时可能丢失它们。因此第一项兼容性改造是：

- 在 `WikiPage` 增加 `metadata: dict`；
- 在 SQLite 索引增加 `metadata_json`；
- Markdown 的未知 frontmatter 必须读取、保留并原样写回（round-trip）；
- `id`、`title`、`page_type`、`tags`、`aliases`、`confidence`、时间和 source cursor 仍保留一等字段，便于索引和兼容旧页面。

首版统一约定的扩展元数据包括：`trace_ids`、`skill_id`、`skill_version`、`intent`、`task_signature`、`domain`、`language`、`risk`、`outcome`、`namespace`、`source_actor`、`status`、`supersedes`、`activity_epoch`、`candidate_reason`、`reviewed_at`。`source_actor` 只记录写入来源（`user`、`main_agent`、`maintenance_agent`、`system`），不代表多用户身份、租户或所有权。字段可缺省；不可把未经验证的推测伪装成稳定事实。

#### 5.9.2 搜索、Case 和图关系

扩展 `wiki_search`，在现有 `query`、`limit`、`tag` 之外支持 `page_types`、`metadata_filters`、`namespace`、`status`、`intent`、`skill_id`、`risk`、`outcome`、`trace_id`、`created_after` 等过滤条件。这样不必创建独立的 Case 存储或 Case 搜索引擎：

```text
case_search = wiki_search(page_types=["case"], metadata_filters={...})
```

增加只读 `wiki_neighbors(selector, relations=None, depth=1)`，返回受权限过滤的关系邻居。它用于从 Case 找到对应的 `trace_summary`、Skill、项目或相互矛盾的决策；默认深度为 1，禁止无界图遍历进入上下文。

#### 5.9.3 受限维护写入

新增 `wiki_propose_maintenance`，只接受结构化候选，不直接让维护 Agent 改写 active 页面：

```json
{
  "action": "create_case|update_case|merge|correct|archive",
  "target_page_id": "optional",
  "expected_version": 3,
  "source_page_ids": ["optional"],
  "content": "候选正文或摘要",
  "metadata": {
    "trace_ids": ["trace-123"],
    "intent": "security_code_review",
    "skill_id": "security-review"
  },
  "reason": "基于哪些证据作出此维护建议",
  "confidence": 0.88
}
```

后台服务负责版本冲突检查（`expected_version`）、权限与敏感信息校验、落入 candidate/staging、更新索引和审计。`merge`、`correct` 必须保留来源与 `supersedes`；`archive` 只改变检索状态和权重，不销毁页面。

#### 5.9.4 按责任域划分的工具白名单

| 责任域 | 暴露给 Agent 的工具 | 由后台服务直接完成 |
|---|---|---|
| 当前用户任务 | `wiki_search`、`wiki_read`、受策略约束的显式写入工具；`trace_search`、`trace_read_summary` | 权限判定、索引刷新、审计持久化 |
| 受限维护 Agent | `memory_review_batch`、`wiki_search`、`wiki_read`、`wiki_neighbors`、`trace_read_summary`、`skill_catalog_search`、`skill_read`、`wiki_propose_maintenance`、`skill_propose` | workspace 锁、调度、原始 Trace 读取授权、提案校验与落盘 |
| Skill 候选流水线 | 无直接发布权；只产生 `skill_propose` 输出 | `skill_candidate_validate`、`skill_candidate_evaluate`、`skill_candidate_publish`、Git PR/workspace adopt |
| 生命周期与删除 | 无维护 Agent 直连删除权 | archive 执行、索引清理、明确授权后的永久删除 |

Trace 工具属于 Audit，而不是 Wiki：`trace_search` 做受时间、workspace 和权限约束的定位，`trace_read_summary` 返回脱敏摘要，`trace_read` 仅在诊断需要且权限允许时返回最小原始片段。Skill 工具属于 Skill 系统：`skill_catalog_search` 查询 manifest 摘要，`skill_read` 按需读取正文，`skill_propose` 只写入 staging。索引重建、候选评测和发布均不可交给模型自行决定或执行。

### 5.10 工具契约、单 workspace 权限与 Skill 定义

本项目是单用户、单 workspace、无租户设计。这里的权限不是用户身份、`user_id` 或 RBAC；它只防止不同运行角色越过当前任务的操作边界。所有工具执行前统一经过 `ToolPolicy`：

```text
调用者类型（main_agent | maintenance_agent | backend_service）
  + 当前 workspace 边界
  + 工具名与已校验参数
  + 本轮是否已有用户明确确认
  + 数据敏感级别
  → allow | require_confirmation | deny
```

`read_only` 只表示可无副作用并发执行，`_scopes` 只表示工具可被哪类 Agent 加载；两者都不能代替 `ToolPolicy`。策略判断必须发生在实际执行之前，不能只依赖模型对工具描述的理解。

用户确认采用**本轮、一次、动作指纹绑定**的语义：确认记录包含 `tool_name + target_id + 关键参数摘要`，只能消费一次；目标、关键参数或会话 turn 改变后失效。确认“归档 `case-123`”不能授权删除其他页面、发布 Skill 或在下一轮重复执行。后台服务只能执行已通过该门禁的动作，不能把候选提案自行扩大成正式发布。

#### 5.10.1 每个工具必须声明的契约

无论是已有工具的升级，还是新增工具，都必须定义下列项：

```text
name / version
use_when（适用）与 do_not_use_when（明确不适用）
caller_allowlist（可调用角色）与 risk（read_only | low_risk_write | restricted | external_side_effect）
JSON Schema 输入：必填项、范围、枚举、分页 cursor、最大结果数、additionalProperties=false
前置条件、幂等键或 expected_version、可否重试
确认条件、脱敏与 workspace 路径约束
结构化成功/失败输出、稳定 error_code、audit_id 与来源证据
```

工具参数不能只写“字符串”或“内容”。例如 `wiki_upsert` 需要明确目标页面标识、更新模式、`expected_version`、来源 Trace/Cursor、敏感级别和写入原因；`wiki_import(path)` 必须先解析并验证路径位于当前 workspace 的允许导入目录内。`wiki_unlink` 在未指定 relation 时会删除多个关系，因此默认属于 `restricted`，不允许普通 Agent 静默调用。

#### 5.10.2 统一输出与错误语义

现有 Wiki 工具主要返回面向人阅读的字符串；首版升级后，所有新工具及改造后的记忆工具应返回可 JSON 序列化的统一信封。底层 nanobot 若仍以文本承载工具结果，则把该 JSON 序列化为文本，同时保留 `ToolResult` 的错误元数据。

```json
{
  "ok": true,
  "status": "completed|staged|needs_confirmation|not_found|rejected",
  "data": {},
  "warnings": [],
  "next_actions": [],
  "provenance": {"page_ids": [], "trace_ids": [], "skill_ids": []},
  "audit_id": "audit_...",
  "error": null
}
```

失败时 `ok=false`，并使用稳定的 `error.code`：`invalid_argument`、`not_found`、`conflict`、`permission_denied`、`confirmation_required`、`sensitive_content`、`precondition_failed`、`rate_limited`、`internal_error`；同时给出 `retryable` 和面向模型的安全下一步。搜索结果必须带 `limit`、`next_cursor`、命中 ID、摘要、时间、置信度与来源，而不是只返回拼接文本。

#### 5.10.3 首批工具的最小 schema 与权限矩阵

| 工具 | 关键输入 | 调用者与策略 | 必须输出 |
|---|---|---|---|
| `wiki_search` | `query`、`page_types`、`metadata_filters`、`limit<=25`、`cursor` | 主 Agent、维护 Agent；只读 | 命中摘要、过滤原因、`next_cursor`、来源 |
| `wiki_read` | `selector`、`view=summary|full` | 主 Agent、维护 Agent；只读 | 页面、版本、metadata、关联来源；长正文可裁剪 |
| `wiki_neighbors` | `selector`、`relations`、`depth=1`、`limit` | 主 Agent、维护 Agent；只读 | 受限一跳邻居、关系类型、截断状态 |
| `trace_search` | `query`、时间窗、`status`、`limit`、`cursor` | 主 Agent、维护 Agent；只读且按 workspace 范围过滤 | 脱敏 Trace 摘要、`trace_id`、时间、状态 |
| `trace_read_summary` | `trace_id` | 主 Agent、维护 Agent；只读 | 脱敏步骤、结果、失败/恢复、关联工具 |
| `wiki_propose_maintenance` | action、目标/来源页面、`expected_version`、候选内容/metadata、reason、confidence | 仅维护 Agent；只写 candidate | `candidate_id`、校验状态、冲突/敏感告警、`audit_id` |
| `skill_catalog_search` | `query`、capabilities、最大风险、可用性、`limit`、`cursor` | 主 Agent、维护 Agent；只读 | manifest 摘要、版本/hash、依赖、风险、最小调用示例 |
| `skill_propose` | operation、目标 Skill/版本、结构化 Skill definition、Case/Trace evidence、评测计划 | 仅维护 Agent；只写 staging | `candidate_id`、缺失字段、评测要求、`audit_id` |

`wiki_forget(archive=false)`、正式 Skill 发布、Git PR、外部写操作和有副作用工具统一为 `require_confirmation`；`backend_service` 只在已有本轮动作指纹确认后代为执行。低风险自动写入只适用于 5.7.1 的白名单，并且必须经过来源、脱敏、schema 和路径校验；不等同于可以自动删除、覆盖或发布。

#### 5.10.4 Skill 的正式定义与候选结构

Skill 是一个**可版本化、可评测、可审查的可复用任务方法包**，不是单次任务记录、Trace 副本或工具代码。它必须说明何时适用和不适用、前置条件、允许工具、执行步骤、验证标准、停止条件与安全边界。Case 保留“这一次怎么做、结果如何”；多个 Case 及评测证据证明可复用后，才可能生成 Skill。

```yaml
name: repo-code-review
description: 对可读代码仓库执行只读问题定位与代码审查。
triggers: [代码审查, 仓库问题定位]
non_goals: [不修改代码, 不提交 PR, 不访问未授权外部系统]
risk: read_only
required_tools: [read_file, grep, list_dir]
preconditions: [目标仓库可读]
workflow: 具体步骤与每步证据要求
verification: [给出文件与行号, 区分事实与推断]
stop_conditions: [缺少仓库访问权限, 需要写操作]
references: [case-..., trace-...]
```

`skill_propose` 不只接收一段 Markdown，至少应包含 `operation=create|revise|merge|add_reference`、`target_skill_id`、`expected_version`、上述 `definition`、`case_ids`、`trace_ids`、`reason`、`evaluation_plan`。后台服务再检查定义完整性、工具风险是否匹配、证据是否充分、是否与现有 Skill 重复；通过前只写入 `memory/skill-staging/`。

#### 5.10.5 工具说明与 few-shot

工具 description 必须自包含地写明“何时用、何时不用、数据范围、不可执行的动作和确认要求”。少量 few-shot 用于复杂组合工具，但不作为权限控制：

- 对普通工具，在 schema description 中给出最小参数示例和常见边界；
- `tool_search` / `skill_catalog_search` 在返回候选时附一个最小合法调用示例及一个常见错误；
- `wiki_propose_maintenance`、`skill_propose` 的 1～2 个高质量 few-shot 放在受限维护 Agent 的专用 prompt/template 中，而不是注入每轮主 Agent 上下文；
- few-shot 之外仍由 JSON Schema、`ToolPolicy`、版本检查、脱敏扫描和审计强制执行。

## 6. 用户意图路由层

### 6.1 初始意图集合

```text
CHAT                 普通对话
SESSION_CONTINUE     当前任务延续
MEMORY_READ          查询长期事实
MEMORY_WRITE         记住或更新事实
MEMORY_FORGET        忘记、删除或撤销事实
TRACE_INSPECT        查看执行过程、失败和工具调用
SKILL_DISCOVER       查找技能或流程
SKILL_EXECUTE        执行已知技能
CASE_RETRIEVE        查找相似历史案例
EVOLUTION_REVIEW     评估或优化 Skill
SYSTEM_CONTROL       停止、恢复、压缩、导出
```

### 6.2 路由输出

```json
{
  "intent": "CASE_RETRIEVE",
  "confidence": 0.91,
  "entities": ["github review", "private repository"],
  "memory_scopes": ["skill_case", "trace_summary"],
  "required_tools": ["wiki_search", "wiki_read"],
  "write_policy": "read_only",
  "risk_level": "low"
}
```

路由层必须先于大规模检索，避免每轮都加载全部 Wiki、Trace 和 Skill。低置信度时优先只读检索或向用户澄清；写入、忘记、执行外部副作用和自动进化都必须走权限策略。

## 7. 记忆何时进入上下文，以及如何进入

五层记忆不应在每轮都全部注入。推荐采用“少量基础注入 + 路由后检索注入 + Agent 主动深查”的混合模式：

```text
收到用户消息
  ↓
加载当前会话尾部、活动目标、最近摘要
  ↓
意图路由和风险判断
  ↓
按 memory scope 检索 Wiki/Trace/Case
  ↓
把高置信、短摘要放入 dynamic context
  ↓
模型开始执行
  ├─ 已有信息足够：直接回答或执行
  └─ 需要细节：调用 memory/wiki/trace/case 工具继续查询
```

### 7.1 三种进入方式

| 方式 | 适用内容 | 触发时机 | 设计要求 |
|---|---|---|---|
| 基础注入 | 当前会话尾部、活动目标、最近压缩摘要、极少量高稳定事实 | 每个 turn 的 ContextBuilder | 有硬 token 上限；不放完整 Trace 和案例 |
| 路由检索注入 | 与当前意图强相关的事实、决策、案例摘要、失败提示 | 路由完成后、模型第一次调用前 | 由 scope、权限、置信度和预算共同过滤 |
| Agent 主动工具查询 | 低置信度、长文档、完整 Trace、深层图谱、用户明确要求 | 模型执行期间 | 工具只返回摘要/分页结果；查询行为写入 Trace |

### 7.2 推荐的具体注入内容

- 第 1 层当前上下文：直接由 `Session.get_history()` 和 `ContextGovernor` 管理，每轮必有。
- 第 2 层 Trace：默认不注入原始事件；只在 `TRACE_INSPECT`、失败恢复、评测和用户要求时，通过 `trace_search`/`trace_read` 查询。
- 第 3 层知识图谱：不把整个 Wiki 放入 system prompt；路由后最多初召回 8 个高分页面，重排后只注入最多 3 条短摘要，必要时再调用 `wiki_read`。
- 第 4 层 Skill：Skill summary 可保留在稳定上下文；完整 SKILL.md 和案例仅在意图命中后加载，避免所有 Skill 常驻。
- 第 5 层 Retention：不作为模型事实注入，而是作为检索过滤器、排序因子和写入门禁。

### 7.3 为什么不是“全由工具查询”

完全依赖 Agent 自己查，会出现模型不知道该查什么、忘记调用工具、先做出错误判断等问题。因此高稳定、低体量和高相关信息必须由 ContextBuilder 预先注入。

### 7.4 为什么不是“全部由 ContextBuilder 注入”

全部注入会造成上下文膨胀、旧事实冲突、敏感信息暴露和检索噪声。Trace 原文、长 Wiki 页面和完整案例应保持按需工具查询。

### 7.5 首版推荐策略

首版只实现：

1. ContextBuilder 保留当前会话、活动目标和最近摘要；
2. 路由器为 `MEMORY_READ`、`CASE_RETRIEVE`、`TRACE_INSPECT` 做小规模预检索；
3. 以短摘要形式注入动态上下文；
4. 提供 `wiki_search`、`wiki_read`、`case_search`、`trace_search` 工具供 Agent 深查；
5. 所有检索结果携带来源 ID、置信度和时间，不把无来源文本当作事实。

因此，推荐答案是：**ContextBuilder 负责“预取和注入最可能有用的少量记忆”，工具负责“模型执行过程中的深查、核验和扩展”**。

## 8. Skill 与 Tool 的发现和上下文装载策略

你提出的两个方向并不是完全互斥，但适合处理的对象不同：

1. “把简略信息放到一个文档”适合做**目录/索引**；
2. “提供 search 工具”适合做**动态发现和按需装载**。

### 8.1 与当前 nanobot 的对比

当前 nanobot 已经对 Skill 采用了一个半成品的渐进式方案：

- `SkillsLoader.build_skills_summary()` 把 Skill 名称、description、路径和可用性放进系统上下文；
- `always=true` 的 Skill 才会把完整内容常驻加载；
- 其他 Skill 只给摘要，模型需要时通过 `read_file` 读取完整 `SKILL.md`；
- `ToolRegistry.get_definitions()` 目前会把已注册工具的完整 schema 一次性提交给模型，并用稳定排序和缓存保持 prompt 稳定。

因此，当前 Skill 已经接近方向一；Tool 目前仍是“全部注册、全部暴露”的方向。

### 8.2 方向一：单一目录文档

优点：实现简单、模型容易理解、可利用现有 prompt cache。

问题：

- 文档变大后，每轮都增加 token；
- 文档更新会导致整个稳定上下文失效；
- description、路径、依赖和权限容易过期；
- 模型仍然需要另一次 `read_file` 才能得到完整 Skill；
- 对工具来说，目录文档不能替代真实 JSON schema，无法保证参数校验。

结论：保留“生成式目录”作为索引，但不把所有 Skill 正文和 Tool schema 放入一个大文档。目录应是机器生成的 metadata manifest，而不是手工维护的百科文档。

建议目录字段：

```json
{
  "name": "github-code-review",
  "kind": "skill|tool",
  "description": "...",
  "capabilities": ["read_repo", "review_diff"],
  "requirements": {"bins": ["git"], "env": []},
  "risk": "read_only|write|external_side_effect",
  "source": "builtin|workspace|mcp|plugin",
  "version": "1.3.0",
  "content_hash": "..."
}
```

### 8.3 方向二：search 工具

Search 工具适合工具数量和 Skill 数量较多的情况，但不能只返回名称。它至少需要支持：

- capability/query 搜索；
- 风险和权限过滤；
- requirements 检查；
- Skill 的完整内容或路径获取；
- Tool 的完整 JSON schema 获取；
- 版本和来源返回；
- 结果分页和候选数量上限。

工具发现不能停在“告诉模型工具名”。因为当前 `ToolRegistry` 只有已注册工具才能执行，模型也只有在请求中看到了 schema 才能可靠地产生参数。因此真正的动态工具发现需要增加一条激活流程：

```text
初始请求：核心工具 + tool_search schema
  ↓
模型调用 tool_search("查找 PDF 提取工具")
  ↓
系统从 ToolRegistry/MCP/plugin catalog 返回候选 schema
  ↓
当前 turn 动态激活候选工具
  ↓
下一次模型请求携带核心工具 + 已激活工具 schema
  ↓
模型正常调用真实工具
```

这比单纯新增一个 search function 更复杂，需要修改 AgentRunner 的工具 schema 生命周期、缓存 digest、MCP 工具加载和 Audit 事件。

### 8.4 推荐方案：混合渐进式工具发现

| 对象 | 常驻上下文 | 动态搜索 | 完整内容/Schema |
|---|---|---|---|
| 核心工具 | 保留少量高频、低风险工具 schema | 不需要 | 启动时注册 |
| 非核心工具 | 只保留能力目录或不放 schema | `tool_search` | 搜索后动态激活 |
| Skill | 保留短摘要和名称 | `skill_search` 或目录检索 | 命中后读取完整 `SKILL.md` |
| 案例 | 不常驻 | `case_search` | 命中后加载摘要和关键步骤 |
| Trace | 不常驻 | `trace_search` | 用户要求或失败恢复时读取 |

首版建议不要立即把全部工具改成动态搜索，而是分两步：

1. **先做 Skill 目录 + 按需正文加载**：沿用当前 `SkillsLoader`，将 summary 改成机器生成 manifest，增加 `skill_search`，不改变 ToolRegistry。
2. **再做 Tool Search + 动态激活**：先将低频、重 schema、外部副作用强的 MCP/plugin 工具放入 discoverable 集合，核心文件、搜索、消息和安全控制工具保持常驻。

### 8.5 工具分组建议

- `core_always_on`：`read_file`、`list_dir`、基础搜索、状态查询、停止/恢复等。
- `task_common`：shell、patch、web fetch 等，按 Agent 类型或 workspace 配置启用。
- `discoverable`：MCP 工具、第三方 API、图片/音频、复杂 CLI、低频专业工具。
- `restricted`：写入外部系统、删除、发送消息、凭据相关工具，必须经过权限和确认。

### 8.6 选型结论

最终选择：**Skill 使用“目录摘要 + 按需读取”；Tool 使用“核心工具常驻 + 非核心工具搜索后动态激活”；案例和 Trace 只通过搜索工具按需查询。**

这比单一大文档更节省上下文，也比一开始让所有工具都动态化更符合当前 nanobot 的稳定性和兼容性要求。

## 9. nanobot-llm-wiki 集成方案

`nanobot-llm-wiki` 定位为第三层知识图谱，兼容承载第四层案例索引，不替代 Session 或 Audit。

### 9.1 第一阶段接入

- 继续使用 Markdown 作为人类可读源文件。
- 使用 SQLite FTS5、标签、别名和关系做第一版检索。
- 通过已有 `wiki_search`、`wiki_read`、`wiki_link`、`wiki_status`、`wiki_doctor` 工具接入；写入通过受策略保护的 `wiki_upsert` 或 `wiki_propose_maintenance` 落地，详见 5.9。
- 先完成 `metadata` / `metadata_json` 和 frontmatter round-trip，再让 Case、Trace、Skill 关联字段进入生产页面，避免更新页面时丢失来源证据。
- `wiki_search` 增加 page type 和元数据过滤；`case_search` 是其 `page_types=["case"]` 的受限别名，不另建 Case 数据库或重复工具。
- 增加 `wiki_neighbors`，只读查询 Case、Trace 摘要、Skill、项目与决策之间的一跳关系。
- 所有 Wiki 写入带 `trace_id`、来源 cursor、agent 身份和 namespace。
- 继续禁止写入 API key、cookie、私钥、凭据和未经验证的猜测。

### 9.2 页面类型建议

```text
fact          稳定事实
decision      架构/产品决策
project       项目背景和目标
entity        人、服务、仓库、工具等实体
trace_summary Trace 的可读摘要
case          已验证的任务案例
anti_pattern  失败模式和禁止做法
```

### 9.3 关系建议

```text
tracks、depends_on、uses、supersedes、derived_from、validated_by、
similar_to、implements、contradicts、owned_by、scoped_to
```

## 10. 缓存与检索设计

### 10.1 缓存分层

- L0：当前 turn 内的 Python 对象。
- L1：`SessionManager` 会话缓存。
- L2：稳定 system prompt、tool schema、skill summary cache。
- L3：Wiki SQLite FTS、关系索引和页面元数据。
- L4：Audit/Trace 冷存储。

首版不启用 embedding、向量索引或向量 reranker。只有 SQLite FTS5、字段过滤和图关系的离线召回评测明确不足时，才单独提出 SQLite 内向量扩展或 `sqlite-vec` 的评估；它不属于当前实施范围。

### 10.2 混合排序

```text
score =
  0.35 * lexical_bm25
+ 0.20 * task_signature_and_entity_overlap
+ 0.15 * authority_and_confidence
+ 0.10 * skill_match
+ 0.10 * graph_proximity
+ 0.10 * recency_and_usage
```

不同意图使用不同权重：事实查询偏 lexical/authority，案例查询偏任务签名、实体和 Skill 匹配，架构决策偏 authority/graph proximity。

### 10.3 检索流程

```text
意图路由
  → memory scope 过滤
  → FTS/BM25 初召回
  → 权限、namespace、时间和状态过滤
  → 图邻居扩展
  → rerank
  → 去重与冲突检测
  → token budget 裁剪
  → 写入 dynamic context
```

页面 chunk 应附带标题、项目、类型、来源、时间和关系上下文，避免只以裸文本进行关键词匹配。

### 10.4 首版上下文与深查预算

首版不按模型的完整上下文窗口无限放大记忆注入。自动记忆注入使用“少量高信号 + 按需深查”的预算，既控制输入成本，也避免上下文污染：

| 场景 | 首版默认上限 |
|---|---:|
| 路由后自动注入的记忆总量 | 软上限 2,000 tokens；硬上限 3,000 tokens |
| 自动注入项 | 最多 3 条；通常为最多 2 条事实/决策和最多 2 条 Case，但总数不超过 3 |
| 初召回候选 | 最多 8 条，仅用于重排，不全部注入 |
| 单条注入摘要 | 400～700 tokens，必须带来源、时间、置信度与风险 |
| `wiki_read(view=summary)` | 800 tokens |
| `wiki_read(view=full)` | 3,500 tokens；超出时使用 cursor 分页 |
| `trace_read_summary` | 1,000 tokens；不自动注入原始 Trace |
| 单次深查累计工具结果 | 5,000 tokens；超出后先压缩已有结果或分页 |

自动注入硬上限按模型窗口自适应：

```text
min(3,000, max(1,200, context_window_tokens × 6%))
```

稳定的 system prompt、工具 schema 和 Skill 目录保持在 stable 段以复用 prompt cache；检索记忆只放入 dynamic 段。长 Wiki/Trace 工具结果不应永久跟随之后的每次模型调用：任务完成或阶段切换时，由 `ContextGovernor` 压缩为短工作摘要或清除原始结果。

## 11. Trace 派生和自进化闭环

```text
采集 Trace
  → 脱敏与权限过滤
  → 识别重复任务
  → 构造评测集
  → 运行当前 Skill 基线
  → 生成候选 Skill/工具描述/提示词
  → held-out 验证
  → 约束检查
  → staging
  → 人工或策略批准
  → Git 分支/PR
  → 灰度
  → 线上 Trace 对比
  → 接受或回滚
```

### 11.1 Hermes 借鉴点

- 使用真实执行 Trace 解释“为什么失败”，而不是只看成功率。
- 按风险从低到高优化：Skill 文本 → 工具描述 → prompt section → 工具代码。
- 使用 LLM judge、规则指标和测试门禁组合评估。
- 任何生产变更走 Git 分支和人工 PR，不直接覆盖线上 Skill。

### 11.2 SkillOpt 借鉴点

- harvest 与 replay 分离。
- 候选先进入 staging，再 adopt。
- 使用 held-out 任务防止过拟合历史样本。
- 支持按 Skill 分组、单独采用和回滚。
- 记录 evidence log，保留候选来源和评测过程。

### 11.3 Skill 总结触发和去重

Skill 总结不在每次会话结束时执行，也不因为超时就直接生成 Skill。首版采用固定的空闲触发窗口：**会话连续 45 分钟没有新消息后，只触发一次 Skill review 检查**。

触发流程以连续会话批次为单位，而不是以单条消息为单位：

```text
会话内连续消息持续写入
  → 每条消息只刷新同一 session 的 due_at = last_activity_at + 45m
  → 45 分钟无新消息
  → 固定 (last_review_cursor, snapshot_cursor] 作为本轮批次
  → 触发一次 Skill review
  → Agent 判断是否存在可复用流程
  → 无流程：结束
  → 一次性经验：只保存 Case
  → 稳定、可重复流程：生成 Skill candidate
```

review 完成后，下一条新消息才开启新的 `activity_epoch`，之后重新计算 45 分钟窗口。若 review 期间有新消息到达，该消息不进入已固定的 `snapshot_cursor`，留给下一次 idle 批次。为避免重复总结，状态至少记录：

- `activity_epoch`；
- `last_skill_review_at`；
- `skill_review_cursor`；
- `last_message_cursor`、`snapshot_cursor`、`due_at`；
- 本轮是否已经 review。

Skill review 的准入条件：流程有多个步骤、结果可验证、不是一次性偶然操作、与现有 Skill 不重复，并且能抽象成跨任务可复用的方法。超时只负责触发检查，不能直接决定生成 Skill。

Skill review 由后台维护服务发起，并使用受限维护 Agent 完成语义判断；它不是当前用户 turn 的普通子 Agent。对于已有 Skill，维护 Agent 只能提出新建、修改合并或 references candidate，正式 Skill 的整合必须经过评测和人工确认。

### 11.4 自动评测与发布闭环

自进化不是“生成 Skill 后直接覆盖文件”，而是一个可恢复的状态机：

```text
真实会话 / Trace
  → Case 与失败模式
  → 维护 Agent 生成 Skill candidate
  → 同时生成该 Skill 的 EvalPack 草案
  → 评测审核 Agent 检查题目、rubric、fixture 和禁止行为
  → 后台服务封存 EvalPack（dataset_hash）
  → baseline 与 candidate 在隔离 workspace 分别回放
  → 独立评分器按 Skill 专属 rubric 打分
  → 安全、工具风险、成本、延迟和回归 gate
  → staging / awaiting_confirmation
  → 用户确认后 Git PR 或 workspace adopt
  → 线上 Trace 持续补充新题和失败回归题
  → 定期重新评测，必要时回滚
```

职责边界：

- **维护 Agent**：从 Case/Trace 生成 candidate 和 EvalPack 草案，只能写 staging；
- **评测审核 Agent**：检查题目是否覆盖适用范围、非目标和安全边界，不能发布 Skill；
- **后台评测服务**：固定数据集、创建隔离 workspace、运行 baseline/candidate、统计 token、延迟、工具风险并保存 evidence log；
- **独立评分器**：按固定 rubric 对结果评分，不能修改 EvalPack；LLM judge 只作为评分组件，不能单独决定发布；
- **用户**：确认是否采用已通过 gate 的候选。

候选状态统一为：

```text
candidate_created
→ eval_pack_missing
→ eval_pack_ready
→ evaluating
→ passed | failed | insufficient_evidence
→ awaiting_confirmation
→ adopted | rejected | rolled_back
```

Hermes 的主要借鉴是自动合成评测题、SessionDB 挖掘、train/validation/holdout 划分、baseline 对比、Trace 反思和 PR 发布。Hermes 当前实现允许生成题目和 LLM judge 使用同一评测模型；nanobot 首版增加 EvalPack 封存、独立审核、禁止同一 Agent 自评自批，以及候选状态机，避免评测泄漏和虚假通过。

Hermes 当前默认 `eval_dataset_size=20`、`train_ratio=0.5`、`val_ratio=0.25`、`holdout_ratio=0.25`，并使用随机打乱；它没有强制最少有效题、固定随机种子、题目去重或 holdout 封存。nanobot 采用其 20/10/5/5 作为启动基线，但把上述质量门槛、稳定划分、版本/hash 和封存作为正式发布前的硬约束。Hermes 的 synthetic 数据可以启动优化，但 nanobot 不允许仅凭 synthetic EvalPack 发布正式 Skill，必须逐步加入真实 Trace、失败回归题或可验证 fixture。

评测集不需要在 Phase 0～4 开始前准备好。没有足够真实任务时，可以先使用自动合成题和少量真实题生成 EvalPack 草案；正式 Skill 发布前必须达到最低证据要求。真实任务不足时，candidate 只做 staging，不阻塞 Trace、Case、检索和 Skill review 的实现。积累到足够的脱敏、可复现任务后，再启动该 Skill 的正式 baseline/held-out 评测。

## 12. 遗忘、降权、归档和删除

### 12.1 降权公式

```text
effective_score =
  confidence
  × authority
  × task_match
  × exp(-age / half_life)
  × (1 + log(1 + access_count))
```

### 12.2 生命周期

```text
candidate → active → stale → archived → deleted
             ↑         │
             └─ reaffirm ┘
```

### 12.3 保留规则

Trace 默认分层保留：原始 Audit 事件保留 **18 天**；可能含完整明文的 payload 保留 **7 天**；脱敏 Trace 摘要、用户确认的知识、Case 和架构决策长期保留。期限配置化，但首版以该默认值实现。

永久保留：用户明确标记、仍有效的安全规则、未被取代的架构决策、当前项目关键事实。

自动归档：长期未访问、已被新事实替代、只具历史价值或只属于已结束项目的内容。

默认不硬删除：内容过时或冲突时优先合并、修正、标记 `supersedes`、降低置信度或归档。只有用户明确要求、发现秘密、权限违规、错误写入或数据主体删除请求时，才执行永久删除。

删除必须同步处理页面、关系、FTS 索引和案例引用；Audit 只保留最小化删除事件，不保留被删除的敏感正文。

## 13. 与现有 nanobot 的集成点

### 13.1 ContextBuilder

在 `build_system_sections()` 中增加按 intent 的 memory retrieval，保持 stable/dynamic 分段；知识和案例只进入 dynamic 部分，并受 token 上限控制。

### 13.2 AgentLoop

在 turn 初始化时生成路由结果，把路由结果放入运行上下文和 Audit，不默认写入长期记忆。

### 13.3 Audit

新增或扩展事件记录：

- memory retrieval started/finished；
- retrieved memory IDs；
- rejected memory IDs 及原因；
- injected context digest；
- memory write candidate；
- forget/archive action；
- case promoted/rejected；
- skill evaluation result。

### 13.4 MemoryStore/Dream

Dream 继续维护 `SOUL.md`、`USER.md`、`MEMORY.md`；对于 SKILL.md 只生成候选版本，不直接覆盖正式 Skill。Dream 可生成派生 Wiki candidate，但不直接编辑 Wiki SQLite。Wiki 记录应关联 history cursor 和 trace_id。

### 13.5 SkillLoader/ToolRegistry

SkillLoader 增加版本/hash/状态读取；ToolRegistry 增加工具示例、能力标签和风险等级，但保持稳定 schema 顺序，避免破坏 provider prompt cache。

## 14. 分阶段实施路线

### Phase 0：契约和观测

目标：不改变模型行为，先统一协议。

工作项：

- 定义 memory、case、retrieval、retention schema。
- 定义 Wiki 扩展 `metadata` schema、frontmatter round-trip 规则及 `wiki_*` / `trace_*` / `skill_*` 工具契约。
- 实现单 workspace `ToolPolicy` 骨架，以及统一输出信封、错误码和调用审计字段；不引入用户身份或 RBAC。
- 统一 `trace_id`、`history_cursor`、`skill_version`、`namespace`。
- 增加检索和写入的审计事件。
- 设计秘密检测、脱敏和权限边界。

验收：现有测试通过；Trace 能关联一次检索和一次工具执行；不新增默认上下文内容。

### Phase 1：Wiki 知识层

目标：引入可读、可编辑、可检索的长期知识层。

工作项：

- 集成 `nanobot-llm-wiki` 工具或 MCP。
- 实现页面类型、标签、关系、source refs、`metadata_json` 与 frontmatter round-trip。
- 扩展 `wiki_search` 的 page type/元数据过滤，增加 `wiki_neighbors`；把 Case 检索实现为受限搜索别名，不增加独立 Case 事实源。
- 增加 `MEMORY_READ/WRITE/FORGET` 路由。
- 只读检索默认启用，低风险写入可自动执行，修正/合并/归档按策略执行，永久删除需明确意图。

验收：事实可搜索、可读、可关联、可归档；修正或归档后检索结果符合新状态；工具权限和审计完整。

### Phase 2：按需检索和上下文注入

目标：让知识、Trace 摘要和 Skill 案例只在需要时进入上下文。

工作项：

- 引入 intent router。
- 实现 FTS/BM25 + 元数据过滤 + rerank。
- 接入 token budget、去重和冲突检测。
- 验证稳定 prompt/tool schema cache 不被破坏。

验收：上下文 token 下降；相关信息召回率提升；无关记忆不进入 prompt；检索事件可回溯。

### Phase 3：Trace 派生案例

目标：把成功和失败过程转化为可复用案例。

工作项：

- 从 Audit Trace 生成 trace_summary、case、anti_pattern。
- 加入敏感信息检测、去重、用户确认和案例准入。
- 将案例与 Skill 版本、工具调用、Trace 建立关系。
- 实现 `memory_review_batch`、`trace_search`、`trace_read_summary` 和 `wiki_propose_maintenance`；维护 Agent 只产生候选提案。
- 由维护 Agent 自动生成 Skill 专属 EvalPack 草案；评测集不足时只保存 `eval_pack_missing` / `insufficient_evidence` 状态，不阻塞 Case 沉淀。

验收：案例可按任务特征检索；失败案例不会被误当作成功模板；每个案例有完整来源。

### Phase 4：Dream、Wiki、Skill 协同

目标：统一五层记忆的写入和生命周期。

工作项：

- Dream 生成 Wiki candidate，不直接修改结构化数据库。
- 实现 stale、archive、reaffirm、forget 流程。
- 将重复工作流候选转为 SKILL.md staging。
- 增加 Skill 与案例的版本兼容检查。
- 增加 45 分钟空闲窗口和每个 activity epoch 仅一次的 Skill review 检查。
- 增加 workspace 级串行后台维护服务，以及受限维护 Agent 的白名单工具和结构化动作校验。
- 增加 `skill_catalog_search`、`skill_read`、`skill_propose` 和由后台服务执行的 candidate validate/evaluate/publish 流程。
- 增加 EvalPack 审核、封存、隔离回放、独立评分和 candidate 状态机；维护 Agent 不得自评自批。

验收：重复事实不增长；旧事实可降权/归档；用户 forget 可穿透所有索引；Skill 更新可回滚。

### Phase 5：离线自进化

目标：引入 Hermes/SkillOpt 风格的可验证优化。

工作项：

- 从 Trace/Case 构造评测集。
- 运行 baseline 与 candidate 对比。
- 引入 held-out gate、LLM judge、成本和安全指标。
- 自动生成并封存每个 Skill 的 EvalPack；按 Skill 专属 rubric 评估，不使用一套全局题目替代。
- EvalPack 默认按 20 道题、10/5/5 划分启动；发布前至少满足 10 道有效题、3 道 holdout、3 类任务和 1 道失败/边界题，并通过去重、可执行性和敏感信息检查。
- 评测包达到最低证据要求后，运行隔离 workspace 的 baseline/candidate 回放；真实任务不足时不发布。
- 首版 gate 固定为：held-out 成功率不低于 baseline；不新增高风险工具调用；无安全违规；单任务总 token 成本增幅不超过 15%；P50 完成延迟增幅不超过 20%。若成功率明显提升但成本或延迟超标，候选仅保留在 staging，由用户确认是否采用。
- 所有采用操作通过 staging、Git 分支和 PR。

验收：候选在 held-out 集上无回归；测试、大小、缓存兼容和安全检查通过；可生成回滚提交。

### Phase 6：受控持续运行

目标：在低风险范围内实现夜间或空闲时自动整理。

工作项：

- 只处理重复、高价值、低敏感任务。
- 默认生成候选，不自动合并。
- 记录完整 evidence log。
- 定期比较成功率、成本、延迟、恢复率和安全事件。
- 线上失败 Trace 自动转为该 Skill 的回归题，定期更新 EvalPack；更新后重新封存版本并回归评测。

验收：连续运行不会造成记忆膨胀、权限越界或 Skill 漂移；出现回归可自动停止并回滚候选。

## 15. 风险与控制措施

| 风险 | 控制 |
|---|---|
| 错误事实进入长期记忆 | 来源引用、置信度、用户确认、冲突检测 |
| Trace 泄露秘密 | payload 脱敏、敏感字段扫描、默认摘要化 |
| 不同运行角色越过工具边界 | `ToolPolicy`、调用者白名单、workspace 路径约束、确认门禁 |
| Skill 自我强化错误 | held-out gate、人工审核、版本回滚 |
| 后台维护与用户任务冲突 | workspace 级互斥锁、新消息取消未开始任务、正式资产只读 |
| 上下文污染 | intent scope、rerank、token budget、摘要化 |
| 自动遗忘误删 | archive 优先、可恢复、用户确认、删除审计 |
| prompt cache 失效 | stable/dynamic 分离、tool schema digest、离线发布 |
| 评测过拟合 | 独立 held-out 集、真实任务回放、长期线上指标 |
| 数据无限增长 | TTL、半衰期、访问热度、冷热分层和归档 |

## 16. 建议的第一批任务

1. 创建统一 schema 和 memory scope 枚举，不改变默认行为。
2. 为 Wiki 工具补充 `trace_id`、namespace、`source_actor`、source refs 和 `ToolPolicy` 校验。
3. 在 Audit 中记录 retrieval/write/forget 事件。
4. 实现一个只读 `MEMORY_READ` 路由器。
5. 为一个 Skill 建立最小 Wiki 案例页面、索引字段和人工确认流程。
6. 先用约 20 道自动生成题建立 EvalPack 草案；真实任务积累后补入至少 10 道脱敏、可复现任务和失败回归题，再建立正式 baseline/held-out 数据集。
7. 运行一次“只生成候选、不自动采用”的 SkillOpt 风格离线实验。

## 17. 参考资料

- nanobot 源码：`nanobot/agent/memory.py`、`nanobot/session/manager.py`、`nanobot/agent/context.py`、`nanobot/agent/context_governance.py`、`nanobot/agent/skills.py`、`nanobot/agent/tools/registry.py`、`nanobot/audit/`
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic: Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
- [Anthropic: Introducing advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)
- `../hermes-agent-self-evolution/README.md`、`PLAN.md`
- `../SkillOpt-main/SkillOpt-main/docs/sleep/README.md`
- `../nanobot-llm-wiki/README.md`

## 18. 已确认决策与仍需确认事项

### 18.1 已确认

1. Wiki 采用插件插入方式，优先通过 `nanobot-llm-wiki` 的插件/MCP/entry point 接入，不把 Wiki 实现硬编码进 nanobot 核心。
2. 长期记忆允许低风险自动写入，但必须满足来源、脱敏、namespace、置信度和可回滚要求；普通内容默认通过合并、修正、降权和归档维护，不做永久删除。
3. 暂不更换向量方案。首版使用 SQLite、FTS5、标签、别名和图关系；只有在实际规模和召回指标证明不足时，才评估额外向量数据库。
4. 会话连续 45 分钟无新消息后，只触发一次 Skill review 检查；是否生成案例或 Skill candidate 由 Agent 根据可复用性判断。
5. Trace 默认保留：原始 Audit 事件 18 天；含完整明文的 payload 7 天；脱敏摘要、用户确认知识、Case 和架构决策长期保留。
6. 后台维护使用 SQLite 持久状态与单 worker；每个会话采用可延期防抖状态，在 idle 后按 `last_review_cursor` 到 `snapshot_cursor` 的整批内容处理，而不按单条消息处理。
7. Wiki/Case 修改先保存不可变 revision；workspace Skill adopt 永不覆盖旧版本，采用新版本目录和 current 指针回滚。
8. 低风险自动写入遵循 5.7.1 白名单；Case、修正建议和 Skill candidate 先进入 candidate，正式资产、删除、外部写入和 Git PR 必须用户确认。
9. 用户确认只对本轮、一次、单个动作指纹有效；目标或关键参数改变后失效。
10. 自动记忆注入采用 2,000 tokens 软上限、3,000 tokens 硬上限和 6% 窗口自适应规则；完整内容通过分页深查加载。
11. 首版 Skill 评测 gate：held-out 成功率不低于 baseline、不新增高风险工具、无安全违规、单任务总 token 成本增幅不超过 15%、P50 完成延迟增幅不超过 20%；超标但效果明显提升的候选停留在 staging，需用户确认。
12. 自动进入 active 的 `trace_summary` 必须具备完整 Trace 来源、脱敏和可验证工具结果，且 `confidence >= 0.85`；不满足时仅写入 candidate。
13. 每个 Skill 使用自己的 EvalPack；评测题和 rubric 由 Agent 根据 Skill、Case 和 Trace 自动生成草案，后台审核、封存和回放，用户不需要手工编写整套评测集。
14. EvalPack 不足时 candidate 只能停留 staging；生成、审核、执行、评分和发布职责分离，禁止同一 Agent 自评自批。
15. 线上失败 Trace 和用户纠正可自动转为回归评测题；EvalPack 版本化并通过 `dataset_hash` 封存。
16. EvalPack 默认采用 Hermes 的 20 道题起步配置（10 train / 5 validation / 5 holdout），但正式发布必须满足 nanobot 的最低质量门槛、固定划分和 holdout 封存规则；synthetic 题只能启动候选流程，不能单独作为正式发布依据。

### 18.2 建议决策

1. 首个自进化 Skill 建议选择“只读代码审查/仓库问题定位”类任务：频率较高、结果可用测试或人工 rubric 评估，不直接修改代码、不合并 PR，适合作为低风险 baseline。
2. 建议区分两种采用方式：
   - **Git PR 采用**：生成版本化分支和 PR，适合 builtin/shared skill、需要团队审查的变更，具备完整 diff、评测记录和回滚能力。
   - **workspace skill adopt**：把候选复制到当前 workspace 的 `skills/`，适合个人实验和快速试用，不经过远端 PR，治理和审查能力较弱。
   - 推荐默认：共享或内置 Skill 必须 Git PR；个人 workspace Skill 可人工确认后 adopt，但仍保留候选、评测和原版本备份。

### 18.3 暂不阻塞实施的事项

1. 首个代码审查/仓库定位 Skill 的 10～20 个脱敏真实任务尚未积累；Phase 0～4 先采集真实 Trace、生成 Case 和 EvalPack 草案，待任务足够后再启动 Phase 5 正式 baseline/held-out 评测。
