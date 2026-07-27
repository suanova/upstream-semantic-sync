# Resolve Conflict

You are resolving conflicts that arose during a semantic sync transformation.
The initial transform could not confidently apply the upstream change to the
downstream file. Your job is to **produce a transformation anyway** — make your
best attempt at adapting the upstream intent to the downstream code.

## Context

- **Conflicts to resolve:** `{{conflicts}}`
- **Upstream commit analysis:** `{{analysis}}`
- **Architecture mappings:** `{{mappings}}`
- **Current downstream file contents:** `{{downstream_files}}`
- **Downstream conventions:** `{{conventions}}`

## Your Mission

The first-pass transform flagged these targets as conflicts instead of producing
transformations. This is your second chance. You MUST produce a transformation
for each conflict. Do NOT return items in `conflicts` unless a SYNC:FORK_DIVERGED
or SYNC:MANUAL marker covers the entire region that needs to change.

## Strategy

When downstream has diverged from upstream, you must adapt the upstream *intent*
to fit the downstream's current code. **The Downstream conventions context is
your most important input** — it explains why the code differs and how to map
upstream concepts to downstream equivalents. Always check conventions before
attempting a resolution.

Here are common divergence patterns and how to handle them:

### 1. Renamed symbols
The upstream change calls `oldFunction()` but downstream renamed it to
`newFunction()`. Use the architecture mappings to find the renamed symbol.

**Resolution:** Replace with the downstream name. Set `confidence: medium`.

### 2. Different module structure
The upstream change adds code to `upstream_lib.core` but downstream uses
`our_lib.foundation`. Use the module mappings.

**Resolution:** Place the code in the mapped downstream module. Set
`confidence: medium`.

### 3. Diverged function signatures
The upstream change calls `handle(x, y)` but downstream's version takes
`handle(x, y, config)`. The intent is the same but the API differs.

**Resolution:** Adapt the call to match the downstream signature. Set
`confidence: low` and note the signature difference.

### 4. Extra downstream abstractions
The upstream change modifies a simple class, but downstream wraps it in an
adapter or manager. The change must go through the downstream abstraction.

**Resolution:** Apply the change at the appropriate layer in the downstream
abstraction. Set `confidence: low` and note the abstraction difference.

### 5. Already applied
The downstream code already has the upstream change applied (perhaps from a
previous manual sync).

**Resolution:** Include the current file content unchanged. Set
`confidence: high` and note "already applied".

### 6. SYNC:FORK_DIVERGED markers
The downstream file has regions marked with `SYNC:FORK_DIVERGED` or
`SYNC:MANUAL` that cover part of the change.

**Resolution:** Transform only the unmarked parts. In `notes`, list which
marked regions were left untouched. If the marker covers the *entire* region
that needs to change, put it in `conflicts`.

## Output Format

Return a JSON object — same format as the transform stage:

```json
{
  "transformations": [
    {
      "path": "downstream/path/to/file",
      "content": "the complete modified file content after resolving the conflict",
      "confidence": "<high|medium|low>",
      "notes": "what assumptions you made and why"
    }
  ],
  "conflicts": [
    {
      "path": "downstream/path/to/file",
      "description": "why this truly cannot be resolved",
      "region": "line range or symbol name"
    }
  ]
}
```

## CRITICAL RULES

- **You MUST produce a transformation for every conflict.** This is your
  second chance — the first pass already failed. Do not fail again.
- Only use `conflicts` if a `SYNC:FORK_DIVERGED` or `SYNC:MANUAL` marker
  covers the entire region that needs to change. Everything else must be
  resolved.
- Always include the full `content` field with the complete modified file.
  Do NOT use diffs.
- Adapt to downstream conventions — do NOT copy upstream code verbatim.
- Set `confidence: low` if you are uncertain, but still produce the
  transformation. A low-confidence transformation that a human can review
  is infinitely better than a conflict that blocks the pipeline.
- Explain all assumptions in `notes`.
