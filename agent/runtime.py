"""
upstream-semantic-sync — Runtime

Main entry point for the semantic sync agent. Orchestrates the full pipeline:
analyze → map → transform → fix → PR.

Modes:
    --commit <sha>      Sync a single upstream commit (creates its own PR)
    --range <sha1..sha2>  Sync a range of commits (one consolidated PR)
    --since-last        Sync all upstream commits since the last synced SHA
                        (one consolidated PR — this is the mode used by
                        scheduled GitHub Action runs)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.planner import Planner
from agent.executor import Executor

log = logging.getLogger("sync.runtime")

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
BUILTIN_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def resolve_knowledge_dir(repo_path: str) -> Path:
    """Pick the knowledge directory: repo's knowledge/ if it exists, else builtin."""
    repo_knowledge = Path(repo_path) / "knowledge"
    if repo_knowledge.exists():
        log.info("Using knowledge dir from downstream repo: %s", repo_knowledge)
        return repo_knowledge
    log.info("Using builtin knowledge dir: %s", BUILTIN_KNOWLEDGE_DIR)
    return BUILTIN_KNOWLEDGE_DIR


def load_conventions(knowledge_dir: Path) -> str:
    """Load downstream conventions from knowledge/conventions.md.

    This file is human-maintained and explains how the fork diverges from
    upstream: renamed symbols, structural differences, intentional divergence,
    and any other context that helps the LLM adapt upstream changes.
    """
    path = knowledge_dir / "conventions.md"
    if path.exists():
        content = path.read_text().strip()
        if content:
            log.info("Loaded conventions from %s (%d chars)", path, len(content))
            return content
    return ""


def apply_transformations(repo_path: str, transformations: list[dict[str, Any]]) -> list[str]:
    """Apply transformation diffs to the downstream repo on disk.

    Each transformation may contain:
    - 'diff': a unified diff to apply via git apply
    - 'path': a file path (for git add tracking)
    - 'content': full file content to write (fallback if diff fails)

    Returns list of paths that were modified.
    """
    import subprocess

    modified = []
    for t in transformations:
        path = t.get("path", "")
        diff = t.get("diff", "")
        content = t.get("content", "")

        if diff:
            # Apply the unified diff
            r = subprocess.run(
                ["git", "apply", "--allow-empty"],
                input=diff, cwd=repo_path,
                capture_output=True, text=True,
            )
            if r.returncode == 0 and path:
                modified.append(path)
                log.info("Applied diff to %s", path)
            else:
                log.warning("git apply failed for %s: %s", path, r.stderr.strip())
                # Fallback: if content is provided, write it directly
                if content and path:
                    full_path = os.path.join(repo_path, path)
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, "w") as f:
                        f.write(content)
                    modified.append(path)
                    log.info("Wrote full content to %s", path)
        elif content and path:
            # No diff, but full content provided — write directly
            full_path = os.path.join(repo_path, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            modified.append(path)
            log.info("Wrote content to %s", path)

    return modified


def append_changelog(
    repo_path: str,
    upstream_repo: str,
    upstream_branch: str,
    shas: list[str],
    analyses: list[dict[str, Any]],
) -> None:
    """Append adopted upstream commits to CHANGELOG.sync.md in the downstream repo.

    Format:
        ## YYYY-MM-DD — <count> commits from <upstream_repo#branch>

        | SHA | Intent | Type | Risk |
        |-----|--------|------|------|
        | `abc1234` | Add simplify command | feature | medium |
        | `def5678` | Fix retry logic | bugfix | low |
    """
    changelog_path = Path(repo_path) / "CHANGELOG.sync.md"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    repo_label = upstream_repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    header = f"## {now} — {len(shas)} commit{'s' if len(shas) != 1 else ''} from {repo_label}#{upstream_branch}"

    lines = [header, "", "| SHA | Intent | Type | Risk |", "|-----|--------|------|------|"]
    for sha, analysis in zip(shas, analyses):
        short = sha[:12]
        intent = analysis.get("intent", "")
        ctype = analysis.get("change_type", "")
        risk = analysis.get("risk", "")
        lines.append(f"| `{short}` | {intent} | {ctype} | {risk} |")
    lines.append("")

    entry = "\n".join(lines)

    if changelog_path.exists():
        existing = changelog_path.read_text()
        # Prepend the new entry after the header line if present
        if existing.startswith("# Upstream Sync Changelog"):
            parts = existing.split("\n", 2)
            if len(parts) >= 3:
                changelog_path.write_text(parts[0] + "\n\n" + entry + parts[2])
            else:
                changelog_path.write_text(existing + "\n" + entry)
        else:
            changelog_path.write_text(existing + "\n" + entry)
    else:
        changelog_path.write_text(
            "# Upstream Sync Changelog\n\n"
            "This file is auto-generated by [upstream-semantic-sync](https://github.com/upstream-semantic-sync). "
            "It records every upstream commit that was adopted into this downstream fork.\n\n"
            + entry
        )

    log.info("Appended %d commits to %s", len(shas), changelog_path)


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
    skipped: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.skipped:
            return True
        if self.errors:
            return False
        if self.build_status == "fail":
            return False
        if self.conflicts and not self.transformations:
            return False
        return True


@dataclass
class BatchResult:
    """Result of a consolidated sync run (multiple commits → one PR)."""

    commit_shas: list[str] = field(default_factory=list)
    analyses: list[dict[str, Any]] = field(default_factory=list)
    all_transformations: list[dict[str, Any]] = field(default_factory=list)
    all_conflicts: list[dict[str, Any]] = field(default_factory=list)
    skipped_commits: list[str] = field(default_factory=list)
    failed_commits: list[str] = field(default_factory=list)
    build_status: str = "unknown"
    pr_url: str = ""
    pr_number: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.errors:
            return False
        if self.build_status == "fail":
            return False
        if self.all_conflicts and not self.all_transformations:
            return False
        return True

    @property
    def synced_count(self) -> int:
        return len(self.analyses)


# ── Pipeline stages ──────────────────────────────────────────────────────────

def stage_fetch_commit(repo_url: str, commit_sha: str, branch: str, knowledge_dir: Path) -> str:
    """Fetch the commit diff from upstream so the LLM can analyze it."""
    import subprocess

    upstream_dir = knowledge_dir / ".upstream-cache"
    upstream_dir.mkdir(parents=True, exist_ok=True)

    repo_name = repo_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    clone_dir = upstream_dir / repo_name

    if clone_dir.exists():
        log.info("Fetching upstream %s", repo_url)
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(clone_dir), capture_output=True, text=True, check=True,
        )
    else:
        log.info("Cloning upstream %s", repo_url)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", repo_url, str(clone_dir)],
            capture_output=True, text=True, check=True,
        )

    result = subprocess.run(
        ["git", "show", "--no-patch", "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n    %s%n%n%b", commit_sha],
        cwd=str(clone_dir), capture_output=True, text=True, check=True,
    )
    header = result.stdout.strip()

    result = subprocess.run(
        ["git", "diff-tree", "--patch", "--stat", commit_sha],
        cwd=str(clone_dir), capture_output=True, text=True, check=True,
    )
    diff = result.stdout
    if len(diff) > 8192:
        diff = diff[:8192] + "\n... (truncated)"

    return f"## Commit metadata\n\n{header}\n\n## Diff\n\n```diff\n{diff}\n```"


def stage_analyze(executor: Executor, repo_url: str, commit_sha: str, branch: str, commit_data: str) -> dict:
    """Stage 1: Analyze the upstream commit."""
    log.info("Analyzing upstream commit %s", commit_sha)
    return executor.run_skill(
        "analyze_commit",
        inputs={"commit_data": commit_data},
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


def stage_transform(executor: Executor, analysis: dict, mappings: dict, repo_path: str = "", conventions: str = "") -> dict:
    """Stage 3: Transform upstream changes for downstream."""
    log.info("Transforming code for %d targets", len(mappings.get("downstream_targets", [])))

    # Read the current content of each downstream target file so the LLM
    # can produce accurate transformations instead of guessing
    downstream_files = {}
    for target in mappings.get("downstream_targets", []):
        path = target.get("downstream", "")
        if path and repo_path:
            full = os.path.join(repo_path, path)
            if os.path.exists(full):
                try:
                    with open(full) as f:
                        downstream_files[path] = f.read()
                except Exception:
                    pass

    return executor.run_skill(
        "transform_code",
        inputs={
            "analysis": analysis,
            "mappings": mappings,
            "downstream_targets": mappings.get("downstream_targets", []),
            "downstream_files": downstream_files,
            "conventions": conventions,
        },
    )


def stage_resolve_conflicts(
    executor: Executor,
    conflicts: list[dict],
    analysis: dict,
    mappings: dict,
    repo_path: str = "",
    conventions: str = "",
) -> dict:
    """Stage 3b: Resolve transform conflicts via LLM second pass.

    When the initial transform flags targets as conflicts instead of producing
    transformations, this stage re-sends them to the LLM with a more aggressive
    prompt that demands a resolution.
    """
    log.info("Resolving %d conflicts via LLM second pass", len(conflicts))

    # Re-read downstream files for conflict paths
    downstream_files = {}
    for c in conflicts:
        path = c.get("path", "")
        if path and repo_path:
            full = os.path.join(repo_path, path)
            if os.path.exists(full):
                try:
                    with open(full) as f:
                        downstream_files[path] = f.read()
                except Exception:
                    pass

    return executor.run_skill(
        "resolve_conflict",
        inputs={
            "conflicts": conflicts,
            "analysis": analysis,
            "mappings": mappings,
            "downstream_files": downstream_files,
            "conventions": conventions,
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
    upstream_refs: list[str],
    analyses: list[dict],
    transformations: list[dict],
    build_result: dict,
    branch_name: str,
    repo_path: str,
) -> dict:
    """Stage 5: Create a consolidated pull request."""
    log.info("Creating consolidated PR for %d upstream commits", len(upstream_refs))
    return executor.run_skill(
        "create_pr",
        inputs={
            "branch_name": branch_name,
            "upstream_refs": upstream_refs,
            "analyses": analyses,
            "transformations": transformations,
            "build_result": build_result,
            "repo_path": repo_path,
        },
    )


# ── Single-commit pipeline (for --commit) ────────────────────────────────────

def run_sync(
    downstream_repo: str,
    upstream_repo: str,
    commit_sha: str,
    branch: str = "main",
    dry_run: bool = False,
) -> SyncResult:
    """Run the full sync pipeline for a single upstream commit."""

    result = SyncResult(commit_sha=commit_sha)
    knowledge_dir = resolve_knowledge_dir(downstream_repo)
    planner = Planner(knowledge_dir=knowledge_dir)
    executor = Executor(skills_dir=SKILLS_DIR, knowledge_dir=knowledge_dir)
    conventions = load_conventions(knowledge_dir)

    try:
        commit_data = stage_fetch_commit(upstream_repo, commit_sha, branch, knowledge_dir)
        analysis = stage_analyze(executor, upstream_repo, commit_sha, branch, commit_data)
        analysis["commit_sha"] = commit_sha
        result.intent = analysis.get("intent", "")
        result.change_type = analysis.get("change_type", "")
        result.risk = analysis.get("risk", "")

        if not planner.should_sync(analysis):
            log.info("Planner recommends skipping this commit: %s", analysis.get("intent"))
            result.skipped = True
            result.intent = analysis.get("intent", "")
            return result

        mappings = stage_map(executor, analysis)
        result.downstream_targets = mappings.get("downstream_targets", [])

        transform_out = stage_transform(executor, analysis, mappings, downstream_repo, conventions)
        result.transformations = transform_out.get("transformations", [])
        result.conflicts = transform_out.get("conflicts", [])

        # Resolve conflicts via LLM second pass
        if result.conflicts:
            log.info(
                "Transform produced %d conflicts — attempting resolution",
                len(result.conflicts),
            )
            resolve_out = stage_resolve_conflicts(
                executor, result.conflicts, analysis, mappings, downstream_repo, conventions,
            )
            resolved = resolve_out.get("transformations", [])
            if resolved:
                result.transformations.extend(resolved)
                apply_transformations(downstream_repo, resolved)
                log.info("Resolved %d conflicts into transformations", len(resolved))
            # Keep only truly unresolved conflicts
            result.conflicts = resolve_out.get("conflicts", [])

        if dry_run:
            log.info("Dry run — stopping before build fix and PR creation")
            return result

        build_out = stage_build_fix(executor, transform_out, downstream_repo)
        result.build_status = build_out.get("build_status", "unknown")

        branch_name = f"sync/upstream-{commit_sha[:12]}"
        pr_out = stage_create_pr(
            executor, [commit_sha], [analysis], [transform_out], build_out,
            branch_name, downstream_repo,
        )
        result.pr_url = pr_out.get("pr_url", "")
        result.pr_number = pr_out.get("pr_number", 0)

        # Record adopted commit in changelog
        append_changelog(downstream_repo, upstream_repo, branch, [commit_sha], [analysis])

        new_mappings = mappings.get("new_mappings", [])
        if new_mappings:
            planner.persist_new_mappings(new_mappings)

    except Exception as exc:
        log.exception("Pipeline failed for commit %s", commit_sha)
        result.errors.append(str(exc))

    return result


# ── Batch pipeline (for --since-last and --range) ────────────────────────────

def run_sync_batch(
    downstream_repo: str,
    upstream_repo: str,
    commits: list[str],
    branch: str = "main",
    dry_run: bool = False,
) -> BatchResult:
    """Analyze/map/transform each commit, then create one consolidated PR."""

    result = BatchResult(commit_shas=commits)
    knowledge_dir = resolve_knowledge_dir(downstream_repo)
    planner = Planner(knowledge_dir=knowledge_dir)
    executor = Executor(skills_dir=SKILLS_DIR, knowledge_dir=knowledge_dir)
    conventions = load_conventions(knowledge_dir)

    all_transformations: list[dict] = []
    all_conflicts: list[dict] = []

    for sha in commits:
        try:
            # Fetch + analyze
            commit_data = stage_fetch_commit(upstream_repo, sha, branch, knowledge_dir)
            analysis = stage_analyze(executor, upstream_repo, sha, branch, commit_data)
            analysis["commit_sha"] = sha

            # Check policy
            if not planner.should_sync(analysis):
                log.info("Skipping commit %s: %s", sha[:12], analysis.get("intent"))
                result.skipped_commits.append(sha)
                continue

            # Map + transform
            mappings = stage_map(executor, analysis)
            transform_out = stage_transform(executor, analysis, mappings, downstream_repo, conventions)

            t_list = transform_out.get("transformations", [])
            c_list = transform_out.get("conflicts", [])

            # Apply the transformations to disk
            if t_list:
                apply_transformations(downstream_repo, t_list)

            # Resolve conflicts via LLM second pass
            if c_list:
                log.info(
                    "Transform produced %d conflicts for %s — attempting resolution",
                    len(c_list), sha[:12],
                )
                resolve_out = stage_resolve_conflicts(
                    executor, c_list, analysis, mappings, downstream_repo, conventions,
                )
                resolved = resolve_out.get("transformations", [])
                if resolved:
                    apply_transformations(downstream_repo, resolved)
                    t_list.extend(resolved)
                    log.info("Resolved %d conflicts into transformations for %s", len(resolved), sha[:12])
                # Keep only truly unresolved conflicts
                c_list = resolve_out.get("conflicts", [])

            all_transformations.extend(t_list)
            all_conflicts.extend(c_list)

            result.analyses.append(analysis)

            # Persist new mappings as we go
            new_mappings = mappings.get("new_mappings", [])
            if new_mappings:
                planner.persist_new_mappings(new_mappings)

        except Exception as exc:
            log.exception("Failed to process commit %s", sha)
            result.failed_commits.append(sha)
            result.errors.append(f"{sha[:12]}: {exc}")

    result.all_transformations = all_transformations
    result.all_conflicts = all_conflicts

    if not result.analyses:
        log.info("No commits to sync after policy filtering")
        return result

    if dry_run:
        log.info("Dry run — stopping before build fix and PR creation")
        return result

    # Build fix (one pass for all transformations combined)
    combined = {
        "transformations": all_transformations,
        "conflicts": all_conflicts,
    }
    build_out = stage_build_fix(executor, combined, downstream_repo)
    result.build_status = build_out.get("build_status", "unknown")

    # One consolidated PR
    first_sha = commits[0]
    last_sha = commits[-1]
    if first_sha == last_sha:
        branch_name = f"sync/upstream-{first_sha[:12]}"
    else:
        branch_name = f"sync/upstream-{first_sha[:12]}-{last_sha[:12]}"
    pr_out = stage_create_pr(
        executor, commits, result.analyses, [combined], build_out,
        branch_name, downstream_repo,
    )
    result.pr_url = pr_out.get("pr_url", "")
    result.pr_number = pr_out.get("pr_number", 0)

    # Record adopted commits in changelog
    adopted_shas = [a["commit_sha"] for a in result.analyses if "commit_sha" in a]
    append_changelog(downstream_repo, upstream_repo, branch, adopted_shas, result.analyses)

    # Advance last-synced SHA — only if we actually applied transformations
    if all_transformations:
        planner.set_last_synced_sha(upstream_repo, branch, last_sha)
        log.info("Advanced last synced SHA to %s", last_sha[:12])
    else:
        log.warning("No transformations applied — not advancing last synced SHA")

    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
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
    parser.add_argument("--limit", type=int, default=1, help="Max commits to adopt per run (0 = unlimited)")
    parser.add_argument("--dry-run", action="store_true", help="Stop before creating PR")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # In Docker-based GitHub Actions, the container runs as root but the
    # workspace is owned by the runner user. Git refuses to operate on
    # repos with mismatched ownership unless we mark them as safe.
    import subprocess
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", args.repo],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        capture_output=True, text=True,
    )

    if not args.commit and not args.range_ and not args.since_last:
        parser.error("Must specify --commit, --range, or --since-last")

    if args.since_last:
        knowledge_dir = resolve_knowledge_dir(args.repo)
        planner = Planner(knowledge_dir=knowledge_dir)

        last_sha = planner.get_last_synced_sha(args.upstream, args.branch)
        if not last_sha:
            log.error(
                "No last synced SHA found. Run with --commit first, "
                "or set sync_state in knowledge/mappings.yaml."
            )
            sys.exit(1)

        commits = planner.commits_since(args.upstream, args.branch, last_sha)
        if not commits:
            print("Upstream is up to date.")
            sys.exit(0)

        # Apply limit
        total = len(commits)
        if args.limit > 0 and total > args.limit:
            log.info("Limiting to %d of %d new commits (use --limit to change)", args.limit, total)
            commits = commits[:args.limit]

        batch_result = run_sync_batch(args.repo, args.upstream, commits, args.branch, args.dry_run)

        if args.json:
            print(json.dumps({
                "commit_count": len(commits),
                "synced_count": batch_result.synced_count,
                "skipped_commits": [c[:12] for c in batch_result.skipped_commits],
                "failed_commits": [c[:12] for c in batch_result.failed_commits],
                "transformations": len(batch_result.all_transformations),
                "conflicts": len(batch_result.all_conflicts),
                "build_status": batch_result.build_status,
                "pr_url": batch_result.pr_url,
                "pr_number": batch_result.pr_number,
                "ok": batch_result.ok,
                "errors": batch_result.errors,
            }, indent=2))
        else:
            print(f"Synced {batch_result.synced_count}/{len(commits)} commits")
            if batch_result.skipped_commits:
                print(f"  Skipped: {', '.join(c[:12] for c in batch_result.skipped_commits)}")
            if batch_result.failed_commits:
                print(f"  Failed: {', '.join(c[:12] for c in batch_result.failed_commits)}")
            if batch_result.pr_url:
                print(f"  → {batch_result.pr_url}")
            for err in batch_result.errors:
                print(f"  ⚠ {err}")

        if not batch_result.ok:
            sys.exit(1)

    elif args.commit:
        result = run_sync(args.repo, args.upstream, args.commit, args.branch, args.dry_run)

        if not args.dry_run and result.ok and not result.skipped and result.transformations:
            knowledge_dir = resolve_knowledge_dir(args.repo)
            planner = Planner(knowledge_dir=knowledge_dir)
            planner.set_last_synced_sha(args.upstream, args.branch, args.commit)

        if args.json:
            print(json.dumps({
                "commit_sha": result.commit_sha,
                "intent": result.intent,
                "change_type": result.change_type,
                "risk": result.risk,
                "build_status": result.build_status,
                "pr_url": result.pr_url,
                "ok": result.ok,
                "skipped": result.skipped,
                "errors": result.errors,
            }, indent=2))
        else:
            if result.skipped:
                status = "⊘"
            elif result.ok:
                status = "✓"
            else:
                status = "✗"
            print(f"{status} {result.commit_sha[:12]}  {result.intent}")
            if result.pr_url:
                print(f"  → {result.pr_url}")
            for err in result.errors:
                print(f"  ⚠ {err}")

        if not result.ok:
            sys.exit(1)

    else:
        # --range: batch mode
        knowledge_dir = resolve_knowledge_dir(args.repo)
        planner = Planner(knowledge_dir=knowledge_dir)
        commits = planner.order_commits(args.upstream, args.range_, args.branch)

        # Apply limit
        total = len(commits)
        if args.limit > 0 and total > args.limit:
            log.info("Limiting to %d of %d commits", args.limit, total)
            commits = commits[:args.limit]

        batch_result = run_sync_batch(args.repo, args.upstream, commits, args.branch, args.dry_run)

        if args.json:
            print(json.dumps({
                "commit_count": len(commits),
                "synced_count": batch_result.synced_count,
                "skipped_commits": [c[:12] for c in batch_result.skipped_commits],
                "failed_commits": [c[:12] for c in batch_result.failed_commits],
                "transformations": len(batch_result.all_transformations),
                "conflicts": len(batch_result.all_conflicts),
                "build_status": batch_result.build_status,
                "pr_url": batch_result.pr_url,
                "pr_number": batch_result.pr_number,
                "ok": batch_result.ok,
                "errors": batch_result.errors,
            }, indent=2))
        else:
            print(f"Synced {batch_result.synced_count}/{len(commits)} commits")
            if batch_result.pr_url:
                print(f"  → {batch_result.pr_url}")
            for err in batch_result.errors:
                print(f"  ⚠ {err}")

        if not batch_result.ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
