# Resolve Single Conflict

You are resolving a conflict for **one downstream target file** that the
initial transform could not handle. Your job is to **produce a transformation
anyway** — make your best attempt at adapting the upstream intent to the
downstream code.

## Context

- **Conflict description:** `{{conflict_description}}`
- **Conflict region:** `{{conflict_region}}`
- **Upstream commit analysis:** `{{analysis}}`
- **Architecture mappings:** `{{mappings}}`
- **Downstream conventions:** `{{conventions}}`
- **Target file path:** `{{target_path}}`
- **Current downstream file contents:**

```
{{downstream_file}}
```

## Your Mission

The first-pass transform flagged this file as a conflict. This is your second
chance. You MUST produce a transformation. Do NOT set `is_conflict: true`
unless a `SYNC:FORK_DIVERGED` or `SYNC:MANUAL` marker covers the *entire*
region that needs to change.

## Strategy

**The Downstream conventions context is your most important input** — it
explains why the code differs and how to map upstream concepts to downstream
equivalents. Always check conventions before attempting a resolution.

Common divergence patterns:

1. **Renamed symbols** — upstream calls `oldFunction()`, downstream renamed to
   `newFunction()`. Replace with the downstream name. Set `confidence: medium`.
2. **Different module structure** — upstream `core/X`, downstream `foundation/X`.
   Place in the mapped module. Set `confidence: medium`.
3. **Diverged signatures** — upstream `handle(x, y)`, downstream `handle(x, y, config)`.
   Adapt the call. Set `confidence: low`.
4. **Extra abstractions** — upstream modifies a simple class, downstream wraps it.
   Apply at the right layer. Set `confidence: low`.
5. **Already applied** — downstream already has the change.
   Return current content. Set `confidence: high`.
6. **SYNC markers** — transform only unmarked parts. If the marker covers
   everything, set `is_conflict: true`.

## Output Format

Return ONLY a JSON object — no text before or after:

```json
{
  "path": "downstream/path/to/file",
  "content": "the complete modified file content",
  "confidence": "<high|medium|low>",
  "notes": "what assumptions you made and why",
  "is_conflict": false
}
```

If a SYNC marker truly covers everything:

```json
{
  "path": "downstream/path/to/file",
  "content": "",
  "confidence": "low",
  "notes": "SYNC marker covers entire change region",
  "is_conflict": true,
  "conflict_description": "why this truly cannot be resolved",
  "conflict_region": "line range or symbol name"
}
```

## CRITICAL RULES

- **You MUST produce a transformation.** This is your second chance.
- Only use `is_conflict: true` if a SYNC marker covers the entire region.
- Always include the full `content` field with the complete modified file.
- Adapt to downstream conventions — do NOT copy upstream code verbatim.
- Set `confidence: low` if uncertain, but still produce the transformation.
  A low-confidence transformation is infinitely better than a blocking conflict.
- Explain all assumptions in `notes`.
