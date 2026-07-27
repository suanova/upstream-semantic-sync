# Analyze Upstream Commit

You are analyzing an upstream commit to understand its **semantic intent** — not
just what changed, but *why* it changed and what it means for downstream
consumers.

## Input

- **Repo:** `{{repo_url}}`
- **Commit:** `{{commit_sha}}` on branch `{{branch}}`

## Steps

1. **Fetch the commit diff.** Use `git show {{commit_sha}}` to retrieve the
   full diff, commit message, and metadata.

2. **Read the commit message** for explicit intent signals:
   - Conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `break:`, `deprecate:`).
   - References to issues, PRs, or RFCs.
   - Migration notes or breaking-change warnings.

3. **Classify the change type** based on both the message and the diff:
   | Signal | Classification |
   |--------|---------------|
   | New public function / exported symbol | `feature` |
   | Changed function signature of exported symbol | `breaking` |
   | `@deprecated` annotation added | `deprecation` |
   | Fix in error handling / edge case | `bugfix` |
   | Internal rename, reorder, no API change | `refactor` |
   | Only touches build / CI / test infra | `internal` |

4. **Identify affected surfaces.** A "surface" is any public contract:
   - Exported functions, classes, types, constants.
   - CLI flags or config keys.
   - Wire/protocol fields.
   - Database schema changes.

5. **Estimate sync risk:**
   - `low` — internal-only changes, formatting, tests.
   - `medium` — new features or bugfixes that touch shared code.
   - `high` — breaking changes, deprecations, or changes to core abstractions.

6. **Detect dependency chain.** Check if the commit references or is referenced
   by other recent commits (via `git log --ancestry-path` or commit message
   cross-references).

## Output Format

Return a JSON object:

```json
{
  "intent": "<one-line semantic summary>",
  "affected_paths": ["path/to/file1", "path/to/file2"],
  "change_type": "<feature|bugfix|refactor|breaking|deprecation|internal>",
  "surfaces": ["ExportedSymbol1", "ConfigKey2"],
  "risk": "<low|medium|high>",
  "dependencies": ["<sha1>", "<sha2>"]
}
```

## Important

- Do **not** guess at intent. If the commit message is ambiguous, say so in
  `intent` (e.g., "Unclear: renames internal helper, possibly prep for future change").
- Risk assessment must account for the *downstream* codebase's usage patterns,
  not just the upstream diff size.
