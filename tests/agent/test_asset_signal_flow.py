"""Actual Agent turns settle retrieval events using the same source outcome."""

import pytest

from nanobot.memory.maintenance import open_maintenance_db
from nanobot.providers.base import LLMResponse
from tests.agent.test_asset_context_flow import _enabled
from tests.agent.test_asset_evidence_flow import _loop, _send, _tool
from tests.memory.test_asset_retrieval import _asset
from tests.memory.test_asset_signals import _counts


async def test_failure_correction_and_duplicate_turn_callbacks_transfer_original_injection(tmp_path, monkeypatch):
    import nanobot.memory.turn_evidence as module

    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    (tmp_path / "correct.md").write_text("# Correct\n")
    captured = []
    original = module.capture_turn_evidence

    def capture(conn, workspace, ctx, evolution):
        captured.append((ctx, evolution))
        original(conn, workspace, ctx, evolution)

    monkeypatch.setattr(module, "capture_turn_evidence", capture)
    loop = _loop(tmp_path, [_tool("read_file", {"path": "missing.md"}), LLMResponse(content="文件不存在", usage={}),
                           _tool("read_file", {"path": "correct.md"}), LLMResponse(content="标题 Correct", usage={})])
    loop.refresh_phase6_config(_enabled())
    try:
        await _send(loop, "请查看 fixture missing.md 的标题；不创建或修复文件，不替换目标")
        assert _counts(connection) == (1, 0, 1)
        await _send(loop, "请重新查看 fixture 正确路径 correct.md 的标题；不写入文件或配置")
        assert _counts(connection) == (2, 2, 0)
        for ctx, evolution in captured:
            original(connection, str(tmp_path), ctx, evolution)
        assert _counts(connection) == (2, 2, 0)
        outcomes = [r[0] for r in connection.execute("SELECT asset_settled_outcome FROM retrieval_events WHERE asset_workspace IS NOT NULL ORDER BY created_at")]
        assert outcomes == ["recovered", "success"]
        assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0] == 0
    finally:
        await loop.close_mcp()


@pytest.mark.parametrize("responses,iterations,expected", [
    ([_tool("list_dir", {"path": "."}), LLMResponse(content="成功", usage={})], 40, (1, 1, 0)),
    ([_tool("list_dir", {"path": "."})], 1, (1, 0, 0)),
    ([LLMResponse(content="直接回答", usage={})], 40, (1, 0, 0)),
    ([_tool("list_dir", {"path": "."}), LLMResponse(content="模型失败", finish_reason="error", error_kind="connection", usage={})], 40, (1, 0, 1)),
])
async def test_full_agent_success_partial_no_evidence_and_loop_failure(tmp_path, responses, iterations, expected):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    loop = _loop(tmp_path, responses, max_iterations=iterations)
    loop.refresh_phase6_config(_enabled())
    try:
        answer = await _send(loop, "请查看 fixture 工作区信息并概括")
        assert answer.content
        assert _counts(connection) == expected
    finally:
        await loop.close_mcp()


async def test_settlement_write_failure_keeps_answer_and_counters_consistent(tmp_path):
    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    connection.execute("CREATE TRIGGER reject_result BEFORE UPDATE OF post_injection_success_count ON memory_assets BEGIN SELECT RAISE(ABORT,'result failed'); END")
    connection.commit()
    loop = _loop(tmp_path, [_tool("list_dir", {"path": "."}), LLMResponse(content="回答仍然成功", usage={})])
    loop.refresh_phase6_config(_enabled())
    try:
        answer = await _send(loop, "请查看 fixture 工作区信息并概括")
        assert answer.content == "回答仍然成功"
        assert _counts(connection) == (1, 0, 0)
        assert connection.execute("SELECT asset_settled_outcome FROM retrieval_events WHERE asset_workspace IS NOT NULL").fetchone()[0] is None
    finally:
        await loop.close_mcp()


async def test_retry_after_persisted_correction_can_finish_original_failed_settlement(tmp_path, monkeypatch):
    import nanobot.memory.turn_evidence as module

    connection = open_maintenance_db(tmp_path)
    _asset(connection, tmp_path, "known")
    (tmp_path / "correct.md").write_text("# Correct\n")
    captured = []
    original = module.capture_turn_evidence

    def capture(conn, workspace, ctx, evolution):
        captured.append((ctx, evolution))
        original(conn, workspace, ctx, evolution)

    monkeypatch.setattr(module, "capture_turn_evidence", capture)
    loop = _loop(tmp_path, [_tool("read_file", {"path": "missing.md"}), LLMResponse(content="文件不存在", usage={}),
                           _tool("read_file", {"path": "correct.md"}), LLMResponse(content="标题 Correct", usage={})])
    loop.refresh_phase6_config(_enabled())
    try:
        await _send(loop, "请查看 fixture missing.md 的标题；不创建或修复文件，不替换目标")
        connection.execute("CREATE TRIGGER reject_correction BEFORE UPDATE OF asset_settled_outcome ON retrieval_events WHEN NEW.asset_settled_outcome='recovered' BEGIN SELECT RAISE(ABORT,'correction failed'); END")
        connection.commit()
        answer = await _send(loop, "请重新查看 fixture 正确路径 correct.md 的标题；不写入文件或配置")
        assert answer.content == "标题 Correct"
        assert _counts(connection) == (2, 1, 1)
        assert connection.execute("SELECT status FROM recovery_episodes").fetchone()[0] == "recovered"
        connection.execute("DROP TRIGGER reject_correction")
        connection.commit()
        ctx, evolution = captured[-1]
        original(connection, str(tmp_path), ctx, evolution)
        original(connection, str(tmp_path), ctx, evolution)
        assert _counts(connection) == (2, 2, 0)
    finally:
        await loop.close_mcp()
