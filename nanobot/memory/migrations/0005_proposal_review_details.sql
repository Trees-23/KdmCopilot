ALTER TABLE skill_proposals ADD COLUMN display_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS skill_proposals_display_id ON skill_proposals(display_id);
UPDATE skill_proposals
SET display_id = 'proposal-' || substr(
  lower(replace(replace(replace(proposal_id, 'phase6-proposal:', ''), 'prop-', ''), '-', '')),
  1, 12
)
WHERE display_id IS NULL;

CREATE TABLE IF NOT EXISTS eval_cases (
  eval_pack_id TEXT NOT NULL REFERENCES eval_packs(eval_pack_id) ON DELETE CASCADE,
  case_key TEXT NOT NULL,
  prompt TEXT NOT NULL,
  expected TEXT NOT NULL,
  split TEXT NOT NULL CHECK(split IN ('train','validation','holdout')),
  source_case_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(eval_pack_id, case_key)
);
CREATE INDEX IF NOT EXISTS eval_cases_pack_split ON eval_cases(eval_pack_id, split, case_key);
