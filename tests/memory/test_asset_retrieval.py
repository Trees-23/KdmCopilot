"""Asset-only retrieval, complete context budgets, scope removal and observable degradation."""

import json
import sqlite3
from dataclasses import replace

import pytest

from nanobot.agent.context import ContextBuilder, model_request_context_cache_metadata
from nanobot.config.schema import Phase6EvolutionConfig
from nanobot.memory.asset_render import fit_asset_budget
from nanobot.memory.asset_retriever import AssetHit, SQLiteAssetRetriever, retrieve_asset_context
from nanobot.memory.intent import IntentRouter
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.policy import MemoryScopePolicy
from nanobot.memory.retriever import MemoryRetriever
from nanobot.utils.helpers import estimate_message_tokens


def _asset(connection, workspace, name, *, level="L1", redaction="normal", status="active", text="document fixture", **extra):
    values={"asset_id":name,"workspace":str(workspace),"asset_level":level,"origin_table":"fixture","origin_id":name,
            "task_goal":text,"intent":"查询信息","resource_class":"workspace-doc","operation_family":"read-only-inspect",
            "trigger_condition":"document fixture","family_key":"fixture-family","search_text":text,
            "steps_json":json.dumps([{"operation_family":"read-only-inspect","resource_class":"workspace-doc","status":"ok"}]),
            "step_count":1,"tools_used":'["filesystem-read"]',"outcome":"success","redaction_level":redaction,
            "status":status,"created_at":"2026-10-16T00:00:00+00:00","updated_at":"2026-10-16T00:00:00+00:00"}
    if level=="L2":
        values.update(steps_json="[]",step_count=0,confidence_source="inferred",error_root_cause="resource needs checking",
                      resolution="verify category first",applicability_boundary="workspace documents only")
    values.update(extra)
    connection.execute("INSERT INTO memory_assets("+",".join(values)+") VALUES("+",".join("?" for _ in values)+")",tuple(values.values()))
    connection.commit()


def _config(**kwargs):
    return Phase6EvolutionConfig(asset_layer_enabled=True,asset_retrieval_enabled=True,**kwargs)


def test_assets_are_independent_of_case_wiki_or_legacy_memory_tables(tmp_path):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"case")
    for name in ("cases","wiki_relations","wiki_pages","memory_revisions","memory_records","trace_index"):
        connection.execute("DROP TABLE IF EXISTS "+name)
    connection.commit()
    result=retrieve_asset_context("fixture",workspace=tmp_path,session_key="s",trace_id="trace",turn_id="turn")
    assert result.outcome=="indexed" and result.injected_ids==("case",)
    assert "read-only" in result.text
    row=connection.execute("SELECT scopes_json,injected_ids_json,tokens,trace_id FROM retrieval_events").fetchone()
    assert tuple(row)==('["asset"]','["case"]',result.tokens,"trace")
    assert result.tokens==estimate_message_tokens({"role":"system","content":result.text})<=600


def test_level_preference_count_caps_and_no_source_group_in_prompt(tmp_path):
    connection=open_maintenance_db(tmp_path)
    for i in range(5):
        _asset(connection,tmp_path,"l1:"+str(i),source_group="private-group")
    for i in range(4):
        _asset(connection,tmp_path,"l2:"+str(i),level="L2",source_group="private-group")
    hits=SQLiteAssetRetriever().retrieve("fixture",workspace=str(tmp_path),limit=20)
    rendered=fit_asset_budget(hits)
    levels=[hit.asset_level for hit in rendered.hits]
    assert levels==sorted(levels,reverse=True)
    assert 1 <= levels.count("L2") <=2 and levels.count("L1")<=3
    assert "private-group" not in rendered.text
    assert "适用：" in rendered.text and "根因：" in rendered.text and "边界：" in rendered.text
    assert "离线推断" in rendered.text
    assert rendered.tokens<=600


def test_rendered_metadata_contains_only_records_that_fit_independent_budget(tmp_path):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"short")
    _asset(connection,tmp_path,"long",text="document fixture "+"long "*2000)
    result=retrieve_asset_context("fixture",workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn",token_budget=180)
    assert result.injected_ids==("short",)
    assert result.tokens<=180
    ids=json.loads(connection.execute("SELECT injected_ids_json FROM retrieval_events").fetchone()[0])
    assert ids==["short"]
    assert MemoryRetriever._budgets(100_000)==(2000,3000)


def test_maximal_and_abstract_never_render_goal_or_step_payloads(tmp_path):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"max",redaction="maximal",text="credential fixture goal",tools_used='["process-exec"]',
           steps_json='[{"input_summary":"raw command secret"}]')
    _asset(connection,tmp_path,"abstract",redaction="abstract",text="credential fixture goal",
           steps_json='[{"input_summary":"raw file body"}]')
    result=retrieve_asset_context("fixture",workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn")
    assert result.hits and "credential fixture goal" not in result.text
    assert "raw command" not in result.text and "raw file body" not in result.text
    maximal=next(hit for hit in result.hits if hit.asset_id=="max")
    assert "过程" not in maximal.body and "目标" not in maximal.body
    assert "结果：" in maximal.body and "操作类别：" in maximal.body


def test_only_current_workspace_active_shared_assets_match(tmp_path):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"active")
    _asset(connection,tmp_path,"archived",status="archived")
    _asset(connection,tmp_path,"private",visibility="private")
    _asset(connection,tmp_path/"other","other")
    result=SQLiteAssetRetriever().retrieve("fixture",workspace=str(tmp_path))
    assert [hit.asset_id for hit in result]==["active"]
    assert SQLiteAssetRetriever().retrieve("unrelated-ocean-geology",workspace=str(tmp_path))==()


def test_asset_and_case_scopes_are_distinct_and_case_is_removed(tmp_path):
    router=IntentRouter()
    assert MemoryScopePolicy.normalize(("case","cases","asset","assets"))==("asset",)
    for query in ("ordinary task","review the case","previous conversation history"):
        scope=MemoryScopePolicy().for_intent(router.classify(query))
        assert "case" not in scope.scopes and "cases" not in scope.scopes
        assert "asset" in scope.scopes
    connection=open_maintenance_db(tmp_path)
    connection.execute("INSERT INTO wiki_pages(page_id,namespace,slug,page_type,source_path,content_hash,status,title,summary,created_at,updated_at) "
                       "VALUES('old-case','workspace','case','case','derived://case','digest','candidate','case fixture','case fixture','before','before')")
    connection.commit()
    assert not MemoryRetriever().retrieve("case fixture",workspace=tmp_path).hits
    result=retrieve_asset_context("case fixture",workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn")
    assert result.outcome=="empty"
    for query in ("forget this case","remember this case"):
        result=retrieve_asset_context(query,workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn")
        assert result.outcome=="skipped" and not result.text


def test_missing_or_corrupt_database_degrades_without_creating_one(tmp_path):
    result=retrieve_asset_context("normal task",workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn")
    assert result.outcome=="degraded" and result.error_code=="asset_db_unavailable"
    assert not (tmp_path/".nanobot").exists()
    (tmp_path/".nanobot").mkdir()
    (tmp_path/".nanobot/memory.sqlite3").write_bytes(b"not a sqlite database")
    result=retrieve_asset_context("normal task",workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn")
    assert result.outcome=="degraded" and not result.text
    assert (tmp_path/".nanobot/memory.sqlite3").read_bytes()==b"not a sqlite database"


def test_context_separates_asset_budget_and_emits_audit_cache_metadata(tmp_path):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"observed")
    builder=ContextBuilder(tmp_path,asset_config=_config())
    messages=builder.build_messages([],"document fixture",session_key="session",trace_id="trace",turn_id="turn")
    assert "# Relevant Past Cases (read-only)" in messages[0]["content"]
    metadata=model_request_context_cache_metadata(messages,[])
    assert metadata["asset_retrieval_outcome"]=="indexed" and metadata["asset_retrieval_tokens"]<=600
    assert metadata["asset_retrieval_id"]
    assert "asset" not in json.loads(connection.execute("SELECT scopes_json FROM retrieval_events WHERE retrieval_id!=?",(metadata["asset_retrieval_id"],)).fetchone()[0])
    stable=builder.build_system_sections().stable
    builder.asset_config=Phase6EvolutionConfig(asset_layer_enabled=True,asset_retrieval_enabled=False)
    shadow=builder.build_system_sections(memory_query="document fixture",session_key="session")
    assert shadow.stable==stable and shadow.asset_retrieval is None
    assert "# Relevant Past Cases" not in shadow.dynamic


def test_degraded_assets_remain_observable_when_the_database_cannot_record_events(tmp_path):
    builder=ContextBuilder(tmp_path,asset_config=_config())
    messages=builder.build_messages([],"normal task",session_key="s",trace_id="t",turn_id="turn")
    metadata=model_request_context_cache_metadata(messages,[])
    assert metadata["asset_retrieval_outcome"]=="degraded"
    assert metadata["asset_retrieval_error_code"]=="asset_db_unavailable"
    assert "Asset retrieval unavailable" not in messages[0]["content"]
    explicit=builder.build_system_sections(memory_query="review fixture",session_key="s")
    assert "Asset retrieval unavailable" in explicit.dynamic


def test_asset_retrieval_failure_never_interrupts_context_build(tmp_path,monkeypatch):
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"valid")
    def broken(*args,**kwargs):
        raise sqlite3.OperationalError("query failure")
    monkeypatch.setattr(SQLiteAssetRetriever,"retrieve",broken)
    sections=ContextBuilder(tmp_path,asset_config=_config()).build_system_sections(memory_query="fixture",session_key="s")
    assert sections.asset_retrieval.outcome=="degraded"
    assert not sections.asset_retrieval.hits and "# Relevant Past Cases" not in sections.dynamic


def test_budget_rejects_invalid_limits_and_does_not_trim_boundary_fields():
    hit=AssetHit("l2","L2","fixture","边界："+"bounded "*2000,1.0,None)
    assert not fit_asset_budget((hit,)).hits
    with pytest.raises(ValueError):
        fit_asset_budget((hit,),601)
    tiny=replace(hit,body="small bounded advice")
    assert fit_asset_budget((tiny,)).hits==(tiny,)


def test_failed_event_recording_cannot_leave_untracked_injection(tmp_path,monkeypatch):
    import hashlib

    import nanobot.memory.asset_retriever as module
    connection=open_maintenance_db(tmp_path)
    _asset(connection,tmp_path,"candidate")
    def failed_record(*args,**kwargs):
        raise sqlite3.OperationalError("event writer failed")
    monkeypatch.setattr(module,"record_retrieval",failed_record)
    result=retrieve_asset_context("fixture",workspace=tmp_path,session_key="s",trace_id="t",turn_id="turn")
    assert result.outcome=="degraded" and result.error_code=="asset_event_write_failed"
    assert not result.text and not result.hits and not result.injected_ids
    assert result.digest=="sha256:"+hashlib.sha256(b"").hexdigest()
    assert connection.execute("SELECT count(*) FROM retrieval_events").fetchone()[0]==0
