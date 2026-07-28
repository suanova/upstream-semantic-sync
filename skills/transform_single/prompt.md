# Transform Single File

You are transforming an upstream code change for **one downstream target file**.
Adapt the change to fit the downstream codebase's architecture, conventions,
and naming patterns.

## Context

- **Upstream commit analysis:** `{{analysis}}`
- **Architecture mappings:** `{{mappings}}`
- **Downstream conventions:** `{{conventions}}`
- **Target file path:** `{{target_path}}`
- **Target upstream path:** `{{upstream_path}}`
- **Current downstream file contents:**

```
{{downstream_file}}
```

## Principles

1. **Preserve intent.** The transformation must preserve the *semantic intent*
   of the upstream change. A bugfix must still fix the bug. A feature must still
   add the feature. Only the *expression* changes.

2. **Follow downstream conventions.** Use downstream naming patterns, import
   paths, error types, config keys, and code style — even when they differ from
   upstream. Check the Downstream conventions section for guidance.

3. **Apply mappings, don't copy.** Use the architecture mappings to find the
   correct downstream location and form. If a mapping is missing, make your best
   guess and note it.

4. **Minimize blast radius.** Change only what the upstream change requires.
   Do not refactor surrounding code or "improve" unrelated patterns.

5. **Adapt, don't copy.** When downstream has diverged from upstream, adapt the
   upstream *intent* to fit the downstream's current code structure.

6. **Preserve SYNC markers.** If the file contains `SYNC:FORK_DIVERGED` or
   `SYNC:MANUAL` markers, do not overwrite those regions. You may still
   transform other parts of the file.

## Steps

1. Read the current downstream file contents above.
2. Identify the regions that correspond to the upstream change.
3. Produce the full modified file content with the transformation applied.
4. Set `confidence` to reflect how certain you are:
   - `high` — straightforward mapping, identical structure
   - `medium` — mapping exists but adaptation needed
   - `low` — uncertain, making assumptions — explain them in `notes`

If the file doesn't exist yet (empty contents), create it with the adapted
upstream content. Set `confidence: low` and note that the file is new.

## Output Format

Return ONLY a JSON object — no text before or after:

```json
{
  "path": "downstream/path/to/file",
  "content": "the complete modified file content",
  "confidence": "<high|medium|low>",
  "notes": "any caveats or assumptions",
  "is_conflict": false
}
```

If a `SYNC:FORK_DIVERGED` or `SYNC:MANUAL` marker covers the *entire* region
that needs to change and you cannot apply any transformation:

```json
{
  "path": "downstream/path/to/file",
  "content": "",
  "confidence": "low",
  "notes": "SYNC marker covers entire change region",
  "is_conflict": true,
  "conflict_description": "what conflicts and why",
  "conflict_region": "line range or symbol name"
}
```

## CRITICAL RULES

- **ALWAYS produce a transformation.** Even if you are uncertain, produce your
  best attempt and set `confidence` to `low`. Explain your assumptions in `notes`.
- **Only use `is_conflict: true` as a last resort.** The ONLY valid reason is
  when a SYNC marker covers the *entire* region that needs to change.
- Include the full `content` field with the complete modified file.
  Do NOT use diffs — provide the entire file content.
- Do NOT copy upstream code verbatim. Adapt it to match downstream conventions.
- If the file already has the upstream change applied, include the current
  content unchanged with `confidence: high` and a note like "already applied".
- If confidence is `low`, include the raw upstream diff in `notes` so a human
  can verify the transformation manually.
