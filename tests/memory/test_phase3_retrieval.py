from __future__ import annotations

import sqlite3

from nanobot.agent.context import ContextBuilder
from nanobot.memory.intent import IntentRouter
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.policy import MemoryScopePolicy
from nanobot.memory.retriever import HARD_TOKEN_BUDGET, SOFT_TOKEN_BUDGET, MemoryRetriever


def _workspace_db(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".nanobot").mkdir()
    connection = sqlite3.connect(workspace / ".nanobot" / "memory.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_migrations(connection)
    return workspace, connection


def test_intent_router_is_rule_first_and_explicit():
    router = IntentRouter()
    assert router.classify("请查一下之前的历史对话").intent == "history_query"
    assert router.classify("show me the latest trace").scopes == ("trace",)
    assert router.classify("please remember this decision").intent == "memory_write"
    assert router.classify("normal coding task").intent == "task"
    assert MemoryScopePolicy().for_intent(router.classify("review the case")).allows("asset")
    assert not MemoryScopePolicy().for_intent(router.classify("review the case")).allows("case")


def test_budget_uses_six_percent_with_soft_and_hard_caps():
    assert MemoryRetriever._budgets(10_000) == (600, 600)
    assert MemoryRetriever._budgets(100_000) == (SOFT_TOKEN_BUDGET, HARD_TOKEN_BUDGET)


def _skill(connection):
    connection.execute(
        "INSERT INTO skills(skill_id,name,description,source_kind,status,current_revision_id,source_path,created_at,updated_at) "
        "VALUES('skill:deploy','Deploy guide','Use deploy safely','workspace','active',NULL,'skills/deploy','now','now')"
    )
    connection.execute(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES('rev-1','skill:deploy','1','sha256:fixture','safe deploy instructions','user','now')"
    )
    connection.execute("UPDATE skills SET current_revision_id='rev-1' WHERE skill_id='skill:deploy'")
    connection.commit()


def test_retriever_returns_revision_referenced_skill_and_records_event(tmp_path):
    workspace, connection = _workspace_db(tmp_path)
    _skill(connection)
    connection.close()
    result = MemoryRetriever().retrieve(
        'deploy', session_key='websocket:1', workspace=workspace, trace_id='trace-1', turn_id='turn-1',
    )
    assert result.outcome == 'indexed' and result.hits[0].revision_id == 'rev-1'
    assert result.hits[0].source_ref == 'skills/deploy'
    assert 'revision=rev-1' in result.render()
    check = sqlite3.connect(workspace / '.nanobot/memory.sqlite3')
    assert check.execute('SELECT intent,outcome,trace_id FROM retrieval_events').fetchone() == ('task', 'indexed', 'trace-1')
    check.close()


def test_legacy_wiki_rows_are_not_retrieved_and_tables_remain(tmp_path):
    workspace, connection = _workspace_db(tmp_path)
    connection.execute(
        """INSERT INTO wiki_pages
        (page_id,namespace,slug,page_type,source_path,content_hash,current_revision_id,status,
         title,summary,created_at,updated_at)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("page-1", "workspace", "deploy", "fact", "wiki://deploy", "sha256:x", "rev-1", "candidate",
         "Deploy guide", "Use deploy safely", "2026-09-26T00:00:00+00:00", "2026-09-26T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    result = MemoryRetriever().retrieve("deploy", session_key="s", workspace=workspace)
    assert result.hits == ()
    assert result.hard_budget <= HARD_TOKEN_BUDGET
    check = sqlite3.connect(workspace / '.nanobot/memory.sqlite3')
    assert check.execute('SELECT count(*) FROM wiki_pages').fetchone()[0] == 1
    check.close()
    assert 'wiki' not in MemoryScopePolicy.normalize(('wiki', 'asset'))


def test_context_dynamic_retrieval_does_not_change_stable_prompt(tmp_path):
    workspace, connection = _workspace_db(tmp_path)
    _skill(connection)
    connection.close()
    builder = ContextBuilder(workspace)
    first = builder.build_system_sections(
        session_key="s", memory_query="deploy", workspace=workspace
    )
    assert "rev-1" in first.dynamic
    connection = sqlite3.connect(workspace / ".nanobot" / "memory.sqlite3")
    connection.execute("UPDATE skills SET description='Updated deploy guide'")
    connection.commit()
    connection.close()
    second = builder.build_system_sections(
        session_key="s", memory_query="deploy", workspace=workspace
    )
    assert first.stable == second.stable
    assert first.dynamic != second.dynamic


def test_build_messages_exposes_retrieval_digest_as_dynamic_metadata(tmp_path):
    workspace, connection = _workspace_db(tmp_path)
    _skill(connection)
    connection.close()
    messages = ContextBuilder(workspace).build_messages(
        [], "show deploy information", session_key="s", workspace=workspace,
    )
    metadata = messages[0]["_meta"]["system_context_sections"]
    assert metadata["retrieval_intent"] == "task"
    assert metadata["retrieval_digest"].startswith("sha256:")
    assert "revision=rev-1" in messages[0]["content"]


def test_missing_database_is_session_only_for_normal_task_and_visible_for_history(tmp_path):
    builder = ContextBuilder(tmp_path)
    normal = builder.build_system_sections(session_key="s", memory_query="normal task", workspace=tmp_path)
    assert "Memory retrieval unavailable" not in normal.dynamic
    history = builder.build_system_sections(session_key="s", memory_query="show history", workspace=tmp_path)
    assert "memory_db_unavailable" in history.dynamic
