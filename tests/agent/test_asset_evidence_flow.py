"""Complete user turns through AgentLoop, AgentRunner, SQLite facts and L1 projection."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import ToolResult
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest


def _tool(name, arguments):
    return LLMResponse(content="",tool_calls=[ToolCallRequest(id="call-"+name,name=name,arguments=arguments)],usage={})


def _loop(workspace, responses, **kwargs):
    provider=MagicMock()
    provider.get_default_model.return_value="test-model"
    provider.generation=GenerationSettings()
    provider.chat_with_retry=AsyncMock(side_effect=responses)
    config=Phase6Config(enabled=True,evolution=Phase6EvolutionConfig(
        asset_layer_enabled=True,asset_retrieval_enabled=False,
        stepwise_evidence_enabled=True,stepwise_evidence_mode="enforced",
    ))
    loop=AgentLoop(bus=MessageBus(),provider=provider,workspace=workspace,model="test-model",
                   phase6_config=config,restrict_to_workspace=True,**kwargs)
    loop._connect_mcp=AsyncMock()
    loop.consolidator.maybe_consolidate_by_tokens=AsyncMock(return_value=None)
    return loop


async def _send(loop, content):
    return await loop._process_message(InboundMessage(channel="cli",sender_id="user",chat_id="asset-test",content=content))


@pytest.mark.parametrize("name",["read_file","write_file"])
async def test_real_user_tool_turn_records_resource_and_risk_without_changing_answer(tmp_path,name):
    (tmp_path/"input.md").write_text("# fixture\n")
    args={"path":"input.md"} if name=="read_file" else {"path":"new.md","content":"# synthetic output\n"}
    loop=_loop(tmp_path,[_tool(name,args),LLMResponse(content="任务已完成",usage={})])
    try:
        answer=await _send(loop,"请查看工作区文档标题" if name=="read_file" else "请编写工作区测试文档并确认结果")
        assert answer.content=="任务已完成"
        connection=open_maintenance_db(tmp_path)
        asset=connection.execute("SELECT * FROM memory_assets").fetchone()
        assert asset is not None and asset["outcome"]=="success"
        assert asset["resource_class"]=="workspace-doc"
        assert asset["redaction_level"]==("normal" if name=="read_file" else "abstract")
        assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence").fetchone()[0]=="success"
        assert connection.execute("SELECT resource_class FROM evolution_step_evidence").fetchone()[0]=="workspace-doc"
        assert "synthetic output" not in str(dict(asset))
        assert connection.execute("SELECT count(*) FROM skill_proposals").fetchone()[0]==0
    finally:
        await loop.close_mcp()


async def test_runner_normalized_timeout_reaches_source_and_l1_retryability(tmp_path):
    loop=_loop(tmp_path,[_tool("web_search",{"query":"fixture"}),LLMResponse(content="查询工具超时",usage={})])
    loop.tools.prepare_call=MagicMock(return_value=(None,{"query":"fixture"},None))
    loop.tools.execute=AsyncMock(return_value=ToolResult.error(
        "Search timed out",error_type="TimeoutError",error_code="web_search_timeout",provider="duckduckgo",
    ))
    try:
        await _send(loop,"请查询测试资料并概括用途")
        connection=open_maintenance_db(tmp_path)
        row=connection.execute("SELECT error_code,retryability FROM evolution_step_evidence").fetchone()
        assert tuple(row)==("web_search_timeout","retryable")
        asset=connection.execute("SELECT * FROM memory_assets").fetchone()
        assert asset["outcome"]=="failure" and asset["error_class"]=="timeout"
        assert json.loads(asset["steps_json"])[0]["retryability"]=="retryable"
        assert connection.execute("SELECT failure_retryability FROM recovery_episodes").fetchone()[0]=="retryable"
    finally:
        await loop.close_mcp()


async def test_complete_failure_then_correction_refreshes_original_l1(tmp_path):
    (tmp_path/"correct.md").write_text("# Correct\n")
    loop=_loop(tmp_path,[_tool("read_file",{"path":"missing.md"}),LLMResponse(content="文件不存在",usage={}),
                         _tool("read_file",{"path":"correct.md"}),LLMResponse(content="标题为 Correct",usage={})])
    try:
        first=await _send(loop,"请查看 missing.md 的标题；不创建或修复文件，不替换目标")
        assert first.content=="文件不存在"
        connection=open_maintenance_db(tmp_path)
        old=connection.execute("SELECT * FROM memory_assets").fetchone()
        assert old["outcome"]=="failure"
        second=await _send(loop,"请重新查看正确路径 correct.md 的标题；不写入文件或配置")
        assert second.content=="标题为 Correct"
        updated=connection.execute("SELECT * FROM memory_assets WHERE asset_id=?",(old["asset_id"],)).fetchone()
        assert updated["outcome"]=="recovered" and len(json.loads(updated["source_trace_ids"]))==2
        assert connection.execute("SELECT count(*) FROM memory_assets").fetchone()[0]==2
        assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence WHERE task_id=?",(old["origin_id"],)).fetchone()[0]=="recovered"
    finally:
        await loop.close_mcp()


async def test_loop_level_error_after_successful_tool_is_failure(tmp_path):
    loop=_loop(tmp_path,[_tool("list_dir",{"path":"."}),LLMResponse(content="provider unavailable",finish_reason="error",error_kind="connection",usage={})])
    try:
        await _send(loop,"请查看工作区目录并概括内容")
        connection=open_maintenance_db(tmp_path)
        assert connection.execute("SELECT outcome FROM memory_assets").fetchone()[0]=="failure"
        assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence").fetchone()[0]=="failure"
    finally:
        await loop.close_mcp()


async def test_iteration_exhaustion_is_partial_with_no_fabricated_success(tmp_path):
    loop=_loop(tmp_path,[_tool("list_dir",{"path":"."})],max_iterations=1)
    try:
        await _send(loop,"请查看工作区目录并概括内容")
        connection=open_maintenance_db(tmp_path)
        assert connection.execute("SELECT outcome FROM memory_assets").fetchone()[0]=="partial"
        assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence").fetchone()[0]=="partial"
    finally:
        await loop.close_mcp()


async def test_malformed_resource_diagnostic_cannot_interrupt_normal_error_reply(tmp_path):
    loop=_loop(tmp_path,[_tool("web_fetch",{"url":"https://["}),LLMResponse(content="地址格式无效",usage={})])
    try:
        answer=await _send(loop,"请查看测试URL的内容")
        assert answer.content=="地址格式无效"
        connection=open_maintenance_db(tmp_path)
        assert connection.execute("SELECT resource_class FROM evolution_step_evidence").fetchone()[0]=="unknown"
        assert connection.execute("SELECT outcome FROM memory_assets").fetchone()[0]=="failure"
    finally:
        await loop.close_mcp()
