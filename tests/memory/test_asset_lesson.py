"""Thresholds, deterministic input versions, model evidence and atomic L2 publication."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from nanobot.audit.runtime import AuditRuntime
from nanobot.config.schema import Phase6EvolutionConfig
from nanobot.memory.asset_lesson import (
    ASSET_LESSON_JOB_ID,
    _batch,
    register_asset_lesson_job,
    run_asset_lesson_scan,
)
from nanobot.memory.asset_retriever import retrieve_asset_context
from nanobot.memory.lock import acquire_lock
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.providers.base import LLMResponse
from tests.agent.test_loop_audit import RecordingEmitter
from tests.memory.test_asset_retrieval import _asset


def _config(**kwargs):
    return SimpleNamespace(kill_switch=False, evolution=Phase6EvolutionConfig(
        asset_layer_enabled=True, lesson_abstraction_enabled=True, **kwargs,
    ))


def _fixtures(connection, workspace, count=3, **kwargs):
    for i in range(count):
        _asset(connection, workspace, f"l1:{i}", **kwargs)


def _provider(connection, workspace, *, transform=None, content=None):
    batch = _batch(connection, str(workspace), "fixture-family", 3)
    root_cause = batch.root_causes[0] if batch else "案例记录了该类操作成功，未提供额外根因证据"
    fields = {"error_root_cause": root_cause, "resolution": "先核对目标类别再执行只读检查", "trigger_condition": "需要读取工作区文档时",
              "applicability_boundary": "仅适用于工作区文档，不含远程资源", "supporting_asset_ids": ["l1:0"]}
    if transform:
        fields = transform(fields)
    response = LLMResponse(content=content or json.dumps(fields, ensure_ascii=False), usage={})
    return SimpleNamespace(chat_with_retry=AsyncMock(return_value=response))


async def _scan(workspace, provider, **kwargs):
    return await run_asset_lesson_scan(workspace, _config(), provider=provider, model="test-model", **kwargs)


async def test_threshold_and_existing_l2_do_not_inflate_l1_count(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path, 2)
    _asset(connection, tmp_path, "existing", level="L2")
    provider = _provider(connection, tmp_path)
    result = await _scan(tmp_path, provider)
    assert result["created"] == []
    provider.chat_with_retry.assert_not_awaited()


async def test_offline_two_samples_publish_complete_inferred_l2_and_never_repeat_unchanged_input(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    provider = _provider(connection, tmp_path)
    runtime = AuditRuntime.disabled()
    emitter = RecordingEmitter()
    runtime.emitter = emitter
    result = await _scan(tmp_path, provider, audit_runtime=runtime)
    assert len(result["created"]) == 1 and provider.chat_with_retry.await_count == 2
    row = connection.execute("SELECT * FROM memory_assets WHERE asset_level='L2'").fetchone()
    assert row["confidence_source"] == "inferred" and row["origin_table"] == "memory_assets"
    assert len(json.loads(row["lesson_source_ids_json"])) == 3
    assert len(json.loads(row["lesson_output_digests_json"])) == 2
    assert row["lesson_trace_id"] == result["traces"][0]
    assert row["steps_json"] == "[]" and row["step_count"] == 0 and row["expires_at"] is None
    for name in ("error_root_cause", "resolution", "trigger_condition", "applicability_boundary", "lesson_input_version"):
        assert row[name]
    for call in provider.chat_with_retry.await_args_list:
        assert call.kwargs["temperature"] == 0 and call.kwargs["tools"] is None
        assert "fixture" not in call.kwargs["messages"][1]["content"]
    payloads = [p for p in emitter.payloads if p and p.payload_kind == "model_request"]
    assert len(payloads) == 2 and all(p.content.context_cache["lesson_input_version"] == row["lesson_input_version"] for p in payloads)
    assert not (await _scan(tmp_path, provider))["created"]
    connection.execute("UPDATE memory_assets SET injection_count=40,updated_at='changed' WHERE asset_level='L1'")
    connection.commit()
    assert not (await _scan(tmp_path, provider))["created"]
    assert provider.chat_with_retry.await_count == 2
    assert not connection.execute("SELECT * FROM maintenance_lock").fetchall()
    context = retrieve_asset_context("workspace-doc", workspace=tmp_path, session_key="s", trace_id="trace", turn_id="turn")
    assert context.hits[0].asset_level == "L2" and context.tokens <= 600


@pytest.mark.parametrize("outcome,error", [("failure", "not-found"), ("recovered", "not-found"), ("partial", "timeout"), ("failure", "unknown")])
async def test_failure_families_require_known_error_categories(tmp_path, outcome, error):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path, outcome=outcome, error_class=error)
    provider = _provider(connection, tmp_path)
    result = await _scan(tmp_path, provider)
    assert len(result["created"]) == (0 if error == "unknown" else 1)


async def test_new_evidence_replaces_active_lesson_atomically_and_changed_outputs_remain_inferred(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    provider = _provider(connection, tmp_path)
    first = (await _scan(tmp_path, provider))["created"][0]
    _asset(connection, tmp_path, "fresh")
    response = provider.chat_with_retry.return_value
    changed = json.loads(response.content)
    changed["resolution"] = "先确认资源类别并报告只读检查结果"
    provider.chat_with_retry.side_effect = [response, LLMResponse(content=json.dumps(changed), usage={})]
    result = await _scan(tmp_path, provider)
    assert len(result["created"]) == 1
    old = connection.execute("SELECT status FROM memory_assets WHERE asset_id=?", (first,)).fetchone()
    new = connection.execute("SELECT * FROM memory_assets WHERE asset_id=?", (result["created"][0],)).fetchone()
    assert old[0] == "archived" and new["replaces_asset_id"] == first
    assert new["confidence_source"] == "inferred"
    assert len(set(json.loads(new["lesson_output_digests_json"]))) == 2
    assert connection.execute("SELECT count(*) FROM memory_assets WHERE asset_level='L2' AND status='active'").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    lambda f: {**f, "error_root_cause": "确认由于权限不足"},
    lambda f: {**f, "supporting_asset_ids": ["foreign"]},
    lambda f: {**f, "resolution": "读取 /private/secret.txt"},
    lambda f: {**f, "resolution": ""},
    lambda f: {**f, "unexpected": "extra"},
])
async def test_invalid_abstraction_preserves_previous_active_lesson_and_can_retry(tmp_path, change):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    first = (await _scan(tmp_path, _provider(connection, tmp_path)))["created"][0]
    _asset(connection, tmp_path, "new")
    result = await _scan(tmp_path, _provider(connection, tmp_path, transform=change))
    assert result["failed"] == 1 and result["created"] == []
    assert connection.execute("SELECT status FROM memory_assets WHERE asset_id=?", (first,)).fetchone()[0] == "active"
    assert len((await _scan(tmp_path, _provider(connection, tmp_path)))["created"]) == 1


async def test_timeout_and_database_failure_preserve_previous_version(tmp_path, monkeypatch):
    import nanobot.memory.asset_lesson as module

    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    first = (await _scan(tmp_path, _provider(connection, tmp_path)))["created"][0]
    _asset(connection, tmp_path, "new")
    provider = _provider(connection, tmp_path)

    async def slow(**kwargs):
        await asyncio.Event().wait()

    provider.chat_with_retry.side_effect = slow
    monkeypatch.setattr(module, "LESSON_TIMEOUT_SECONDS", 0.02)
    assert (await _scan(tmp_path, provider))["failed"] == 1
    assert not connection.execute("SELECT * FROM maintenance_lock").fetchall()
    connection.execute("CREATE TRIGGER reject_lesson BEFORE INSERT ON memory_assets WHEN NEW.asset_level='L2' BEGIN SELECT RAISE(ABORT,'write failed'); END")
    connection.commit()
    monkeypatch.setattr(module, "LESSON_TIMEOUT_SECONDS", 180)
    assert (await _scan(tmp_path, _provider(connection, tmp_path)))["failed"] == 1
    assert connection.execute("SELECT status FROM memory_assets WHERE asset_id=?", (first,)).fetchone()[0] == "active"


@pytest.mark.parametrize("mutation", ["facts", "lease", "disabled"])
async def test_publish_checks_facts_lease_and_current_gate_after_model_call(tmp_path, mutation):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    provider = _provider(connection, tmp_path)
    response = provider.chat_with_retry.return_value
    config = _config()

    async def mutate(**kwargs):
        if mutation == "facts":
            connection.execute("UPDATE memory_assets SET outcome='partial' WHERE asset_id='l1:0'")
        elif mutation == "lease":
            connection.execute("UPDATE maintenance_lock SET generation=generation+1")
        else:
            config.kill_switch = True
        connection.commit()
        return response

    provider.chat_with_retry.side_effect = mutate
    result = await run_asset_lesson_scan(tmp_path, config, provider=provider, model="test")
    assert not result["created"]
    assert connection.execute("SELECT count(*) FROM memory_assets WHERE asset_level='L2'").fetchone()[0] == 0


async def test_existing_maintenance_lease_prevents_duplicate_model_calls(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    assert acquire_lock(connection, str(tmp_path), "another-worker", lease_seconds=300)
    provider = _provider(connection, tmp_path)
    assert (await _scan(tmp_path, provider))["skipped"] == 1
    provider.chat_with_retry.assert_not_awaited()


def test_new_config_dependency_and_independent_protected_cron():
    with pytest.raises(ValidationError):
        Phase6EvolutionConfig(lesson_abstraction_enabled=True)
    cron = SimpleNamespace(register_system_job=lambda job: jobs.append(job))
    jobs = []
    assert register_asset_lesson_job(cron, _config())
    assert jobs[0].id == ASSET_LESSON_JOB_ID and jobs[0].payload.kind == "system_event"
    assert jobs[0].schedule.every_ms == 60_000
    disabled = SimpleNamespace(evolution=Phase6EvolutionConfig())
    assert not register_asset_lesson_job(cron, disabled)


async def test_archive_failure_rolls_back_new_version_and_another_family_still_runs(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _fixtures(connection, tmp_path)
    first = (await _scan(tmp_path, _provider(connection, tmp_path)))["created"][0]
    _asset(connection, tmp_path, "new")
    for i in range(3):
        _asset(connection, tmp_path, "other:" + str(i), family_key="other-family")
    connection.execute("CREATE TRIGGER reject_archive BEFORE UPDATE OF status ON memory_assets WHEN NEW.status='archived' BEGIN SELECT RAISE(ABORT,'archive failed'); END")
    connection.commit()
    result = await _scan(tmp_path, _provider(connection, tmp_path))
    # The other family's references must come from that family, too. Supply a
    # valid family-specific response on retry after checking the first rollback.
    assert result["failed"] >= 1
    assert connection.execute("SELECT status FROM memory_assets WHERE asset_id=?", (first,)).fetchone()[0] == "active"
    assert connection.execute("SELECT count(*) FROM memory_assets WHERE asset_level='L2'").fetchone()[0] == 1
    provider = _provider(connection, tmp_path)
    original = provider.chat_with_retry.return_value
    other = json.loads(original.content)
    other["supporting_asset_ids"] = ["other:0"]
    provider.chat_with_retry.side_effect = [original, original, LLMResponse(content=json.dumps(other), usage={}), LLMResponse(content=json.dumps(other), usage={})]
    result = await _scan(tmp_path, provider)
    assert result["failed"] == 1 and len(result["created"]) == 1
    assert connection.execute("SELECT status FROM memory_assets WHERE asset_id=?", (first,)).fetchone()[0] == "active"
