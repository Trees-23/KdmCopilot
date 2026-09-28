"""Controlled GitHub adapter for the private Skill Overlay path.

The adapter is deliberately separate from the QQ command and from the LLM
turn.  It can create a *Draft* PR and inspect CI, but it never merges a PR or
deploys a Gateway.  The final merge/deploy callbacks remain behind the
``publish_enabled`` second-confirmation gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from nanobot.memory.pr_publisher import DraftPRResult, create_draft_pr
from nanobot.memory.proposal_repository import ProposalConflict, ProposalRepository

_RUN = Callable[..., subprocess.CompletedProcess[str]]
_REPO = re.compile(r"^(?:(?:https://github\.com/|git@github\.com:))?([^/]+/[^/]+?)(?:\.git)?$")


@dataclass(frozen=True, slots=True)
class RemoteDraftPRResult:
    """Result of local Draft PR preparation plus the remote PR handoff."""

    status: str
    proposal_id: str
    url: str | None = None
    number: int | None = None
    branch: str | None = None
    commit: str | None = None
    ci_passed: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RemotePRStatus:
    """Audited remote PR/CI projection used by the notification worker."""

    status: str
    proposal_id: str
    url: str | None = None
    number: int | None = None
    ci_passed: bool = False
    is_draft: bool = True
    reason: str | None = None


def _digest(value: Mapping[str, Any] | str) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_output(result: subprocess.CompletedProcess[str]) -> Any:
    try:
        return json.loads((result.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("GitHub CLI 返回的不是有效 JSON") from exc


def _error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "GitHub CLI 调用失败").strip()[:300]


def _normalize_repo(value: str) -> str:
    match = _REPO.fullmatch(value.strip())
    if not match:
        raise ValueError("Overlay remote 必须是 github.com/<owner>/<repo>")
    return match.group(1).removesuffix(".git")


class GitHubOverlayClient:
    """Create and inspect Draft PRs in one explicitly configured private repo."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        repository: str,
        base_branch: str = "main",
        actor: str = "phase6-overlay",
        run: _RUN = subprocess.run,
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.repository = _normalize_repo(repository)
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch) or base_branch.startswith("/"):
            raise ValueError("Overlay base branch 不安全")
        self.base_branch = base_branch
        self.actor = actor
        self.run = run

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(["git", *args], cwd=self.repo_path, check=False, capture_output=True, text=True)

    def _gh(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run(["gh", *args], cwd=self.repo_path, check=False, capture_output=True, text=True)

    def _verify_repository(self) -> None:
        root = self._git("rev-parse", "--show-toplevel")
        if root.returncode != 0 or Path(root.stdout.strip()).resolve() != self.repo_path:
            raise RuntimeError("Overlay repo_path 不是预期 Git 工作区")
        remote = self._git("remote", "get-url", "origin")
        if remote.returncode != 0 or _normalize_repo(remote.stdout) != self.repository:
            raise RuntimeError("Overlay origin 与配置的私有仓库不一致")

    @staticmethod
    def _proposal_action(connection: sqlite3.Connection, proposal_id: str, action: str) -> Mapping[str, Any] | None:
        row = connection.execute(
            "SELECT result_json FROM proposal_actions WHERE proposal_id=? AND action=? "
            "ORDER BY created_at DESC LIMIT 1",
            (proposal_id, action),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _record(self, connection: sqlite3.Connection, proposal_id: str, action: str,
                result_status: str, result: Mapping[str, Any]) -> None:
        record = ProposalRepository(connection).get(proposal_id)
        if record is None:
            raise KeyError(proposal_id)
        ProposalRepository(connection)._record_action(  # type: ignore[attr-defined]
            proposal_id=proposal_id,
            workspace=record.workspace,
            action=action,
            actor_openid=self.actor,
            group_openid=None,
            idempotency_key=f"{action}:{proposal_id}:{_digest(result)}",
            request_digest=_digest({"proposal_id": proposal_id, "action": action}),
            result_status=result_status,
            result=dict(result),
        )

    def create_draft_pr(self, connection: sqlite3.Connection, proposal_id: str, config: Any) -> RemoteDraftPRResult:
        """Create a local candidate, push its dedicated branch and open Draft PR.

        Every remote operation is explicit and repository-scoped.  A failure
        leaves the branch/PR evidence intact and marks the Proposal failed; it
        never falls back to another repository or to a non-draft PR.
        """

        try:
            self._verify_repository()
            local: DraftPRResult = create_draft_pr(
                connection, proposal_id, repo_path=self.repo_path, actor=self.actor, config=config, run_git=self.run
            )
            if local.status not in {"pr_created", "idempotent"}:
                return RemoteDraftPRResult(local.status, proposal_id, branch=local.branch, commit=local.commit,
                                           reason=local.reason)
            branch, commit = local.branch, local.commit
            if not branch or not commit:
                return RemoteDraftPRResult("invalid", proposal_id, reason="本地 Draft PR 元数据不完整")

            existing = self._gh(
                "pr", "list", "--repo", self.repository, "--head", branch, "--base", self.base_branch,
                "--state", "all", "--json", "number,url,isDraft,headRefName,baseRefName,state",
            )
            if existing.returncode != 0:
                raise RuntimeError(_error(existing))
            rows = _json_output(existing)
            if not isinstance(rows, list):
                raise RuntimeError("GitHub CLI PR 列表格式异常")
            matching = [row for row in rows if row.get("headRefName") == branch and row.get("baseRefName") == self.base_branch]
            if len(matching) > 1:
                raise RuntimeError("同一 Overlay 分支存在多个远端 PR")
            if matching:
                pr = matching[0]
                if not bool(pr.get("isDraft")):
                    raise RuntimeError("远端 PR 不是 Draft，拒绝继续")
                result = {"url": pr.get("url"), "number": pr.get("number"), "branch": branch,
                          "commit": commit, "repository": self.repository, "base": self.base_branch,
                          "is_draft": True}
                self._record(connection, proposal_id, "remote_draft_pr", "idempotent", result)
                return RemoteDraftPRResult("idempotent", proposal_id, url=pr.get("url"), number=pr.get("number"),
                                           branch=branch, commit=commit, ci_passed=False)

            pushed = self._git("push", "--set-upstream", "origin", branch)
            if pushed.returncode != 0:
                raise RuntimeError(_error(pushed))
            body = local.body or ""
            created = self._gh(
                "pr", "create", "--repo", self.repository, "--base", self.base_branch, "--head", branch,
                "--draft", "--title", local.title or f"功能（Skill进化）：更新 {proposal_id}", "--body", body,
            )
            if created.returncode != 0:
                raise RuntimeError(_error(created))
            url = (created.stdout or "").strip().splitlines()[-1].strip()
            if not url.startswith("https://github.com/"):
                raise RuntimeError("GitHub CLI 未返回 PR URL")
            result = {"url": url, "branch": branch, "commit": commit, "repository": self.repository,
                      "base": self.base_branch, "is_draft": True}
            self._record(connection, proposal_id, "remote_draft_pr", "created", result)
            return RemoteDraftPRResult("pr_created", proposal_id, url=url, branch=branch, commit=commit)
        except (OSError, RuntimeError, sqlite3.Error, ProposalConflict, ValueError) as exc:
            record = ProposalRepository(connection).get(proposal_id)
            if record and record.status == "pr_created":
                try:
                    ProposalRepository(connection).transition(
                        proposal_id, expected_status="pr_created", new_status="failed", reason=str(exc)[:300]
                    )
                except (ProposalConflict, sqlite3.Error):
                    pass
            return RemoteDraftPRResult("failed", proposal_id, reason=str(exc)[:300])

    def inspect_ci(self, connection: sqlite3.Connection, proposal_id: str) -> RemotePRStatus:
        """Read PR draft/base/head/check state without changing the PR."""

        remote = self._proposal_action(connection, proposal_id, "remote_draft_pr")
        if not remote or not remote.get("url"):
            return RemotePRStatus("not_found", proposal_id, reason="未找到远端 Draft PR 记录")
        viewed = self._gh("pr", "view", str(remote["url"]), "--json",
                          "number,url,isDraft,baseRefName,headRefName,statusCheckRollup,state")
        if viewed.returncode != 0:
            return RemotePRStatus("unavailable", proposal_id, url=str(remote["url"]), reason=_error(viewed))
        data = _json_output(viewed)
        if not isinstance(data, dict) or data.get("baseRefName") != self.base_branch or data.get("headRefName") != remote.get("branch"):
            return RemotePRStatus("mismatch", proposal_id, url=str(remote["url"]), reason="PR base/head 与 Proposal 不一致")
        if not bool(data.get("isDraft")):
            return RemotePRStatus("mismatch", proposal_id, url=str(remote["url"]), reason="远端 PR 已脱离 Draft 状态")
        checks = data.get("statusCheckRollup") or []
        ci_passed = bool(checks) and all(
            str(item.get("conclusion") or item.get("state") or "").upper() == "SUCCESS"
            for item in checks
        )
        status = "ci_passed" if ci_passed else "ci_pending"
        projection = {"url": data.get("url"), "number": data.get("number"), "base": self.base_branch,
                      "branch": data.get("headRefName"), "is_draft": bool(data.get("isDraft")),
                      "ci_passed": ci_passed}
        self._record(connection, proposal_id, "remote_pr_status", status, projection)
        return RemotePRStatus(status, proposal_id, url=data.get("url"), number=data.get("number"),
                              ci_passed=ci_passed, is_draft=bool(data.get("isDraft")))


__all__ = ["GitHubOverlayClient", "RemoteDraftPRResult", "RemotePRStatus"]
