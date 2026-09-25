# nanobot 五层记忆体系实施设计

> 文档状态：实施设计（基于 2026-09-25 代码快照）
> 适用范围：当前仓库的 Python Agent、Session、Audit/Trace、Skill、ToolRegistry，以及通过插件/MCP/entry point 接入的 Wiki。
> 本文只描述实现，不修改生产代码；所有新增能力都必须先以 feature flag 和迁移脚本落地。

## 1. 结论与实施边界

五层记忆不是一个“大记忆表”，而是五种职责不同的存储和检索边界：

1. 当前上下文：仍由 `ContextBuilder`、`SessionManager`、`ContextGovernor` 负责。
2. Trace 过程记忆：继续以 Audit 事件/JSONL 为事实源，新增 SQLite 查询索引。
3. 知识图谱：由 Wiki 插件保存 Markdown 页面和关系，SQLite 只建可重建索引。
4. Skill/Case：Skill 继续是版本化 `SKILL.md`，Case 是 Wiki 的 `page_type=case` 页面；SQLite 保存关联、状态和评测索引。
5. Retention：统一处理评分、降权、归档、TTL 和 tombstone，不把生命周期状态当作事实内容。

本阶段只新增 `_other/NANOBOT-FIVE-LAYER-MEMORY-IMPLEMENTATION-DESIGN.md`。实际开发应按 Phase 0→6 分批提交；任何阶段都不允许直接覆盖 Markdown 事实源、正式 Skill 或原始 Audit。

## 2. 现状盘点与计划项分类

### 2.1 当前真实实现

| 组件 | 现状 | 设计中的使用方式 |
|---|---|---|
| `AgentLoop`/`AgentRunner` | 已有消息消费、运行规范、工具循环、恢复和回调 | 保持主流程；在构建上下文前插入只读 memory scope，在 turn 完成后发出派生事件 |
| `ContextBuilder` | stable/dynamic sections、身份/bootstrap、长期记忆、近期历史、always skill、skills summary；已有 digest/cache metadata | 扩展为 `IntentRouter + MemoryRetriever`，只将稳定摘要放 dynamic overlay |
| `ContextGovernor` | token 预算、裁剪、孤立 tool result 清理、非法工具调用修复 | 作为最终防线；新增记忆预算不能绕过它 |
| `MemoryStore` | `SOUL.md`、`USER.md`、`memory/MEMORY.md`、`history.jsonl`、Dream cursor、原子写入/GitStore | 保持为文件事实源；加 revision/candidate 编排，不将 SQLite 变成正文库 |
| `SessionManager` | JSONL 会话、cursor、缓存、fork、损坏恢复和 last_consolidated | 复用 session key/cursor；维护任务另建持久状态表 |
| `SkillsLoader` | builtin/workspace、frontmatter、`always`、依赖、disabled、渐进式加载 | 复用发现；新增外部 entry point/MCP skill catalog 适配器 |
| `ToolRegistry` | 注册、稳定排序、schema 校验、结构化错误、schema digest | 复用注册和校验；加入 `ToolPolicy` 能力边界和角色检查 |
| Audit | 事件/载荷 JSONL、红脱敏、trace/run/turn 父子关系、索引、完整性、WebUI 查询 | 继续为 Trace 事实源；增发 retrieval/write/forget/review 事件 |
| Wiki | 独立项目，支持本地 Markdown、SQLite/FTS、MCP/CLI/UI，不属于 nanobot 核心 | 通过插件/MCP/entry point 接入，不硬编码 Wiki 类和表 |
| Hermes self-evolution | 独立离线 pipeline，dataset、replay、judge、PR guardrail | 借鉴流程，不作为运行时依赖 |
| SkillOpt-Sleep | harvest→mine→replay→stage→adopt，具备证据和多 Skill gate | 借鉴状态/证据/隔离回放；不复制其跨平台适配器 |

### 2.2 计划逐项分类

| 计划项 | 分类 | 依据/实施结论 |
|---|---|---|
| 当前会话上下文、session JSONL | 已存在，可直接复用 | 仅增加 scope 元数据和 digest |
| MemoryStore 文件长期记忆、Dream cursor | 已存在，但需要扩展 | 增加 revision、candidate、统一写入审计和 retention |
| ContextGovernor token 控制 | 已存在，可直接复用 | 记忆检索结果必须作为受控 dynamic overlay |
| Audit Trace、红脱敏、JSONL 事实源 | 已存在，可直接复用 | 仅新增事件类型和 trace_index 派生器 |
| Trace→摘要/Case/Skill 关联 | 当前不存在，需要新增 | 新增派生 worker、去重和候选状态机 |
| Wiki Markdown/FTS/关系图 | 依赖外部项目 | nanobot 只实现适配协议和失败降级 |
| SQLite 统一索引 | 当前不存在，需要新增 | workspace 级数据库、迁移、重建和单 writer |
| intent 路由和 memory scope | 当前不存在，需要新增 | 在 ContextBuilder 前增加纯函数路由器 |
| `skill_catalog_search`/`skill_read`/`skill_propose` | 当前不存在，需要新增 | 通过 ToolRegistry 暴露；写入由维护服务执行 |
| 45 分钟 review 防抖任务 | 当前不存在，需要新增 | `maintenance_jobs` + 单 worker + workspace 锁 |
| tombstone、18/7 天 retention | 当前不存在，需要新增 | 归档优先，安全删除独立分支 |
| Skill revision/current/rollback | 已存在，但需要扩展 | 当前 loader 只有文件发现，无发布指针和 hash 绑定 |
| EvalPack/EvalRun/held-out gate | 当前不存在，需要新增 | 参考 Hermes/SkillOpt；每 Skill 独立包 |
| 首个只读代码审查 Skill fixture | 依赖外部项目 | 由评测 harness 提供临时 workspace，不改变 nanobot 默认工具 |
| 独立评测 Agent/审核 Agent | 当前不存在，需要新增 | 角色隔离和 ToolPolicy 强制禁止自评自批 |
| 独立向量数据库、多租户/RBAC | 需要延期 | 已确认不引入，SQLite FTS5/标签/关系图足够首版 |
| 自动正式发布、自动 Git merge/PR | 需要延期 | 只到 staging；用户确认后才 adopt 或开 PR |

## 3. 目标组件和目录

建议新增但尚未实现的包（命名可在 Phase 0 评审时固定）：

```text
nanobot/memory/
  schema.py          # 枚举、迁移版本、SQLite 连接
  index.py           # FTS/标签/关系/trace 索引
  repository.py      # transaction、current 指针、tombstone
  retriever.py       # scope、FTS、rerank、预算
  intent.py          # intent 和 scope 路由
  maintenance.py     # 单 worker、lease、状态机
  derivation.py      # trace→summary/case/skill candidate
  evaluation.py      # EvalPack、EvalRun、gate
  policy.py          # ToolPolicy 和确认门禁
nanobot/plugins/memory_wiki.py  # entry point/MCP 适配器
```

数据库路径固定为 `<workspace>/.nanobot/memory.sqlite3`；不得把它放入 `runtime/workspace` 之外，也不得把 SQLite 正文作为用户可编辑事实源。数据库启用 WAL、`foreign_keys=ON`、`busy_timeout=5000`，所有写操作由 workspace 单 worker 串行执行。

## 4. SQLite schema（建议 v1）

约定：所有时间为 UTC ISO-8601 文本；ID 为不可变 ULID/UUID 字符串；`content_hash` 为 `sha256:<hex>`；JSON 字段使用 TEXT 并在边界用 Pydantic/JSON Schema 校验。`created_at`、`updated_at` 由服务端填写，不能由 Agent 工具传入。

### 4.1 记忆、Wiki、Case、Skill

```sql
CREATE TABLE memory_records (
  memory_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL DEFAULT 'workspace',
  memory_type TEXT NOT NULL CHECK(memory_type IN ('fact','decision','trace_summary','case','skill_ref')),
  title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  source_refs_json TEXT NOT NULL DEFAULT '[]', entities_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]', sensitivity TEXT NOT NULL DEFAULT 'private',
  confidence REAL NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 1),
  authority REAL NOT NULL DEFAULT 0 CHECK(authority BETWEEN 0 AND 1),
  salience REAL NOT NULL DEFAULT 0 CHECK(salience BETWEEN 0 AND 1),
  effective_score REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'candidate',
  deletion_state TEXT NOT NULL DEFAULT 'none', source_actor TEXT NOT NULL,
  current_revision_id TEXT, supersedes_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_accessed_at TEXT,
  access_count INTEGER NOT NULL DEFAULT 0, archived_at TEXT, last_skill_review_at TEXT
);
CREATE INDEX memory_scope ON memory_records(namespace,memory_type,status,effective_score DESC);
CREATE INDEX memory_hashless_source ON memory_records(source_actor,updated_at);

CREATE TABLE memory_revisions (
  revision_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_records(memory_id),
  revision_no INTEGER NOT NULL, content TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL, previous_revision_id TEXT REFERENCES memory_revisions(revision_id),
  supersedes_revision_id TEXT REFERENCES memory_revisions(revision_id),
  author_actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(memory_id,revision_no), UNIQUE(memory_id,content_hash)
);
CREATE INDEX memory_revision_history ON memory_revisions(memory_id,revision_no DESC);

CREATE TABLE wiki_pages (
  page_id TEXT PRIMARY KEY, namespace TEXT NOT NULL DEFAULT 'workspace', slug TEXT NOT NULL,
  page_type TEXT NOT NULL CHECK(page_type IN ('fact','decision','case','skill_note','index')),
  source_path TEXT NOT NULL, content_hash TEXT NOT NULL, current_revision_id TEXT,
  status TEXT NOT NULL DEFAULT 'candidate', sensitivity TEXT NOT NULL DEFAULT 'private',
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
  source_refs_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  archived_at TEXT, UNIQUE(namespace,slug)
);
CREATE INDEX wiki_lookup ON wiki_pages(namespace,page_type,status,updated_at DESC);
CREATE INDEX wiki_hash ON wiki_pages(content_hash);

CREATE TABLE wiki_relations (
  relation_id TEXT PRIMARY KEY, from_page_id TEXT NOT NULL REFERENCES wiki_pages(page_id),
  to_page_id TEXT NOT NULL REFERENCES wiki_pages(page_id), relation_type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
  source_revision_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(from_page_id,to_page_id,relation_type)
);
CREATE INDEX wiki_rel_from ON wiki_relations(from_page_id,status);
CREATE INDEX wiki_rel_to ON wiki_relations(to_page_id,status);

CREATE TABLE cases (
  case_id TEXT PRIMARY KEY, page_id TEXT NOT NULL UNIQUE REFERENCES wiki_pages(page_id),
  skill_id TEXT, skill_version TEXT, intent TEXT NOT NULL, task_signature_json TEXT NOT NULL,
  preconditions_json TEXT NOT NULL DEFAULT '[]', steps_json TEXT NOT NULL DEFAULT '[]',
  outcome_json TEXT NOT NULL, failure_patterns_json TEXT NOT NULL DEFAULT '[]',
  trace_ids_json TEXT NOT NULL DEFAULT '[]', confidence REAL NOT NULL DEFAULT 0,
  user_confirmed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'candidate',
  revision_id TEXT NOT NULL, previous_revision_id TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX cases_skill_status ON cases(skill_id,status,confidence DESC);
CREATE INDEX cases_intent ON cases(intent,status);

CREATE TABLE skills (
  skill_id TEXT PRIMARY KEY, namespace TEXT NOT NULL DEFAULT 'workspace', name TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('builtin','workspace','entrypoint','mcp')),
  source_path TEXT, current_revision_id TEXT, current_version TEXT, status TEXT NOT NULL DEFAULT 'active',
  description TEXT NOT NULL DEFAULT '', tool_policy_json TEXT NOT NULL DEFAULT '{}',
  references_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(namespace,name)
);
CREATE INDEX skills_catalog ON skills(namespace,status,name);

CREATE TABLE skill_revisions (
  revision_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  skill_version TEXT NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL,
  previous_revision_id TEXT REFERENCES skill_revisions(revision_id), supersedes_revision_id TEXT,
  source_case_ids_json TEXT NOT NULL DEFAULT '[]', eval_pack_id TEXT, author_actor TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'staging', created_at TEXT NOT NULL,
  UNIQUE(skill_id,skill_version), UNIQUE(skill_id,content_hash)
);
CREATE INDEX skill_revision_current ON skill_revisions(skill_id,status,created_at DESC);
```

`memory_records`、`wiki_pages`、`cases`、`skills` 的正文/版本均不可原地改写；状态、访问计数和 current 指针可变。普通删除只把状态置为 `archived` 或写 `supersedes`；安全删除不更新正文，改写为 tombstone（见 4.3）。

### 4.1.1 表级生命周期矩阵

| 表 | 主键/外键关系 | 可变字段 | 状态字段 | 删除/归档语义 | 迁移与重建 |
|---|---|---|---|---|---|
| `memory_records` | `memory_id`；`current_revision_id`→`memory_revisions` | score、访问计数、current、状态 | candidate/active/stale/archived | 不删正文；archive 或 supersedes；合规删除写 tombstone | 从 `MEMORY.md`/`USER.md`/`SOUL.md` 和 history 摘要重建 |
| `memory_revisions` | `revision_id`；`memory_id`→records；previous 自引用 | 不可变 | 无（由父记录控制） | 永不更新/删除，除非安全清除并留无正文 tombstone | 由 Markdown revision header/sidecar 或导入快照重建 |
| `wiki_pages` | `page_id`；current→外部/本地 revision | title、summary、tags、current、状态 | candidate/active/stale/archived | Markdown 仍在；索引只标状态；删除由 Wiki adapter 执行 | 扫描页面 frontmatter、hash、关系后全量重建 |
| `wiki_relations` | relation→两 page | weight、状态 | active/archived | 关系失效归档，不级联删除页面 | 从页面 links/frontmatter 重建 |
| `cases` | `case_id`、`page_id`；可选 `skill_id` | confidence、使用计数、状态 | candidate/active/archived/rejected | Case 正文是 Wiki page；不硬删，除合规 tombstone | 从 `page_type=case` 页面结构解析 |
| `skills` | `skill_id`；current→skill revision | current、状态、描述元数据 | staging/active/archived/rejected | 旧版本保留；禁用/回滚只切指针 | 从 Skill 目录、entry point/MCP manifest 重扫 |
| `skill_revisions` | revision→skill、previous | 不可变 | staging/approved/rejected/archived | 不删；候选失败留证据，安全删除写 tombstone | 从版本目录和 hash manifest 重建 |
| `trace_index` | `trace_id` | summary、计数、索引状态 | indexed/degraded/expired | 原始 JSONL 由 TTL 清理；摘要可保留 | 从 Audit catalog/events JSONL 重放 |
| `eval_packs` | pack→skill revision | 仅 draft 字段可变，sealed 后不可变 | draft/sealed/retired | 旧包 retired，不删除题目证据 | 从封存 JSON manifest 重建 |
| `eval_runs` | run→pack、baseline/candidate | 运行状态和结束指标 | running/succeeded/failed/stale | 永久保留指标；敏感响应只保 digest/TTL | 不能从结果反推题目，依赖 pack manifest |
| `eval_case_results` | `(run_id,case_key)`→run | 不可变（修正需新 run） | outcome 字段 | 随 run 保留；无正文删除 | 从 runner evidence 导入并校验 hash |
| `maintenance_jobs` | `job_id`，workspace/session 唯一 | lease、cursor、retry、状态 | scheduled/due/leased/reviewing/retry_wait/succeeded/failed/superseded | 成功任务保留最近状态；历史错误不覆盖 | SQLite WAL 恢复；无需事实源重建 |
| `tombstones` | `tombstone_id`；逻辑对象唯一 | 不可变 | 无（存在即阻断） | 永不自动恢复正文；可按合规策略过期元数据 | 从合规删除日志导入，不能从正文推导 |
| `retrieval_events` | `retrieval_id` | 不可变 | outcome | 按审计保留期归档/清理，不能改写 | 从 Audit retrieval 事件重建 |

### 4.2 Trace、评测和维护

```sql
CREATE TABLE trace_index (
  trace_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, root_run_id TEXT, session_key TEXT,
  started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT NOT NULL DEFAULT 'unknown',
  event_count INTEGER NOT NULL DEFAULT 0, tool_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '', summary_hash TEXT, redaction_version TEXT NOT NULL,
  event_cursor TEXT, payload_expire_at TEXT, trace_expire_at TEXT,
  source_path TEXT NOT NULL, index_status TEXT NOT NULL DEFAULT 'indexed',
  last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX trace_session_time ON trace_index(session_key,started_at DESC);
CREATE INDEX trace_outcome_time ON trace_index(outcome,started_at DESC);
CREATE INDEX trace_expiry ON trace_index(trace_expire_at,payload_expire_at);

CREATE TABLE eval_packs (
  eval_pack_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  skill_revision_id TEXT NOT NULL REFERENCES skill_revisions(revision_id), dataset_hash TEXT NOT NULL,
  fixture_hash TEXT NOT NULL, rubric_json TEXT NOT NULL, split_policy_json TEXT NOT NULL,
  source_manifest_json TEXT NOT NULL, question_count INTEGER NOT NULL, valid_count INTEGER NOT NULL,
  evidence_grade TEXT NOT NULL CHECK(evidence_grade IN ('standard','limited','insufficient_evidence')),
  status TEXT NOT NULL DEFAULT 'draft', sealed_by TEXT, sealed_at TEXT, created_at TEXT NOT NULL,
  UNIQUE(skill_revision_id,dataset_hash)
);
CREATE INDEX eval_pack_skill ON eval_packs(skill_id,status,created_at DESC);

CREATE TABLE eval_runs (
  eval_run_id TEXT PRIMARY KEY, eval_pack_id TEXT NOT NULL REFERENCES eval_packs(eval_pack_id),
  baseline_revision_id TEXT NOT NULL REFERENCES skill_revisions(revision_id),
  candidate_revision_id TEXT REFERENCES skill_revisions(revision_id), baseline_hash TEXT NOT NULL,
  candidate_hash TEXT, model_id TEXT NOT NULL, tool_schema_digest TEXT NOT NULL,
  fixture_hash TEXT NOT NULL, dataset_hash TEXT NOT NULL, seed TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running', gate_result TEXT, metrics_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL, ended_at TEXT, error_message TEXT
);
CREATE INDEX eval_run_pack ON eval_runs(eval_pack_id,started_at DESC);

CREATE TABLE eval_case_results (
  eval_run_id TEXT NOT NULL REFERENCES eval_runs(eval_run_id), case_key TEXT NOT NULL,
  split TEXT NOT NULL CHECK(split IN ('train','validation','holdout')),
  outcome TEXT NOT NULL, score REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms REAL NOT NULL DEFAULT 0, tools_json TEXT NOT NULL DEFAULT '[]',
  high_risk_tool_count INTEGER NOT NULL DEFAULT 0, security_violation INTEGER NOT NULL DEFAULT 0,
  judge_actor TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '', response_digest TEXT,
  PRIMARY KEY(eval_run_id,case_key)
);
CREATE INDEX eval_results_split ON eval_case_results(eval_run_id,split,outcome);

CREATE TABLE maintenance_jobs (
  job_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, session_key TEXT NOT NULL,
  activity_epoch INTEGER NOT NULL, last_activity_at TEXT NOT NULL, last_message_cursor TEXT NOT NULL,
  last_review_cursor TEXT NOT NULL, snapshot_cursor TEXT NOT NULL, due_at TEXT NOT NULL,
  lease_until TEXT, status TEXT NOT NULL DEFAULT 'scheduled', retry_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT, worker_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(workspace,session_key)
);
CREATE INDEX maintenance_due ON maintenance_jobs(status,due_at);

CREATE TABLE retrieval_events (
  retrieval_id TEXT PRIMARY KEY, trace_id TEXT, turn_id TEXT, session_key TEXT NOT NULL,
  intent TEXT NOT NULL, scopes_json TEXT NOT NULL, query_digest TEXT NOT NULL,
  result_ids_json TEXT NOT NULL, injected_ids_json TEXT NOT NULL DEFAULT '[]',
  injected_context_digest TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL
);
CREATE INDEX retrieval_session_time ON retrieval_events(session_key,created_at DESC);
```

`maintenance_jobs` 只有 worker 可更新 lease/status；新消息由 AgentLoop 调用 `upsert_activity`，不能创建第二行。`eval_case_results` 不允许候选 Agent 写入，只能由评测 runner 和独立 judge 写入。

### 4.3 Tombstone 与全文索引

```sql
CREATE TABLE tombstones (
  tombstone_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
  content_hash TEXT NOT NULL, reason_code TEXT NOT NULL, requested_by TEXT NOT NULL,
  deleted_at TEXT NOT NULL, expires_at TEXT, source_revision_id TEXT,
  UNIQUE(object_type,object_id)
);
CREATE INDEX tombstone_hash ON tombstones(object_type,content_hash);

CREATE VIRTUAL TABLE memory_fts USING fts5(
  object_type UNINDEXED, object_id UNINDEXED, title, summary, body, tags,
  tokenize='unicode61 remove_diacritics 2'
);
```

FTS 行是派生物，可整体删除再从事实源重建。查询必须先排除 `tombstones`，再过滤 `status='active'`；不能仅依赖 FTS 删除，因为旧快照/失败重试可能重新写回。

### 4.4 迁移策略

迁移表 `schema_meta(version, applied_at, app_build)`；每个迁移是事务内的幂等 SQL，启动时只执行 `version > current`。大表重建使用新表、校验行数/hash、事务 rename；失败保留旧表。SQLite 备份使用在线 backup API，任何 downgrade 都通过新数据库重建，不执行破坏性 `DROP`。

## 5. 事实源、revision 与索引一致性

### 5.1 事实源归属

| 数据 | 事实源 | SQLite 内容 |
|---|---|---|
| 会话/历史 | `sessions/*.jsonl`、`memory/history.jsonl` | 不复制全文；只存 cursor、digest、派生摘要 |
| SOUL/USER/MEMORY | Markdown 文件 | revision 元数据、FTS 摘要和状态 |
| Wiki/Case | Wiki 插件维护的 Markdown 页面 | page 元数据、FTS、关系和 Case 结构化索引 |
| Trace | Audit events/payload JSONL、catalog/index | trace 元数据、摘要、事件和工具计数；payload 不复制 |
| Skill | `skills/<name>/SKILL.md` 或 entry point/MCP 版本包 | hash、版本、current 指针、引用和评测关系 |
| 任务/评测/检索事件 | SQLite | SQLite 唯一事实源，按表结构迁移和备份 |

### 5.2 写入顺序和并发

1. 先写不可变事实源（Markdown/JSONL/Skill 新版本目录），fsync 后计算 `content_hash`。
2. 在 SQLite 单事务中插入 revision、更新对象元数据和 current 指针；FTS 更新可放入 outbox。
3. outbox/`maintenance_jobs` 重试索引更新，按 `object_id + content_hash` 幂等；旧 hash 永远不能覆盖新 hash。
4. 读取 current 时同时检查 `content_hash` 和 tombstone；发现不一致返回旧索引降级或触发重建，不把脏索引注入 prompt。
5. 后台采用 compare-and-swap：`UPDATE ... WHERE current_revision_id = expected_revision_id`；影响行数为 0 表示用户已有更新，任务改为 `superseded`，不得覆盖。

## 6. ContextBuilder 集成设计

### 6.1 插入点

当前 `ContextBuilder.build_system_sections()` 已将 stable instruction 与 dynamic facts 分离。扩展为：

```text
InboundMessage
  -> IntentRouter.classify(message, session metadata)
  -> MemoryRetriever.retrieve(scope, query, budget)
  -> build_system_sections(..., memory_overlay=overlay)
  -> ContextGovernor.finalize(messages, tools)
```

`AgentLoop` 负责把 `session_key`、`trace_id`、channel 和当前任务元数据传给 ContextBuilder；`AgentRunner` 不直接访问 SQLite/Wiki，避免 runner 的工具循环改变 prompt 事实。

### 6.2 intent 与 scope

路由器输出结构：

```json
{"intent":"task|history_query|memory_write|forget|skill_discovery|trace_query|review",
 "scopes":["session","memory","wiki","case","skill","trace"],
 "explicit_history":false,"explicit_mutation":false,"confidence":0.0}
```

规则优先于模型分类：`forget/delete/记住/上次 trace/历史` 等显式词强制进入对应 intent；不确定时只使用 session+低风险 memory。普通检索失败只记录 `retrieval_events` 并继续主任务；`history_query`、`memory_write`、`forget` 失败必须返回结构化错误并让 Agent 明示失败。

### 6.3 检索和注入

默认注入：当前 session 摘要、active 高置信 `memory_records`、匹配的 Wiki/Case/Skill 摘要；Trace 只注入摘要和 trace_id，不注入完整 payload。完整页面、原始 trace、案例步骤只能通过工具分页读取。

预算执行顺序：

* 软上限 2,000 tokens，硬上限 3,000 tokens；在模型窗口剩余少于 6% 时按比例缩小。
* 先保留 session/用户明确要求内容，再按 `effective_score`、新鲜度、来源权威和冲突惩罚排序。
* 每个结果带 `[memory_id|page_id|revision_id]` 引用；超限只保留 summary，不截断 JSON 中间结构。
* `ContextGovernor` 最终裁剪，若裁剪掉引用则同时删除其引用标记。

每次检索写一条 `retrieval_events`，`injected_context_digest=sha256(canonical(ids,revision_ids,overlay))`。此 digest 进入 dynamic section metadata，不进入 stable system prompt；因此记忆变化不会污染 provider 的稳定 prompt cache。工具 schema digest 继续使用现有 `tool_schema_digest`。

## 7. Skill/Tool 发现、协议和权限

### 7.1 与现有机制的关系

`SkillsLoader` 继续扫描 builtin/workspace Skill 并解析 frontmatter；`ToolRegistry` 继续负责 schema 排序、参数校验和结构化错误。新增 `CapabilityCatalog` 合并三类来源：loader、本地 entry point、Wiki MCP。发现失败不阻塞普通任务，目录条目标记 `availability='degraded'`。

目录条目最小结构：`name, description, source_kind, version, content_hash, required_tools, risk_level, read_only, examples, policy_ref, current_revision_id`。初始 system prompt 只放名称、用途、风险和 `skill_catalog_search`；`always` Skill 才加载全文。

### 7.2 三个工具

`skill_catalog_search`：输入 `{query, scopes?, limit<=10}`；输出 `{items:[{skill_id,name,summary,version,risk,source}], next_cursor}`。只读，失败码 `catalog_unavailable`。

`skill_read`：输入 `{skill_id, revision_id?, section?, max_tokens<=2000}`；输出 `{skill_id,revision_id,content,content_hash,refs}`。只读，校验 current/tombstone；错误 `not_found|stale_revision|redacted`。

`skill_propose`：输入 `{skill_id?, title, intent, evidence_refs, proposed_content, change_summary}`；输出 `{proposal_id,status:'candidate',required_confirmation,validation_errors}`。只能创建 staging revision/candidate，不得写 current、Git、外部服务；错误 `policy_denied|evidence_missing|secret_detected|invalid_schema`。

所有工具输出使用统一 envelope：`{ok, data, error:{code,message,retryable,details}}`；禁止把异常 traceback 或秘密放入 message。

### 7.3 ToolPolicy 与角色

* 普通 Agent：可读 catalog/page/trace 摘要；可提出低风险 memory candidate；禁止发布、删除、外部写入、Git。
* 维护 Agent：可读 Audit、生成摘要/Case/EvalPack draft、更新 SQLite 状态；只允许白名单只读 workspace 工具和 `skill_propose`；不能批准自己的候选。
* 评测 Agent：仅访问封存 EvalPack 的指定 split 和临时 fixture；不能访问 holdout 答案、candidate 生成日志或写正式资产。
* 审核 Agent/用户确认：可独立审核 EvalPack、逐题评分、确认 adopt/PR；发布动作仍由后台 service 执行。

ToolPolicy 在 ToolRegistry 调用前检查角色、risk、workspace 路径、网络/Git 开关、确认 token 和动作指纹；schema description 必须包含“能做什么/不能做什么/失败时如何恢复”。few-shot 示例放在 Skill 的 `examples` 元数据和评测题中，不直接注入所有 prompt。

## 8. 45 分钟维护任务状态机

### 8.1 状态和字段

一行以 `(workspace,session_key)` 唯一。`activity_epoch` 每次新消息递增；`last_message_cursor` 指向 Session/Memory cursor；`last_review_cursor` 是已处理边界；`snapshot_cursor` 是开始 review 时冻结的上界；`due_at=last_activity_at+45min`；`lease_until` 是 worker 租约；`retry_count` 指数退避；`error_message` 仅存脱敏摘要。

状态：`scheduled → due → leased → snapshotting → reviewing → succeeded`；失败为 `retry_wait`，超过 5 次为 `failed`；新消息在 `leased` 前将旧任务标记 `superseded` 并回到 `scheduled`，在 `reviewing` 中只让当前批次完成，下一 epoch 新建 review。

### 8.2 事件和幂等

每条新消息执行：

```sql
INSERT ... ON CONFLICT(workspace,session_key) DO UPDATE SET
 activity_epoch=activity_epoch+1,last_activity_at=:now,last_message_cursor=:cursor,
 due_at=datetime(:now,'+45 minutes'),status='scheduled',updated_at=:now;
```

不按消息创建任务；worker 只 `BEGIN IMMEDIATE` 抢占 `status IN ('due','retry_wait') AND due_at<=now` 且 `lease_until` 为空/已过期的行。workspace 锁使用 SQLite `maintenance_lock(workspace PRIMARY KEY, owner, lease_until)` 或同一数据库的 `BEGIN IMMEDIATE`；单 worker 不允许并行处理同一 workspace。

Gateway 重启后扫描 `scheduled/due/retry_wait`；过期 lease 回收为 `retry_wait`。抢到 snapshot 后记录 `snapshot_cursor`，只处理 `last_review_cursor..snapshot_cursor`；review 成功 CAS 更新 `last_review_cursor`，若 `activity_epoch` 未变化才置 `succeeded`，否则重新计算 due_at。失败按 1/2/4/8/16 分钟退避，保留错误摘要和审计事件。

## 9. Trace → Case → Skill

1. **采集/脱敏**：Audit emitter 现有 redactor 先清理事件和 payload；派生器只读取允许的 trace view。秘密、完整 token、个人隐私和未授权文件路径在进入 Case 前再次扫描。
2. **判定结果**：以工具结果、测试/检查事件、用户确认、恢复事件和最终状态组合判定 `success|failure|mixed|anti_pattern|unknown`；未知不能晋升。
3. **Case candidate**：生成 `intent/task_signature/preconditions/steps/outcome/failure_patterns/trace_ids`，写 Wiki `page_type=case` 新 revision，同时插入 `cases.status='candidate'`。
4. **去重/合并**：规范化 intent、工具序列、输入签名和 outcome；相同 `content_hash` 直接合并 source refs；相似案例只产生 `supersedes`/合并建议，不覆盖原页面。
5. **Skill candidate**：至少两个独立、脱敏、可验证 Case，跨时间或输入变体，且没有安全违规；维护 Agent 生成 `skill_revisions.status='staging'`，绑定 case ids 和 baseline hash。
6. **EvalPack draft**：从 Skill、Case、失败回归 Trace 和 fixture 自动生成题目/rubric；独立审核 Agent 做 schema、去重、可执行性、秘密检查后封存。
7. **评测/准入**：只读代码审查 Skill 在临时 fixture workspace 回放，禁网、禁写、禁 Git；运行 baseline/candidate，逐题结果入 `eval_case_results`。
8. **采用**：共享 Skill 生成中文 Git PR（含 diff、EvalRun、风险和回滚）；个人 Skill 经用户确认后 workspace adopt，新目录/新版本和 current 指针并存，永不覆盖旧文件。
9. **回滚**：确认旧 `revision_id` 非 tombstoned 且 hash 可读，CAS 切换 `skills.current_revision_id`；写 `skill.rollback` Audit 事件并使依赖该 candidate 的 baseline 标记 stale。

## 10. EvalPack 与 EvalRun

### 10.1 EvalPack JSON 结构

```json
{
  "eval_pack_id":"eval_repo_review_v3",
  "skill_id":"repo-code-review",
  "skill_revision_id":"skillrev_...",
  "dataset_hash":"sha256:...",
  "fixture_hash":"sha256:...",
  "baseline":{"revision_id":"skillrev_old","content_hash":"sha256:...","model_id":"...","tool_schema_digest":"..."},
  "sources":[{"case_id":"case_1","kind":"real_trace","trace_id":"trace_1"},{"case_id":"q2","kind":"failure_regression"}],
  "cases":[{"case_key":"q1","prompt":"...","fixture_ref":"fixture://repo-a","split":"train","origin":"real","rubric":{"must_include":["..."],"forbidden_tools":["write_file"]}}],
  "split":{"seed":"stable:skill_id+dataset_hash","train":10,"validation":5,"holdout":5},
  "evidence_grade":"standard","sealed_at":"..."
}
```

题目来源区分 `synthetic`、`real_trace`、`failure_regression`、`fixture`；synthetic 只能进入 train 或候选草案，不能单独证明正式发布。20 道及以上按 10/5/5；10～19 道 holdout≥3、validation≥2，标记 `limited`；少于 10 道有效题标记 `insufficient_evidence`，不得正式发布。split 使用 `sha256(eval_pack_id|case_key|seed)` 稳定排序，封存后不可重切分。

### 10.2 回放隔离和评分

评测 runner 为每题创建只读临时 fixture，环境变量关闭网络、写入和 Git；candidate 只能拿到当前 split 的 prompt、fixture 和工具白名单，不得读取 holdout 答案、其他 split、生成日志或 judge 输出。baseline 和 candidate 使用相同模型配置、工具 schema、fixture 和超时。

每题记录：成功/失败、rubric 分、token、P50/P95 延迟、工具调用列表、高风险工具数、安全违规、恢复次数和 judge rationale。judge 必须是独立 actor；题目生成 Agent、candidate Agent、评分 Agent、发布 Agent 的 actor_id 不得相同。

发布 gate：holdout 成功率不低于 baseline；新增高风险工具数为 0；安全违规为 0；每题总 token 增幅≤15%；P50 延迟增幅≤20%。通过最低证据和 gate 才能 `eligible_for_confirmation`；效果提升但成本/延迟超标只能 `staging`。任何 hash（Skill、tool schema、system prompt、model、fixture、EvalPack）变化都将 baseline 标记 `stale_baseline`，必须重跑。

## 11. 版本、合并与回滚

* `revision_id`：一次不可变正文快照的身份。
* `content_hash`：正文规范化后的完整性校验，重复内容去重。
* `skill_version`：人可读语义版本；候选用 `candidate-<n>`，发布后才分配正式版本。
* `current_revision`：对象当前生效 revision 指针，可 CAS 切换。
* `previous_revision_id`：线性历史；`supersedes` 表示语义替代/合并关系，不删除被替代内容。
* Wiki/Case 合并：新页面 revision 记录两个来源，旧页置 `archived`，关系图保留 `merged_into`。
* Skill references：记录 Case、EvalPack、工具 schema digest 和模型约束；引用失效时 Skill 不自动发布。

Git PR 适用于 builtin/shared Skill：新分支、新目录或版本文件、中文 PR 说明、EvalRun 和回滚 revision。workspace adopt 适用于个人实验：复制候选到新版本目录，更新本地 current 指针，保留备份和评测证据；两者都不覆盖旧版本，也不自动合并 main。

## 12. 遗忘、降权、归档、tombstone

建议评分：

```text
freshness = exp(-ln(2) * age_days / half_life_days)
effective_score = confidence * (0.45 + 0.25*authority + 0.20*salience)
                  * freshness * (1 + min(log1p(access_count), 2)*0.05)
                  - conflict_penalty - stale_penalty
```

`stale`：超过类型 half-life 且未访问/确认；`archived`：stale 且低于检索阈值或被新 revision supersede。active 查询只取 `status='active'`；archive 仅在显式历史查询或无 active 结果时取摘要。普通记忆不硬删除，优先合并、修正、降权、归档。

安全/合规删除创建无正文 tombstone，记录 object/hash/reason/requester/time；所有重建、同步和查询先检查 tombstone，防止旧 Markdown/JSONL 索引复活。Trace 保留由 retention worker 执行：原始事件 18 天，含完整明文 payload 7 天；到期先删除/截断 payload 并保留脱敏摘要和 hash，再删除事件段索引。归档数据压缩为摘要+revision refs，SQLite `VACUUM` 只在维护窗口、备份成功且无活跃 worker 时执行。

## 13. 迁移、失败恢复和可观测性

每个写入动作先写 Audit（candidate/create、revision/commit、retrieval、forget、publish、rollback），再写 SQLite；若 SQLite 失败，事实源仍保留，outbox 重试。若事实源写入成功但索引失败，读取侧返回 `index_degraded`，普通任务继续；显式历史/修改返回错误并给出 retryable code。所有后台 job 具备最大重试次数、lease 恢复、幂等 key 和脱敏 error。

必须提供 CLI/内部 API：`memory schema`, `memory rebuild --from-facts`, `memory verify`, `memory jobs`, `memory tombstone`, `memory eval show/run`, `memory skill rollback`。`rebuild` 先在临时数据库生成并校验 count/hash，再原子替换；不能使用 `docker compose down -v` 或清理用户 workspace。

## 14. 分阶段实施与验收

### Phase 0：协议和空 schema

新增枚举、迁移、SQLite 连接、schema verify；默认不改变 prompt。验收：新库/升级库可重复迁移，外键、WAL、备份和重建通过。

### Phase 1：Trace 索引和检索事件

从 Audit catalog/JSONL 构建 `trace_index`，增加 redacted summary 和 `retrieval_events`；验收：重建结果一致、payload TTL 可验证、Trace 查询不是 404。

### Phase 2：Wiki/Case 插件

定义 entry point/MCP adapter；实现 Markdown revision、FTS、关系和 Case candidate。验收：插件缺失不阻塞普通任务；页面写入先 revision 后索引；tombstone 可穿透所有查询。

### Phase 3：ContextBuilder scope

接入 intent/router/retriever、2k/3k 预算、digest 和降级。验收：同一 stable prompt 下 dynamic digest 随记忆变化；普通检索失败继续回答，显式历史失败可见。

### Phase 4：维护 worker 和 ToolPolicy

实现 45 分钟状态机、CAS、lease、workspace 单 worker、三个 skill 工具和角色白名单。验收：连续消息只一行任务；重启可恢复；旧任务不会覆盖新 epoch；高风险工具被拒绝。

### Phase 5：Case→Skill、EvalPack/EvalRun

实现候选、封存、fixture 回放、独立 judge、gate 和 staging。验收：10/19/20 题边界正确；holdout 不可读；自评自批被拒；stale baseline 被强制重跑。

### Phase 6：受控运行

夜间/空闲只处理低风险候选，持续记录 evidence；失败 Trace 自动生成回归题；只生成 PR/adopt proposal，不自动合并。验收：连续运行无记忆膨胀、越权或漂移，回归可停机并切回旧 revision。

## 15. 测试清单

* 单元：迁移幂等、hash/CAS、FTS 查询、评分/半衰期、split 稳定性、JSON schema、tombstone 过滤。
* 集成：ContextBuilder token 预算、stable/dynamic cache digest、普通/显式失败降级、ToolPolicy 角色矩阵。
* 并发：两个 worker 抢同一 job、Gateway 重启、lease 超时、新消息覆盖旧 epoch、索引失败重试。
* 安全：secret redaction、路径越界、网络/Git/写入拒绝、holdout 泄露、自评自批、删除后旧索引复活。
* 真实场景：使用长期 Gateway 的新 WebUI 会话验证 memory retrieval/review trace；记录构建标识、trace URL、提示词、预期/实际结果，不把旧容器页面当作证据。

## 16. 不实施或延期项

首版不引入独立向量数据库、多租户字段、`user_id/tenant_id/RBAC`、自动 Git merge、自动正式 Skill 发布、全量 Trace prompt 注入、独立 Wiki 核心实现和跨项目统一 EvalPack。只有 SQLite FTS/标签/关系图在实际召回指标上不足，或外部 Wiki 明确提供稳定 entry point/MCP 契约后，才重新评估延期项。

## 17. 外部项目借鉴边界

`nanobot-llm-wiki` 提供本地 Markdown、SQLite/FTS、MCP/CLI/UI 的插件参考，nanobot 只依赖协议，不复制其存储实现。Hermes self-evolution 提供 dataset→replay→judge→PR 的离线优化范式和测试/大小/cache/人工 PR guardrail。SkillOpt-Sleep 提供 harvest/mine/replay/stage/adopt、证据日志、独立 split 和多 Skill gate 的参考；其跨平台 transcript adapter、具体 GEPA/DSPy 依赖和自动调度器不进入 nanobot 核心。

## 18. 实施完成判定

只有同时满足以下条件，才可把五层记忆声明为可用：schema 可重建、事实源和索引边界清晰、ContextBuilder 有预算和降级、45 分钟任务可恢复、Trace→Case→Skill 有独立审核和版本回滚、EvalPack 有不可读 holdout、tombstone 可阻断复活、首个只读代码审查 Skill 在隔离 fixture 上通过 gate，并且所有正式发布仍等待用户确认。
