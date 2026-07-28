# Analyze Upstream Commit

You are analyzing an upstream commit to understand its **semantic intent** — not
just what changed, but *why* it changed and what it means for downstream
consumers.

## Input

The commit metadata, a `--stat` file list, and a **partial** diff are
provided below. The diff may be truncated — do not assume you've seen every
hunk. You do NOT need to fetch anything.

Note: the authoritative list of changed paths is extracted from git by the
caller — you do not need to reconstruct `affected_paths` from the diff.
Return the paths you see in the `--stat` block as a courtesy; the caller
will override with git's list anyway.

---

{{commit_data}}

---

## Classification rules

Classify the change type based on both the message and the diff:

| Signal | Classification |
|--------|---------------|
| New public function / exported symbol | `feature` |
| Changed function signature of exported symbol | `breaking` |
| `@deprecated` annotation added | `deprecation` |
| Fix in error handling / edge case | `bugfix` |
| Internal rename, reorder, no API change | `refactor` |
| Only touches build / CI / test infra | `internal` |

## Affected surfaces

A "surface" is any public contract:
- Exported functions, classes, types, constants.
- CLI flags or config keys.
- Wire/protocol fields.
- Database schema changes.

## Risk estimation

- `low` — internal-only changes, formatting, tests.
- `medium` — new features or bugfixes that touch shared code.
- `high` — breaking changes, deprecations, or changes to core abstractions.

## Output

Return ONLY a JSON object — no text before or after it:

```json
{
  "intent": "<one-line semantic summary>",
  "affected_paths": ["path/to/file1", "path/to/file2"],
  "change_type": "<feature|bugfix|refactor|breaking|deprecation|internal>",
  "surfaces": ["ExportedSymbol1", "ConfigKey2"],
  "risk": "<low|medium|high>",
  "dependencies": []
}
```
