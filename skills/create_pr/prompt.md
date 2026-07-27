# Create Sync Pull Request

You are creating a pull request that packages a semantically-synced upstream
change for downstream review and merge.

## Context

- **Branch:** `{{branch_name}}`
- **Upstream ref:** `{{upstream_ref}}`
- **Analysis:** `{{analysis}}`
- **Transformations:** `{{transformations}}`
- **Build result:** `{{build_result}}`

## Steps

1. **Create the branch** from the target branch (usually `main`):
   ```
   git checkout -b {{branch_name}} origin/main
   ```

2. **Stage and commit** the transformed files:
   - Include both the transformation diffs and any build fixes.
   - Use a commit message that follows this format:
     ```
     sync(upstream): {{analysis.intent}}

     Upstream: {{upstream_ref}}
     Change-Type: {{analysis.change_type}}
     Risk: {{analysis.risk}}
     Surfaces: {{analysis.surfaces}}
     ```

3. **Push the branch** to origin.

4. **Generate the PR body** using the template below.

5. **Create the PR** via the GitHub API (`gh pr create`).

6. **Apply labels** based on the analysis:
   - `upstream-sync` (always)
   - `breaking-change` if `analysis.change_type == breaking`
   - `needs-manual-review` if any transformations have `confidence == low`
   - `has-conflicts` if any conflicts were detected

7. **If build failed**, add the `build-failing` label and a comment
   describing the unresolved errors.

## PR Body Template

```markdown
## Upstream Sync: {{analysis.intent}}

### Upstream Reference
- **Commit:** {{upstream_ref}}
- **Change type:** {{analysis.change_type}}
- **Risk level:** {{analysis.risk}}

### What Changed
{{analysis.intent}}

### Affected Surfaces
{{analysis.surfaces formatted as list}}

### Transformations Applied
| Downstream File | Confidence | Notes |
|----------------|------------|-------|
{{#each transformations}}
| {{path}} | {{confidence}} | {{notes}} |
{{/each}}

{{#if conflicts}}
### ⚠️ Conflicts
{{#each conflicts}}
- **{{path}}** ({{region}}): {{description}}
{{/each}}
{{/if}}

{{#if build_result.unresolved}}
### ❌ Unresolved Build Failures
{{#each build_result.unresolved}}
- **{{path}}**: {{error}} — _Suggestion: {{suggestion}}_
{{/each}}
{{/if}}

{{#if transformations.new_mapping_candidates}}
### 🔍 New Mapping Candidates
These upstream→downstream mappings were discovered and need confirmation:
{{#each transformations.new_mapping_candidates}}
- `{{upstream}}` → `{{downstream_guess}}` — {{reason}}
{{/each}}
{{/if}}

---
🤖 Created by [upstream-semantic-sync](https://github.com/upstream-semantic-sync)
```

## Output Format

```json
{
  "pr_url": "https://github.com/org/repo/pull/123",
  "pr_number": 123
}
```
