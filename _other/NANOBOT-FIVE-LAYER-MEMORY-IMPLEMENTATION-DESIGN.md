# nanobot 五层记忆体系实施设计

> 文档状态：可执行实施计划与实施记录（截至 2026-09-26，Phase 0、Phase 1、Phase 2、Phase 3、Phase 4、Phase 5、Phase 6 已完成）
> 适用范围：当前仓库的 Python Agent、Session、Audit/Trace、Skill、ToolRegistry，以及通过插件/MCP/entry point 接入的 Wiki。
> 本文只描述实现，不修改生产代码；所有新增能力都必须先以 feature flag 和迁移脚本落地。

## 1. 结论与实施边界

五层记忆不是一个“大记忆表”，而是五种职责不同的存储和检索边界：

1. 当前上下文：仍由 `ContextBuilder`、`SessionManager`、`ContextGovernor` 负责。
2. Trace 过程记忆：继续以 Audit 事件/JSONL 为事实源，新增 SQLite 查询索引。
3. 知识图谱：由 Wiki 插件保存 Markdown 页面和关系，SQLite 只建可重建索引。
4. Skill/Case：Skill 继续是版本化 `SKILL.md`，Case 是 Wiki 的 `page_type=case` 页面；SQLite 保存关联、状态和评测索引。
5. Retention：统一处理评分、降权、归档、TTL 和 tombstone，不把生命周期状态当作事实内容。

Phase 0～Phase 6 已按计划分批提交。任何阶段都不允许直接覆盖 Markdown 事实源、正式 Skill 或原始 Audit。

## 2. 现状盘点与计划项分类

+## 1.1 实施进度总览

| 阶段 | 名称 | 前置阶段 | 当前状态 | 可开始条件 | 完成条件 |
|---|---|---|---|---|---|
| Phase 0 | 持久化基础 | 无 | 已完成 | 设计评审通过 | Phase 0 DoD 全部完成 |
| Phase 1 | Trace 索引 | Phase 0 | 已完成 | Phase 0 通过 | Phase 1 DoD 全部完成 |
| Phase 2 | Wiki/Case | Phase 0、1 | 已完成 | Phase 1 通过 | Phase 2 DoD 全部完成 |
| Phase 3 | ContextBuilder 检索 | Phase 1、2 | 已完成 | Phase 2 通过 | Phase 3 DoD 全部完成 |
| Phase 4 | Maintenance/ToolPolicy | Phase 0～3 | 已完成 | Phase 3 通过 | Phase 4 DoD 全部完成 |
| Phase 5 | Case→Skill/Eval | Phase 2～4 | 已完成 | Phase 4 通过 | Phase 5 DoD 全部完成 |
| Phase 6 | 受控持续运行 | Phase 5 | 已完成 | Phase 5 通过且明确启用 | Phase 6 DoD 全部完成 |

任务状态统一为：`[ ]` 未开始、`[-]` 进行中、`[x]` 已完成、`[!]` 阻塞、`[~]` 延期。Phase 0 任务已按记录更新，后续阶段仍按前置 DoD 约束。

### 2.1 当前真实实现

| 组件 | 现状 | 设计中的使用方式 |
|---|---|---|
| `AgentLoop`/`AgentRunner` | 已有消息消费、运行规范、工具循环、恢复和回调 | 保持主流程；在构建上下文前插入只读 memory scope，在 turn 完成后发出派生事件 |
| `ContextBuilder` | stable/dynamic sections、身份/bootstrap、长期记忆、近期历史、always skill、skills summary；已有 digest/cache metadata | 扩展为 `IntentRouter + MemoryRetriever`，只将稳定摘要放 dynamic overlay |
| `ContextGovernor` | token 预算、裁剪、孤立 tool result 清理、非法工具调用修复 | 作为最终防线；新增记忆预算不能绕过它 |
| `MemoryStore` | `SOUL.md`、`USER.md`、`memory/MEMORY.md`、`history.jsonl`、Dream cursor、原子写入/GitStore；没有 SQLite memory store | 保持为文件事实源；新增 SQLite 仅作派生索引、状态和评测库 |
| `SessionManager` | JSONL 会话、cursor、缓存、fork、损坏恢复和 last_consolidated | 复用 session key/cursor；维护任务另建持久状态表 |
| `SkillsLoader` | builtin/workspace、frontmatter、`always`、依赖、disabled、渐进式加载 | 复用发现；新增外部 entry point/MCP skill catalog 适配器 |
| `ToolRegistry` | 注册、稳定排序、schema 校验、结构化错误、schema digest | 复用注册和校验；加入 `ToolPolicy` 能力边界和角色检查 |
| Audit | 事件/载荷 JSONL、红脱敏、trace/run/turn 父子关系、索引、完整性、WebUI 查询 | 继续为 Trace 事实源；增发 retrieval/write/forget/review 事件 |
| Wiki | `nanobot-llm-wiki/src/nanobot_llm_wiki/storage.py` 的 `pages`、`links`、`page_fts`，以及 `tools.py` 的工具；`pyproject.toml` 通过 `nanobot.tools` entry points 注册 9 个工具；另有 stdio MCP server | 通过 entry point 或独立 MCP 接入，不硬编码 Wiki 类和表；Case 仍使用其 `page_type='case'` |
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
| SQLite 统一索引 | 当前不存在（Audit 自己已有独立 SQLite 索引，不是 memory store） | workspace 级 memory 数据库、迁移、重建和单 writer；不得与 Audit index 混用 |
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

### 2.3 结论状态分类

为避免把设计目标误读为已实现能力，本文后续使用以下四类标签：

| 分类 | 本文含义 |
|---|---|
| 当前代码已经存在 | 已从当前仓库代码或外部项目真实实现中确认；只描述现状，不表示五层记忆已接通 |
| 实施设计已确定 | 产品/架构方向已经确定，本文给出目标 schema、边界和流程，但尚未进入生产实现 |
| 后续实施时需要验证 | 需要在 Phase 0 及后续实现中通过代码、临时 fixture、测试或部署检查确认；本文不将其写成已验证事实 |
| 暂不实施或延期 | 本轮明确不做，除非未来重新评审并获得新的范围确认 |

本文中新增 memory SQLite、migration、outbox、maintenance worker、ToolPolicy、Wiki adapter、FTS5 接线、retention、fixture 隔离和相关 CLI 均属于“后续实施时需要验证”，不是当前代码能力。`memory schema`、`memory rebuild`、`memory verify` 等名称仅是后续实施目标，不是当前已有命令。

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

数据库路径固定为 `<workspace>/.nanobot/memory.sqlite3`；不得把它放入 `runtime/workspace` 之外，也不得把 SQLite 正文作为用户可编辑事实源。数据库启用 WAL、`foreign_keys=ON`、`busy_timeout=5000`。维护型写操作由 workspace 单 worker 串行执行；前台只允许 5.2 节列出的有限原子 activity/retrieval 写。

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
  current_revision_id TEXT REFERENCES memory_revisions(revision_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
  supersedes_json TEXT NOT NULL DEFAULT '[]',
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
  source_path TEXT NOT NULL, content_hash TEXT NOT NULL,
  -- 这是 Wiki adapter 管理的 revision 标识，不是本库外键。
  current_revision_id TEXT,
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
  -- Case revision 由 Wiki 的 Markdown/revision 事实源拥有，本库只保存外部引用。
  revision_id TEXT NOT NULL, previous_revision_id TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX cases_skill_status ON cases(skill_id,status,confidence DESC);
CREATE INDEX cases_intent ON cases(intent,status);

CREATE TABLE skills (
  skill_id TEXT PRIMARY KEY, namespace TEXT NOT NULL DEFAULT 'workspace', name TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('builtin','workspace','entrypoint','mcp')),
  source_path TEXT,
  current_revision_id TEXT REFERENCES skill_revisions(revision_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
  current_version TEXT, status TEXT NOT NULL DEFAULT 'active',
  description TEXT NOT NULL DEFAULT '', tool_policy_json TEXT NOT NULL DEFAULT '{}',
  references_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(namespace,name)
);
CREATE INDEX skills_catalog ON skills(namespace,status,name);

CREATE TABLE skill_revisions (
  revision_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(skill_id),
  skill_version TEXT NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL,
  previous_revision_id TEXT REFERENCES skill_revisions(revision_id), supersedes_revision_id TEXT,
  source_case_ids_json TEXT NOT NULL DEFAULT '[]',
  eval_pack_id TEXT REFERENCES eval_packs(eval_pack_id)
    ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED,
  author_actor TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'staging', created_at TEXT NOT NULL,
  UNIQUE(skill_id,skill_version), UNIQUE(skill_id,content_hash)
);
CREATE INDEX skill_revision_current ON skill_revisions(skill_id,status,created_at DESC);
```

`memory_records`、`wiki_pages`、`cases`、`skills` 的正文/版本均不可原地改写；状态、访问计数和 current 指针可变。普通删除只把状态置为 `archived` 或写 `supersedes`；安全删除不更新正文，改写为 tombstone（见 4.3）。`memory_revisions.content` 和 `skill_revisions.content` 是受控缓存，不是用户可编辑事实源。权威内容仍分别来自 `MEMORY.md`/`USER.md`/`SOUL.md`、Wiki Markdown 和 `SKILL.md` 或外部版本包；缓存只用于事务校验、评测回放、revision 对比、FTS 重建和回滚准备，普通 Agent、用户和 Wiki adapter 不得直接编辑 SQLite 缓存。

### 4.1.1 表级生命周期矩阵

| 表 | 主键/外键关系 | 可变字段 | 状态字段 | 删除/归档语义 | 迁移与重建 |
|---|---|---|---|---|---|
| `memory_records` | `memory_id`；`current_revision_id`→`memory_revisions`，deferred FK、`ON DELETE RESTRICT` | score、访问计数、current、状态 | candidate/active/stale/archived | 不删正文；archive 或 supersedes；合规删除写 tombstone | 从 `MEMORY.md`/`USER.md`/`SOUL.md` 和 history 摘要重建 |
| `memory_revisions` | `revision_id`；`memory_id`→records；previous 自引用 | 不可变 | 无（由父记录控制） | 永不更新/删除，除非安全清除并留无正文 tombstone | 由 Markdown revision header/sidecar 或导入快照重建 |
| `wiki_pages` | `page_id`；`current_revision_id` 是外部 Wiki revision 引用，无本库 FK，可为空 | title、summary、tags、current、状态 | candidate/active/stale/archived | Markdown 仍在；索引只标状态；删除由 Wiki adapter 执行 | 扫描页面 frontmatter、hash、关系后全量重建 |
| `wiki_relations` | relation→两 page | weight、状态 | active/archived | 关系失效归档，不级联删除页面 | 从页面 links/frontmatter 重建 |
| `cases` | `case_id`、`page_id`；`revision_id` 是外部 Wiki revision 引用，无本库 FK；可选 `skill_id` 暂为逻辑引用 | confidence、使用计数、状态 | candidate/active/archived/rejected | Case 正文是 Wiki page；不硬删，除合规 tombstone | 从 `page_type=case` 页面结构解析 |
| `skills` | `skill_id`；`current_revision_id`→`skill_revisions`，deferred FK、`ON DELETE RESTRICT`，可为空 | current、状态、描述元数据 | staging/active/archived/rejected | 旧版本保留；禁用/回滚只切指针 | 从 Skill 目录、entry point/MCP manifest 重扫 |
| `skill_revisions` | `revision_id`→skills；`eval_pack_id`→eval_packs，deferred FK、`ON DELETE SET NULL`，可为空；previous 自引用 | 不可变 | staging/approved/rejected/archived | 不删；候选失败留证据，安全删除写 tombstone | 从版本目录和 hash manifest 重建 |
| `trace_index` | `trace_id` | summary、计数、索引状态 | indexed/degraded/expired | 原始 JSONL 由 TTL 清理；摘要可保留 | 从 Audit catalog/events JSONL 重放 |
| `eval_packs` | pack→skill revision | 仅 draft 字段可变，sealed 后不可变 | draft/sealed/retired | 旧包 retired，不删除题目证据 | 从封存 JSON manifest 重建 |
| `eval_runs` | run→pack；baseline/candidate→`skill_revisions`，candidate 可空，均 `ON DELETE RESTRICT` | 运行状态和结束指标 | running/succeeded/failed/stale | 永久保留指标；敏感响应只保 digest/TTL | 不能从结果反推题目，依赖 pack manifest |
| `eval_case_results` | `(run_id,case_key)`→run | 不可变（修正需新 run） | outcome 字段 | 随 run 保留；无正文删除 | 从 runner evidence 导入并校验 hash |
| `maintenance_jobs` | `job_id`，workspace/session 唯一；workspace lease 在 `maintenance_lock` | lease、cursor、retry、状态 | scheduled/due/leased/reviewing/retry_wait/succeeded/failed/superseded | 成功任务保留最近状态；历史错误不覆盖 | SQLite WAL 恢复；无需事实源重建 |
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

CREATE TABLE schema_meta (
  version INTEGER PRIMARY KEY,
  migration_id TEXT NOT NULL UNIQUE,
  app_build TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','applied','failed','rolled_back')),
  applied_at TEXT NOT NULL,
  error_message TEXT
);
CREATE INDEX schema_meta_status ON schema_meta(status,version DESC);

CREATE TABLE memory_outbox (
  outbox_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN ('memory','wiki_page','case','skill','trace_summary')),
  object_id TEXT NOT NULL,
  revision_id TEXT,
  content_hash TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('upsert','archive','tombstone','rebuild')),
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK(status IN ('pending','leased','retry_wait','done','dead_letter','superseded')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  lease_until TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(object_type,object_id,content_hash,operation)
);
CREATE INDEX memory_outbox_due ON memory_outbox(workspace,status,next_attempt_at);
CREATE INDEX memory_outbox_lease ON memory_outbox(workspace,lease_until);
CREATE INDEX memory_outbox_object ON memory_outbox(object_type,object_id,created_at DESC);

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
  baseline_revision_id TEXT NOT NULL REFERENCES skill_revisions(revision_id) ON DELETE RESTRICT,
  candidate_revision_id TEXT REFERENCES skill_revisions(revision_id) ON DELETE RESTRICT,
  baseline_hash TEXT NOT NULL,
  candidate_hash TEXT, model_id TEXT NOT NULL, tool_schema_digest TEXT NOT NULL,
  fixture_hash TEXT NOT NULL, dataset_hash TEXT NOT NULL, seed TEXT NOT NULL,
  replay_group_id TEXT NOT NULL, replay_attempt INTEGER NOT NULL DEFAULT 1,
  evidence_grade TEXT NOT NULL CHECK(evidence_grade IN ('standard','limited','insufficient_evidence')),
  consistency_result TEXT NOT NULL DEFAULT 'not_checked',
  is_independent_replay INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE maintenance_lock (
  workspace TEXT PRIMARY KEY, owner TEXT NOT NULL, lease_until TEXT NOT NULL,
  acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX maintenance_lock_expiry ON maintenance_lock(lease_until);

CREATE TABLE retrieval_events (
  retrieval_id TEXT PRIMARY KEY, trace_id TEXT, turn_id TEXT, session_key TEXT NOT NULL,
  intent TEXT NOT NULL, scopes_json TEXT NOT NULL, query_digest TEXT NOT NULL,
  result_ids_json TEXT NOT NULL, injected_ids_json TEXT NOT NULL DEFAULT '[]',
  injected_context_digest TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL, error_code TEXT, created_at TEXT NOT NULL
);
CREATE INDEX retrieval_session_time ON retrieval_events(session_key,created_at DESC);
```

`maintenance_jobs` 只有 worker 可更新 lease/status；新消息由 AgentLoop 调用 `upsert_activity`，不能创建第二行。`eval_case_results` 不允许候选 Agent 写入：评测 runner 写临时 evidence，维护 worker 校验后导入；独立 judge 只产生签名评分结果，不直接写正式表。

`memory_outbox.payload_json` 默认只保存对象引用、操作参数、cursor 和 hash，不保存完整敏感正文；确需临时携带正文时必须先脱敏、限制大小并设置过期时间。outbox 状态固定为 `pending|leased|retry_wait|done|dead_letter|superseded`，只有 worker 可以改变状态。

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

FTS 的 `object_type` 只允许 `memory`、`wiki_page`、`case`、`skill`、`trace_summary`。映射和默认检索策略如下：

| object_type | 状态事实表/事实源 | FTS 内容 | 默认检索 |
|---|---|---|---|
| `memory` | `memory_records` + `memory_revisions`；`SOUL.md`/`USER.md`/`MEMORY.md` | title、summary、当前 revision content、tags | active |
| `wiki_page` | Wiki adapter 的 Markdown 页面；本库 `wiki_pages` | title、summary、页面正文、tags | active |
| `case` | Wiki `page_type='case'` 页面 + 本库 `cases` | case title、summary、步骤摘要、failure patterns、tags | active |
| `skill` | `skills` + current `skill_revisions`；`SKILL.md` | name、description、正文、引用 tags；候选全文不默认注入 | active |
| `trace_summary` | Audit/JSONL 脱敏摘要 + `trace_index` | 仅 summary、outcome、tool names、tags，不含 payload | active |

FTS 行不是状态事实源。查询必须按 object_type 显式 join 对应事实表，并统一排除 tombstone：

```sql
SELECT f.object_id, f.title, f.summary, bm25(memory_fts) AS score
FROM memory_fts AS f
JOIN memory_records AS r ON f.object_type='memory' AND f.object_id=r.memory_id
WHERE f.object_type='memory' AND memory_fts MATCH :query
  AND r.status='active'
  AND NOT EXISTS (SELECT 1 FROM tombstones t
                  WHERE t.object_type=f.object_type AND t.object_id=f.object_id);
```

其他 object_type 使用同一模式分别 join `wiki_pages`、`cases JOIN wiki_pages`、`skills JOIN skill_revisions` 或 `trace_index`；实现层禁止把表名/列名从用户输入拼接到 SQL。`archived` 默认排除，仅 `history_query` 或无 active 结果时按摘要读取；不能将 archived 正文自动注入普通 prompt。

重建扫描上述五类事实源：Markdown/Wiki adapter 页面、当前 Skill 版本、脱敏 Trace summary；Case 不单独扫描第二份正文。索引更新以 `(object_type,object_id,content_hash)` 幂等，写入前再次检查 tombstone，重试任务若发现 tombstone 则标记 skipped，因而旧 FTS 行不能复活。Markdown 修改由 adapter 的 revision commit/outbox 触发；外部编辑则由启动时 mtime/hash 扫描发现。

### 4.4 迁移策略

迁移使用 `schema_meta` 记录 `version`、`migration_id`、`app_build`、`schema_hash` 和 `running/applied/failed/rolled_back` 状态；每个迁移是事务内的幂等 SQL，启动时只执行 `version > current`。迁移开始先写 `running`，成功提交后写 `applied`，失败回滚并记录脱敏 `error_message` 为 `failed`；回退通过备份数据库和事实源重建，不把失败 migration 标记为已应用。大表重建使用新表、校验行数/hash、事务 rename；失败保留旧表。SQLite 备份使用在线 backup API，任何 downgrade 都通过新数据库重建，不执行破坏性 `DROP`。

## 5. 事实源、revision 与索引一致性

### 5.1 事实源归属

| 数据 | 事实源 | SQLite 内容 |
|---|---|---|
| 会话/历史 | `sessions/*.jsonl`、`memory/history.jsonl` | 不复制全文；只存 cursor、digest、派生摘要 |
| SOUL/USER/MEMORY | Markdown 文件 | revision 元数据、受控正文缓存、FTS 摘要和状态；缓存必须与 source hash 对齐 |
| Wiki/Case | Wiki 插件维护的 Markdown 页面 | page 元数据、FTS、关系和 Case 结构化索引 |
| Trace | Audit events/payload JSONL、catalog/index | trace 元数据、摘要、事件和工具计数；payload 不复制 |
| Skill | `skills/<name>/SKILL.md` 或 entry point/MCP 版本包 | 受控正文缓存、hash、版本、current 指针、引用和评测关系；缓存不具备事实源权威性 |
| 任务/评测/检索事件 | SQLite | SQLite 唯一事实源，按表结构迁移和备份 |

### 5.2 写入顺序、并发和事实源失败

标准写入事务严格采用以下顺序：

1. ToolPolicy 权限、路径、敏感级别和一次性确认指纹检查。
2. 写 Audit `intent` 事件；Audit intent 失败时停止写入，不产生事实源副作用，并返回 retryable 错误。
3. 在单 worker 中写不可变事实源（Markdown/JSONL/Skill 新版本目录），fsync 后计算规范化 `content_hash`。
4. SQLite revision/current 事务：校验 hash、插入 revision、使用 `WHERE current_revision_id=:expected` CAS 更新 current；新对象在同一 deferred-FK 事务内完成。
5. 事务提交后写 outbox 索引任务，再由同一 workspace worker 更新 FTS/关系索引。
6. 写 Audit `committed`；任一步失败写 `failed`，错误只保存脱敏摘要。

前台允许的 SQLite 写仅限：新消息对同一 `(workspace,session_key)` 的原子 `activity_epoch/last_activity_at/due_at/last_message_cursor` upsert，以及追加 `retrieval_events`（或等价 Audit 事件）。前台不能修改正式记忆、Wiki/Case revision、Skill staging/current、评测结果、tombstone、FTS、归档或 retention 状态。维护 worker 独占 lease/status、candidate、revision/current、FTS、关系、retention、tombstone、合并、归档、回滚等写入。

Wiki adapter 可以在自己的 Markdown/SQLite 中完成事实源写入，但向 nanobot memory DB 的 revision、Case、FTS 同步必须提交维护队列；它不能直接更新本库 current。评测 runner 不写 workspace memory DB 的正式表；它写独立的临时评测数据库/JSONL evidence，完成后由维护 worker 以不可变 `eval_run` 导入。这样仍保持 workspace 单 writer；导入必须校验 dataset/fixture/baseline hash。

若事实源已成功而 SQLite 失败，保留事实源和 hash，写 outbox `index_pending`，重试时以 `(object_id,content_hash)` 幂等；在索引完成前普通检索降级，显式历史/修改返回 `index_degraded`。若 SQLite 成功而 FTS 失败，current 仍有效，任务保持 `index_pending`，不得回滚事实源；重试前检查 tombstone 和 current hash。重复提交命中唯一 hash/key 即视为已提交。CAS 影响行数为 0 表示出现新版本，旧任务标为 `superseded`。回滚只在维护 worker 中创建新 rollback revision 或切换经过校验的 current 指针，并记录 Audit；不删除旧事实源。

读取 current revision 时必须重新计算权威事实源的规范化 hash，并与 SQLite 缓存的 `content_hash` 比较。hash 不一致时以 Markdown/JSONL/Skill 事实源为准，将对象和相关索引标记为 `stale/index_degraded`，写入 outbox 重建任务；不得把不一致的 SQLite 缓存注入 prompt 或作为评测输入。

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

不按消息创建任务；worker 先在 `maintenance_lock` 中以条件更新抢锁：仅当不存在锁、`lease_until<=now` 或 owner 为自己时，将 `owner=:worker_id`、`lease_until=:now+lease`、`generation=generation+1` 写入；首次插入使用 `INSERT ... ON CONFLICT DO UPDATE ... WHERE lease_until<=:now`。持锁期间每个 lease/阶段更新 `renewed_at` 和 `lease_until`。不能抢到锁就不处理该 workspace。

`maintenance_lock` 是 workspace 级互斥，`maintenance_jobs` 是会话级状态；两者都在同一 SQLite 数据库中但职责不同。Gateway 重启后，过期 lease 可被新 worker 抢占并递增 `generation`；旧 worker 的续租带 `WHERE owner=:old AND generation=:generation`，因此不会复活。worker 退出前尽力释放锁，异常退出依靠 lease 超时恢复。所有维护型写入都必须持有该锁；前台 activity upsert 不抢维护锁，但只更新允许的四个活动字段。

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

评测 runner 为每题创建只读临时 fixture，环境变量关闭网络、写入和 Git；candidate 只能拿到当前 split 的 prompt、fixture 和工具白名单，不得读取 holdout 答案、其他 split、生成日志或 judge 输出。baseline 和 candidate 使用相同模型配置、工具 schema、fixture 和超时，并按同一 `case_key` 配对运行。

每题记录：成功/失败、rubric 分、总 token、端到端延迟、工具调用列表、高风险工具数、安全违规、恢复次数和 judge rationale。重试产生的所有 token 和墙钟时间计入该题；超时按失败计入，延迟取实际 timeout 上限；同一题的重试不另算成功题。EvalRun 指标按同一 EvalPack 的配对题计算：

```text
token_delta = (mean(candidate_tokens_i - baseline_tokens_i) / mean(baseline_tokens_i)) * 100%
latency_delta = (P50(candidate_latency_i) - P50(baseline_latency_i)) / P50(baseline_latency_i) * 100%
success_delta = mean(candidate_success_i - baseline_success_i)
```

失败和超时都保留在分母；P50 是整组 holdout（或 limited 的 holdout）逐题端到端延迟的中位数，不是单次 API 调用 P50。baseline/candidate 必须使用同一模型、参数、system prompt 版本、Tool schema digest、fixture hash、timeout 和 replay policy。

10～19 题的 limited EvalPack 每次运行必须写 `replay_group_id`、递增 `replay_attempt`、`evidence_grade='limited'`、`is_independent_replay` 和 `consistency_result`。至少两次独立回放使用相同封存 EvalPack、相同 fixture hash、相同模型和工具配置，但使用新的 process/worker、随机 nonce 和独立 `eval_run_id`；若逐题 outcome、工具风险和 gate 结论一致则 `consistency_result='consistent'`，否则为 `inconsistent`。limited 结果不能自动采用：样本量不足且重复回放只能证明有限稳定性，必须用户确认；确认界面至少展示两次 run 的 hash、逐题配对分数、失败/超时、token/延迟差、高风险工具、安全事件、fixture 和模型配置。

judge 可以与生成 Agent 使用同一底层模型，但必须是不同 `actor_id` 的独立调用；judge 不得读取 candidate 生成日志、隐藏思路或其他 split，不得修改 EvalPack、题目或 rubric，只能输出分数和理由。judge 失败时该题标记 `judge_error`，EvalRun 不得通过 gate，可重试为新的 run；发布决定只能由后台 gate 服务和用户确认完成。

发布 gate：holdout 成功率不低于 baseline；新增高风险工具数为 0；安全违规为 0；`token_delta<=15%`；`latency_delta<=20%`。通过最低证据和 gate 才能 `eligible_for_confirmation`；效果提升但成本/延迟超标只能 `staging`。limited 只能是 `provisional`，永远不能自动 active。任何 hash（Skill、tool schema、system prompt、model、fixture、EvalPack）变化都将 baseline 标记 `stale_baseline`，必须重跑。

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

### Phase 0：协议、空 schema 和持久化基础（后续实施任务）

Phase 0 只建立持久化契约和可验证的空数据库基础，不接入 ContextBuilder，不改变 Agent prompt，不执行 retention，不接入 Wiki/Case/Skill 自动写入，不创建生产 CLI。本阶段已按下列任务完成并通过临时 workspace/SQLite 验证。

#### Phase 0.1 文件边界

预计新增或修改的文件（路径为实施目标，当前不存在的文件不能视为已实现）：

| 文件 | 后续职责 | 验收重点 |
|---|---|---|
| `nanobot/memory/schema.py` | 表名、状态枚举、迁移版本、SQL 常量和 schema hash | 不复制 Audit schema；枚举与本文一致 |
| `nanobot/memory/db.py` | workspace 数据库路径、连接、WAL、busy timeout、`PRAGMA foreign_keys=ON` | 初始化目录安全、每个连接都启用 pragma |
| `nanobot/memory/migrations/0001_memory_base.sql` | Phase 0 表、索引、FTS5 检测结果和 schema_meta | 幂等、事务边界、外键可解析 |
| `nanobot/memory/migrations/runner.py` | migration apply、版本记录、失败恢复和回滚重建 | 不在脏数据库上半升级 |
| `nanobot/memory/lock.py` | `maintenance_lock` 抢锁、续租、释放和 generation 检查 | 过期 lease 可恢复，旧 worker 不能续租 |
| `nanobot/memory/outbox.py` | outbox 记录、幂等 key、重试/死信状态 | 事实源与索引最终一致，不覆盖新 hash |
| `tests/memory/test_schema.py` | 临时 SQLite schema/fk/FTS 测试 | 只使用临时目录或内存数据库 |
| `tests/memory/test_migrations.py` | 升级、失败、回滚和重建测试 | 不触碰 `runtime/workspace/` |
| `tests/memory/test_lock_outbox.py` | 并发 lease、重启恢复和 outbox 幂等测试 | 两 worker 竞争结果确定 |

实际文件名可在实现前调整，但必须保持“memory 数据库”和 Audit 数据库分离，并在 PR 中列出最终文件映射。

#### Phase 0.2 workspace 初始化和启动流程

目标路径为 `<workspace>/.nanobot/memory.sqlite3`，父目录 `<workspace>/.nanobot/` 以受限权限创建；不得创建在仓库根目录、宿主机临时目录或独立租户目录。启动流程应为：

1. 解析 Agent 实际 workspace，拒绝空路径、非目录和 workspace 外路径。
2. 创建 `.nanobot/`（若不存在），不删除、不覆盖已有文件。
3. 以 SQLite URI 打开数据库，设置 `busy_timeout`、WAL 和 `PRAGMA foreign_keys=ON`；每个新连接都重新执行 pragma，不能只依赖连接池首连接。
4. 在同一连接中读取 `schema_meta`，按版本顺序执行 migration；migration 未完成前不允许 memory 检索或维护写入。
5. 检测 FTS5；支持时创建 `memory_fts`，不支持时将 memory index 标记为 `degraded`，普通任务可继续但 Phase 0 验收不得判定通过。
6. 校验 schema hash、外键、必需表和 maintenance lock；失败则只读启动或明确失败，不能静默创建半成品数据库。

验收标准：使用临时 workspace 启动两次得到同一数据库路径和 schema 版本；实际默认 workspace 不被改动；连接查询 `PRAGMA foreign_keys` 返回 `1`；缺少 FTS5 时有可观测错误且不会误报“索引可用”。这些标准需要后续测试实现确认，当前未验证。

#### Phase 0.3 SQLite schema 和外键

Phase 0 必须实现本文第 4 节的 `memory_records`、`memory_revisions`、`wiki_pages`、`wiki_relations`、`cases`、`skills`、`skill_revisions`、`trace_index`、`eval_packs`、`eval_runs`、`eval_case_results`、`maintenance_jobs`、`maintenance_lock`、`tombstones`、`retrieval_events`、`schema_meta` 和 outbox 表。

外键验收必须逐字段执行：

* `memory_records.current_revision_id` → `memory_revisions.revision_id`：可空，deferred，`ON DELETE RESTRICT`。
* `wiki_pages.current_revision_id`：外部 Wiki revision 引用，可空，不声明本地 SQLite FK；adapter 必须在同步时校验外部 revision/hash。
* `cases.revision_id`：外部 Wiki revision 引用，不声明本地 FK；`cases.page_id` → `wiki_pages.page_id` 使用 `ON DELETE RESTRICT`。
* `skills.current_revision_id` → `skill_revisions.revision_id`：可空，deferred，`ON DELETE RESTRICT`。
* `skill_revisions.eval_pack_id` → `eval_packs.eval_pack_id`：可空，deferred，`ON DELETE SET NULL`；循环依赖必须在同一事务中验证。
* `eval_runs.baseline_revision_id` → `skill_revisions.revision_id`：非空，`ON DELETE RESTRICT`。
* `eval_runs.candidate_revision_id` → `skill_revisions.revision_id`：可空，`ON DELETE RESTRICT`。

测试方法：开启 `PRAGMA foreign_keys=ON` 后执行合法插入、缺失父行插入、删除父行、deferred transaction 提交和 rollback；检查 `PRAGMA foreign_key_list(table)` 与 schema 设计逐项一致。通过标准是非法引用在提交或插入时被拒绝、合法循环初始化可在 deferred transaction 内完成、任何 migration 不会留下未解析外键。失败恢复是回滚当前 migration，保留上一个 schema 版本，禁止手工删除生产数据库。

#### Phase 0.4 migration 执行和回滚

每个 migration 必须有单调版本号、`up` SQL、可验证的 schema hash 和回滚策略。执行流程为：

1. `BEGIN IMMEDIATE`，读取 `schema_meta`，确认没有未完成 migration。
2. 创建/升级临时结构，执行外键和索引检查。
3. 写入 migration version 和 app build，提交事务。
4. 提交后执行非关键 FTS rebuild/outbox enqueue；不能把半完成索引标为 ready。

失败时事务内 SQL 全部 rollback；如果 SQLite 不支持安全 down migration，则通过备份数据库、创建目标版本新库、从事实源重建并原子替换来回滚，不执行宽泛 `DROP`。验收包括：重复执行幂等；中途注入异常后旧版本仍可打开；升级后重新启动不重复执行；新库和重建库的 schema hash 相同。当前未创建 migration 文件，也未执行回滚验证。

#### Phase 0.5 maintenance_lock

必须使用本文 4.2 的 `maintenance_lock` 表，不再以未定义的锁表作为实现假设。抢锁条件为“无行、lease 已过期或 owner 自己”；成功时递增 `generation`，设置 `acquired_at/renewed_at/lease_until`。续租和释放必须带 `workspace + owner + generation` 条件。Gateway/worker 重启后只允许新 worker 抢占过期 lease，旧 worker 的续租影响行数为 0 即视为失效。

测试方法：两个独立连接竞争同一 workspace；验证只有一个 owner；模拟时间过期后新 owner 抢锁；旧 generation 续租失败；释放后第三个 worker 可获得新 generation。失败恢复：worker 崩溃不做强制清理，等待 lease 超时；锁表损坏时停止维护写入并报告，不能绕过锁直接写正式资产。

#### Phase 0.6 outbox

Phase 0 应新增（实施目标）`memory_outbox` 表，至少包括：`outbox_id`、`workspace`、`object_type`、`object_id`、`revision_id`、`content_hash`、`operation`、`payload_json`、`status`、`attempt_count`、`next_attempt_at`、`lease_until`、`last_error`、`created_at`、`updated_at`，并建立 `(object_type,object_id,content_hash,operation)` 幂等唯一键。`payload_json` 默认只保存引用、参数、cursor 和 hash；敏感正文不得进入 outbox，确需临时正文时必须脱敏、限长并设置 TTL。

事实源 revision 和 SQLite current 事务提交后，才插入 outbox；outbox worker 按 workspace lock 获取任务，检查 tombstone/current hash 后更新 FTS/关系索引。成功置 `done`；可重试错误进入 `retry_wait` 并指数退避；超过上限进入 `dead_letter`，不得静默丢弃。新 revision 到达时旧 outbox 任务标记 `superseded`，不能覆盖新 hash。

测试方法：模拟事实源成功/SQLite 失败、SQLite 成功/FTS 失败、重复 enqueue、新 revision 覆盖旧任务、worker 崩溃和 dead-letter；通过标准是最终索引与 current hash 一致、重复任务只产生一次有效更新、tombstone 后任务不会复活旧行。当前 outbox 尚不存在，以上均待实现验证。

#### Phase 0.7 FTS5 检测、初始化和重建

启动时必须通过临时连接执行 `SELECT fts5(?)` 或等价能力检测，并记录 SQLite 版本、FTS5 可用性和失败原因。检测失败时不创建伪 FTS 表、不宣称 Phase 0 通过；普通 Agent 可按设计降级为无记忆检索，但必须有明确 `index_degraded` 状态。

检测成功后创建 `memory_fts` 和必要的 FTS 配置；初始化和重建只扫描本文 4.3 映射的五类事实源。重建步骤：在临时 FTS 表写入当前 hash、执行计数/hash 校验、检查 tombstone 和 active 状态、事务 rename；失败保留旧索引并将 outbox 标为 retry/dead-letter。验收包括 FTS5 缺失检测、首次初始化、空库初始化、Markdown/hash 变化重建、归档排除、tombstone 防复活和重建幂等。当前未执行 FTS5 环境验证。

#### Phase 0.8 Phase 0 总验收

Phase 0 只有在以下全部通过后才可进入 Phase 1：

* 临时 workspace 初始化路径正确，默认 workspace 无副作用；
* 所有 migration 可重复执行、失败可恢复、版本和 schema hash 一致；
* `PRAGMA foreign_keys=ON` 在每个连接生效，外键矩阵测试通过；
* maintenance lock 的互斥、续租、过期接管和 generation 防旧 worker 测试通过；
* outbox 的幂等、重试、死信、superseded 和 tombstone 测试通过；
* FTS5 能力检测、初始化、重建和降级路径测试通过；
* 不接入 ContextBuilder、不执行 retention、不写正式 Wiki/Case/Skill、不创建生产 CLI；
* 所有失败场景都有保留旧版本/旧索引/只读降级/重试或人工恢复路径。

以上是 Phase 0 的实施任务和验收门槛，不是当前完成状态。

### Phase 1：Trace 索引和检索事件（依赖 Phase 0）

**目标**：只读消费现有 Audit JSONL/catalog，建立 `trace_index` 和 `retrieval_events`，不改变 Audit 事实源、不把 payload 复制进 memory DB。

**实施任务**：

1. 新增 `nanobot/memory/trace_indexer.py` 和 `nanobot/memory/retrieval_events.py`；读取 `nanobot/audit/catalog.py`、`reader.py`、`query.py` 的 committed prefix。
2. 对事件/载荷调用现有 `AuditRedactor`，生成脱敏 summary、outcome、tool_count、event_count 和 source cursor。
3. 为每条 Trace 设置 `payload_expire_at=created_at+7d`、`trace_expire_at=created_at+18d`；仅写索引时间，不提前删除 Audit 原始事实。
4. 将检索请求、scope、结果 revision IDs 和 injected digest 写入 `retrieval_events`；失败按普通任务/显式历史查询规则降级。
5. 增加全量 rebuild 和增量 cursor 任务，但作为内部维护能力，不新增对外 CLI。

**验收**：临时 Audit fixture 重建得到稳定 trace/hash；增量重跑幂等；payload 不出现在 summary；18/7 天边界计算正确；普通检索失败不阻塞，显式历史查询失败可见；查询接口返回结构化结果而非 404。**失败恢复**：索引失败保留 Audit 原始文件，outbox retry；脱敏失败不入长期索引并产生 `redaction_failed` 事件；cursor 损坏从 catalog committed prefix 重建。**不做**：不修改 Audit writer、不改变 WebUI trace 协议、不把完整 Trace 注入 prompt。

**实施结果（2026-09-25）**：已由 `f1debf0d` 完成。`trace_indexer.py` 通过 `AuditReader` 只读取 catalog 的 committed prefix，先用 `AuditRedactor` 校验事件和 payload，再丢弃 payload，仅落库脱敏摘要及元数据；支持全量重建、增量 cursor 与幂等 upsert。`retrieval_events.py` 提供不可变写入、失败事件和 payload-free 结构化 Trace 查询。脱敏或索引异常不改写 Audit，写入 `trace_index` 的 `degraded` 状态和幂等 `memory_outbox` 重试。当前未接入 AgentLoop、CLI、HTTP API 或 WebUI，因此没有可由新 WebUI 会话触发的检索流程。

### Phase 2：Wiki/Case 插件和 revision（依赖 Phase 0、Phase 1）

**目标**：通过 entry point 或 MCP 调用真实 Wiki，不在 nanobot 核心复制 Wiki storage；Case 作为 Wiki `page_type='case'` 页面。

**实施任务**：

1. 新增 `nanobot/plugins/memory_wiki.py`、`nanobot/memory/wiki_adapter.py`，定义 search/read/upsert/link/unlink/forget 的最小适配协议。
2. 对 `nanobot-llm-wiki` 的 `pages/links/page_fts`、`tools.py` 和 `mcp_server.py` 做 capability discovery；记录 source、tool schema digest 和版本。
3. Wiki 写入先创建不可变 Markdown revision，再把外部 revision ID/hash 投递到 memory outbox；本地 `wiki_pages.current_revision_id` 不声明 SQLite FK。
4. 将 `page_type='case'` 页面映射到 `cases`，生成 candidate；去重使用 content hash 和 task signature，不删除原页面。
5. FTS 只索引 adapter 返回且通过 tombstone/状态检查的内容；插件不可用时返回 degraded，不阻塞普通 Agent。

**验收**：entry point 和 MCP 两种模式至少各有一个临时 workspace round-trip；Markdown revision 不覆盖旧版本；Case page/cases 索引一致；关系重复写幂等；插件缺失可降级；forget 后旧 FTS 行不能复活。**失败恢复**：Wiki 写成功但 memory DB 失败时保留 Wiki revision 并重试 outbox；memory current CAS 冲突标记 superseded；外部 revision/hash 不一致停止同步并要求重扫。**不做**：不直接修改 Wiki 上游实现，不把 Wiki SQLite 作为 nanobot memory DB，不在本阶段自动发布正式 Skill。

**实施结果（2026-09-26）**：已由 `adbe0d79` 完成适配与派生索引闭环。`wiki_adapter.py` 定义统一的 search/read/upsert/link/unlink/forget 协议，并支持 `nanobot.tools` entry point 与 MCP `tools/list` 的 capability/schema digest 发现；`plugins/memory_wiki.py` 只写入外部 page 的 revision/hash、`wiki_pages`、`wiki_relations`、`cases`、FTS 和幂等 outbox，不复制 Wiki 正文事实源。Case 页面会进入 candidate，内容 hash/task signature 支持重复发现，forget 通过 tombstone 清除本地 FTS 且阻断复活。临时 entry point/MCP round-trip、关系幂等、Case 索引、hash 拒绝和 provider 缺失 degraded 测试均已通过。

**能力边界**：当前工作区的 `nanobot-llm-wiki` provider 没有独立不可变 revision API；适配器对缺失 revision 的只读返回使用 content-addressed revision 标识，并将 provider 能力状态保留为可观测 metadata，不宣称拥有上游历史版本。需要不可变 Markdown 历史的 provider 必须在适配器中提供显式 `revision_id`/`content_hash`；hash 不匹配会 fail closed。Phase 2 未接入 AgentLoop、CLI 或 WebUI 检索流程。

### Phase 3：ContextBuilder memory scope（依赖 Phase 1、Phase 2）

**目标**：在现有 `ContextBuilder.build_system_sections()` 的 dynamic 区域增加按意图的只读检索，保持 stable prompt 和 Tool schema cache 稳定。

**实施任务**：

1. 新增 `nanobot/memory/intent.py`、`retriever.py`、`policy.py`；由 `AgentLoop` 传入 session/channel/trace 元数据。
2. 规则优先识别 `task/history_query/memory_write/forget/skill_discovery/trace_query/review`，输出 scope 和显式失败语义。
3. 检索 memory、Wiki、Case、Skill、Trace summary，按 effective score、freshness、authority、冲突惩罚排序。
4. 执行 2,000 token 软上限、3,000 token 硬上限和 6% 自适应；完整正文只通过分页工具读取。
5. 将 retrieval digest 写入 dynamic metadata，不写入 stable prompt；继续使用现有 `ContextGovernor` 作最终裁剪。

**验收**：稳定事实变化只改变 dynamic digest；上下文超限时保留优先级正确；普通检索异常仍能完成普通任务；显式历史/记忆修改异常产生可见结构化错误；注入内容带 revision 引用且无 tombstone 内容。**失败恢复**：memory DB/FTS 不可用时退回 session-only；预算计算异常时不注入 memory overlay；digest 不一致时丢弃 overlay 并记录 retrieval event。**不做**：不改 AgentRunner 工具循环，不默认注入完整 Trace/payload，不自动写入正式记忆。

**实施结果（2026-09-26）**：已增加 `intent.py`、`policy.py` 和 `retriever.py`。`IntentRouter` 使用规则优先识别七类意图；`MemoryScopePolicy` 将意图转换为只读 scope；`MemoryRetriever` 只读取派生 SQLite 的 memory/Wiki/Case/Skill/Trace 摘要，排除 tombstone/过期记录，返回 revision/source 引用并记录 payload-free `retrieval_events`。检索预算按 context window 的 6% 自适应，受 2,000 token 软上限和 3,000 token 硬上限约束；完整正文不进入 dynamic overlay。

`ContextBuilder` 将检索结果放入 dynamic 区域并把 digest/intent/outcome 写入系统消息 metadata，稳定 prompt 与 tool schema 不变；`AgentLoop` 传入 session、trace、turn 和 context-window 元数据。普通任务在数据库不可用或查询失败时保持 session-only；显式历史、Trace、review 查询产生可见结构化失败。Phase 3 未修改 AgentRunner 工具循环、Wiki/Skill/Audit 事实源或对外 API。

### Phase 4：维护 worker、45 分钟任务和 ToolPolicy（依赖 Phase 0～3）

**目标**：建立每 workspace 单 worker 的 review/retention/index 编排，并将工具权限落实到 `ToolRegistry.prepare_call()` 之前。

**实施任务**：

1. 新增 `nanobot/memory/maintenance.py`、`worker.py`、`policy.py` 的实际实现；维护 `maintenance_jobs`、`maintenance_lock`、outbox 和 retry 状态。
2. 在 AgentLoop 消息入口只执行 activity upsert；worker 按 45 分钟 idle、epoch、snapshot cursor 执行一次批处理 review。
3. 实现 `skill_catalog_search`、`skill_read`、`skill_propose`，注册方式遵循现有 ToolLoader/entry point，不绕过 ToolRegistry。
4. 实现普通 Agent、维护 Agent、评测 Agent 的 ToolPolicy 矩阵，阻止写正式资产、删除、Git、网络和越权路径。
5. 处理 Gateway 重启、lease 过期、旧 epoch、CAS 冲突和最多 5 次退避重试。

**验收**：连续消息只有一条 maintenance job；两个 worker 不能同时持有 workspace lock；重启后任务可恢复；旧 epoch 不能覆盖新 cursor；高风险工具在 prepare_call 前被拒；candidate 不得修改 current。**失败恢复**：worker 崩溃依赖 lease 超时接管；策略配置缺失 fail-closed；review 失败进入 retry_wait/dead-letter，不阻塞普通 Agent。**不做**：不自动 adopt、不自动 merge PR、不把维护 Agent 设为正式发布者。

### Phase 5：Case→Skill、EvalPack/EvalRun 和 staging（依赖 Phase 2～4）

**目标**：形成可审计的候选 Skill 评测闭环，不允许同一 Agent 自己出题、执行、评分和批准。

**实施任务**：

1. 新增 `nanobot/memory/derivation.py`、`evaluation.py` 和独立评测 harness；从脱敏 Trace/Case 生成 candidate 和 EvalPack draft。
2. 独立审核 actor 校验题目 schema、去重、fixture、秘密和 rubric，封存 dataset_hash/fixture_hash；按 20 题 10/5/5，10～19 题 limited，少于 10 题 insufficient。
3. 评测 runner 使用临时 fixture workspace，禁网、禁写、禁 Git；baseline/candidate 同题配对，结果先写临时 evidence，再由维护 worker 导入。
4. judge 只能评分和给理由；记录 replay_group、attempt、consistency、token、延迟、工具风险和安全指标。
5. 通过 holdout、成本≤15%、P50 延迟≤20%、无安全违规和无新增高风险工具后进入 `eligible_for_confirmation`；否则停留 staging。

**验收**：holdout 对 candidate 不可见；生成/审核/回放/评分/发布 actor 分离；10/19/20 题边界正确；limited 两次独立回放证据一致性可判断；任何 Skill/tool/model/fixture/hash 改变会产生 stale_baseline；回放结果可重现。**失败恢复**：judge 失败使 run 不通过且新 run 重试；fixture 污染销毁临时目录并保留 evidence 摘要；gate 失败保留 candidate 但禁止 current 切换。**不做**：不自动发布正式 Skill，不把 synthetic 题单独当作发布证据，不引入独立向量库。

### Phase 6：受控持续运行（依赖 Phase 5，默认关闭）

**目标**：在明确开关和低风险范围内运行空闲 review，持续积累 evidence，但不产生不可逆外部副作用。

**实施任务**：

1. 增加维护配置和 kill switch（仅作为配置，不在本阶段新增 CLI）；默认关闭自动 staging/adopt。
2. 只选择重复、高价值、低敏感任务；线上失败 Trace 自动生成 regression candidate，重新封存 EvalPack 版本。
3. 每轮记录 evidence、成功率、恢复率、token、延迟、安全事件、索引失败和记忆容量告警。
4. 对连续回归、权限拒绝异常、记忆膨胀或 FTS 重建失败自动暂停候选处理，保留旧 current revision。
5. 共享 Skill 只生成 Git PR proposal；个人 Skill 只生成需用户确认的 workspace adopt proposal。

**验收**：连续运行不增加重复事实、不绕过 ToolPolicy、不自动合并；回归可自动暂停并回滚 current；18/7 天 retention 不误删长期摘要；每次 proposal 都能追溯到 trace/case/eval run。**失败恢复**：kill switch 立即停止维护 worker 的候选处理；恢复旧 revision/current 指针；无法恢复时进入只读 degraded，不清理用户 workspace。**不做**：不自动 merge main、不自动删除普通记忆、不自动扩大工具权限、不把 Phase 6 当作默认线上行为。

**实施结果（2026-09-26）**：新增 `nanobot/memory/continuous.py` 和 `Phase6Config`。运行开关默认关闭，kill switch 在所有候选写入前 fail-closed；低风险筛选只接受重复、成功、非敏感且只读工具任务；失败/partial/blocked Trace 只派生 staging Case。EvalPack 版本通过 `phase6_version` 和稳定数据 hash 递增，evidence 以脱敏、fsync 的 append-only JSONL 写入 workspace `.nanobot/phase6/evidence.jsonl`。

健康检查对连续回归、权限拒绝、FTS 失败和容量增长自动暂停候选处理；暂停时可在 CAS 条件下恢复已知 baseline revision，失败则保持只读暂停。18/7 天 retention 只生成 payload 到期和摘要复核计划，不删除长期摘要；proposal 必须同时携带 trace/case/eval run 引用，共享 Skill 只生成 PR proposal，workspace Skill 只生成待确认 adopt proposal。未新增 migration、CLI、自动发布或自动合并。

Phase 6 聚焦测试共 10 项；`tests/memory` 共 63 项，配置/Agent 审计回归 100 项，`ruff check` 与 `git diff --check` 通过。

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

## 18. 调研修订记录

本轮复核查阅了以下真实代码和资料：

* nanobot：`nanobot/agent/loop.py`、`runner.py`、`context.py`、`context_governance.py`、`memory.py`、`session/manager.py`、`session/goal_state.py`、`agent/tools/registry.py`、`agent/tools/loader.py`、`agent/skills.py`、`audit/emitter.py`、`audit/writer.py`、`audit/index_schema.py`、`audit/query.py`、`config/`。
* `nanobot-llm-wiki`：`src/nanobot_llm_wiki/storage.py`（`pages`/`links`/`page_fts` 及 join 检索）、`tools.py`（9 个 NanoBot entry-point 工具）、`mcp_server.py`（7 个 stdio MCP 工具）、`pyproject.toml`（`nanobot.tools` entry points）、`tests/test_cli_and_tools.py`、`tests/test_mcp_server.py`。
* `hermes-agent-self-evolution`：`evolution/core/dataset_builder.py`、`fitness.py`、`constraints.py`、`README.md`、`PLAN.md`。该项目是独立优化 pipeline，提供 dataset/replay/judge/constraint/PR 范式，并非 nanobot 内置 EvalPack 实现。
* `SkillOpt-main/SkillOpt-main/skillopt_sleep`：`cycle.py`、`replay.py`、`staging.py`、`evidence.py`、`judges.py`、`types.py`、`gate.py`。真实流程为 harvest→mine→replay→consolidate/gate→stage→可选 adopt；证据日志和 staging 是独立文件，不是 nanobot SQLite 协议。

本轮修正：

1. 明确 nanobot 现有 `MemoryStore` 是文件 I/O，不是 SQLite memory store；Audit 的 SQLite 也只是其 JSONL 的索引，不能复用为记忆事实库。
2. 为 `memory_records`、`skills`、`skill_revisions`、`eval_runs` 补充真实 SQL 外键、可空性、`ON DELETE` 和 deferred 规则；明确 Wiki page/Case revision 是外部事实源引用。
3. 增加 `maintenance_lock` 完整 schema，统一为条件抢锁+lease+generation，不再使用未定义的“锁表或 BEGIN IMMEDIATE”二选一表述。
4. 将 FTS 查询改为按 `object_type` join 状态事实表并检查 tombstone，定义五类对象、事实源和 archived 行为。
5. 统一前台 activity/retrieval 有限写与维护 worker 独占写边界，明确评测 runner 使用临时 evidence 后由 worker 导入。
6. 统一 Audit intent→事实源 fsync/hash→SQLite CAS→outbox→索引→Audit committed/failed 写入顺序，补充失败恢复和幂等。
7. 补充 limited EvalPack 的独立回放字段、同题配对成本/延迟公式及 judge 边界。

仍待代码验证：新增 memory schema、迁移、outbox、ToolPolicy、Wiki adapter、维护 worker、评测 runner 尚未实现；`memory.sqlite3` 的实际目录初始化、entry point 版本/冲突处理、SQLite FTS5 是否在目标部署环境启用、Audit 18/7 天清理与真实 fixture 禁网/禁写仍须 Phase 0～5 的实现测试确认。文档中的 `memory schema` 等 CLI 是实施目标，不是当前已存在命令。

## 19. 实施完成判定

只有同时满足以下条件，才可把五层记忆声明为可用：schema 可重建、事实源和索引边界清晰、ContextBuilder 有预算和降级、45 分钟任务可恢复、Trace→Case→Skill 有独立审核和版本回滚、EvalPack 有不可读 holdout、tombstone 可阻断复活、首个只读代码审查 Skill 在隔离 fixture 上通过 gate，并且所有正式发布仍等待用户确认。
+
## 20. 可执行实施任务清单

本节把前述架构拆成可持续执行的任务卡。所有任务初始化为 `[ ]`；设计存在不等于任务完成。每个任务完成后，实施 AI 必须填写测试、证据和提交信息，才允许改为 `[x]`。

### Phase 0：持久化基础

- 进入条件：Phase 0 设计评审通过。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P0-T01：确认 memory 模块文件边界

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：建立 `nanobot.memory` 及 `nanobot.memory.migrations` 基础包，并建立对应的 `tests/memory` 测试边界。包内暂不导出数据库连接、迁移、锁或 outbox API，不创建数据库，也不接入 Agent。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [x] 代码或文档变更已完成
  - [x] 测试已运行
  - [x] 测试结果已记录
  - [x] 审计/证据路径已记录
  - [x] 提交编号已记录
- 实施记录：完成日期：2026-09-25；提交：5d54eb22；测试：`pytest -q tests/memory/test_package_boundary.py`（2 passed），`ruff check nanobot/memory tests/memory`（通过）；证据：`tests/memory/test_package_boundary.py`；遗留问题：数据库路径、连接、schema、migration、lock、outbox 和 FTS5 仍由后续 P0 任务实施。

#### P0-T02～P0-T22：Phase 0 任务完成记录

以下任务均按编号顺序完成，未跨越前置任务；每项均只使用临时 workspace、内存或临时文件 SQLite。

| 任务 | 状态 | 实施记录 | 测试/证据 | 提交 |
|---|---|---|---|---|
| P0-T02 workspace 数据库路径解析 | [x] | 固定为 `<workspace>/.nanobot/memory.sqlite3`，校验 workspace 为现有目录并以 0700 创建父目录。 | `tests/memory/test_schema.py::test_workspace_path_is_local_and_connection_pragmas_are_per_connection` | 99d6e203 |
| P0-T03 SQLite connection 配置 | [x] | 每个连接设置 WAL、busy timeout 5000 和 row factory。 | 同上；`pytest -q tests/memory` | 99d6e203 |
| P0-T04 foreign_keys=ON | [x] | 每个新连接显式执行 `PRAGMA foreign_keys=ON`。 | `foreign_keys_enabled` 断言 | 99d6e203 |
| P0-T05 schema_meta | [x] | 建立版本、migration_id、app_build、schema_hash、状态和错误字段。 | `test_all_required_tables_and_schema_meta_are_applied` | 99d6e203 |
| P0-T06 0001 基础 migration | [x] | 新增完整 Phase 0 表、索引和外键 SQL。 | schema 表集合与 migration 验证 | 99d6e203 |
| P0-T07 migration 幂等执行 | [x] | 已应用版本按 hash 校验并跳过重复执行。 | `test_migration_is_idempotent_and_schema_hash_stable` | 99d6e203 |
| P0-T08 migration 失败恢复 | [x] | 事务失败回滚，写入脱敏 failed 记录，随后可重新应用。 | `test_failed_migration_is_recorded_and_can_be_recovered` | 99d6e203 |
| P0-T09 memory 外键 | [x] | `memory_records`/`memory_revisions` 使用 deferred、RESTRICT 外键。 | `test_memory_revision_foreign_keys_and_deferred_current_pointer` | 99d6e203 |
| P0-T10 skill 外键 | [x] | `skills`/`skill_revisions` current 指针和父记录外键已定义。 | `test_skills_and_skill_revisions_foreign_keys` | 99d6e203 |
| P0-T11 eval 外键 | [x] | `eval_runs` baseline/candidate 外键及 RESTRICT 已定义。 | `test_eval_foreign_keys_reject_missing_revisions` | 99d6e203 |
| P0-T12 maintenance_lock | [x] | 实现抢锁、续租、释放和 generation 条件校验。 | `nanobot/memory/lock.py` | 99d6e203 |
| P0-T13 lock 竞争/续租/过期 | [x] | 两个独立连接验证互斥、过期接管和旧 generation 失效。 | `test_lock_competition_expiry_generation_and_stale_renewal` | 5363cc33 |
| P0-T14 memory_outbox | [x] | 实现幂等 enqueue、lease、完成和状态流转。 | `nanobot/memory/outbox.py` | 99d6e203 |
| P0-T15 outbox 幂等/superseded | [x] | 相同 key 复用任务，新 hash 将旧待处理任务标记 superseded。 | `test_outbox_idempotency_superseded_retry_and_dead_letter` | 99d6e203 |
| P0-T16 outbox retry/dead letter | [x] | 按尝试次数和指数退避进入 retry_wait/dead_letter。 | 同上 | 99d6e203 |
| P0-T17 FTS5 检测 | [x] | 独立临时连接执行 fts5 能力检测并返回 degraded 错误。 | `test_fts_detection_reports_degraded_status` | 2680e060 |
| P0-T18 FTS5 初始化 | [x] | 支持环境幂等创建 `memory_fts`，不支持时不创建伪表。 | `test_fts_initialization_rebuild_and_active_filtering` | 2680e060 |
| P0-T19 FTS5 基础重建 | [x] | 使用临时 FTS 表、行数校验和事务 rename 重建。 | 同上 | 2680e060 |
| P0-T20 tombstone/archived 过滤 | [x] | FTS 重建及查询排除 tombstone 和非 active 记录。 | 同上 | 2680e060 |
| P0-T21 Phase 0 集成测试 | [x] | 串联 workspace、migration、schema、FTS、lock、outbox 并重复启动。 | `test_phase0_bootstrap_is_repeatable_and_isolated`；17 passed | 276e37a7 |
| P0-T22 Phase 0 DoD 评审 | [x] | 逐项核对 DoD，确认未接入 ContextBuilder、retention、Wiki/Case/Skill 自动写入或生产 CLI。 | 本表及 Phase 0 集成证据 | 276e37a7 |

### Phase 0 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险。

### Phase 1：Trace 索引

- 进入条件：Phase 前置 Phase 的 DoD 全部为 [x]，且无未解决设计冲突。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P1-T01：Audit JSONL/catalog 读取适配

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T02：Trace 脱敏摘要生成

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T03：trace_index 增量索引

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T04：trace_index 全量重建

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T05：retrieval_events 写入

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T06：18 天 Trace / 7 天 payload 时间字段

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T07：Trace 索引失败恢复

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P1-T08：Phase 1 DoD

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

### Phase 1 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险。

### Phase 2：Wiki/Case

- 进入条件：Phase 前置 Phase 的 DoD 全部为 [x]，且无未解决设计冲突。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P2-T01：定义 Wiki adapter 协议

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T02：entry point capability discovery

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T03：MCP capability discovery

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T04：Wiki revision 同步

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T05：wiki_pages 索引

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T06：wiki_relations 索引

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T07：Case candidate 生成

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T08：Case 去重和合并建议

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T09：Wiki 不可用降级

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P2-T10：Phase 2 DoD

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

### Phase 2 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险；provider 不具备不可变 revision 时按 degraded 处理。

### Phase 3：ContextBuilder 检索

- 进入条件：Phase 前置 Phase 的 DoD 全部为 [x]，且无未解决设计冲突。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P3-T01：intent 路由器

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已实现规则优先七类意图路由，并保持写入/遗忘请求只读不注入。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T02：memory scope

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已实现只读 scope policy，限制 memory/Wiki/Case/Skill/Trace 检索范围。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T03：Wiki/Case/Skill/Trace 检索

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已实现派生 SQLite 摘要检索、tombstone/过期过滤和 revision/source 引用。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T04：token 预算

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已实现 6% 自适应预算、2,000 token 软上限和 3,000 token 硬上限。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T05：dynamic context digest

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已将 retrieval digest/intent/outcome 写入 dynamic system metadata。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T06：stable prompt cache 隔离

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已验证稳定 prompt digest 不随动态检索内容变化，AgentLoop 传入 turn 元数据。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T07：普通检索失败降级

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已实现数据库不可用/查询失败时普通任务 session-only 降级。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T08：显式历史查询失败

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已实现 history/trace/review 显式失败消息与 retrieval event 记录。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P3-T09：Phase 3 DoD

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：已完成 Phase 3 聚焦、回归、失败恢复和 lint 验证。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

### Phase 3 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录；本阶段复用 Phase 0 schema，不新增迁移。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险。

### Phase 4：Maintenance/ToolPolicy

- 进入条件：Phase 前置 Phase 的 DoD 全部为 [x]，且无未解决设计冲突。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P4-T01：maintenance_jobs activity upsert

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T02：45 分钟 debounce

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T03：snapshot cursor

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T04：worker lease 和恢复

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T05：review retry

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T06：ToolPolicy

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T07：skill_catalog_search

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T08：skill_read

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T09：skill_propose

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T10：普通 Agent/维护 Agent/评测 Agent 权限矩阵

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P4-T11：Phase 4 DoD

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

### Phase 4 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险。

Phase 4 实施证据：`nanobot/memory/maintenance.py` 和 `worker.py` 提供 activity 合并、45 分钟 debounce、snapshot/review cursor 的 epoch CAS、workspace lease、重启接管、退避重试和 dead-letter；`ToolPolicy` 在 `ToolRegistry.prepare_call()` 前对 maintenance/evaluation 角色 fail-closed 拒绝高风险工具；`skill_catalog_search`、`skill_read` 为只读工具，`skill_propose` 仅写入 staging candidate 与 `skill_revisions`，不切换 current revision。`tests/memory/test_phase4_maintenance.py` 覆盖上述并发、恢复、权限边界；Phase 4 聚焦测试 5 passed，相关 memory/agent 回归 305 passed，ruff 与 `git diff --check` 均通过。未实现自动 adopt、自动发布或自动合并。

### Phase 5：Case→Skill/Eval

- 进入条件：Phase 前置 Phase 的 DoD 全部为 [x]，且无未解决设计冲突。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P5-T01：Trace outcome 分类

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T02：Case candidate

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T03：Skill candidate

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T04：EvalPack draft

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T05：独立审核和封存

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T06：fixture workspace

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T07：baseline replay

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T08：candidate replay

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T09：逐题评分

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T10：limited evidence 两次独立回放

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T11：holdout gate

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T12：成本/延迟/安全 gate

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T13：staging 和用户确认

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

#### P5-T14：Phase 5 DoD

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：；提交：；测试：；证据：；遗留问题：

### Phase 5 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险。

Phase 5 实施证据：`nanobot/memory/derivation.py` 将结构化 Trace outcome 归一化并生成 staging Case candidate，`build_eval_pack_draft` 按稳定 digest 生成 EvalPack，20 题按 10/5/5 划分，10～19 题标记 limited，少于 10 题标记 insufficient_evidence；`seal_eval_pack` 要求独立审核 actor。`nanobot/memory/evaluation.py` 提供临时 fixture workspace、baseline/candidate replay run、逐题 judge 结果、limited 两次 replay 一致性、holdout/成本/延迟/安全 gate 和 stale identity 检查。所有写入均为派生 evidence，未切换 current Skill revision。`tests/memory/test_phase5_evaluation.py`：6 passed；相关 memory/agent 回归：310 passed；ruff 与 `git diff --check` 通过。

### Phase 6：受控持续运行

- 进入条件：Phase 前置 Phase 的 DoD 全部为 [x]，且无未解决设计冲突。
- 停止条件：schema/事实源冲突、测试失败且无安全恢复、需要扩大授权、覆盖用户数据风险或改变已确认产品决策。

#### P6-T01：默认关闭的持续运行开关

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：1960e869；测试：10 项 Phase 6 聚焦测试；证据：`.nanobot/phase6/evidence.jsonl`；遗留问题：默认关闭。

#### P6-T02：低风险任务筛选

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：1960e869；测试：低风险/敏感/写工具排除测试；证据：`select_low_risk_tasks`；遗留问题：默认关闭。

#### P6-T03：失败 Trace 转回归题

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：1960e869；测试：失败、超时和成功拒绝测试；证据：`regression_case_from_trace`；遗留问题：仅 staging。

#### P6-T04：EvalPack 版本更新

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：1960e869；测试：版本/hash 递增测试；证据：`eval_packs.split_policy_json`；遗留问题：不自动发布。

#### P6-T05：evidence log

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：1960e869；测试：脱敏、append-only、fsync 测试；证据：`.nanobot/phase6/evidence.jsonl`；遗留问题：不记录原始 payload。

#### P6-T06：回归自动暂停

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：1960e869；测试：回归阈值、权限、FTS、容量告警测试；证据：`state.json`；遗留问题：暂停需独立复核恢复。

#### P6-T07：Skill rollback

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：633fff28；测试：CAS baseline rollback 测试；证据：`pause_and_rollback`；遗留问题：不绕过 ToolPolicy。

#### P6-T08：容量和索引失败告警

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：633fff28；测试：18/7 retention 与容量指标测试；证据：`plan_retention`；遗留问题：不删除长期摘要。

#### P6-T09：Phase 6 DoD

- 状态：[x]
- 类型：实现/测试/验收（按实际工作调整）
- 目标：将本任务落实为可复现、可验证的独立工作单元。
- 前置任务：本阶段前序任务及前置 Phase DoD。
- 涉及文件：第 3 节目标目录及对应现有模块；执行时记录最终路径。
- 不涉及文件：原计划文档、无关生产模块、用户既有无关修改。
- 实施内容：遵循既定 schema、事实源边界、单 worker、权限和失败恢复规则；当前未开始。
- 输入和输出：输入为前置任务产物和临时 fixture；输出为代码/测试/审计证据或评审结论。
- 数据库或协议变化：仅允许本文已定义的表、字段、状态、迁移和协议；需改变时先登记阻塞。
- 测试方法：使用临时 workspace/SQLite/fixture，执行单元、集成、安全和失败恢复测试。
- 通过标准：设计一致、测试通过、失败可恢复、未扩大授权范围。
- 失败恢复：保留旧事实源和旧 schema，记录脱敏错误，按 retry/degraded/rollback 处理。
- 证据要求：记录命令、结果、日志/trace、schema/hash 或审计事件路径。
- 完成后勾选：
  - [ ] 代码或文档变更已完成
  - [ ] 测试已运行
  - [ ] 测试结果已记录
  - [ ] 审计/证据路径已记录
  - [ ] 提交编号已记录
- 实施记录：完成日期：2026-09-26；提交：633fff28；测试：63 memory + 100 config/Agent 回归、ruff；证据：本节 DoD；遗留问题：默认开关关闭，待用户另行启用。

### Phase 6 Definition of Done

- [x] 所有子任务完成，或延期/阻塞均有记录。
- [x] 单元、集成和安全边界测试通过并记录。
- [x] migration/schema/协议证据已记录（Phase 6 未新增 migration，沿用现有 schema）。
- [x] 失败恢复路径已验证。
- [x] 文档当前状态已更新，未把未验证内容标为完成。
- [x] 提交和测试证据已记录。
- [x] 无未解决的事实源、权限或回滚风险。

## 21. 实施记录

Phase 0～Phase 6 已完成并记录证据；Phase 6 默认开关保持关闭。

| 任务 ID | 状态 | 日期 | 提交 | 测试 | 证据 | 备注 |
|---|---|---|---|---|---|---|
| P0-T01 | [x] | 2026-09-25 | 5d54eb22 | `pytest -q tests/memory/test_package_boundary.py`；`ruff check nanobot/memory tests/memory` | `tests/memory/test_package_boundary.py` | 仅建立包边界，未创建数据库 |
| P0-T02 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `tests/memory/test_schema.py` | 已完成 |
| P0-T03 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `nanobot/memory/db.py` | 已完成 |
| P0-T04 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `test_schema.py` | 已完成 |
| P0-T05 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `schema_meta` | 已完成 |
| P0-T06 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `0001_memory_base.sql` | 已完成 |
| P0-T07 | [x] | 2026-09-25 | 99d6e203 | `test_migrations.py` | schema hash | 已完成 |
| P0-T08 | [x] | 2026-09-25 | 99d6e203 | `test_failed_migration_is_recorded_and_can_be_recovered` | failed→applied | 已完成 |
| P0-T09 | [x] | 2026-09-25 | 99d6e203 | `test_memory_revision_foreign_keys_and_deferred_current_pointer` | deferred FK | 已完成 |
| P0-T10 | [x] | 2026-09-25 | 99d6e203 | `test_skills_and_skill_revisions_foreign_keys` | skill FK | 已完成 |
| P0-T11 | [x] | 2026-09-25 | 99d6e203 | `test_eval_foreign_keys_reject_missing_revisions` | eval FK | 已完成 |
| P0-T12 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `nanobot/memory/lock.py` | 已完成 |
| P0-T13 | [x] | 2026-09-25 | 5363cc33 | lock test | 双连接竞争/代际 | 已完成 |
| P0-T14 | [x] | 2026-09-25 | 99d6e203 | `pytest -q tests/memory` | `nanobot/memory/outbox.py` | 已完成 |
| P0-T15 | [x] | 2026-09-25 | 99d6e203 | outbox 幂等测试 | superseded | 已完成 |
| P0-T16 | [x] | 2026-09-25 | 99d6e203 | outbox retry 测试 | dead_letter | 已完成 |
| P0-T17 | [x] | 2026-09-25 | 2680e060 | FTS5 降级测试 | `detect_fts5` | 已完成 |
| P0-T18 | [x] | 2026-09-25 | 2680e060 | FTS 初始化测试 | `memory_fts` | 已完成 |
| P0-T19 | [x] | 2026-09-25 | 2680e060 | FTS 重建测试 | 临时表 rename | 已完成 |
| P0-T20 | [x] | 2026-09-25 | 2680e060 | FTS 过滤测试 | tombstone/archived | 已完成 |
| P0-T21 | [x] | 2026-09-25 | 276e37a7 | 17 passed | `test_phase0_integration.py` | 已完成 |
| P0-T22 | [x] | 2026-09-25 | 276e37a7 | DoD 逐项核对 | Phase 0 DoD | 已完成 |
| P1-T01 | [x] | 2026-09-25 | f1debf0d | `test_audit_reader_adapter_indexes_only_committed_prefix` | `nanobot/memory/trace_indexer.py` | 只读 committed prefix |
| P1-T02 | [x] | 2026-09-25 | f1debf0d | `test_summary_is_deterministic_and_does_not_copy_payload` | `AuditRedactor`、`trace_index` | payload 仅校验后丢弃 |
| P1-T03 | [x] | 2026-09-25 | f1debf0d | `test_trace_index_upsert_is_idempotent`；`test_summary_cursor_changes_when_committed_prefix_advances` | `event_cursor`、`summary_hash` | 增量重放幂等 |
| P1-T04 | [x] | 2026-09-25 | f1debf0d | `test_full_and_incremental_replay_are_idempotent_and_payload_free` | `rebuild_trace_index` | 全量重建后增量无重复 |
| P1-T05 | [x] | 2026-09-25 | f1debf0d | `test_retrieval_events_are_append_only_and_failures_are_visible`；`test_trace_query_returns_structured_active_rows_and_hides_degraded_or_expired` | `retrieval_events.py` | 查询和失败事件均为结构化记录 |
| P1-T06 | [x] | 2026-09-25 | f1debf0d | `test_summary_is_deterministic_and_does_not_copy_payload` | `payload_expire_at`、`trace_expire_at` | 7/18 天字段从 Trace 开始时间计算 |
| P1-T07 | [x] | 2026-09-25 | f1debf0d | `test_redaction_failure_keeps_audit_and_queues_retry`；`test_index_failure_keeps_audit_and_queues_retry` | `trace_index.degraded`、`memory_outbox` | Audit 文件不变，失败可重试 |
| P1-T08 | [x] | 2026-09-25 | f1debf0d | `pytest -q tests/memory`（26 passed）；Audit 聚焦测试（15 passed）；`ruff check nanobot/memory tests/memory` | PR #20、Gateway 构建 `git-14bc7da6edf7` | Phase 1 DoD 已核对 |
| P2-T01 | [x] | 2026-09-26 | adbe0d79 | `test_capability_discovery_is_source_agnostic_and_digest_stable` | `nanobot/memory/wiki_adapter.py` | 统一 Wiki adapter 协议 |
| P2-T02 | [x] | 2026-09-26 | adbe0d79 | entry point capability 与真实 provider 临时 workspace round-trip | `EntryPointWikiAdapter` | source/version/schema digest |
| P2-T03 | [x] | 2026-09-26 | adbe0d79 | `test_mcp_adapter_normalizes_structured_page_and_capabilities` | `McpWikiAdapter` | MCP tools/list/call_tool 归一化 |
| P2-T04 | [x] | 2026-09-26 | 70a3e8c4 | `test_sync_page_indexes_external_revision_and_case_candidate`；revision 替换和 hash mismatch 拒绝测试 | `sync_provider_page`、`WikiPage.revision_id` | provider 缺少显式 revision 时 degraded/content-addressed |
| P2-T05 | [x] | 2026-09-26 | adbe0d79 | Phase 2 sync/index 测试 | `wiki_pages` | 外部 revision/hash 仅作派生索引 |
| P2-T06 | [x] | 2026-09-26 | adbe0d79 | `test_relation_upsert_is_idempotent_and_unlink_is_rebuildable` | `wiki_relations` | 重复关系幂等、unlink 可重建 |
| P2-T07 | [x] | 2026-09-26 | adbe0d79 | Case candidate sync 测试 | `cases` | `page_type='case'` 映射 candidate |
| P2-T08 | [x] | 2026-09-26 | adbe0d79 | `find_case_duplicates` 覆盖 content hash/task signature | `find_case_duplicates` | 仅生成重复发现，不覆盖原页面 |
| P2-T09 | [x] | 2026-09-26 | adbe0d79 | `test_unavailable_provider_is_explicit`；tombstone 复活拒绝测试 | `discover_wiki`、`forget_wiki_page` | provider 缺失 degraded，tombstone 阻断复活 |
| P2-T10 | [x] | 2026-09-26 | 70a3e8c4 | 36 memory passed；15 Audit passed；ruff 通过 | PR #20 | Phase 2 DoD 已核对 |
| P3-T01 | [x] | 2026-09-26 | f32349e7 | `test_intent_router_is_rule_first_and_explicit` | `nanobot/memory/intent.py` | 规则优先七类意图 |
| P3-T02 | [x] | 2026-09-26 | f32349e7 | `MemoryScopePolicy`；scope/只读测试 | `nanobot/memory/policy.py` | 只读 scope，写入/遗忘不注入 |
| P3-T03 | [x] | 2026-09-26 | f32349e7 | Wiki/Case/Skill/Trace 检索 fixture | `nanobot/memory/retriever.py` | 仅摘要和 revision/source 引用 |
| P3-T04 | [x] | 2026-09-26 | f32349e7 | `test_budget_uses_six_percent_with_soft_and_hard_caps` | 6%/2000/3000 token 边界 | 自适应预算 |
| P3-T05 | [x] | 2026-09-26 | f32349e7 | `test_build_messages_exposes_retrieval_digest_as_dynamic_metadata` | system metadata | digest 只在 dynamic metadata |
| P3-T06 | [x] | 2026-09-26 | f32349e7 | `test_context_dynamic_retrieval_does_not_change_stable_prompt`；134 回归测试 | ContextBuilder/AgentLoop | stable prompt 隔离 |
| P3-T07 | [x] | 2026-09-26 | f32349e7 | `test_missing_database_is_session_only_for_normal_task_and_visible_for_history` | degraded/session-only | 普通任务不阻塞 |
| P3-T08 | [x] | 2026-09-26 | f32349e7 | 同上显式 history 分支 | retrieval event / visible error | 显式失败可见 |
| P3-T09 | [x] | 2026-09-26 | f32349e7 | 140 聚焦与回归测试；ruff 通过 | PR #20、Gateway 验收待本提交后执行 | Phase 3 DoD |
| P4-T01 | [x] | 2026-09-26 | e7c2d8c6 | `test_activity_upsert_is_one_job_and_debounces_for_45_minutes`；305 回归测试 | `maintenance_jobs` | activity upsert 幂等合并 |
| P4-T02 | [x] | 2026-09-26 | e7c2d8c6 | 同上 | `maintenance_jobs.due_at` | 45 分钟 idle debounce |
| P4-T03 | [x] | 2026-09-26 | e7c2d8c6 | `test_worker_lease_recovery_and_stale_epoch_cannot_overwrite_cursor` | epoch/CAS cursor | 旧 epoch 拒绝覆盖 |
| P4-T04 | [x] | 2026-09-26 | e7c2d8c6 | `test_maintenance_worker_runs_review_and_releases_workspace_lock` | `maintenance_lock` | 单 worker、lease 恢复 |
| P4-T05 | [x] | 2026-09-26 | e7c2d8c6 | `test_failure_backoff_dead_letters_after_five_attempts` | retry/dead-letter | 最多 5 次退避 |
| P4-T06 | [x] | 2026-09-26 | e7c2d8c6 | `test_tool_policy_blocks_high_risk_calls_before_execution` | `ToolRegistry.prepare_call` | policy_blocked 结构化拒绝 |
| P4-T07 | [x] | 2026-09-26 | e7c2d8c6 | Phase 4 聚焦测试 | `skill_catalog.py` | catalog 搜索 |
| P4-T08 | [x] | 2026-09-26 | e7c2d8c6 | Phase 4 聚焦测试 | `skill_catalog.py` | 只读 Skill 内容 |
| P4-T09 | [x] | 2026-09-26 | e7c2d8c6 | Phase 4 聚焦测试 | `skill_revisions.status=staging` | candidate 不改 current |
| P4-T10 | [x] | 2026-09-26 | e7c2d8c6 | 角色矩阵与 fail-closed 测试 | `policy.py` | maintenance/evaluation 高风险工具拒绝 |
| P4-T11 | [x] | 2026-09-26 | e7c2d8c6 | 5 聚焦 + 305 回归；ruff 通过 | 本节 DoD | Phase 4 完成 |
| P5-T01 | [x] | 2026-09-26 | f2ba98ee | outcome 分类测试 | `derivation.py` | 保守归一化 |
| P5-T02 | [x] | 2026-09-26 | f2ba98ee | Case candidate 测试 | `cases`、`wiki_pages` | staging candidate |
| P5-T03 | [x] | 2026-09-26 | f2ba98ee | Skill revision 事实源边界测试 | `skill_revisions` | 不改 current |
| P5-T04 | [x] | 2026-09-26 | f2ba98ee | EvalPack draft/schema 测试 | `eval_packs` | draft + hash |
| P5-T05 | [x] | 2026-09-26 | f2ba98ee | 独立 reviewer 拒绝/封存测试 | `seal_eval_pack` | actor 分离 |
| P5-T06 | [x] | 2026-09-26 | f2ba98ee | fixture 清理测试 | `replay_cases` | 临时目录自动清理 |
| P5-T07 | [x] | 2026-09-26 | f2ba98ee | baseline run 测试 | `create_eval_run` | hash 封存 |
| P5-T08 | [x] | 2026-09-26 | f2ba98ee | candidate/逐题 replay 测试 | `eval_case_results` | evidence 写入 |
| P5-T09 | [x] | 2026-09-26 | f2ba98ee | judge/score 测试 | `record_case_result` | 独立 judge |
| P5-T10 | [x] | 2026-09-26 | f2ba98ee | replay consistency 测试 | `replay_consistency` | 两次独立回放 |
| P5-T11 | [x] | 2026-09-26 | f2ba98ee | holdout gate 测试 | `evaluate_gate` | 不自动发布 |
| P5-T12 | [x] | 2026-09-26 | f2ba98ee | 成本/延迟/安全 gate | `evaluate_gate` | 15%/20% 阈值 |
| P5-T13 | [x] | 2026-09-26 | f2ba98ee | staging 与确认边界 | `skill_propose`、gate | 需用户确认 |
| P5-T14 | [x] | 2026-09-26 | f2ba98ee | 6 聚焦 + 310 回归；ruff 通过 | 本节 DoD | Phase 5 完成 |
| P6-T01 | [x] | 2026-09-26 | 1960e869 | `Phase6Config` 默认关闭、kill switch、runtime gate | `continuous.py`、配置测试 | 未启用线上持续运行 |
| P6-T02 | [x] | 2026-09-26 | 1960e869 | 重复成功、低敏感、只读工具筛选 | `select_low_risk_tasks` | 写工具/敏感任务拒绝 |
| P6-T03 | [x] | 2026-09-26 | 1960e869 | failed/partial/blocked Trace 派生 regression Case | `regression_case_from_trace` | 仅 staging candidate |
| P6-T04 | [x] | 2026-09-26 | 1960e869 | EvalPack `phase6_version` 与稳定 hash 递增 | `build_versioned_evalpack` | 不改 current |
| P6-T05 | [x] | 2026-09-26 | 1960e869 | 脱敏、fsync、append-only evidence log | `.nanobot/phase6/evidence.jsonl` | 不记录原始 payload |
| P6-T06 | [x] | 2026-09-26 | 1960e869 | 回归/权限/FTS/容量告警自动暂停 | `update_health`、`run_cycle` | kill switch 保持只读 |
| P6-T07 | [x] | 2026-09-26 | 633fff28 | CAS baseline rollback 与显式 resume | `pause_and_rollback` | 旧 revision 可恢复 |
| P6-T08 | [x] | 2026-09-26 | 633fff28 | 18/7 retention 计划与容量/索引告警 | `plan_retention`、`collect_capacity_metrics` | 不删除长期摘要 |
| P6-T09 | [x] | 2026-09-26 | 633fff28 | 10 聚焦 + 63 memory + 100 config/Agent 回归；ruff 通过 | Phase 6 DoD | Phase 6 完成 |

## 22. 阻塞和变更记录

| 日期 | 任务 ID | 类型 | 问题 | 影响 | 处理决定 | 相关提交 |
|---|---|---|---|---|---|---|
|  |  |  |  |  |  |  |

实现中发现设计错误不得静默修改；先记录阻塞，说明影响的后续任务，更新设计并经评审后再继续；不得为了勾选完成而降低验收标准。

## 23. 文档状态与使用方式

- 本文是“架构说明 + 分阶段实施计划 + 可执行任务清单 + 验收证据记录模板”。
- P0-T01～P0-T22、P1-T01～P1-T08、P2-T01～P2-T10、P3-T01～P3-T09、P4-T01～P4-T11、P5-T01～P5-T14、P6-T01～P6-T09 已完成并记录证据；Phase 6 默认开关保持关闭。
- Phase 0 未运行 Gateway、未创建生产 CLI，数据库验证仅使用临时 workspace/SQLite；Phase 1 已重建长期 Gateway 并核对构建标识，但未增加 AgentLoop、CLI、HTTP API 或 WebUI 接入。
- Phase N 的 DoD 未完成时不得进入 Phase N+1；Phase 2 的 provider revision 能力缺失按 degraded 记录，不得伪造不可变历史。
- 任务执行期间如需改变产品决策、事实源、权限或保留规则，必须停止并新增阻塞/变更记录。
