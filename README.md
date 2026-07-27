# upstream-semantic-sync

A semantic sync agent that tracks upstream repository changes and adapts them
to a downstream fork — preserving intent while respecting architectural
differences.

## Why not just merge?

Blind merging breaks when your downstream fork has:

- **Different module structure** — imports don't resolve
- **Renamed symbols** — method calls reference names that don't exist
- **Diverged APIs** — signatures have evolved independently
- **Extra abstractions** — downstream wraps upstream concepts in new layers

Semantic sync understands *what the upstream change means* and *how it maps
to your codebase*, then applies the transformation instead of a raw merge.

## Pipeline

```
upstream commit
      │
      ▼
 ┌─────────────┐
 │  analyze     │  Classify intent, risk, affected surfaces
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  map         │  Map upstream paths → downstream targets
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  transform   │  Adapt code to downstream architecture
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  build-fix   │  Fix import errors, type mismatches, test failures
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  create-pr   │  Open a PR with full traceability
 └──────┬──────┘
        │
        ▼
   pull request
```

## Quick start

### CLI

```bash
# Initial sync: seed the last-synced SHA with the commit your fork is based on
python -m agent.runtime \
  --repo /path/to/downstream \
  --upstream https://github.com/upstream/repo \
  --commit abc1234

# After that, sync everything since the last run
python -m agent.runtime \
  --repo /path/to/downstream \
  --upstream https://github.com/upstream/repo \
  --since-last

# Sync a specific range
python -m agent.runtime \
  --repo /path/to/downstream \
  --upstream https://github.com/upstream/repo \
  --range abc1234..def5678

# Dry run (transform but don't create PR)
python -m agent.runtime \
  --repo /path/to/downstream \
  --upstream https://github.com/upstream/repo \
  --since-last \
  --dry-run
```

### GitHub Action

**Scheduled (weekly) — the normal mode:**

```yaml
on:
  schedule:
    - cron: '0 6 * * 1'  # every Monday at 6am
  workflow_dispatch:       # or trigger manually

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: your-org/upstream-semantic-sync@v1
        with:
          anthropic_auth_token: ${{ secrets.ANTHROPIC_AUTH_TOKEN }}
          upstream_repo: 'https://github.com/upstream/repo'
          # since_last defaults to true — syncs all new commits
          # since the last successful run
```

**One-off (specific commit):**

```yaml
- uses: your-org/upstream-semantic-sync@v1
  with:
    anthropic_auth_token: ${{ secrets.ANTHROPIC_AUTH_TOKEN }}
    upstream_repo: 'https://github.com/upstream/repo'
    since_last: 'false'
    commit: 'abc1234'
```

> **Required secret:** Add `ANTHROPIC_AUTH_TOKEN` as a repository or
> organization secret. The skills that power the pipeline (analyze,
> transform, build-fix) call the Anthropic API, so the auth token must
> be available. `GITHUB_TOKEN` is provided automatically by Actions for
> PR creation.

#### Bootstrap: setting the initial SHA

On first run, there's no last-synced SHA yet. You have two options:

1. **Run once with `commit`** — sync one commit; the agent records its SHA
   as the baseline. Subsequent `since_last` runs pick up from there.
2. **Set it manually** in `knowledge/mappings.yaml`:
   ```yaml
   sync_state:
     "https://github.com/upstream/repo#main": "the-sha-your-fork-is-based-on"
   ```

#### Custom endpoint / model

To point at a proxy or local gateway instead of the default Anthropic API:

```yaml
- uses: your-org/upstream-semantic-sync@v1
  with:
    anthropic_auth_token: ${{ secrets.ANTHROPIC_AUTH_TOKEN }}
    anthropic_base_url: 'http://127.0.0.1:8080'
    model: 'claude-sonnet-5-20250514'
    upstream_repo: 'https://github.com/upstream/repo'
```

### Docker

```bash
docker run --rm \
  -e ANTHROPIC_AUTH_TOKEN=<token> \
  -e ANTHROPIC_BASE_URL=http://127.0.0.1:8080 \
  -e ANTHROPIC_MODEL=claude-sonnet-5-20250514 \
  -e GITHUB_TOKEN=<token> \
  -v /path/to/repo:/repo \
  upstream-semantic-sync \
  --repo /repo \
  --upstream https://github.com/upstream/repo \
  --since-last
```

## Knowledge files

The agent's intelligence lives in `knowledge/`:

| File | Purpose |
|------|---------|
| `mappings.yaml` | Maps upstream modules, symbols, and config keys to downstream equivalents |
| `decisions.yaml` | Policy: risk thresholds, skip lists, PR settings |

These start empty and grow over time. The agent auto-discovers new mappings
and persists them for future runs.

### Marking fork divergence

When downstream code has intentionally diverged from upstream, mark it:

```python
# SYNC:FORK_DIVERGED
# We handle errors differently here because our retry logic
# is more aggressive than upstream's.
def handle_error(exc):
    ...
```

The sync agent will never overwrite marked regions.

## Skills

| Skill | Input | Output |
|-------|-------|--------|
| `analyze_commit` | Commit SHA | Intent, change type, risk, surfaces |
| `architecture_mapping` | Upstream paths | Downstream targets, unmapped paths |
| `transform_code` | Analysis + mappings | Code diffs, conflicts |
| `build_fix` | Transformations + errors | Fixes applied, unresolved failures |
| `create_pr` | All pipeline output | PR URL and number |

Each skill has a `prompt.md` (LLM prompt template) and optionally a
`skill.yaml` (metadata) and `rules.yaml` (deterministic rules).

## Publishing as a GitHub Action

To use `uses: upstream-semantic-sync@v1` in downstream workflows, this repo
must be published as a GitHub Action. Here's how.

### Prerequisites

- The repo lives on GitHub (e.g. `github.com/your-org/upstream-semantic-sync`)
- `action.yml` is at the repo root (already done)
- `Dockerfile` is at the repo root (already done — this is a Docker-based action)

### Step 1: Make the repo public

GitHub Actions can only be referenced via `uses:` from **public** repos
(unless the consumer is in the same org and uses an internal repo).

```bash
# Via GitHub CLI
gh repo edit --visibility public
```

### Step 2: Create a semver tag

GitHub Actions resolve `@v1` by finding the git tag `v1`. You need **two**
tags per release: an exact version tag (`v1.0.0`) and a moving major tag
(`v1`) that points to the same commit.

```bash
# Tag the release
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0

# Create or update the moving major tag
git tag -fa v1 -m "Release v1.0.0"
git push origin v1 --force
```

The `--force` on `v1` is intentional — it moves the major tag forward each
release so `@v1` always resolves to the latest `v1.x.x`.

### Step 3: Verify

In a downstream repo, create a workflow:

```yaml
# .github/workflows/upstream-sync.yml
name: Upstream Sync

on:
  schedule:
    - cron: '0 6 * * 1'   # every Monday at 6am UTC
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4

      - uses: your-org/upstream-semantic-sync@v1
        with:
          anthropic_auth_token: ${{ secrets.ANTHROPIC_AUTH_TOKEN }}
          upstream_repo: 'https://github.com/upstream/repo'
          upstream_branch: 'main'
          # since_last: 'true' is the default — syncs all new commits
```

You must also add `ANTHROPIC_AUTH_TOKEN` as a repository secret:
**Settings → Secrets and variables → Actions → New repository secret**.

Push it and confirm the action appears in the Actions tab.

### How `@v1` resolves

```
@v1          → tag v1 → commit (latest v1.x.x release)
@v1.0        → tag v1.0 → commit (latest v1.0.x patch)
@v1.0.0      → tag v1.0.0 → exact commit
@main        → branch main → HEAD (unstable, not recommended)
```

### Releasing a new version

```bash
# Bump version (e.g. patch release)
git tag -a v1.0.1 -m "Release v1.0.1"
git push origin v1.0.1

# Move the major tag
git tag -fa v1 -m "Release v1.0.1"
git push origin v1 --force

# If breaking changes, bump the major tag too
git tag -a v2.0.0 -m "Release v2.0.0"
git push origin v2.0.0
git tag -fa v2 -m "Release v2.0.0"
git push origin v2 --force
```

Downstreams pinned to `@v1` will get v1.0.1 automatically. Downstreams
pinned to `@v1.0.0` will stay on the exact release.

### GitHub Marketplace (optional)

To list the action on the [GitHub Marketplace](https://github.com/marketplace):

1. Go to the repo → **About** → check **List this action in the Marketplace**
2. The `action.yml` `branding` field (already set to `git-pull-request` / `blue`)
   controls the icon shown in the listing.

## License

MIT
