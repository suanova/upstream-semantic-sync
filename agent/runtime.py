"""
upstream-semantic-sync — Runtime

Main entry point for the semantic sync agent. Orchestrates the pipeline:
analyze → map → apply → resolve → fix → PR.

The pipeline is deliberately LLM-light:
- `apply` rewrites each upstream per-file diff to the mapped downstream
  path and runs `git apply` directly — no LLM at all for clean applies.
- The LLM is only invoked for files whose diff fails to apply, using
  knowledge/conventions.md to adapt the change (one small call per file).
This keeps prompts tiny and avoids the gateway timeouts caused by sending
whole changesets (or whole files) through the model.

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


def apply_edits(file_text: str, edits: list[dict[str, str]]) -> str:
    """Apply search/replace edits to file text.

    Each edit is {"old": ..., "new": ...}. The `old` block must appear
    exactly once in the file — this keeps edits unambiguous without any
    line-number math from the LLM (which is frequently off by a few lines).

    Raises ValueError describing the first edit that cannot be applied.
    Returns the modified text.
    """
    for i, edit in enumerate(edits, 1):
        old = edit.get("old", "")
        new = edit.get("new", "")
        if not old:
            raise ValueError(f"edit {i}: empty 'old' block")
        count = file_text.count(old)
        if count == 0:
            snippet = old[:80].replace("\n", "\\n")
            raise ValueError(f"edit {i}: 'old' block not found in file: {snippet!r}")
        if count > 1:
            snippet = old[:80].replace("\n", "\\n")
            raise ValueError(
                f"edit {i}: 'old' block matches {count} locations — "
                f"needs more surrounding context: {snippet!r}"
            )
        file_text = file_text.replace(old, new, 1)
    return file_text


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
    pr_branch: str = ""
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

def _artifacts_dir(knowledge_dir: Path, commit_sha: str) -> Path:
    """Per-commit directory for reviewable sync artifacts."""
    d = knowledge_dir / ".sync-artifacts" / commit_sha[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hunks_only(diff_text: str) -> str:
    """Strip extended headers (index lines etc.) for compact display."""
    skip = ("index ", "similarity index", "old mode", "new mode", "new file mode", "deleted file mode")
    return "\n".join(
        line for line in diff_text.splitlines()
        if not any(line.startswith(s) for s in skip)
    )


def _extract_hunk_lines(diff_text: str) -> list[int]:
    """Return the downstream line numbers touched by unified-diff hunks.

    Parses ``@@ -old_start,old_count +new_start,new_count @@`` headers
    and returns a sorted list of *new* (downstream) line numbers that the
    hunks modify.  Used to extract only the relevant region from a large
    downstream file so the LLM prompt stays small.
    """
    import re as _re
    lines: list[int] = []
    for m in _re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff_text):
        start = int(m.group(1))
        count = int(m.group(2) or "1")
        lines.extend(range(start, start + count))
    return sorted(set(lines))


def _unified_diff(old: str, new: str, path: str) -> str:
    import difflib
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _append_resolution_summary(
    knowledge_dir: Path,
    commit_sha: str,
    result_path: str,
    result: dict,
    resolved_ok: bool,
    resolution: str,
    conflict: dict,
) -> None:
    """Append the LLM resolution outcome to the per-commit SUMMARY.md."""
    art = _artifacts_dir(knowledge_dir, commit_sha or "unknown")
    summary_path = art / "SUMMARY.md"
    safe = result_path.replace("/", "__")
    lines = ["", f"### Resolution — `{result_path}`", ""]
    if resolved_ok:
        lines.append(f"- Status: resolved (confidence {result.get('confidence', '?')})")
        notes = result.get("notes", "")
        if notes:
            lines.append(f"- Notes: {notes}")
        lines.append(f"- Resolution diff: `{safe}.resolution.diff`")
        if resolution:
            lines.append("")
            lines.append("```diff")
            lines.append(resolution.rstrip("\n"))
            lines.append("```")
    else:
        desc = result.get("conflict_description") or conflict.get("description", "")
        lines.append("- Status: **unresolved**")
        if desc:
            lines.append(f"- Reason: {desc}")
    lines.append("")
    with open(summary_path, "a") as f:
        f.write("\n".join(lines))


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

    # --stat gives the classifier the full file list + change sizes for free;
    # --name-only (parsed locally by fetch_changed_paths) is the authoritative
    # path list used for mapping/applying — we never trust the LLM's paths.
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--stat", commit_sha],
        cwd=str(clone_dir), capture_output=True, text=True, check=True,
    )
    stat = result.stdout

    # The classification task only needs a *taste* of the diff — change_type
    # and risk are judged from the message + file list + a couple of KB of
    # actual hunks. Keeping this small is the main token saving in analyze.
    result = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--patch", commit_sha],
        cwd=str(clone_dir), capture_output=True, text=True, check=True,
    )
    diff = result.stdout
    if len(diff) > 2048:
        diff = diff[:2048] + "\n... (truncated — full file list in the stat above)"

    return f"## Commit metadata\n\n{header}\n\n## Changed files (stat)\n\n```\n{stat}\n```\n\n## Diff (partial)\n\n```diff\n{diff}\n```"


def fetch_changed_paths(repo_url: str, commit_sha: str, branch: str, knowledge_dir: Path) -> list[str]:
    """Return the exact list of paths changed by the commit, from git.

    Authoritative replacement for the LLM's affected_paths — an LLM can
    hallucinate or drop paths, git cannot. Renames report the new path,
    which is what the mapping stage needs.
    """
    import subprocess

    upstream_dir = knowledge_dir / ".upstream-cache"
    repo_name = repo_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    clone_dir = upstream_dir / repo_name

    if not clone_dir.exists():
        return []

    result = subprocess.run(
        ["git", "diff-tree", "--root", "--no-commit-id", "-r",
         "--diff-filter=ACDMRT", "--name-only", commit_sha],
        cwd=str(clone_dir), capture_output=True, text=True,
    )
    return [p for p in result.stdout.splitlines() if p.strip()]


def fetch_file_diff(
    repo_url: str,
    commit_sha: str,
    branch: str,
    knowledge_dir: Path,
    file_path: str,
    max_chars: int = 4096,
) -> str:
    """Fetch the upstream diff for a single file within a commit.

    Returns just the diff hunks for the specified file, much smaller than
    the full commit diff. Pass max_chars=0 to get the complete untruncated
    diff — required when the diff will be fed to `git apply`, since a
    truncated patch can never apply.

    The trailing newline is preserved deliberately: `git apply` expects the
    patch to end with one.
    """
    import subprocess

    upstream_dir = knowledge_dir / ".upstream-cache"
    repo_name = repo_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    clone_dir = upstream_dir / repo_name

    if not clone_dir.exists():
        return ""

    result = subprocess.run(
        # --no-commit-id: diff-tree otherwise prints the commit SHA as its
        # first line, which corrupts the patch for `git apply`.
        ["git", "diff-tree", "--root", "--no-commit-id", "--patch", commit_sha, "--", file_path],
        cwd=str(clone_dir), capture_output=True, text=True,
    )
    diff = result.stdout
    if not diff.strip():
        return ""
    if max_chars and len(diff) > max_chars:
        diff = diff[:max_chars] + "\n... (truncated)\n"
    return diff


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


def rewrite_diff_paths(diff_text: str, upstream_path: str, downstream_path: str) -> tuple[str, str, str]:
    """Rewrite a per-file unified diff so it applies at the downstream path.

    Only header lines are rewritten — hunk bodies are left untouched.
    Rename metadata (similarity index / rename from / rename to) is stripped,
    turning a rename-with-edits into a plain modify diff against the
    downstream file. Pure renames (no hunks) cannot be expressed as a modify
    diff and are reported by the caller as conflicts instead.

    Returns (rewritten_diff, rename_from, rename_to).
    """
    rename_from = ""
    rename_to = ""
    kept: list[str] = []
    for line in diff_text.split("\n"):
        if line.startswith("similarity index "):
            continue
        if line.startswith("rename from "):
            rename_from = line[len("rename from "):].strip()
            continue
        if line.startswith("rename to "):
            rename_to = line[len("rename to "):].strip()
            continue
        kept.append(line)

    # For renames, the downstream file's identity is the mapped path
    # regardless of which side of the rename upstream_path refers to.
    old_path = rename_from or upstream_path
    new_path = rename_to or upstream_path

    out: list[str] = []
    for line in kept:
        if line.startswith("diff --git "):
            line = line.replace(f"a/{old_path}", f"a/{downstream_path}")
            line = line.replace(f"b/{new_path}", f"b/{downstream_path}")
        elif line == f"--- a/{old_path}":
            line = f"--- a/{downstream_path}"
        elif line == f"+++ b/{new_path}":
            line = f"+++ b/{downstream_path}"
        out.append(line)

    return "\n".join(out), rename_from, rename_to


def stage_apply_direct(analysis: dict, mappings: dict, repo_path: str, knowledge_dir: Path) -> dict:
    """Stage 3: apply upstream per-file diffs directly to the downstream repo.

    For each mapped target, fetch the upstream diff for just that file,
    rewrite its paths to the downstream path, and run `git apply`. Files
    that apply cleanly need NO LLM call — this is what eliminates the huge
    prompts that caused gateway timeouts.

    Only files whose diff fails to apply become conflicts, to be resolved
    later by stage_resolve_conflicts (one small LLM call per file, guided
    by knowledge/conventions.md).

    Returns the same shape as the old transform stage:
    {"transformations": [...], "conflicts": [...]}.
    """
    import subprocess

    targets = mappings.get("downstream_targets", [])
    upstream_repo = analysis.get("_upstream_repo", "")
    upstream_branch = analysis.get("_upstream_branch", "main")
    commit_sha = analysis.get("commit_sha", "")

    log.info("Applying upstream diffs directly for %d targets (no LLM)", len(targets))

    applied: list[dict] = []
    conflicts: list[dict] = []

    for target in targets:
        upstream_path = target.get("upstream", "")
        downstream_path = target.get("downstream", "")

        if not upstream_path or not downstream_path:
            log.warning("Skipping target with missing path: %s", target)
            continue

        # Full, untruncated diff — a truncated patch can never apply.
        diff = fetch_file_diff(
            upstream_repo, commit_sha, upstream_branch, knowledge_dir,
            upstream_path, max_chars=0,
        )

        if not diff:
            conflicts.append({
                "path": downstream_path,
                "upstream_path": upstream_path,
                "upstream_diff": "",
                "description": f"No upstream diff found for {upstream_path} in this commit",
                "region": "",
            })
            continue

        rewritten, rename_from, rename_to = rewrite_diff_paths(diff, upstream_path, downstream_path)

        # Pure rename (no hunks) — nothing textual to apply; the equivalent
        # downstream rename needs a human (or a mapping update).
        if rename_from and "@@" not in rewritten:
            conflicts.append({
                "path": downstream_path,
                "upstream_path": upstream_path,
                "upstream_diff": "",
                "description": (
                    f"Upstream renamed {rename_from} → {rename_to} with no content change; "
                    "perform the equivalent rename downstream"
                ),
                "region": "",
            })
            continue

        # Binary changes can't be applied from a text diff.
        if "Binary files" in rewritten and "GIT binary patch" not in rewritten:
            conflicts.append({
                "path": downstream_path,
                "upstream_path": upstream_path,
                "upstream_diff": "",
                "description": "Binary file changed upstream — cannot apply textually",
                "region": "",
            })
            continue

        r = subprocess.run(
            ["git", "apply", "--allow-empty"],
            input=rewritten, cwd=repo_path, capture_output=True, text=True,
        )
        if r.returncode == 0:
            note = "upstream diff applied directly via git apply"
            if rename_from:
                note += f" (content edits only; upstream also renamed {rename_from} → {rename_to})"
            applied.append({
                "path": downstream_path,
                "content": "",
                "confidence": "high",
                "notes": note,
            })
            log.info("Applied %s → %s", upstream_path, downstream_path)
        else:
            stderr = r.stderr.strip()
            conflicts.append({
                "path": downstream_path,
                "upstream_path": upstream_path,
                "upstream_diff": rewritten,
                "description": f"git apply failed: {stderr[:400]}",
                "region": "",
            })
            log.info(
                "Apply failed for %s: %s",
                downstream_path,
                stderr.splitlines()[0] if stderr else "unknown error",
            )
            # Save the failed patch for review (full-fidelity copy; the
            # human-readable summary is written after the loop).
            art = _artifacts_dir(knowledge_dir, commit_sha)
            safe_name = downstream_path.replace("/", "__")
            (art / f"{safe_name}.failed.patch").write_text(rewritten)

    # ── Per-commit review summary ─────────────────────────────────────────
    art = _artifacts_dir(knowledge_dir, commit_sha)
    if not conflicts:
        summary_path = art / "SUMMARY.md"
        if summary_path.exists():
            summary_path.unlink()
        log.info("Commit %s: all %d files applied cleanly", commit_sha[:12], len(targets))
    else:
        summary = [f"# Sync review — commit {commit_sha[:12]}", ""]
        summary.append(f"- Applied cleanly: {len(applied)} file(s)")
        summary.append(f"- Failed direct apply: {len(conflicts)} file(s)")
        summary.append("")
        if applied:
            summary.append("## Applied (no LLM)")
            for t in applied:
                summary.append(f"- `{t['path']}` — {t.get('notes', '')}")
            summary.append("")
        summary.append("## Conflicts (upstream hunks that failed to apply)")
        for c in conflicts:
            cpath = c.get("path", "?")
            safe = cpath.replace("/", "__")
            desc = c.get("description", "")
            summary.append(f"### `{cpath}`")
            summary.append("")
            summary.append(f"- Reason: {desc.splitlines()[0] if desc else '?'}")
            if c.get("upstream_diff"):
                summary.append(f"- Failed patch: `{safe}.failed.patch`")
                summary.append("")
                summary.append("```diff")
                summary.append(_hunks_only(c["upstream_diff"]))
                summary.append("```")
                summary.append("")
        (art / "SUMMARY.md").write_text("\n".join(summary))

        # Verbose: show each conflict's failing hunks inline.
        for c in conflicts:
            if c.get("upstream_diff"):
                log.debug(
                    "─── Conflict: %s — hunks that failed to apply ───\n%s",
                    c.get("path", "?"), _hunks_only(c["upstream_diff"]),
                )

    return {
        "transformations": applied,
        "conflicts": conflicts,
        "new_mapping_candidates": [],
        "skipped": [],
    }


def stage_resolve_conflicts(
    executor: Executor,
    conflicts: list[dict],
    analysis: dict,
    repo_path: str = "",
    conventions: str = "",
) -> dict:
    """Stage 3b: resolve files whose upstream diff failed to apply directly.

    One LLM call per conflicting file, guided by knowledge/conventions.md.
    Prompts stay small because the conflict already carries the per-file
    upstream diff — the only large input is the downstream file itself,
    which is truncated.

    Returns {"transformations": [...], "conflicts": [...]} where the second
    list holds only files that remain genuinely unresolved.
    """
    log.info("Resolving %d apply failures via LLM (one call each)", len(conflicts))

    # Lightweight analysis summary
    analysis_summary = {
        "intent": analysis.get("intent", ""),
        "change_type": analysis.get("change_type", ""),
        "risk": analysis.get("risk", ""),
        "surfaces": analysis.get("surfaces", []),
        "commit_sha": analysis.get("commit_sha", ""),
    }

    MAX_FILE_SIZE = 30000
    MAX_DIFF_SIZE = 4096

    all_transformations: list[dict] = []
    remaining_conflicts: list[dict] = []

    for conflict in conflicts:
        path = conflict.get("path", "")
        upstream_path = conflict.get("upstream_path", "")

        # The conflict should already carry the per-file upstream diff
        # (attached by stage_apply_direct). Fall back to fetching it.
        upstream_file_diff = conflict.get("upstream_diff", "")
        if not upstream_file_diff and upstream_path:
            upstream_repo = analysis.get("_upstream_repo", "")
            upstream_branch = analysis.get("_upstream_branch", "main")
            if upstream_repo:
                upstream_file_diff = fetch_file_diff(
                    upstream_repo, analysis.get("commit_sha", ""),
                    upstream_branch, executor.knowledge_dir, upstream_path,
                )
        if len(upstream_file_diff) > MAX_DIFF_SIZE:
            upstream_file_diff = upstream_file_diff[:MAX_DIFF_SIZE] + "\n... (truncated)"

        # Read the downstream file — but only send the region around the
        # conflict hunks to the LLM, not the entire file.  The edit-applier
        # re-reads the full file from disk, so the LLM never needs more
        # than enough context to write correct old/new edit blocks.
        CONTEXT_MARGIN = 50  # lines above/below each hunk region
        downstream_file = ""
        if path and repo_path:
            full = os.path.join(repo_path, path)
            if os.path.exists(full):
                try:
                    with open(full) as f:
                        downstream_file = f.read()
                except Exception:
                    pass

        hunk_lines = _extract_hunk_lines(upstream_file_diff)
        if hunk_lines and len(downstream_file) > MAX_FILE_SIZE:
            # Extract only the lines around the conflict — raw, no line
            # numbers.  The LLM copies `old` blocks verbatim from this text,
            # so any prefix (line numbers) would break the exact match in
            # apply_edits.  A header note tells it which lines are shown
            # and that edits apply to the full file on disk.
            file_lines = downstream_file.splitlines()
            # Merge overlapping margins into a single span.
            lo = max(1, hunk_lines[0] - CONTEXT_MARGIN)
            hi = min(len(file_lines), hunk_lines[-1] + CONTEXT_MARGIN)
            snippet = file_lines[lo - 1 : hi]
            downstream_file = "\n".join(snippet)
            downstream_file = (
                f"(showing lines {lo}–{hi} of {len(file_lines)} of the file; "
                f"edits are applied to the full file on disk)\n\n"
                + downstream_file
            )
        elif len(downstream_file) > MAX_FILE_SIZE:
            # No hunk info available — fall back to truncated head.
            downstream_file = downstream_file[:MAX_FILE_SIZE] + "\n... (truncated)"

        log.info("Resolving %s", path)

        try:
            result = executor.run_skill(
                "resolve_single",
                inputs={
                    "conflict_description": conflict.get("description", ""),
                    "conflict_region": conflict.get("region", ""),
                    "analysis": analysis_summary,
                    "conventions": conventions,
                    "target_path": path,
                    "upstream_path": upstream_path,
                    "downstream_file": downstream_file,
                    "upstream_file_diff": upstream_file_diff,
                },
            )

            if result.get("is_conflict"):
                remaining_conflicts.append({
                    "path": result.get("path", path),
                    "description": result.get("conflict_description", conflict.get("description", "")),
                    "region": result.get("conflict_region", conflict.get("region", "")),
                })
                continue

            result_path = result.get("path", path)
            edits = result.get("edits", [])
            content = result.get("content", "")
            resolution = ""
            resolved_ok = False

            if edits:
                # Targeted-edit resolution: apply to the FULL file read
                # fresh from disk (never the truncated prompt copy), so
                # large files are edited in place without data loss and
                # the LLM completion stays tiny.
                full_path = os.path.join(repo_path, result_path)
                try:
                    with open(full_path) as f:
                        full_text = f.read()
                    new_text = apply_edits(full_text, edits)
                    with open(full_path, "w") as f:
                        f.write(new_text)
                    notes = result.get("notes", "")
                    all_transformations.append({
                        "path": result_path,
                        "content": "",
                        "confidence": result.get("confidence", "low"),
                        "notes": f"{notes} (applied {len(edits)} targeted edits)".strip(),
                    })
                    log.info("Resolved %s via %d targeted edits", result_path, len(edits))
                    resolved_ok = True
                    if new_text != full_text:
                        resolution = _unified_diff(full_text, new_text, result_path)
                except FileNotFoundError:
                    remaining_conflicts.append({
                        "path": result_path,
                        "description": "resolve returned edits but the file does not exist downstream",
                        "region": conflict.get("region", ""),
                    })
                except ValueError as exc:
                    # The LLM's old/new blocks didn't match.  The edits
                    # were returned but unusable — record as an
                    # unresolved conflict rather than silently dropping
                    # the file, and stash the failed edits for review.
                    log.warning("Edits failed for %s: %s", result_path, exc)
                    art = _artifacts_dir(
                        executor.knowledge_dir,
                        analysis_summary.get("commit_sha", "unknown"),
                    )
                    safe_name = result_path.replace("/", "__")
                    try:
                        (art / f"{safe_name}.failed_edits.json").write_text(
                            json.dumps(
                                {"edits": edits, "error": str(exc)},
                                indent=2,
                            )
                        )
                    except Exception:
                        pass
                    remaining_conflicts.append({
                        "path": result_path,
                        "description": f"LLM edits did not apply: {exc}",
                        "region": conflict.get("region", ""),
                    })
            elif content:
                # Full-content resolution (new files, or files small enough
                # that a complete rewrite is cheap). Show + save a diff too.
                old_text = ""
                full_path = os.path.join(repo_path, result_path)
                if os.path.exists(full_path):
                    try:
                        with open(full_path) as f:
                            old_text = f.read()
                    except Exception:
                        pass
                if content != old_text:
                    resolution = _unified_diff(old_text, content, result_path)
                all_transformations.append({
                    "path": result_path,
                    "content": content,
                    "confidence": result.get("confidence", "low"),
                    "notes": result.get("notes", ""),
                })
                resolved_ok = True
            else:
                remaining_conflicts.append({
                    "path": result_path,
                    "description": "resolve returned neither edits nor content",
                    "region": conflict.get("region", ""),
                })

            # Persist the resolution diff for review and show it in verbose mode.
            if resolved_ok and resolution:
                art = _artifacts_dir(executor.knowledge_dir, analysis_summary.get("commit_sha", "unknown"))
                safe_name = result_path.replace("/", "__")
                (art / f"{safe_name}.resolution.diff").write_text(resolution)
                log.debug(
                    "─── Resolution: %s (confidence %s) — applied diff ───\n%s",
                    result_path, result.get("confidence", "?"), resolution,
                )

            # Append the outcome to the per-commit summary written by
            # stage_apply_direct.
            _append_resolution_summary(
                executor.knowledge_dir,
                analysis_summary.get("commit_sha", "unknown"),
                result_path,
                result,
                resolved_ok,
                resolution,
                conflict,
            )

        except Exception as exc:
            log.warning("Resolve failed for %s: %s", path, exc)
            remaining_conflicts.append({
                "path": path,
                "description": f"Resolve call failed: {exc}",
                "region": conflict.get("region", ""),
            })

    return {
        "transformations": all_transformations,
        "conflicts": remaining_conflicts,
    }


def stage_build_fix(executor: Executor, transformations: dict, repo_path: str, conventions: str = "") -> dict:
    """Stage 4: Fix any build failures from the transformation.

    This stage has no access to a real build/toolchain inside the action
    container, so it cannot produce genuine build errors — it only has
    the conflict descriptions left by the apply/resolve stages.  When
    there are no conflicts, the build is assumed to pass.  When there
    ARE conflicts, we report them as unresolved rather than spending an
    LLM call that has nothing concrete to fix (and tends to 504/return
    prose).

    The LLM-driven build_fix is therefore disabled by default.  Set
    BUILD_FIX_LLM=true in the environment to opt back in.
    """
    conflicts = transformations.get("conflicts", [])
    if not conflicts:
        log.info("No conflicts — build assumed to pass")
        return {
            "fixes_applied": [],
            "unresolved": [],
            "build_status": "pass",
            "iterations_used": 0,
        }

    # There are unresolved conflicts.  We could ask the LLM to attempt
    # fixes, but without real build errors it's working blind and the
    # call frequently times out the gateway.  Report them as unresolved
    # instead — they surface in the PR body for a human.
    unresolved = [
        {
            "path": c.get("path", ""),
            "error": c.get("description", ""),
            "category": "unresolved_conflict",
            "attempts": 0,
            "suggestion": "Resolve manually — see the conflict summary in this PR.",
        }
        for c in conflicts
    ]
    log.info(
        "Build fix: %d unresolved conflict(s) reported (LLM build_fix disabled; "
        "set BUILD_FIX_LLM=true to enable)",
        len(unresolved),
    )

    if os.environ.get("BUILD_FIX_LLM", "").lower() not in ("true", "1", "yes"):
        return {
            "fixes_applied": [],
            "unresolved": unresolved,
            "build_status": "fail" if unresolved else "pass",
            "iterations_used": 0,
        }

    # Opt-in path: send only the conflict summaries to the LLM.
    log.info("BUILD_FIX_LLM=true — sending conflicts to LLM for fix attempts")
    slim_conflicts = [
        {
            "path": c.get("path", ""),
            "description": c.get("description", ""),
            "region": c.get("region", ""),
        }
        for c in conflicts
    ]
    return executor.run_skill(
        "build_fix",
        inputs={
            "transformations": {"conflicts": slim_conflicts},
            "build_errors": "",
            "conventions": conventions,
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
    stack_base: str = "",
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
            "stack_base": stack_base,
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
        # SHA blocklist is a pure string match — check it before spending
        # an LLM call on analysis.
        skip_shas = planner.decisions.get("skip_commits", [])
        for skip in skip_shas:
            if commit_sha.startswith(skip):
                log.info("Skipping: commit in skip list (%s)", skip)
                result.skipped = True
                return result

        commit_data = stage_fetch_commit(upstream_repo, commit_sha, branch, knowledge_dir)
        analysis = stage_analyze(executor, upstream_repo, commit_sha, branch, commit_data)
        analysis["commit_sha"] = commit_sha
        analysis["_upstream_repo"] = upstream_repo
        analysis["_upstream_branch"] = branch
        # Authoritative path list from git — never trust the LLM's copy.
        analysis["affected_paths"] = fetch_changed_paths(upstream_repo, commit_sha, branch, knowledge_dir)
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

        # Apply upstream diffs directly — no LLM for clean applies
        apply_out = stage_apply_direct(analysis, mappings, downstream_repo, knowledge_dir)
        result.transformations = apply_out.get("transformations", [])
        result.conflicts = apply_out.get("conflicts", [])

        # Resolve the files that failed to apply, using conventions.md
        if result.conflicts:
            log.info(
                "%d files failed to apply directly — resolving via LLM",
                len(result.conflicts),
            )
            resolve_out = stage_resolve_conflicts(
                executor, result.conflicts, analysis, downstream_repo, conventions,
            )
            resolved = resolve_out.get("transformations", [])
            if resolved:
                result.transformations.extend(resolved)
                apply_transformations(downstream_repo, resolved)
                log.info("Resolved %d apply failures into transformations", len(resolved))
            # Keep only truly unresolved conflicts
            result.conflicts = resolve_out.get("conflicts", [])

        if dry_run:
            log.info("Dry run — stopping before build fix and PR creation")
            return result

        build_out = stage_build_fix(executor, apply_out, downstream_repo, conventions)
        result.build_status = build_out.get("build_status", "unknown")

        branch_name = f"sync/upstream-{commit_sha[:12]}"
        pr_out = stage_create_pr(
            executor, [commit_sha], [analysis], [apply_out], build_out,
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
    stack_base: str = "",
) -> BatchResult:
    """Analyze/map/apply each commit, then create one consolidated PR.

    stack_base: name of a previous batch's sync branch to stack this batch's
    PR on (used by chunked multi-batch runs in --since-last mode).
    """

    result = BatchResult(commit_shas=commits)
    knowledge_dir = resolve_knowledge_dir(downstream_repo)
    planner = Planner(knowledge_dir=knowledge_dir)
    executor = Executor(skills_dir=SKILLS_DIR, knowledge_dir=knowledge_dir)
    conventions = load_conventions(knowledge_dir)

    all_transformations: list[dict] = []
    all_conflicts: list[dict] = []
    # Per-commit bundles so create_pr can make one commit per upstream commit.
    synced_shas: list[str] = []
    per_commit_bundles: list[dict] = []

    for sha in commits:
        try:
            # SHA blocklist is a pure string match — check it before
            # spending an LLM call on analysis.
            skip_shas = planner.decisions.get("skip_commits", [])
            if any(sha.startswith(skip) for skip in skip_shas):
                log.info("Skipping commit %s: in skip list", sha[:12])
                result.skipped_commits.append(sha)
                continue

            # Fetch + analyze
            commit_data = stage_fetch_commit(upstream_repo, sha, branch, knowledge_dir)
            analysis = stage_analyze(executor, upstream_repo, sha, branch, commit_data)
            analysis["commit_sha"] = sha
            analysis["_upstream_repo"] = upstream_repo
            analysis["_upstream_branch"] = branch
            # Authoritative path list from git — never trust the LLM's copy.
            analysis["affected_paths"] = fetch_changed_paths(upstream_repo, sha, branch, knowledge_dir)

            # Check policy
            if not planner.should_sync(analysis):
                log.info("Skipping commit %s: %s", sha[:12], analysis.get("intent"))
                result.skipped_commits.append(sha)
                continue

            # Track this commit's analysis even if apply fails later
            result.analyses.append(analysis)

            # Map + apply upstream diffs directly (no LLM for clean applies)
            mappings = stage_map(executor, analysis)
            apply_out = stage_apply_direct(analysis, mappings, downstream_repo, knowledge_dir)

            t_list = apply_out.get("transformations", [])
            c_list = apply_out.get("conflicts", [])

            # Resolve the files that failed to apply, using conventions.md
            if c_list:
                log.info(
                    "%d files failed to apply directly for %s — resolving via LLM",
                    len(c_list), sha[:12],
                )
                resolve_out = stage_resolve_conflicts(
                    executor, c_list, analysis, downstream_repo, conventions,
                )
                resolved = resolve_out.get("transformations", [])
                if resolved:
                    apply_transformations(downstream_repo, resolved)
                    t_list.extend(resolved)
                    log.info("Resolved %d apply failures into transformations for %s", len(resolved), sha[:12])
                # Keep only truly unresolved conflicts
                c_list = resolve_out.get("conflicts", [])

            synced_shas.append(sha)
            per_commit_bundles.append({
                "transformations": t_list,
                "conflicts": c_list,
            })
            all_transformations.extend(t_list)
            all_conflicts.extend(c_list)

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
        # Everything was deliberately skipped (and nothing failed) — advance
        # the pointer past the skipped commits so they aren't re-analyzed
        # (and re-billed) on every future run.
        if result.skipped_commits and not result.failed_commits:
            planner.set_last_synced_sha(upstream_repo, branch, commits[-1])
            log.info(
                "All %d commits skipped — advanced last synced SHA to %s",
                len(result.skipped_commits), commits[-1][:12],
            )
        return result

    if dry_run:
        log.info("Dry run — stopping before build fix and PR creation")
        return result

    # Build fix (one pass for all transformations combined)
    combined = {
        "transformations": all_transformations,
        "conflicts": all_conflicts,
    }
    build_out = stage_build_fix(executor, combined, downstream_repo, conventions)
    result.build_status = build_out.get("build_status", "unknown")

    # One PR containing one commit per upstream commit.
    first_sha = commits[0]
    last_sha = commits[-1]
    if first_sha == last_sha:
        branch_name = f"sync/upstream-{first_sha[:12]}"
    else:
        branch_name = f"sync/upstream-{first_sha[:12]}-{last_sha[:12]}"

    # Write the changelog + advance the sync pointer BEFORE creating the PR,
    # so both ride into the PR's last commit (previously they were written
    # after create_pr, so they never made it into the branch).
    adopted_shas = [a["commit_sha"] for a in result.analyses if "commit_sha" in a]
    append_changelog(downstream_repo, upstream_repo, branch, adopted_shas, result.analyses)

    old_sync_state = planner.get_last_synced_sha(upstream_repo, branch)
    advanced = False
    if all_transformations:
        planner.set_last_synced_sha(upstream_repo, branch, last_sha)
        advanced = True
        log.info("Advanced last synced SHA to %s", last_sha[:12])
    else:
        log.warning("No transformations applied — not advancing last synced SHA")

    pr_out = stage_create_pr(
        executor, synced_shas, result.analyses, per_commit_bundles, build_out,
        branch_name, downstream_repo, stack_base=stack_base,
    )
    result.pr_url = pr_out.get("pr_url", "")
    result.pr_number = pr_out.get("pr_number", 0)
    # Stack only when a branch was actually pushed (PR created). A
    # "no_changes" or error result leaves stack_base unchanged for the
    # next batch.
    if pr_out.get("status") == "created" and result.pr_url:
        result.pr_branch = branch_name

    if advanced and not result.pr_url:
        # PR creation failed — roll the pointer back so the next run retries
        # these commits instead of silently skipping them.
        if old_sync_state:
            planner.set_last_synced_sha(upstream_repo, branch, old_sync_state)
        log.warning("PR creation failed — reverted last synced SHA")

    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_review_hint(repo_path: str, knowledge_dir: Path, dry_run: bool, has_conflicts: bool) -> None:
    """Point the user at reviewable output after a run."""
    hints = []
    if dry_run:
        hints.append(f"review applied changes:  git -C {repo_path} diff")
        hints.append(f"discard them:            git -C {repo_path} checkout -- .")
    art = knowledge_dir / ".sync-artifacts"
    if art.exists():
        # In a GitHub Action the workspace is discarded after the run — the
        # artifacts only survive if the workflow uploads them.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            hints.append(
                "conflict+resolution reports: upload them with "
                "actions/upload-artifact (path: knowledge/.sync-artifacts/)"
            )
        else:
            hints.append(f"per-commit conflict+resolution review: {art}/<sha>/SUMMARY.md")
    if has_conflicts:
        hints.append("unresolved conflicts remain — see the log above and the artifacts")
    for h in hints:
        print(f"  ▸ {h}")


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
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Commits per batch/PR. If more new commits exist, the runtime "
             "loops over additional batches, stacking each PR on the previous "
             "(0 = single batch with everything). Default: 10",
    )
    parser.add_argument("--dry-run", action="store_true", help="Stop before creating PR")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Debug output: conflict hunks, LLM requests/responses, resolution diffs. "
             "Can also be enabled by setting VERBOSE=true in the environment "
             "(this is how the GitHub Action passes it).",
    )

    args = parser.parse_args()
    # Allow the Action to enable verbose logs via env var (action.yml maps
    # its `verbose` input to VERBOSE). CLI flag wins if both are set.
    if not args.verbose and os.environ.get("VERBOSE", "").lower() in ("true", "1", "yes"):
        args.verbose = True

    # In GitHub Actions, verbose debug logs are written to a file under
    # the artifacts directory instead of stdout — this keeps the step log
    # clean while still preserving all debug detail as a downloadable
    # artifact. When running locally, verbose logs go to stdout as before.
    _verbose_log_path: Path | None = None
    if args.verbose and os.environ.get("GITHUB_ACTIONS") == "true":
        art_root = resolve_knowledge_dir(args.repo) / ".sync-artifacts"
        art_root.mkdir(parents=True, exist_ok=True)
        _verbose_log_path = art_root / "debug.log"
        _verbose_log_path.touch()

        # INFO+ goes to stdout (visible in the Actions step log).
        # DEBUG+ goes to the debug.log file (captured as an artifact).
        file_handler = logging.FileHandler(str(_verbose_log_path), mode="a")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "[%(name)s] %(levelname)s: %(message)s",
        ))

        logging.basicConfig(
            level=logging.INFO,
            format="[%(name)s] %(levelname)s: %(message)s",
            stream=sys.stdout,
        )
        logging.getLogger().addHandler(file_handler)
        log.info("Verbose debug log → %s", _verbose_log_path)
    else:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            # No timestamps: GitHub Actions prefixes its own on every log line,
            # so ours would be redundant noise. Logs go to stdout (not the
            # default stderr) so they appear inline in the Action log stream.
            format="[%(name)s] %(levelname)s: %(message)s",
            stream=sys.stdout,
        )

    # In verbose mode the anthropic/httpx SDKs dump raw request dicts as a
    # single escaped line — unreadable. We emit our own readable request log
    # in executor instead, so keep the SDK loggers at INFO regardless.
    for noisy in ("anthropic", "anthropic._base_client", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.INFO)

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

        # Chunk into batches of --limit commits. Each batch becomes one PR;
        # when there are multiple batches the runtime loops, stacking each
        # batch's branch on the previous one (upstream commits are
        # sequential — batch N's diffs only apply cleanly on batch N-1's
        # tree). A dry run processes only the first batch: no PRs are
        # created, so reviewing one batch is enough.
        total = len(commits)
        if args.limit <= 0:
            batches = [commits]
        else:
            batches = [commits[i:i + args.limit] for i in range(0, total, args.limit)]

        if args.dry_run and len(batches) > 1:
            log.info(
                "%d new commits (%d batches of %d) — dry run processes the first batch only",
                total, len(batches), args.limit,
            )
            batches = batches[:1]
        elif len(batches) > 1:
            log.info(
                "%d new commits — running %d batches of up to %d commits each (one PR per batch, stacked)",
                total, len(batches), args.limit,
            )

        all_results: list[BatchResult] = []
        batch_commits: list[list[str]] = []
        stack_base = ""
        overall_ok = True

        for idx, batch in enumerate(batches, 1):
            if len(batches) > 1:
                log.info("── Batch %d/%d: %d commits ──", idx, len(batches), len(batch))

            batch_result = run_sync_batch(
                args.repo, args.upstream, batch, args.branch, args.dry_run,
                stack_base=stack_base,
            )
            all_results.append(batch_result)
            batch_commits.append(batch)

            # Stack the next batch on this batch's branch.
            if batch_result.pr_branch:
                stack_base = batch_result.pr_branch

            if not batch_result.ok:
                overall_ok = False
                log.error("Batch %d/%d failed — stopping", idx, len(batches))
                break

        if args.json:
            print(json.dumps({
                "batch_count": len(all_results),
                "commit_count": sum(len(c) for c in batch_commits),
                "synced_count": sum(r.synced_count for r in all_results),
                "skipped_commits": [c[:12] for r in all_results for c in r.skipped_commits],
                "failed_commits": [c[:12] for r in all_results for c in r.failed_commits],
                "transformations": sum(len(r.all_transformations) for r in all_results),
                "conflicts": sum(len(r.all_conflicts) for r in all_results),
                "build_status": all_results[-1].build_status if all_results else "unknown",
                "pr_urls": [r.pr_url for r in all_results if r.pr_url],
                "pr_numbers": [r.pr_number for r in all_results if r.pr_number],
                "ok": overall_ok,
                "errors": [e for r in all_results for e in r.errors],
            }, indent=2))
        else:
            synced = sum(r.synced_count for r in all_results)
            done = sum(len(c) for c in batch_commits)
            print(f"Synced {synced}/{done} commits across {len(all_results)} batch(es)")
            for i, r in enumerate(all_results):
                prefix = f"  batch {i+1}: " if len(all_results) > 1 else "  "
                if r.skipped_commits:
                    print(f"{prefix}Skipped: {', '.join(c[:12] for c in r.skipped_commits)}")
                if r.failed_commits:
                    print(f"{prefix}Failed: {', '.join(c[:12] for c in r.failed_commits)}")
                if r.pr_url:
                    print(f"{prefix}→ {r.pr_url}")
                for err in r.errors:
                    print(f"{prefix}⚠ {err}")
            _print_review_hint(
                args.repo, knowledge_dir, args.dry_run,
                any(r.all_conflicts for r in all_results),
            )

        if not overall_ok:
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
