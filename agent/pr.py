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


def create_pr(
    repo_path: str,
    branch_name: str,
    upstream_refs: list[str],
    analyses: list[dict[str, Any]],
    transformations: list[dict[str, Any]],
    build_result: dict[str, Any],
) -> dict[str, Any]:
    """Create a sync branch, commit transformed files, and open a PR.

    For a single commit: upstream_refs has 1 entry, analyses has 1 entry.
    For a consolidated PR: upstream_refs has N entries, analyses has N entries.
    """

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")

    if not github_token:
        return {"pr_url": "", "pr_number": 0, "status": "error: GITHUB_TOKEN not set"}
    if not repo_slug:
        return {"pr_url": "", "pr_number": 0, "status": "error: GITHUB_REPOSITORY not set"}

    # ── 1. Configure git identity ──────────────────────────────────────────
    # Use --global since the checkout may be in a detached HEAD state
    # where local config is read-only
    subprocess.run(
        ["git", "config", "--global", "user.name", "github-actions[bot]"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"],
        cwd=repo_path, check=True, capture_output=True,
    )

    # ── 2. Configure authenticated push URL ────────────────────────────────
    if github_token and repo_slug:
        authed_url = f"https://x-access-token:{github_token}@github.com/{repo_slug}.git"
        subprocess.run(
            ["git", "remote", "set-url", "origin", authed_url],
            cwd=repo_path, check=True, capture_output=True,
        )

    # ── 3. Create the sync branch ──────────────────────────────────────────
    # Determine the base branch. In Actions, HEAD is often detached,
    # so fall back to GITHUB_BASE_REF or "main".
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    )
    base_branch = result.stdout.strip()
    if not base_branch or base_branch == "HEAD":
        # Fall back to env var or default
        base_branch = os.environ.get("GITHUB_BASE_REF", "") or \
                      os.environ.get("GITHUB_REF_NAME", "main")
        # Strip refs/heads/ prefix if present
        if base_branch.startswith("refs/heads/"):
            base_branch = base_branch.removeprefix("refs/heads/")

    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name, f"origin/{base_branch}"],
        cwd=repo_path, check=True, capture_output=True,
    )

    # ── 4. Stage and commit transformed files ──────────────────────────────
    # Collect all files from all transformations and build fixes
    all_files = []
    for t_bundle in transformations:
        for f in t_bundle.get("transformations", []):
            path = f.get("path", "")
            if path:
                all_files.append(path)

    for f in build_result.get("fixes_applied", []):
        path = f.get("path", "")
        if path:
            all_files.append(path)

    # Deduplicate
    for path in set(all_files):
        full = os.path.join(repo_path, path)
        if os.path.exists(full):
            subprocess.run(
                ["git", "add", path],
                cwd=repo_path, check=True, capture_output=True,
            )

    # Also stage the changelog if it exists
    changelog_path = os.path.join(repo_path, "CHANGELOG.sync.md")
    if os.path.exists(changelog_path):
        subprocess.run(
            ["git", "add", "CHANGELOG.sync.md"],
            cwd=repo_path, check=True, capture_output=True,
        )

    # Build commit message
    if len(upstream_refs) == 1:
        title = f"sync(upstream): {analyses[0].get('intent', 'upstream sync')}"
    else:
        title = f"sync(upstream): {len(upstream_refs)} commits from upstream"

    refs_str = "\n".join(f"  {ref}" for ref in upstream_refs)
    commit_msg = f"{title}\n\nUpstream commits:\n{refs_str}"

    result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning("Nothing to commit — no changes detected")
        return {"pr_url": "", "pr_number": 0, "status": "no_changes"}

    # ── 5. Push the branch ─────────────────────────────────────────────────
    subprocess.run(
        ["git", "push", "origin", branch_name],
        cwd=repo_path, check=True, capture_output=True,
    )

    # ── 6. Create the PR via GitHub API ───────────────────────────────────
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
        "base": base_branch,
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

    # ── 7. Apply labels ───────────────────────────────────────────────────
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
