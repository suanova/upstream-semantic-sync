# Build Fix

You are fixing build failures caused by a semantic sync transformation. The
transformation applied upstream changes to the downstream codebase, and now
the build is broken.

## Context

- **Transformation output:** `{{transformations}}`
- **Build error output:** `{{build_errors}}`
- **Downstream repo path:** `{{repo_path}}`

## Strategy

Build failures after sync typically fall into these categories:

### 1. Import path errors
Upstream uses different import paths than downstream. The transformation may
have missed an import rewrite.

**Fix:** Update import paths using `knowledge/mappings.yaml` import aliases.

### 2. Missing symbols
The upstream change references a symbol that doesn't exist downstream because
it was introduced in an earlier upstream commit that hasn't been synced yet.

**Fix:** Check if the symbol exists in a pending upstream commit. If so, note
the dependency. If not, the symbol may need to be stubbed or the transformation
revised.

### 3. Type mismatches
Downstream uses different types or type hierarchies than upstream.

**Fix:** Apply type mappings from `knowledge/mappings.yaml#type_aliases`. Insert
necessary adapter code.

### 4. API signature drift
Downstream has evolved an API independently (fork divergence) and the upstream
change assumes the original signature.

**Fix:** Check for `SYNC:FORK_DIVERGED` markers. If present, flag for manual
resolution. If not, adapt the call to the downstream signature.

### 5. Test failures
Tests may fail because they reference upstream fixtures, mock data, or test
utilities that differ downstream.

**Fix:** Update test references using the same mapping rules. Do not change
test *intent* — only adapt paths, fixtures, and mock structure.

## Steps

1. Parse the build errors into structured failures (file, line, error type).

2. Categorize each failure using the categories above.

3. For each failure, apply the corresponding fix strategy:
   - Read the failing file.
   - Apply the minimal fix.
   - Re-check that the fix is syntactically valid.

4. Re-run the build (or type-check) to verify. If new failures appear,
   iterate up to 3 times.

5. If failures remain after 3 iterations, report them as unresolved.

## Output Format

```json
{
  "fixes_applied": [
    {
      "path": "file/path",
      "description": "what was fixed",
      "diff": "unified diff"
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
  "iterations_used": 2
}
```

## Guardrails

- Do not modify files outside the transformed set unless fixing imports.
- Do not add `// @ts-ignore`, `# type: ignore`, or similar suppressions.
- Do not delete failing tests — fix them or flag them.
