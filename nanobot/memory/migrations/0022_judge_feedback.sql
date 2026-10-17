-- Optional per-case diagnostics never change the evaluation verdict.
ALTER TABLE ab_case_results ADD COLUMN judge_feedback TEXT NOT NULL DEFAULT '';
