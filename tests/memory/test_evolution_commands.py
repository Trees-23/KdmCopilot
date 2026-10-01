import re
from types import SimpleNamespace

from nanobot.memory.db import connect_memory_db
from nanobot.memory.evolution_commands import (
    EvolutionCommandService,
    PublishCallbacks,
    _format_beijing_time,
)
from nanobot.memory.migrations.runner import apply_migrations
from nanobot.memory.overlay_sync import OverlayInspection, OverlaySkill
from nanobot.memory.proposal_repository import ProposalRepository


def _config(*, enabled=False, publish_enabled=False, admins=("admin-1",), groups=("group-1",)):
    return SimpleNamespace(
        enabled=enabled,
        evolution=SimpleNamespace(
            notification_groups=list(groups),
            approval_admin_openids=list(admins),
            command_require_mention=True,
            notifications_enabled=False,
            adoption_enabled=False,
            publish_enabled=publish_enabled,
        ),
    )


def _metadata(*, sender="member-1", group="group-1", mention=True, message_id="m-1"):
    return {
        "qq_chat_type": "group",
        "group_openid": group,
        "sender_openid": sender,
        "qq_mentioned_bot": mention,
        "message_id": message_id,
    }


def test_confirmation_expiry_is_rendered_as_beijing_time():
    assert _format_beijing_time("2026-09-27T05:25:47+00:00") == (
        "2026年09月27日 13:25:47（北京时间）"
    )


def _proposal(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    repo = ProposalRepository(connection)
    repo.upsert_group_scope(
        group_openid="group-1",
        namespace="qq:group-1",
        observation_enabled=True,
        notification_enabled=True,
        command_require_mention=True,
        daily_notification_limit=12,
    )
    proposal = repo.create_proposal(
        workspace=str(tmp_path),
        skill_name="demo-skill",
        source_kind="workspace",
        target="workspace_adopt_proposal",
        baseline_hash="base",
        candidate_hash="candidate",
    )
    code = repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    connection.close()
    return proposal.proposal_id, code


def _published_proposal(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    repo = ProposalRepository(connection)
    proposal = repo.create_proposal(
        proposal_id="prop-publish-command",
        workspace=str(tmp_path),
        skill_name="overlay-skill",
        source_kind="shared",
        target="git_pr_proposal",
        baseline_hash="sha256:base",
        candidate_hash="sha256:candidate",
    )
    code = repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    repo.approve(
        proposal.proposal_id,
        workspace=str(tmp_path),
        actor_openid="admin-1",
        group_openid="group-1",
        code=code,
        idempotency_key="approve-publish-command",
    )
    repo.transition(proposal.proposal_id, expected_status="approved", new_status="creating_pr")
    repo.transition(proposal.proposal_id, expected_status="creating_pr", new_status="pr_created")
    repo._record_action(  # type: ignore[attr-defined]
        proposal_id=proposal.proposal_id,
        workspace=str(tmp_path),
        action="create_draft_pr",
        actor_openid="admin-1",
        group_openid=None,
        idempotency_key="draft-publish-command",
        request_digest="sha256:draft",
        result_status="pr_created",
        result={"branch": "evolve/prop-publish-command", "commit": "abc123"},
    )
    connection.close()
    return proposal.proposal_id


def test_evolve_commands_are_deterministic_and_admin_gated(tmp_path):
    proposal_id, code = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())

    status = service.handle("status", metadata=_metadata())
    assert "自进化模块状态" in status.content
    assert "- 总开关：关闭" in status.content
    assert "E217" not in status.content

    review = service.handle(f"review {proposal_id}", metadata=_metadata())
    assert proposal_id in review.content
    assert "确认码哈希" in review.content
    assert "状态：已通知待确认" in review.content
    assert "创建日期：" in review.content

    denied = service.handle(
        f"approve {proposal_id} {code}",
        metadata=_metadata(sender="member-1"),
    )
    assert denied.content.startswith("拒绝：")

    approved = service.handle(
        f"approve {proposal_id} {code}",
        metadata=_metadata(sender="admin-1", message_id="approve-1"),
    )
    assert "已记录管理员批准" in approved.content
    assert "未修改 Skill" in approved.content


def test_review_hides_stage_id_and_shows_candidate_eval_and_diff(tmp_path):
    connection = connect_memory_db(tmp_path)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO skills(skill_id,namespace,name,source_kind,status,created_at,updated_at) "
        "VALUES('skill:review','workspace','review-skill','workspace','active','now','now')"
    )
    connection.executemany(
        "INSERT INTO skill_revisions(revision_id,skill_id,skill_version,content_hash,content,author_actor,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("revision:base", "skill:review", "1", "sha256:base", "# Base\n", "test", "now"),
            ("revision:candidate", "skill:review", "2", "sha256:candidate", "# Candidate\n\n只读查询规则。\n", "test", "now"),
        ],
    )
    connection.execute(
        "INSERT INTO eval_packs(eval_pack_id,skill_id,skill_revision_id,dataset_hash,fixture_hash,rubric_json,"
        "split_policy_json,source_manifest_json,question_count,valid_count,evidence_grade,status,created_at) "
        "VALUES('eval-pack:review','skill:review','revision:candidate','sha256:data','sha256:fixture','{}','{}','{}',1,1,'limited','sealed','now')"
    )
    connection.execute(
        "INSERT INTO eval_cases(eval_pack_id,case_key,prompt,expected,split,source_case_id,created_at) "
        "VALUES('eval-pack:review','case-1','请列出 Skill','结构化结果','holdout','case-source','now')"
    )
    connection.execute(
        "INSERT INTO eval_runs(eval_run_id,eval_pack_id,baseline_revision_id,candidate_revision_id,baseline_hash,"
        "candidate_hash,model_id,tool_schema_digest,fixture_hash,dataset_hash,seed,replay_group_id,evidence_grade,"
        "is_independent_replay,status,gate_result,metrics_json,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("eval-run:review", "eval-pack:review", "revision:base", "revision:candidate", "sha256:base",
         "sha256:candidate", "fixture-model", "sha256:tools", "sha256:fixture", "sha256:data", "seed",
         "replay:review", "limited", 1, "completed", "eligible_for_confirmation",
         '{"holdout_passed":true,"security_clean":true}', "now"),
    )
    connection.execute(
        "INSERT INTO eval_case_results(eval_run_id,case_key,split,outcome,score,tools_json,judge_actor) "
        "VALUES('eval-run:review','case-1','holdout','passed',1.0,'[\"skill_read\"]','reviewer')"
    )
    connection.commit()
    proposal = ProposalRepository(connection).create_proposal(
        proposal_id="phase6-proposal:legacy",
        workspace=str(tmp_path), skill_id="skill:review", skill_name="review-skill", source_kind="workspace",
        target="workspace_adopt_proposal", baseline_hash="sha256:base", candidate_hash="sha256:candidate",
        baseline_revision_id="revision:base", candidate_revision_id="revision:candidate",
        trace_ids=("trace-1",), case_ids=("case-source",), eval_run_ids=("eval-run:review",),
    )
    repo = ProposalRepository(connection)
    repo.issue_confirmation(proposal.proposal_id, code="4821", ttl_minutes=720)
    connection.close()

    service = EvolutionCommandService(str(tmp_path), _config())
    review = service.handle(f"review {proposal.public_id}", metadata=_metadata())
    assert "phase6" not in review.content
    assert "只读查询规则" in review.content
    assert "Holdout：1/1" in review.content
    assert "/evolve review " + proposal.public_id + " cases" in review.content

    cases = service.handle(f"review {proposal.public_id} cases", metadata=_metadata())
    assert "请列出 Skill" in cases.content
    assert "结构化结果" in cases.content
    diff = service.handle(f"review {proposal.public_id} diff", metadata=_metadata())
    assert "-# Base" in diff.content
    assert "+# Candidate" in diff.content


def test_evolve_rejects_wrong_group_and_missing_mention(tmp_path):
    proposal_id, _ = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())

    wrong_group = service.handle("list", metadata=_metadata(group="other-group"))
    assert wrong_group.content.startswith("拒绝：")

    missing_mention = service.handle(
        f"review {proposal_id}", metadata=_metadata(mention=False)
    )
    assert "@机器人" in missing_mention.content


def test_overlay_command_lists_unpublished_candidates_and_admin_can_read_content(tmp_path, monkeypatch):
    candidate = tmp_path / "overlay" / "skills" / "demo-candidate" / "SKILL.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("# Demo candidate\n\n未发布内容。\n", encoding="utf-8")
    inspection = OverlayInspection(
        "drifted",
        str(candidate.parents[2]),
        "main",
        (OverlaySkill("demo-candidate", str(candidate), "sha256:a", str(tmp_path / "skills/demo-candidate/SKILL.md"), None, "missing_in_workspace"),),
    )
    monkeypatch.setattr("nanobot.memory.evolution_commands.inspect_overlay", lambda *args, **kwargs: inspection)
    config = _config()
    config.evolution.overlay_checkout_path = str(candidate.parents[2])
    service = EvolutionCommandService(str(tmp_path), config)

    listed = service.handle("overlay", metadata=_metadata(sender="member-1"))
    assert "demo-candidate" in listed.content
    assert "未发布" in listed.content

    denied = service.handle("overlay demo-candidate", metadata=_metadata(sender="member-1"))
    assert denied.content.startswith("拒绝：")
    detail = service.handle("overlay demo-candidate", metadata=_metadata(sender="admin-1"))
    assert "未发布内容" in detail.content


def test_evolve_menu_and_natural_read_only_queries(tmp_path):
    service = EvolutionCommandService(str(tmp_path), _config())
    menu = service.handle("menu", metadata=_metadata())
    assert "自进化查询菜单" in menu.content
    assert "/evolve overlay" in menu.content

    pending = service.handle_natural_query(
        "我想看看现在有哪些待确认的沉淀 Skill", metadata=_metadata()
    )
    assert pending is not None
    assert "没有可显示的 Proposal" in pending.content

    unrelated = service.handle_natural_query("帮我设计一个 Skill", metadata=_metadata())
    assert unrelated is None


def test_evolve_list_splits_review_publish_rejected_and_failed_queues(tmp_path):
    review_id, _ = _proposal(tmp_path)
    publish_id = _published_proposal(tmp_path)
    connection = connect_memory_db(tmp_path)
    repo = ProposalRepository(connection)
    review = repo.get(review_id)
    publish = repo.get(publish_id)
    assert review is not None
    assert publish is not None
    rejected = repo.create_proposal(
        workspace=str(tmp_path), skill_name="rejected-skill", source_kind="workspace",
        target="workspace_adopt_proposal", baseline_hash="sha256:rejected-base",
        candidate_hash="sha256:rejected-candidate", status="rejected_by_admin",
    )
    failed = repo.create_proposal(
        workspace=str(tmp_path), skill_name="failed-skill", source_kind="workspace",
        target="workspace_adopt_proposal", baseline_hash="sha256:failed-base",
        candidate_hash="sha256:failed-candidate", status="failed", gate_result="failed",
    )
    connection.close()
    service = EvolutionCommandService(str(tmp_path), _config())

    pending = service.handle("list pending", metadata=_metadata())
    assert review.public_id in pending.content
    assert publish.public_id in pending.content
    assert "下一步" in pending.content

    review_result = service.handle("list review", metadata=_metadata())
    assert review.public_id in review_result.content
    assert publish.public_id not in review_result.content

    publish_result = service.handle("list publish", metadata=_metadata())
    assert publish.public_id in publish_result.content
    assert "当前发布能力关闭" in publish_result.content

    rejected_result = service.handle("list rejected", metadata=_metadata())
    assert rejected.public_id in rejected_result.content
    assert failed.public_id not in rejected_result.content

    failed_result = service.handle("list failed", metadata=_metadata())
    assert failed.public_id in failed_result.content
    assert rejected.public_id not in failed_result.content

    natural = service.handle_natural_query("看看有哪些待我发布的 Skill", metadata=_metadata())
    assert natural is not None
    assert publish.public_id in natural.content


def test_evolve_reject_is_idempotent_for_replayed_message(tmp_path):
    proposal_id, _ = _proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())
    metadata = _metadata(sender="admin-1", message_id="reject-1")

    first = service.handle(f"reject {proposal_id} bad candidate", metadata=metadata)
    second = service.handle(f"reject {proposal_id} bad candidate", metadata=metadata)
    assert "已拒绝" in first.content
    assert "拒绝失败" in second.content or "已拒绝" in second.content


def test_admin_can_reject_an_unpublished_overlay_draft(tmp_path):
    proposal_id = _published_proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config())

    result = service.handle(
        f"reject {proposal_id} 候选缺少业务语义，拒绝发布",
        metadata=_metadata(sender="admin-1", message_id="reject-overlay-1"),
    )

    assert "已拒绝" in result.content
    connection = connect_memory_db(tmp_path)
    assert connection.execute(
        "SELECT status FROM skill_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()[0] == "rejected_by_admin"
    connection.close()


def test_publish_confirmation_is_group_admin_only_and_runs_in_two_steps(tmp_path):
    proposal_id = _published_proposal(tmp_path)
    calls: list[str] = []
    service = EvolutionCommandService(
        str(tmp_path),
        _config(enabled=True, publish_enabled=True),
        publish_callbacks=PublishCallbacks(
            ci_passed=lambda _proposal_id: True,
            merge=lambda branch, commit: calls.append(f"merge:{branch}:{commit}") or "merge-ref",
            deploy=lambda ref: calls.append(f"deploy:{ref}") or "deploy-ref",
        ),
    )
    denied = service.handle(f"publish {proposal_id}", metadata=_metadata(sender="member-1"))
    assert denied.content.startswith("拒绝：")

    challenge = service.handle(f"publish {proposal_id}", metadata=_metadata(sender="admin-1"))
    assert "群内二次发布确认已签发" in challenge.content
    code = re.search(r"确认码：([0-9]{4,8})", challenge.content).group(1)
    published = service.handle(
        f"publish {proposal_id} {code}", metadata=_metadata(sender="admin-1", message_id="publish-1")
    )
    assert "已发布到个人 Gateway" in published.content
    assert calls == ["merge:evolve/prop-publish-command:abc123", "deploy:merge-ref"]


def test_publish_command_stays_closed_by_default(tmp_path):
    proposal_id = _published_proposal(tmp_path)
    service = EvolutionCommandService(str(tmp_path), _config(enabled=True))
    result = service.handle(f"publish {proposal_id}", metadata=_metadata(sender="admin-1"))
    assert "发布能力当前关闭" in result.content
