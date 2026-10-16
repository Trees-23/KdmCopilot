-- Batch provenance belongs to the derived asset and is removed with it in P6.
ALTER TABLE memory_assets ADD COLUMN lesson_input_version TEXT;
ALTER TABLE memory_assets ADD COLUMN lesson_source_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memory_assets ADD COLUMN lesson_output_digests_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memory_assets ADD COLUMN lesson_trace_id TEXT;
CREATE INDEX memory_assets_lesson_version ON memory_assets(workspace,family_key,lesson_input_version)
  WHERE asset_level='L2';
