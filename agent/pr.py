"""
upstream-semantic-sync — PR Creator

Creates a pull request with the synced upstream changes using the GitHub
API directly (no gh CLI needed). This is a local handler, not an LLM skill.

Supports both single-commit and consolidated (multi-commit) PRs.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

log = logging.getLogger("sync.create_pr")


def _git(repo_path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command with logging. Returns CompletedProcess."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0 and check:
        log.error("git %s failed (rc=%d): %s", " ".join(args), result.returncode, result.stderr.strip())
        raise subprocess.CalledProcessError(result.returncode, ["git"] + list(args), result.stdout, result.stderr)
    return result


def create_pr(
    repo_path: str,
    branch_name: str,
    upstream_refs: list[str],
    analyses: list[dict[str, Any]],
    transformations: list[dict[str, Any]],
    build_result: dict[str, Any],
    stack_base: str = "",
) -> dict[str, Any]:
    """Create a sync branch, commit transformed files, and open a PR.

    When stack_base is set (a previous sync branch from the same multi-batch
    run), the new branch is created off it instead of the base branch, and
    the PR targets stack_base — upstream commits are sequential, so batch N's
    changes only apply correctly on top of batch N-1's.
    """

    github_token = os.environ.get("GITHUB_TOKEN", "")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")

    if not github_token:
        return {"pr_url": "", "pr_number": 0, "status": "error: GITHUB_TOKEN not set"}
    if not repo_slug:
        return {"pr_url": "", "pr_number": 0, "status": "error: GITHUB_REPOSITORY not set"}

    # ── 0. Mark repo as safe directory (Docker runs as root, workspace is runner-owned) ──
    _git(repo_path, "config", "--global", "--add", "safe.directory", repo_path)

    # ── 1. Configure git identity ──────────────────────────────────────────
    _git(repo_path, "config", "--global", "user.name", "github-actions[bot]")
    _git(repo_path, "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com")

    # ── 2. Configure authenticated push URL ────────────────────────────────
    authed_url = f"https://x-access-token:{github_token}@github.com/{repo_slug}.git"
    # Try set-url; if that fails, add the remote
    r = _git(repo_path, "remote", "set-url", "origin", authed_url, check=False)
    if r.returncode != 0:
        log.warning("git remote set-url failed — trying remote add")
        _git(repo_path, "remote", "add", "origin", authed_url, check=False)

    # ── 3. Determine base branch ──────────────────────────────────────────
    r = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    base_branch = r.stdout.strip()
    if not base_branch or base_branch == "HEAD":
        base_branch = os.environ.get("GITHUB_REF_NAME", "main")
        if base_branch.startswith("refs/heads/"):
            base_branch = base_branch.removeprefix("refs/heads/")
    log.info("Base branch: %s", base_branch)

    # ── 4. Create the sync branch ────────────────────────────────────────
    # -B (not -b) so reruns of the same batch reset the branch cleanly.
    _git(repo_path, "branch", "-D", branch_name, check=False)
    if stack_base:
        # Stacked batch: HEAD is the previous batch's branch with this
        # batch's changes already applied in the working tree. Branch off
        # HEAD directly; the uncommitted changes carry over untouched.
        log.info("Stacking %s on top of %s", branch_name, stack_base)
        _git(repo_path, "checkout", "-B", branch_name)
        pr_base = stack_base
    else:
        _git(repo_path, "fetch", "origin", base_branch)
        _git(repo_path, "checkout", "-B", branch_name, f"origin/{base_branch}")
        pr_base = base_branch

    # ── 5. Commit each upstream commit's files separately ─────────────────
    # The caller passes one transformation bundle per upstream commit, in
    # order. We commit bundle-by-bundle so the PR contains one commit per
    # upstream commit (preserving per-commit history) instead of a single
    # squashed commit.
    bundles = transformations if transformations else [{}]
    n_bundles = len(bundles)
    n_commits = 0

    for i, bundle in enumerate(bundles):
        # Match this bundle to its upstream ref + analysis when available.
        ref = upstream_refs[i] if i < len(upstream_refs) else ""
        analysis = analyses[i] if i < len(analyses) else {}

        files = [f.get("path", "") for f in bundle.get("transformations", []) if f.get("path")]
        # Build-fix fixes are appended to the LAST bundle's commit.
        if i == n_bundles - 1:
            files += [f.get("path", "") for f in build_result.get("fixes_applied", []) if f.get("path")]

        for path in set(files):
            _git(repo_path, "add", "-A", "--", path, check=False)

        # Changelog goes in the last commit.
        if i == n_bundles - 1:
            changelog_path = os.path.join(repo_path, "CHANGELOG.sync.md")
            if os.path.exists(changelog_path):
                _git(repo_path, "add", "CHANGELOG.sync.md", check=False)

        intent = analysis.get("intent", "") or "upstream sync"
        if ref:
            msg = f"sync(upstream): {intent}\n\nUpstream-commit: {ref}"
        else:
            msg = f"sync(upstream): {intent}"

        r = _git(repo_path, "commit", "-m", msg, check=False)
        if r.returncode != 0:
            if "nothing to commit" in (r.stdout + r.stderr).lower():
                log.info("Bundle %d (%s): no changes — skipping empty commit", i, ref[:12] or "?")
                continue
            log.warning("Bundle %d commit failed: %s", i, r.stderr.strip())
            continue
        n_commits += 1
        log.info("Committed %s (%s)", ref[:12] or f"bundle {i}", intent[:60])

    if n_commits == 0:
        log.warning("Nothing to commit — no changes detected")
        return {"pr_url": "", "pr_number": 0, "status": "no_changes"}

    # PR title reflects the whole set.
    if len(upstream_refs) == 1:
        title = f"sync(upstream): {analyses[0].get('intent', 'upstream sync')}"
    else:
        title = f"sync(upstream): {len(upstream_refs)} commits from upstream"

    # ── 6. Push the branch ─────────────────────────────────────────────────
    # Use --force-with-lease to overwrite if branch existed from a prior run
    _git(repo_path, "push", "--force-with-lease", "origin", branch_name)

    # ── 7. Create the PR via GitHub API ───────────────────────────────────
    pr_body = _build_pr_body(upstream_refs, analyses, transformations, build_result)

    api_url = f"https://api.github.com/repos/{repo_slug}/pulls"
    headers = [
        "-H", "Accept: application/vnd.github+json",
        "-H", f"Authorization: Bearer {github_token}",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
    ]

    has_conflicts = any(t.get("conflicts") for t in transformations)
    payload = json.dumps({
        "title": title,
        "head": branch_name,
        "base": pr_base,
        "body": pr_body,
        "draft": has_conflicts or build_result.get("build_status") == "fail",
    })

    result = subprocess.run(
        ["curl", "-s", "-X", "POST", api_url] + headers + ["-d", payload],
        capture_output=True, text=True, check=True,
    )

    response = json.loads(result.stdout)
    pr_url = response.get("html_url", "")
    pr_number = response.get("number", 0)

    # ── 8. Apply labels ───────────────────────────────────────────────────
    labels = ["upstream-sync"]
    if any(a.get("change_type") == "breaking" for a in analyses):
        labels.append("breaking-change")
    if any(t.get("confidence") == "low" for t in transformations for tr in t.get("transformations", [])):
        labels.append("needs-manual-review")
    if has_conflicts:
        labels.append("has-conflicts")
    if build_result.get("build_status") == "fail":
        labels.append("build-failing")

    if pr_number and labels:
        label_url = f"https://api.github.com/repos/{repo_slug}/issues/{pr_number}/labels"
        subprocess.run(
            ["curl", "-s", "-X", "POST", label_url] + headers + ["-d", json.dumps({"labels": labels})],
            capture_output=True, text=True,
        )

    log.info("Created PR #%d: %s", pr_number, pr_url)
    return {"pr_url": pr_url, "pr_number": pr_number, "status": "created"}


# ── PR body builder ──────────────────────────────────────────────────────────

def _build_pr_body(
    upstream_refs: list[str],
    analyses: list[dict[str, Any]],
    transformations: list[dict[str, Any]],
    build_result: dict[str, Any],
) -> str:
    """Build the markdown PR body — supports single or consolidated PRs."""

    lines = []

    if len(upstream_refs) == 1:
        lines.append(f"## Upstream Sync: {analyses[0].get('intent', '')}")
        lines.append("")
        lines.append("### Upstream Reference")
        lines.append(f"- **Commit:** `{upstream_refs[0]}`")
        lines.append(f"- **Change type:** {analyses[0].get('change_type', 'unknown')}")
        lines.append(f"- **Risk level:** {analyses[0].get('risk', 'unknown')}")
        lines.append("")
        lines.append("### What Changed")
        lines.append(analyses[0].get("intent", ""))
        lines.append("")
        lines.append("### Affected Surfaces")
        for surface in analyses[0].get("surfaces", []):
            lines.append(f"- `{surface}`")
    else:
        lines.append(f"## Upstream Sync: {len(upstream_refs)} Commits")
        lines.append("")
        lines.append("### Upstream Commits")
        lines.append("| SHA | Intent | Type | Risk |")
        lines.append("|-----|--------|------|------|")
        for ref, analysis in zip(upstream_refs, analyses):
            lines.append(
                f"| `{ref[:12]}` | {analysis.get('intent', '')} | "
                f"{analysis.get('change_type', '')} | {analysis.get('risk', '')} |"
            )

        surfaces = set()
        for a in analyses:
            for s in a.get("surfaces", []):
                surfaces.add(s)
        if surfaces:
            lines.append("")
            lines.append("### Affected Surfaces")
            for s in sorted(surfaces):
                lines.append(f"- `{s}`")

    # Transformations table
    all_transforms = []
    for t_bundle in transformations:
        all_transforms.extend(t_bundle.get("transformations", []))

    if all_transforms:
        lines.append("")
        lines.append("### Transformations Applied")
        lines.append("| Downstream File | Confidence | Notes |")
        lines.append("|----------------|------------|-------|")
        for t in all_transforms:
            lines.append(f"| `{t.get('path', '')}` | {t.get('confidence', '')} | {t.get('notes', '')} |")

    # Conflicts
    all_conflicts = []
    for t_bundle in transformations:
        all_conflicts.extend(t_bundle.get("conflicts", []))

    if all_conflicts:
        lines.append("")
        lines.append("### ⚠️ Conflicts")
        for c in all_conflicts:
            lines.append(f"- **`{c.get('path', '')}`** ({c.get('region', '')}): {c.get('description', '')}")

    # Build failures
    unresolved = build_result.get("unresolved", [])
    if unresolved:
        lines.append("")
        lines.append("### ❌ Unresolved Build Failures")
        for u in unresolved:
            lines.append(f"- **`{u.get('path', '')}`**: {u.get('error', '')} — _Suggestion: {u.get('suggestion', '')}_")

    # New mapping candidates
    candidates = []
    for t_bundle in transformations:
        candidates.extend(t_bundle.get("new_mapping_candidates", []))

    if candidates:
        lines.append("")
        lines.append("### 🔍 New Mapping Candidates")
        for c in candidates:
            lines.append(f"- `{c.get('upstream', '')}` → `{c.get('downstream_guess', '')}` — {c.get('reason', '')}")

    lines.append("")
    lines.append("---")
    lines.append("🤖 Created by [upstream-semantic-sync](https://github.com/upstream-semantic-sync)")

    return "\n".join(lines)
