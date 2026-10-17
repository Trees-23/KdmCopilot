"""Complete Agent turns carry actual asset context and observable degraded metadata."""

import json

from nanobot.audit.runtime import AuditRuntime
from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.providers.base import LLMResponse
from tests.agent.test_asset_evidence_flow import _loop, _send, _tool
from tests.agent.test_loop_audit import RecordingEmitter
from tests.memory.test_asset_retrieval import _asset


def _enabled():
    return Phase6Config(enabled=True,evolution=Phase6EvolutionConfig(
        asset_layer_enabled=True,asset_retrieval_enabled=True,
    ))


async def test_real_agent_prompt_and_retrieval_event_share_injected_ids(tmp_path):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"known-doc")
    (tmp_path/"fixture.txt").write_text("verified document")
    loop=_loop(tmp_path,[_tool("read_file",{"path":"fixture.txt"}),LLMResponse(content="已核对文档",usage={})])
    loop.refresh_phase6_config(_enabled())
    try:
        reply=await _send(loop,"请查看 document fixture 并读取 fixture.txt")
        assert reply.content=="已核对文档"
        system=loop.provider.chat_with_retry.await_args_list[0].kwargs["messages"][0]
        assert "# Relevant Past Cases (read-only)" in system["content"]
        metadata=system["_meta"]["system_context_sections"]
        row=connection.execute("SELECT retrieval_id,trace_id,injected_ids_json,tokens FROM retrieval_events WHERE scopes_json='[\"asset\"]'").fetchone()
        assert row["retrieval_id"]==metadata["asset_retrieval_id"]
        assert json.loads(row["injected_ids_json"])==["known-doc"]
        assert row["tokens"]==metadata["asset_retrieval_tokens"]<=600
        assert connection.execute("SELECT actual_outcome FROM evolution_task_evidence WHERE task_id=?",("evolution-task:"+row["trace_id"],)).fetchone()[0]=="success"
    finally:
        await loop.close_mcp()


async def test_missing_database_does_not_block_reply_and_audit_contains_degraded(tmp_path):
    runtime=AuditRuntime.disabled()
    emitter=RecordingEmitter()
    runtime.emitter=emitter
    loop=_loop(tmp_path,[LLMResponse(content="对话继续正常",usage={})],audit_runtime=runtime)
    loop.refresh_phase6_config(_enabled())
    try:
        assert not (tmp_path/".nanobot/memory.sqlite3").exists()
        reply=await _send(loop,"请说明普通任务会继续处理")
        assert reply.content=="对话继续正常"
        payload=next(p for p in emitter.payloads if p and p.payload_kind=="model_request")
        assert payload.content.context_cache["asset_retrieval_outcome"]=="degraded"
        assert payload.content.context_cache["asset_retrieval_error_code"]=="asset_db_unavailable"
    finally:
        await loop.close_mcp()
