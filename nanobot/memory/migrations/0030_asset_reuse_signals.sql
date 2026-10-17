-- Derived settlement lives with each immutable retrieval event. No historical backfill.
ALTER TABLE retrieval_events ADD COLUMN asset_workspace TEXT;
ALTER TABLE retrieval_events ADD COLUMN asset_settled_outcome TEXT
  CHECK(asset_settled_outcome IS NULL OR asset_settled_outcome IN ('success','failure','recovered','partial'));
CREATE INDEX retrieval_asset_settlement ON retrieval_events(asset_workspace,trace_id)
  WHERE asset_workspace IS NOT NULL;
