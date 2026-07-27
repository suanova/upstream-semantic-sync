# Transform Code

You are transforming upstream code changes to fit the downstream codebase's
architecture, conventions, and naming patterns.

## Context

- **Upstream commit analysis:** `{{analysis}}`
- **Architecture mappings:** `{{mappings}}`
- **Downstream targets:** `{{downstream_targets}}`
- **Current downstream file contents:** `{{downstream_files}}`
- **Downstream conventions:** `{{conventions}}`

## Principles

1. **Preserve intent.** The transformation must preserve the *semantic intent*
   of the upstream change. A bugfix must still fix the bug. A feature must still
   add the feature. Only the *expression* changes.

2. **Follow downstream conventions.** Use downstream naming patterns, import
   paths, error types, config keys, and code style — even when they differ from
   upstream.

3. **Apply mappings, don't copy.** Use the architecture mappings to find the
   correct downstream location and form. If a mapping is missing, make your best
   guess and note it.

4. **Minimize blast radius.** Change only what the upstream change requires.
   Do not refactor surrounding code or "improve" unrelated patterns.

5. **Adapt, don't copy.** When downstream has diverged from upstream, adapt the
   upstream *intent* to fit the downstream's current code structure. Do not
   blindly copy upstream's approach if it doesn't match downstream conventions.

6. **Preserve SYNC markers.** If downstream code contains `SYNC:FORK_DIVERGED`
   or `SYNC:MANUAL` markers, do not overwrite those regions. You may still
   transform other parts of the same file.

7. **Follow downstream conventions.** If the Downstream conventions context is
   provided, use it to understand how the fork diverges from upstream. It
   explains renamed symbols, structural differences, and intentional
   divergence. Use this information to adapt the upstream change rather than
   flagging it as a conflict.

## Steps

1. For each downstream target that has file contents provided:
   a. Read the current downstream file contents provided in the context.
   b. Identify the regions that correspond to the upstream change.
   c. Produce the full modified file content with the transformation applied.
   d. Set `confidence` to reflect how certain you are:
      - `high` — straightforward mapping, identical structure
      - `medium` — mapping exists but adaptation needed
      - `low` — uncertain, making assumptions — explain them in `notes`

2. For targets without file contents (file doesn't exist downstream yet):
   - Create the file with the adapted upstream content.
   - Set `confidence` to `low` and note that the file is new.

3. For SYNC:FORK_DIVERGED or SYNC:MANUAL markers:
   - You may still transform other parts of the file.
   - In `notes`, mention which marked regions were left untouched.

## Output Format

Return a JSON object:

```json
{
  "transformations": [
    {
      "path": "downstream/path/to/file",
      "content": "the complete modified file content after applying the transformation",
      "confidence": "<high|medium|low>",
      "notes": "any caveats or assumptions"
    }
  ],
  "conflicts": [
    {
      "path": "downstream/path/to/file",
      "description": "what conflicts and why",
      "region": "line range or symbol name"
    }
  ],
  "new_mapping_candidates": [],
  "skipped": []
}
```

## CRITICAL RULES

- **ALWAYS produce a transformation.** Even if you are uncertain about how the
  upstream change maps to downstream, produce your best attempt and set
  `confidence` to `low`. Explain your assumptions in `notes`.
- **Only use `conflicts` as a last resort.** The ONLY valid reason to put a
  target in `conflicts` is if a `SYNC:FORK_DIVERGED` or `SYNC:MANUAL` marker
  covers the *entire* region that needs to change, leaving you no room to apply
  the transformation without violating the marker.
- Each transformation MUST include the full `content` field with the complete
  modified file. Do NOT use diffs — provide the entire file content.
- Do NOT copy upstream code verbatim. Adapt it to match downstream conventions.
- If the downstream file already has the upstream change applied, still include
  it as a transformation with `confidence: high` and a note like "already applied".
- If confidence is `low`, include the raw upstream diff in `notes` so a human
  can verify the transformation manually.
