"""Strict remote checks and explicit callbacks for personal Overlay release.

The Gateway does not perform remote Git operations by default.  This module is
an opt-in adapter for a host-side release worker: it verifies that a PR belongs
to the configured private Overlay repository and that its head/base cannot be
silently substituted before any merge or deployment callback is invoked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping

_RUN = Callable[..., subprocess.CompletedProcess[str]]
_SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class OverlayPullRequest:
    number: int
    url: str
    state: str
    is_draft: bool
    base_repository: str
    base_branch: str
    head_branch: str
    head_sha: str
    checks: tuple[Mapping[str, Any], ...]


class OverlayReleaseError(RuntimeError):
    """Raised when the remote Overlay state is unsafe or unavailable."""


def _proposal_branch(proposal_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", proposal_id).strip("-.")
    if not clean:
        raise OverlayReleaseError("proposal id cannot produce a safe branch")
    return f"evolve/{clean[:80]}"


class GitHubOverlayRelease:
    """Use the authenticated ``gh`` CLI for a private Overlay repository.

    The runner is injectable for tests.  No token is accepted as an argument or
    included in errors; ``gh`` resolves authentication from its own secure
    credential store/environment.
    """

    def __init__(self, repository: str, *, base_branch: str = "main", run: _RUN = subprocess.run):
        if not _SAFE_REPOSITORY.fullmatch(repository):
            raise ValueError("Overlay repository must be owner/name")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch) or base_branch in {"", ".", ".."}:
            raise ValueError("unsafe Overlay base branch")
        self.repository = repository
        self.base_branch = base_branch
        self._run = run

    def _gh(self, *args: str) -> str:
        env = os.environ.copy()
        env["GH_PROMPT_DISABLED"] = "1"
        # Compose exposes the repository credential under the project-specific
        # name; gh expects GH_TOKEN/GITHUB_TOKEN. Never include it in errors.
        if env.get("GITHUB_PERSONAL_ACCESS_TOKEN") and not env.get("GH_TOKEN"):
            env["GH_TOKEN"] = env["GITHUB_PERSONAL_ACCESS_TOKEN"]
        result = self._run(
            ["gh", *args], check=False, capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "GitHub CLI failed").strip()
            raise OverlayReleaseError(detail[:300])
        return result.stdout

    @staticmethod
    def _parse_pr(
        payload: Mapping[str, Any], *, default_repository: str | None = None
    ) -> OverlayPullRequest:
        checks = tuple(payload.get("statusCheckRollup") or ())
        base = payload.get("baseRefName")
        head = payload.get("headRefName")
        sha = payload.get("headRefOid")
        repository = payload.get("baseRepository") or default_repository
        if isinstance(repository, Mapping):
            repository = repository.get("nameWithOwner")
        if not all(isinstance(value, str) and value for value in (base, head, sha, repository)):
            raise OverlayReleaseError("GitHub PR metadata is incomplete")
        return OverlayPullRequest(
            number=int(payload["number"]),
            url=str(payload.get("url") or ""),
            state=str(payload.get("state") or "").upper(),
            is_draft=bool(payload.get("isDraft")),
            base_repository=str(repository),
            base_branch=base,
            head_branch=head,
            head_sha=sha,
            checks=checks,
        )

    def find(self, proposal_id: str) -> OverlayPullRequest:
        branch = _proposal_branch(proposal_id)
        output = self._gh(
            "pr",
            "list",
            "--repo",
            self.repository,
            "--base",
            self.base_branch,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number,url,state,isDraft,baseRefName,headRefName,headRefOid",
        )
        try:
            rows = json.loads(output)
        except json.JSONDecodeError as exc:
            raise OverlayReleaseError("GitHub CLI returned invalid PR JSON") from exc
        if not isinstance(rows, list) or len(rows) != 1:
            raise OverlayReleaseError("expected exactly one Overlay PR for Proposal")
        pr = self._parse_pr(rows[0], default_repository=self.repository)
        self._assert_identity(pr, branch)
        # Fine-grained tokens can create/read PRs while GitHub's GraphQL
        # ``statusCheckRollup`` field still returns 403.  Read workflow runs
        # through the Actions REST endpoint instead; it is covered by the
        # repository-scoped Actions read permission and is tied to this exact
        # head SHA.
        if not pr.checks:
            output = self._gh(
                "api",
                f"repos/{self.repository}/actions/runs?head_sha={pr.head_sha}",
            )
            try:
                runs = json.loads(output).get("workflow_runs", [])
            except (TypeError, json.JSONDecodeError) as exc:
                raise OverlayReleaseError("GitHub Actions 返回的 CI JSON 无效") from exc
            if not isinstance(runs, list):
                raise OverlayReleaseError("GitHub Actions 返回的 CI 列表无效")
            pr = OverlayPullRequest(
                number=pr.number,
                url=pr.url,
                state=pr.state,
                is_draft=pr.is_draft,
                base_repository=pr.base_repository,
                base_branch=pr.base_branch,
                head_branch=pr.head_branch,
                head_sha=pr.head_sha,
                checks=tuple(
                    {
                        "status": str(run.get("status") or "").upper(),
                        "conclusion": str(run.get("conclusion") or "").upper(),
                    }
                    for run in runs
                    if isinstance(run, Mapping)
                ),
            )
        return pr

    def _assert_identity(self, pr: OverlayPullRequest, branch: str) -> None:
        if pr.base_repository != self.repository:
            raise OverlayReleaseError("PR base repository is not the configured private Overlay")
        if pr.base_branch != self.base_branch or pr.head_branch != branch:
            raise OverlayReleaseError("PR branch or base branch does not match Proposal")
        if pr.state != "OPEN":
            raise OverlayReleaseError(f"PR is not open: {pr.state.lower()}")

    @staticmethod
    def checks_passed(pr: OverlayPullRequest) -> bool:
        if not pr.checks:
            return False
        return all(
            str(check.get("status") or "").upper() == "COMPLETED"
            and str(check.get("conclusion") or "").upper() == "SUCCESS"
            for check in pr.checks
        )

    def ci_passed(self, proposal_id: str) -> bool:
        return self.checks_passed(self.find(proposal_id))

    def merge(self, proposal_id: str, expected_head_sha: str) -> str:
        """Promote a Draft PR and merge it only when its head is unchanged."""
        pr = self.find(proposal_id)
        if pr.head_sha != expected_head_sha:
            raise OverlayReleaseError("PR head changed since Proposal creation")
        if not self.checks_passed(pr):
            raise OverlayReleaseError("Overlay CI has not passed")
        if pr.is_draft:
            self._gh("pr", "ready", str(pr.number), "--repo", self.repository)
        self._gh(
            "pr",
            "merge",
            str(pr.number),
            "--repo",
            self.repository,
            "--merge",
            "--delete-branch=false",
            "--match-head-commit",
            expected_head_sha,
        )
        return expected_head_sha

    def merge_branch(self, branch: str, expected_head_sha: str) -> str:
        """Adapt the publish gate's ``(branch, commit)`` callback contract."""
        if not branch.startswith("evolve/") or len(branch) <= len("evolve/"):
            raise OverlayReleaseError("publish branch is not an evolution branch")
        proposal_id = branch[len("evolve/") :]
        if _proposal_branch(proposal_id) != branch:
            raise OverlayReleaseError("publish branch is not canonical")
        return self.merge(proposal_id, expected_head_sha)


__all__ = ["GitHubOverlayRelease", "OverlayPullRequest", "OverlayReleaseError"]
