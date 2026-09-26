"""Phase 0 memory schema metadata and constants."""

from __future__ import annotations

import hashlib
from pathlib import Path

MIGRATION_VERSION = 3
MIGRATION_ID = "0003_phase6_delivery_payload"
APP_BUILD = "memory-phase1"

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
BASE_MIGRATION_PATH = MIGRATIONS_DIR / "0001_memory_base.sql"
PHASE6_MIGRATION_PATH = MIGRATIONS_DIR / "0002_phase6_proposals.sql"
DELIVERY_PAYLOAD_MIGRATION_PATH = MIGRATIONS_DIR / "0003_phase6_delivery_payload.sql"


def migration_sql() -> str:
    """Return the checked-in latest migration SQL."""

    return DELIVERY_PAYLOAD_MIGRATION_PATH.read_text(encoding="utf-8")


def base_migration_sql() -> str:
    """Return the immutable Phase 0 migration SQL."""

    return BASE_MIGRATION_PATH.read_text(encoding="utf-8")


def schema_hash(sql: str | None = None) -> str:
    """Return the content hash used to detect a changed migration."""

    payload = (migration_sql() if sql is None else sql).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def phase6_migration_sql() -> str:
    """Return the Proposal tables migration (version 2)."""

    return PHASE6_MIGRATION_PATH.read_text(encoding="utf-8")


REQUIRED_TABLES = frozenset(
    {
        "schema_meta",
        "memory_records",
        "memory_revisions",
        "wiki_pages",
        "wiki_relations",
        "cases",
        "skills",
        "skill_revisions",
        "trace_index",
        "eval_packs",
        "eval_runs",
        "eval_case_results",
        "maintenance_jobs",
        "maintenance_lock",
        "tombstones",
        "retrieval_events",
        "memory_outbox",
        "skill_proposals",
        "proposal_deliveries",
        "proposal_actions",
        "group_memory_scopes",
    }
)

BASE_REQUIRED_TABLES = REQUIRED_TABLES - frozenset(
    {"skill_proposals", "proposal_deliveries", "proposal_actions", "group_memory_scopes"}
)
