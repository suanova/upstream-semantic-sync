"""
upstream-semantic-sync — PR Creator

Creates a pull request with the synced upstream changes using the GitHub
API directly (no gh CLI needed). This is a local handler, not an LLM skill.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("sync.create_pr")


def create_pr(
    repo_path: str,
    branch_name: str,
    upstream_ref: str,
    analysis: dict[str, Any],
    transformations: dict[str, Any],
    build_result: dict[str, Any],
) -> dict[str, Any]:
    """Create a sync branch, commit transformed files, and open a PR.

    Uses GITHUB_TOKEN for both git push (via x-access-token) and the
    GitHub API for PR creation.
    """

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_actor = os.environ.get("GITHUB_ACTOR", "github-actions[bot]")
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "")

    if not github_token:
        return {"pr_url": "", "pr_number": 0, "status": "error: GITHUB_TOKEN not set"}
    if not repo_slug:
        return {"pr_url": "", "pr_number": 0, "status": "error: GITHUB_REPOSITORY not set"}

    # ── 1. Configure git identity ──────────────────────────────────────────
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        cwd=repo_path, check=True, capture_output=True,
    )

    # ── 2. Configure authenticated push URL ────────────────────────────────
    # Rewrite the origin URL to embed the token for push access
    if github_token and repo_slug:
        # repo_slug is "owner/repo" from GITHUB_REPOSITORY
        authed_url = f"https://x-access-token:{github_token}@github.com/{repo_slug}.git"
        subprocess.run(
            ["git", "remote", "set-url", "origin", authed_url],
            cwd=repo_path, check=True, capture_output=True,
        )

    # ── 3. Create the sync branch ──────────────────────────────────────────
    # Determine the default branch (main or master)
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    base_branch = result.stdout.strip()

    # Fetch latest and create branch
    subprocess.run(
        ["git", "fetch", "origin", base_branch],
        cwd=repo_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", branch_name, f"origin/{base_branch}"],
        cwd=repo_path, check=True, capture_output=True,
    )

    # ── 4. Stage and commit transformed files ──────────────────────────────
    t_files = transformations.get("transformations", [])
    b_files = build_result.get("fixes_applied", [])

    # Add all transformed and build-fixed files
    for f in t_files + b_files:
        path = f.get("path", "")
        if path:
            full = os.path.join(repo_path, path)
            if os.path.exists(full):
                subprocess.run(
                    ["git", "add", path],
                    cwd=repo_path, check=True, capture_output=True,
                )

    # Commit
    intent = analysis.get("intent", "upstream sync")
    change_type = analysis.get("change_type", "unknown")
    risk = analysis.get("risk", "unknown")
    surfaces = ", ".join(analysis.get("surfaces", []))

    commit_msg = (
        f"sync(upstream): {intent}\n\n"
        f"Upstream: {upstream_ref}\n"
        f"Change-Type: {change_type}\n"
        f"Risk: {risk}\n"
        f"Surfaces: {surfaces}"
    )

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
    pr_body = _build_pr_body(upstream_ref, analysis, transformations, build_result)

    api_url = f"https://api.github.com/repos/{repo_slug}/pulls"
    headers = [
        "-H", "Accept: application/vnd.github+json",
        "-H", f"Authorization: Bearer {github_token}",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
    ]

    payload = json.dumps({
        "title": f"sync(upstream): {intent}",
        "head": branch_name,
        "base": base_branch,
        "body": pr_body,
        "draft": bool(transformations.get("conflicts")) or build_result.get("build_status") != "pass",
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
    if change_type == "breaking":
        labels.append("breaking-change")
    if any(t.get("confidence") == "low" for t in t_files):
        labels.append("needs-manual-review")
    if transformations.get("conflicts"):
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
    upstream_ref: str,
    analysis: dict[str, Any],
    transformations: dict[str, Any],
    build_result: dict[str, Any],
) -> str:
    """Build the markdown PR body."""

    lines = [
        f"## Upstream Sync: {analysis.get('intent', '')}",
        "",
        "### Upstream Reference",
        f"- **Commit:** `{upstream_ref}`",
        f"- **Change type:** {analysis.get('change_type', 'unknown')}",
        f"- **Risk level:** {analysis.get('risk', 'unknown')}",
        "",
        "### What Changed",
        analysis.get("intent", ""),
        "",
        "### Affected Surfaces",
    ]

    for surface in analysis.get("surfaces", []):
        lines.append(f"- `{surface}`")

    lines.append("")
    lines.append("### Transformations Applied")
    lines.append("| Downstream File | Confidence | Notes |")
    lines.append("|----------------|------------|-------|")

    for t in transformations.get("transformations", []):
        lines.append(f"| `{t.get('path', '')}` | {t.get('confidence', '')} | {t.get('notes', '')} |")

    conflicts = transformations.get("conflicts", [])
    if conflicts:
        lines.append("")
        lines.append("### ⚠️ Conflicts")
        for c in conflicts:
            lines.append(f"- **`{c.get('path', '')}`** ({c.get('region', '')}): {c.get('description', '')}")

    unresolved = build_result.get("unresolved", [])
    if unresolved:
        lines.append("")
        lines.append("### ❌ Unresolved Build Failures")
        for u in unresolved:
            lines.append(f"- **`{u.get('path', '')}`**: {u.get('error', '')} — _Suggestion: {u.get('suggestion', '')}_")

    candidates = transformations.get("new_mapping_candidates", [])
    if candidates:
        lines.append("")
        lines.append("### 🔍 New Mapping Candidates")
        for c in candidates:
            lines.append(f"- `{c.get('upstream', '')}` → `{c.get('downstream_guess', '')}` — {c.get('reason', '')}")

    lines.append("")
    lines.append("---")
    lines.append("🤖 Created by [upstream-semantic-sync](https://github.com/upstream-semantic-sync)")

    return "\n".join(lines)
