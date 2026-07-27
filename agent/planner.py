"""
upstream-semantic-sync — Planner

Decides *whether* and *how* to sync an upstream commit. Reads knowledge
files for policy, ordering, and mapping decisions.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("sync.planner")


class Planner:
    """Plans sync strategy based on analysis and accumulated knowledge."""

    def __init__(self, knowledge_dir: Path) -> None:
        self.knowledge_dir = knowledge_dir
        self.mappings = self._load_yaml(knowledge_dir / "mappings.yaml")
        self.decisions = self._load_yaml(knowledge_dir / "decisions.yaml")

    # ── Last-synced SHA tracking ─────────────────────────────────────────────

    def get_last_synced_sha(self, upstream_repo: str, upstream_branch: str) -> str | None:
        """Read the last upstream SHA that was successfully synced.

        Stored in knowledge/mappings.yaml under sync_state.
        """
        state = self.mappings.get("sync_state", {})
        key = self._state_key(upstream_repo, upstream_branch)
        sha = state.get(key)
        if sha:
            log.info("Last synced SHA for %s: %s", key, sha)
        else:
            log.info("No last synced SHA found for %s — will need initial range", key)
        return sha

    def set_last_synced_sha(self, upstream_repo: str, upstream_branch: str, sha: str) -> None:
        """Persist the last synced SHA so the next scheduled run picks up from here."""
        mappings_path = self.knowledge_dir / "mappings.yaml"
        existing = self._load_yaml(mappings_path)

        state = existing.setdefault("sync_state", {})
        key = self._state_key(upstream_repo, upstream_branch)
        state[key] = sha

        log.info("Persisting last synced SHA for %s: %s", key, sha)
        with open(mappings_path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

    @staticmethod
    def _state_key(upstream_repo: str, upstream_branch: str) -> str:
        """Derive a stable key from repo URL + branch."""
        # Strip trailing .git and normalize
        repo = upstream_repo.rstrip("/").removesuffix(".git")
        return f"{repo}#{upstream_branch}"

    # ── Policy: should we sync this commit? ──────────────────────────────────

    def should_sync(self, analysis: dict[str, Any]) -> bool:
        """Return True if the commit should be synced, False to skip."""

        change_type = analysis.get("change_type", "unknown")

        # Check skip rules from decisions.yaml
        skip_types = self.decisions.get("skip_change_types", [])
        if change_type in skip_types:
            log.info("Skipping change type: %s", change_type)
            return False

        # Check risk threshold
        max_risk = self.decisions.get("max_auto_risk", "high")
        risk_order = {"low": 0, "medium": 1, "high": 2}
        commit_risk = analysis.get("risk", "high")
        if risk_order.get(commit_risk, 2) > risk_order.get(max_risk, 2):
            log.info("Skipping: risk %s exceeds threshold %s", commit_risk, max_risk)
            return False

        # Check explicit skip list (by SHA prefix or intent pattern)
        skip_shas = self.decisions.get("skip_commits", [])
        commit_sha = analysis.get("commit_sha", "")
        for skip in skip_shas:
            if commit_sha.startswith(skip):
                log.info("Skipping: commit in skip list (%s)", skip)
                return False

        return True

    # ── Ordering: which commits to sync and in what order ────────────────────

    def order_commits(self, repo_url: str, sha_range: str, branch: str) -> list[str]:
        """Return an ordered list of commit SHAs to sync.

        Respects dependency chains — if commit B depends on commit A,
        A must appear first in the list.
        """

        sha1, sha2 = sha_range.split("..", 1)

        # Get commits in the range, oldest first
        result = subprocess.run(
            [
                "git", "log", "--reverse", "--format=%H",
                f"{sha1}..{sha2}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        commits = result.stdout.strip().split("\n")

        # Check for known dependency overrides in decisions.yaml
        dep_overrides = self.decisions.get("dependency_overrides", {})
        if dep_overrides:
            log.info("Applying %d dependency overrides", len(dep_overrides))

        # For now, git's topological order is sufficient.
        # If we need reordering, apply dep_overrides here.
        return [c for c in commits if c]

    def commits_since(
        self,
        upstream_repo: str,
        upstream_branch: str,
        since_sha: str,
    ) -> list[str]:
        """Fetch the upstream repo and return all commits after since_sha.

        Returns commits oldest-first (the order they should be synced).
        """
        # Clone/fetch the upstream repo
        upstream_dir = self.knowledge_dir / ".upstream-cache"
        upstream_dir.mkdir(parents=True, exist_ok=True)

        repo_name = upstream_repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        clone_dir = upstream_dir / repo_name

        if clone_dir.exists():
            log.info("Fetching upstream %s", upstream_repo)
            subprocess.run(
                ["git", "fetch", "origin", upstream_branch],
                cwd=str(clone_dir),
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            log.info("Cloning upstream %s", upstream_repo)
            subprocess.run(
                ["git", "clone", "--filter=blob:none", upstream_repo, str(clone_dir)],
                capture_output=True,
                text=True,
                check=True,
            )

        # Get commits since the last synced SHA, oldest first
        result = subprocess.run(
            [
                "git", "log", "--reverse", "--format=%H",
                f"{since_sha}..origin/{upstream_branch}",
            ],
            cwd=str(clone_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        commits = [c for c in result.stdout.strip().split("\n") if c]

        log.info("Found %d upstream commits since %s", len(commits), since_sha[:12])
        return commits

    def upstream_head_sha(self, upstream_repo: str, upstream_branch: str) -> str:
        """Get the current HEAD SHA of the upstream branch."""
        upstream_dir = self.knowledge_dir / ".upstream-cache"
        repo_name = upstream_repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        clone_dir = upstream_dir / repo_name

        result = subprocess.run(
            ["git", "rev-parse", f"origin/{upstream_branch}"],
            cwd=str(clone_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    # ── Mapping persistence ──────────────────────────────────────────────────

    def persist_new_mappings(self, new_mappings: list[dict[str, Any]]) -> None:
        """Write newly discovered mappings to knowledge/mappings.yaml."""

        mappings_path = self.knowledge_dir / "mappings.yaml"
        existing = self._load_yaml(mappings_path)

        module_maps = existing.setdefault("modules", {})
        surface_maps = existing.setdefault("surfaces", {})

        added = 0
        for m in new_mappings:
            upstream = m.get("upstream", "")
            downstream = m.get("downstream_guess", "")
            if upstream and downstream and upstream not in module_maps:
                module_maps[upstream] = {
                    "downstream": downstream,
                    "confidence": "low",
                    "auto_discovered": True,
                    "reason": m.get("reason", ""),
                }
                added += 1

        if added > 0:
            log.info("Persisting %d new mappings to %s", added, mappings_path)
            with open(mappings_path, "w") as f:
                yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with open(path) as f:
            return yaml.safe_load(f) or {}
