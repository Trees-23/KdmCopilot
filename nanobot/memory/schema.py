"""Phase 0 memory schema metadata and constants."""

from __future__ import annotations

import hashlib
from pathlib import Path

MIGRATION_VERSION = 13
MIGRATION_ID = "0013_evolution_candidate_staging"
APP_BUILD = "memory-phase1"

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
BASE_MIGRATION_PATH = MIGRATIONS_DIR / "0001_memory_base.sql"
PHASE6_MIGRATION_PATH = MIGRATIONS_DIR / "0002_phase6_proposals.sql"
DELIVERY_PAYLOAD_MIGRATION_PATH = MIGRATIONS_DIR / "0003_phase6_delivery_payload.sql"
ALERT_DELIVERY_MIGRATION_PATH = MIGRATIONS_DIR / "0004_phase6_alert_deliveries.sql"
PROPOSAL_REVIEW_MIGRATION_PATH = MIGRATIONS_DIR / "0005_proposal_review_details.sql"
SEMANTIC_QUALITY_MIGRATION_PATH = MIGRATIONS_DIR / "0006_semantic_quality_gate.sql"
RECOVERY_CASES_MIGRATION_PATH = MIGRATIONS_DIR / "0007_recovery_cases.sql"
AB_QUALITY_EVALUATIONS_MIGRATION_PATH = MIGRATIONS_DIR / "0008_ab_quality_evaluations.sql"
FAILURE_ISSUES_MIGRATION_PATH = MIGRATIONS_DIR / "0009_failure_issues.sql"
FAILURE_ISSUE_CANDIDATES_MIGRATION_PATH = MIGRATIONS_DIR / "0010_failure_issue_candidates.sql"
RECOVERY_SEMANTIC_FAMILIES_MIGRATION_PATH = MIGRATIONS_DIR / "0011_recovery_semantic_families.sql"
STEPWISE_EVOLUTION_EVIDENCE_MIGRATION_PATH = MIGRATIONS_DIR / "0012_stepwise_evolution_evidence.sql"
CANDIDATE_STAGING_MIGRATION_PATH = MIGRATIONS_DIR / "0013_evolution_candidate_staging.sql"


def migration_sql() -> str:
    """Return the checked-in latest migration SQL."""

    return CANDIDATE_STAGING_MIGRATION_PATH.read_text(encoding="utf-8")


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
        "phase6_alert_deliveries",
        "eval_cases",
        "semantic_task_evidence",
        "semantic_quality_reviews",
        "recovery_episodes",
        "recovery_case_reviews",
        "ab_evaluations",
        "ab_case_results",
        "failure_issues",
        "failure_issue_actions",
        "failure_issue_deliveries",
        "failure_issue_candidates",
        "evolution_task_evidence",
        "evolution_step_evidence",
        "evolution_task_links",
        "evolution_candidate_staging",
    }
)

BASE_REQUIRED_TABLES = REQUIRED_TABLES - frozenset(
    {
        "skill_proposals", "proposal_deliveries", "proposal_actions", "group_memory_scopes",
        "phase6_alert_deliveries", "eval_cases", "semantic_task_evidence",
        "semantic_quality_reviews", "recovery_episodes", "recovery_case_reviews", "ab_evaluations",
        "ab_case_results", "failure_issues", "failure_issue_actions", "failure_issue_deliveries",
        "failure_issue_candidates",
        "evolution_task_evidence", "evolution_step_evidence", "evolution_task_links",
        "evolution_candidate_staging",
    }
)

STEPWISE_EVOLUTION_TABLES = frozenset(
    {"evolution_task_evidence", "evolution_step_evidence", "evolution_task_links"}
)

CANDIDATE_STAGING_TABLES = frozenset({"evolution_candidate_staging"})
