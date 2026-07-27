# Downstream Conventions

This file explains how this downstream fork diverges from upstream. The
semantic sync agent reads this file to understand *why* code differs, which
helps it adapt upstream changes instead of flagging them as conflicts.

## Usage

Place this file at `knowledge/conventions.md` in your downstream repo.
If the repo has its own `knowledge/` directory, it takes precedence over
the builtin one.

## Sections

### Renamed symbols

List symbols that were renamed downstream and what they map to.

- `upstreamFunction` → `downstreamFunction` (reason)
- `UpstreamError` → `DownstreamError` (reason)

### Structural differences

Describe how the module structure differs from upstream.

- Upstream `core/permissions.py` was split into `auth/tiers.py` + `auth/circuit_breaker.py`
- Downstream wraps `Storage` in `StorageManager` which adds retry logic

### Intentional divergence

Describe regions that have intentionally diverged. Prefer using
`SYNC:FORK_DIVERGED` markers in code for fine-grained marking, but you can
also describe broader divergence patterns here.

- Error handling uses more aggressive retry logic than upstream
- Config validation is stricter than upstream's

### Import rewrites

List import path differences.

- `upstream_lib.core` → `our_lib.foundation`
- `upstream_lib.utils` → `our_lib.helpers`

### API signature differences

List functions whose signatures differ from upstream.

- `handle(x, y)` → `handle(x, y, config)` — downstream added a config parameter
- `process(data)` → `process(data, context=None)` — downstream added optional context
