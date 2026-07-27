"""
upstream-semantic-sync — Runtime

Main entry point for the semantic sync agent. Orchestrates the full pipeline:
analyze → map → transform → fix → PR.

Modes:
    --commit <sha>      Sync a single upstream commit
    --range <sha1..sha2>  Sync a range of commits
    --since-last        Sync all upstream commits since the last synced SHA
                        (this is the mode used by scheduled GitHub Action runs)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.planner import Planner
from agent.executor import Executor

log = logging.getLogger("sync.runtime")

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


# ── Pipeline result ──────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    """Accumulated output from the full sync pipeline."""

    commit_sha: str
    intent: str = ""
    change_type: str = ""
    risk: str = ""
    downstream_targets: list[dict[str, Any]] = field(default_factory=list)
    transformations: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    build_status: str = "unknown"
    pr_url: str = ""
    pr_number: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0 and self.build_status == "pass"


# ── Pipeline stages ──────────────────────────────────────────────────────────

def stage_analyze(executor: Executor, repo_url: str, commit_sha: str, branch: str) -> dict:
    """Stage 1: Analyze the upstream commit."""
    log.info("Analyzing upstream commit %s", commit_sha)
    return executor.run_skill(
        "analyze_commit",
        inputs={
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "branch": branch,
        },
    )


def stage_map(executor: Executor, analysis: dict) -> dict:
    """Stage 2: Map upstream architecture to downstream."""
    log.info("Mapping architecture for %s", analysis.get("intent", "<unknown>"))
    return executor.run_skill(
        "architecture_mapping",
        inputs={
            "upstream_paths": analysis.get("affected_paths", []),
            "change_type": analysis.get("change_type", "unknown"),
        },
    )


def stage_transform(executor: Executor, analysis: dict, mappings: dict) -> dict:
    """Stage 3: Transform upstream changes for downstream."""
    log.info("Transforming code for %d targets", len(mappings.get("downstream_targets", [])))
    return executor.run_skill(
        "transform_code",
        inputs={
            "analysis": analysis,
            "mappings": mappings,
            "downstream_targets": mappings.get("downstream_targets", []),
        },
    )


def stage_build_fix(executor: Executor, transformations: dict, repo_path: str) -> dict:
    """Stage 4: Fix any build failures from the transformation."""
    log.info("Fixing build issues")
    return executor.run_skill(
        "build_fix",
        inputs={
            "transformations": transformations,
            "repo_path": repo_path,
        },
    )


def stage_create_pr(
    executor: Executor,
    commit_sha: str,
    analysis: dict,
    transformations: dict,
    build_result: dict,
    branch_name: str,
    repo_path: str,
) -> dict:
    """Stage 5: Create a pull request with the synced changes."""
    log.info("Creating PR for upstream %s", commit_sha)
    return executor.run_skill(
        "create_pr",
        inputs={
            "branch_name": branch_name,
            "upstream_ref": commit_sha,
            "repo_path": repo_path,
            "analysis": analysis,
            "transformations": transformations,
            "build_result": build_result,
        },
    )


# ── Main pipeline ────────────────────────────────────────────────────────────

def run_sync(
    downstream_repo: str,
    upstream_repo: str,
    commit_sha: str,
    branch: str = "main",
    dry_run: bool = False,
) -> SyncResult:
    """Run the full sync pipeline for a single upstream commit."""

    result = SyncResult(commit_sha=commit_sha)
    planner = Planner(knowledge_dir=KNOWLEDGE_DIR)
    executor = Executor(skills_dir=SKILLS_DIR, knowledge_dir=KNOWLEDGE_DIR)

    try:
        # 1. Analyze
        analysis = stage_analyze(executor, upstream_repo, commit_sha, branch)
        result.intent = analysis.get("intent", "")
        result.change_type = analysis.get("change_type", "")
        result.risk = analysis.get("risk", "")

        # Check if planner says we should skip
        if not planner.should_sync(analysis):
            log.info("Planner recommends skipping this commit: %s", analysis.get("intent"))
            result.errors.append("Skipped by planner policy")
            return result

        # 2. Map
        mappings = stage_map(executor, analysis)
        result.downstream_targets = mappings.get("downstream_targets", [])

        # 3. Transform
        transform_out = stage_transform(executor, analysis, mappings)
        result.transformations = transform_out.get("transformations", [])
        result.conflicts = transform_out.get("conflicts", [])

        if dry_run:
            log.info("Dry run — stopping before build fix and PR creation")
            return result

        # 4. Build fix
        build_out = stage_build_fix(executor, transform_out, downstream_repo)
        result.build_status = build_out.get("build_status", "unknown")

        # 5. Create PR
        branch_name = f"sync/upstream-{commit_sha[:12]}"
        pr_out = stage_create_pr(
            executor, commit_sha, analysis, transform_out, build_out,
            branch_name, downstream_repo,
        )
        result.pr_url = pr_out.get("pr_url", "")
        result.pr_number = pr_out.get("pr_number", 0)

        # 6. Persist any new mappings discovered during the run
        new_mappings = mappings.get("new_mappings", [])
        if new_mappings:
            planner.persist_new_mappings(new_mappings)

    except Exception as exc:
        log.exception("Pipeline failed for commit %s", commit_sha)
        result.errors.append(str(exc))

    return result


def run_sync_range(
    downstream_repo: str,
    upstream_repo: str,
    sha_range: str,
    branch: str = "main",
    dry_run: bool = False,
) -> list[SyncResult]:
    """Run the sync pipeline for a range of upstream commits."""

    planner = Planner(knowledge_dir=KNOWLEDGE_DIR)
    commits = planner.order_commits(upstream_repo, sha_range, branch)

    results = []
    for sha in commits:
        result = run_sync(downstream_repo, upstream_repo, sha, branch, dry_run)
        results.append(result)
        if not result.ok:
            log.warning("Commit %s did not sync cleanly — continuing", sha)

    return results


def run_sync_since_last(
    downstream_repo: str,
    upstream_repo: str,
    branch: str = "main",
    dry_run: bool = False,
) -> list[SyncResult]:
    """Sync all upstream commits since the last successfully synced SHA.

    This is the mode used by scheduled (e.g. weekly) runs:
      1. Read last synced SHA from knowledge/mappings.yaml
      2. Fetch upstream, list all commits after that SHA
      3. Sync each commit
      4. Update the last synced SHA

    If no last SHA is recorded, logs a warning and returns empty.
    Use --range or --commit for the initial sync.
    """
    planner = Planner(knowledge_dir=KNOWLEDGE_DIR)

    last_sha = planner.get_last_synced_sha(upstream_repo, branch)
    if not last_sha:
        log.error(
            "No last synced SHA found in knowledge/mappings.yaml. "
            "Run an initial sync with --commit or --range first, "
            "or set sync_state manually."
        )
        return []

    # Fetch upstream and find new commits
    commits = planner.commits_since(upstream_repo, branch, last_sha)
    if not commits:
        log.info("Upstream is up to date — no new commits since %s", last_sha[:12])
        return []

    log.info("Syncing %d new upstream commits since %s", len(commits), last_sha[:12])

    results = []
    for sha in commits:
        result = run_sync(downstream_repo, upstream_repo, sha, branch, dry_run)
        results.append(result)

        # After a successful sync, advance the last-synced pointer
        # so the next run picks up from here. If this commit failed,
        # we still advance — the failure is recorded in the PR labels
        # and the next run will continue from the next commit.
        if not dry_run:
            planner.set_last_synced_sha(upstream_repo, branch, sha)
            log.info("Advanced last synced SHA to %s", sha[:12])
        else:
            log.info("Dry run — not advancing last synced SHA")

        if not result.ok:
            log.warning("Commit %s did not sync cleanly — continuing", sha)

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # GitHub Actions' conditional expressions produce empty strings ""
    # for omitted args. Filter them out before argparse sees them.
    import sys
    sys.argv = [a for a in sys.argv if a != ""]

    parser = argparse.ArgumentParser(description="Semantic upstream sync agent")
    parser.add_argument("--repo", required=True, help="Downstream repo path or URL")
    parser.add_argument("--upstream", required=True, help="Upstream repo URL")
    parser.add_argument("--commit", help="Single upstream commit SHA to sync")
    parser.add_argument("--range", dest="range_", help="Commit range (sha1..sha2) to sync")
    parser.add_argument(
        "--since-last",
        action="store_true",
        help="Sync all upstream commits since the last synced SHA (for scheduled runs)",
    )
    parser.add_argument("--branch", default="main", help="Upstream branch (default: main)")
    parser.add_argument("--dry-run", action="store_true", help="Stop before creating PR")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not args.commit and not args.range_ and not args.since_last:
        parser.error("Must specify --commit, --range, or --since-last")

    if args.since_last:
        results = run_sync_since_last(args.repo, args.upstream, args.branch, args.dry_run)
    elif args.commit:
        result = run_sync(args.repo, args.upstream, args.commit, args.branch, args.dry_run)
        results = [result]

        # For one-off --commit, also update the last-synced pointer
        if not args.dry_run and result.ok:
            planner = Planner(knowledge_dir=KNOWLEDGE_DIR)
            planner.set_last_synced_sha(args.upstream, args.branch, args.commit)
    else:
        results = run_sync_range(args.repo, args.upstream, args.range_, args.branch, args.dry_run)

    if args.json:
        data = [
            {
                "commit_sha": r.commit_sha,
                "intent": r.intent,
                "change_type": r.change_type,
                "risk": r.risk,
                "build_status": r.build_status,
                "pr_url": r.pr_url,
                "ok": r.ok,
                "errors": r.errors,
            }
            for r in results
        ]
        print(json.dumps(data, indent=2))
    else:
        if not results:
            print("Nothing to sync.")
        for r in results:
            status = "✓" if r.ok else "✗"
            print(f"{status} {r.commit_sha[:12]}  {r.intent}")
            if r.pr_url:
                print(f"  → {r.pr_url}")
            for err in r.errors:
                print(f"  ⚠ {err}")


if __name__ == "__main__":
    main()
