"""Schema-only upgrade retains legacy facts and enforces new asset shapes."""

import sqlite3

import pytest
from pydantic import ValidationError

from nanobot.config.schema import Phase6EvolutionConfig
from nanobot.memory.continuous import Phase6RuntimeConfig
from nanobot.memory.maintenance import open_maintenance_db
from nanobot.memory.migrations.runner import apply_migrations


def test_upgrade_creates_empty_assets_and_never_backfills_legacy_completed(tmp_path):
    connection=open_maintenance_db(tmp_path)
    connection.execute("DROP TABLE memory_assets")
    for name in ("retryability","error_source","error_type","error_code","resource_class"):
        connection.execute("ALTER TABLE evolution_step_evidence DROP COLUMN "+name)
    connection.execute("DELETE FROM schema_meta WHERE version IN (20,29,31)")
    connection.execute(
        "INSERT INTO evolution_task_evidence(task_id,workspace,goal,actual_outcome,qualification,redaction_status,created_at,updated_at) "
        "VALUES('old',?,'old fact','completed','partial_evidence','safe','original-created','original-updated')",(str(tmp_path),),
    )
    connection.execute(
        "INSERT INTO evolution_step_evidence(step_id,task_id,workspace,sequence_no,tool_name,status,risk_level,candidate_use,redaction_status,resource_key,created_at) "
        "VALUES('old-step','old',?,0,'read_file','error','R0','unverified','safe','sha256:USER.md','original-step')",(str(tmp_path),),
    )
    connection.commit()
    before=dict(connection.execute("SELECT * FROM evolution_task_evidence").fetchone())
    statements=[]
    connection.set_trace_callback(statements.append)
    apply_migrations(connection)
    apply_migrations(connection)
    assert dict(connection.execute("SELECT * FROM evolution_task_evidence").fetchone())==before
    step=connection.execute("SELECT resource_class,retryability,resource_key,created_at FROM evolution_step_evidence").fetchone()
    assert tuple(step)==("unknown",None,"sha256:USER.md","original-step")
    assert connection.execute("SELECT count(*) FROM memory_assets").fetchone()[0]==0
    assert not any(s.lstrip().upper().startswith("DELETE") for s in statements)
    assert connection.execute("PRAGMA foreign_key_check").fetchall()==[]
    assert {row[0] for row in connection.execute("SELECT version FROM schema_meta WHERE version>=20")}=={20,21,22,25,26,27,28,29,30,31,32}


@pytest.mark.parametrize("field,value",[("steps_json",'[{}]'),("expires_at","2099-01-01"),
                                       ("error_root_cause",""),("resolution",""),("trigger_condition",""),
                                       ("applicability_boundary",""),("confidence_source",None)])
def test_l2_database_rejects_instance_steps_expiry_or_empty_required_fields(tmp_path,field,value):
    connection=open_maintenance_db(tmp_path)
    values={"asset_id":"lesson","workspace":str(tmp_path),"asset_level":"L2","origin_table":"memory_assets",
            "origin_id":"representative","family_key":"family","search_text":"safe advice","outcome":"failure",
            "error_root_cause":"bounded cause","resolution":"verify the resource category","trigger_condition":"missing resource",
            "applicability_boundary":"known resource only","confidence_source":"inferred","status":"active",
            "created_at":"now","updated_at":"now"}
    values[field]=value
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO memory_assets("+",".join(values)+") VALUES("+",".join("?" for _ in values)+")",tuple(values.values()))


def test_shadow_config_and_invalid_retrieval_dependency():
    parsed=Phase6EvolutionConfig.model_validate({"assetLayerEnabled":True,"assetRetrievalEnabled":False})
    assert parsed.asset_layer_enabled and not parsed.asset_retrieval_enabled
    runtime=Phase6RuntimeConfig.from_config(type("Config",(),{"evolution":parsed})())
    assert runtime.asset_layer_enabled and not runtime.asset_retrieval_enabled
    with pytest.raises(ValidationError):
        Phase6EvolutionConfig(asset_retrieval_enabled=True)
    with pytest.raises(ValueError):
        Phase6RuntimeConfig(asset_retrieval_enabled=True)
