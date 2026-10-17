-- L1/L2 asset schema only. Runtime data reset is a separate authorized operation.
CREATE TABLE memory_assets (
  -- ===== A. 身份与来源 =====
  asset_id          TEXT PRIMARY KEY,
  workspace         TEXT NOT NULL,
  asset_level       TEXT NOT NULL CHECK(asset_level IN ('L1','L2')),

  source_group      TEXT,                       -- 群标识，可空（§6.6.3）
  source_session    TEXT,                       -- session_key
  source_trace_ids  TEXT NOT NULL DEFAULT '[]',
  origin_table      TEXT NOT NULL,              -- 'evolution_task_evidence' 等
  origin_id         TEXT NOT NULL,
  replaces_asset_id TEXT,                       -- L2 取代旧 L2 时指向被取代者

  -- ===== B. 检索入口 =====
  task_goal         TEXT,                       -- 原始目标（脱敏后），L1 必填
  intent            TEXT,                       -- 意图分类
  resource_class    TEXT,                       -- 资源类别（非实例）
  operation_family  TEXT,                       -- 操作族
  trigger_condition TEXT,                       -- 何时该翻出这篇
  family_key        TEXT NOT NULL,              -- 聚合键（§6.5.1），两级别均需
  search_text       TEXT NOT NULL,              -- 归一化检索文本

  -- ===== C. 执行过程（仅 L1） =====
  steps_json        TEXT NOT NULL DEFAULT '[]', -- 结构化步骤数组
  step_count        INTEGER NOT NULL DEFAULT 0,
  tools_used        TEXT NOT NULL DEFAULT '[]', -- 工具类别（非原始名）

  -- ===== D. 错误与结果 =====
  outcome           TEXT NOT NULL CHECK(outcome IN ('success','failure','recovered','partial')),
  error_class       TEXT,
  error_root_cause  TEXT,                       -- 根因（L2 必填）
  resolution        TEXT,                       -- 解决方案（L2 必填）
  applicability_boundary TEXT,                  -- 不适用边界（L2 必填）

  -- ===== E. 复用信号 =====
  injection_count   INTEGER NOT NULL DEFAULT 0,
  last_injected_at  TEXT,
  post_injection_success_count INTEGER NOT NULL DEFAULT 0,
  post_injection_failure_count INTEGER NOT NULL DEFAULT 0,

  -- ===== F. 置信与脱敏 =====
  confidence_source TEXT CHECK(confidence_source IN ('confirmed','inferred')),
  redaction_level   TEXT NOT NULL DEFAULT 'normal'
                    CHECK(redaction_level IN ('normal','abstract','maximal')),
  visibility        TEXT NOT NULL DEFAULT 'shared'
                    CHECK(visibility IN ('private','shared')),

  -- ===== G. 生命周期 =====
  status            TEXT NOT NULL CHECK(status IN ('active','archived','tombstone')),
  expires_at        TEXT,                       -- L1 可过期；L2 必须为 NULL
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,

  UNIQUE(workspace, origin_table, origin_id),

  -- L1 必填项
  CHECK(asset_level <> 'L1' OR (
    task_goal   IS NOT NULL AND length(trim(task_goal)) > 0 AND
    family_key  IS NOT NULL AND length(trim(family_key)) > 0
  )),

  -- L2 必填项：根因、解决方案、触发条件、适用边界、置信来源
  CHECK(asset_level <> 'L2' OR (
    error_root_cause        IS NOT NULL AND length(trim(error_root_cause)) > 0 AND
    resolution              IS NOT NULL AND length(trim(resolution)) > 0 AND
    trigger_condition       IS NOT NULL AND length(trim(trigger_condition)) > 0 AND
    applicability_boundary  IS NOT NULL AND length(trim(applicability_boundary)) > 0 AND
    confidence_source       IS NOT NULL
  )),

  -- L2 是抽象经验，不含实例步骤，不过期
  CHECK(asset_level <> 'L2' OR (
    step_count = 0 AND
    steps_json = '[]' AND
    expires_at IS NULL
  )),

  -- L1 是事实记录，无置信度概念
  CHECK(asset_level <> 'L1' OR confidence_source IS NULL)
);

CREATE INDEX memory_assets_retrieval ON memory_assets(workspace, status, asset_level, updated_at DESC);
CREATE INDEX memory_assets_family    ON memory_assets(workspace, family_key, asset_level, status);
CREATE INDEX memory_assets_source    ON memory_assets(workspace, source_group, status, created_at DESC);
CREATE INDEX memory_assets_signal    ON memory_assets(workspace, asset_level, injection_count DESC, status);
CREATE INDEX memory_assets_expiry    ON memory_assets(workspace, status, expires_at);
