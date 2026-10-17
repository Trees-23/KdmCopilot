-- L0 Markdown memory is independent and remains untouched.
UPDATE memory_records SET current_revision_id=NULL;
DROP TABLE memory_revisions;
DROP TABLE memory_records;
