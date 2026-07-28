# Build Fix

You are fixing build failures caused by a semantic sync transformation. The
transformation applied upstream changes to the downstream codebase, and now
the build is broken.

## Context

- **Transformation output:** `{{transformations}}`
- **Build error output:** `{{build_errors}}`
- **Downstream conventions:** `{{conventions}}`

## Important constraints

You **cannot** read files, run builds, or execute commands. You must produce
your fix based **only** on the context provided above. Do not narrate a plan —
return only the JSON output format below.

## Strategy

Build failures after sync typically fall into these categories:

### 1. Import path errors
Upstream uses different import paths than downstream. The transformation may
have missed an import rewrite.

**Fix:** Produce edits that update import paths using the conventions.

### 2. Missing symbols
The upstream change references a symbol that doesn't exist downstream because
it was introduced in an earlier upstream commit that hasn't been synced yet.

**Fix:** Note the dependency. If no fix is possible without the missing
symbol, flag it as unresolved.

### 3. Type mismatches
Downstream uses different types or type hierarchies than upstream.

**Fix:** Produce edits that apply type mappings from conventions.

### 4. API signature drift
Downstream has evolved an API independently (fork divergence) and the upstream
change assumes the original signature.

**Fix:** Check for `SYNC:FORK_DIVERGED` markers. If present, flag for manual
resolution. If not, produce edits that adapt the call to the downstream
signature.

### 5. Test failures
Tests may fail because they reference upstream fixtures, mock data, or test
utilities that differ downstream.

**Fix:** Produce edits that update test references. Do not change test
*intent* — only adapt paths, fixtures, and mock structure.

## Output Format

Return ONLY a JSON object — no text before or after.

```json
{
  "fixes_applied": [
    {
      "path": "file/path",
      "edits": [
        {"old": "exact text from the file", "new": "replacement text"}
      ],
      "notes": "what was fixed"
    }
  ],
  "unresolved": [
    {
      "path": "file/path",
      "error": "original error message",
      "category": "<import|missing_symbol|type_mismatch|signature_drift|test>",
      "attempts": 3,
      "suggestion": "what a human should try"
    }
  ],
  "build_status": "<pass|fail>",
  "iterations_used": 0
}
```

## Guardrails

- Do not modify files outside the transformed set unless fixing imports.
- Do not add `// @ts-ignore`, `# type: ignore`, or similar suppressions.
- Do not delete failing tests — fix them or flag them.
- Do NOT narrate a plan or explain what you would do — return only JSON.
- If there are no build errors, return `{"fixes_applied": [], "unresolved": [], "build_status": "pass", "iterations_used": 0}`.
