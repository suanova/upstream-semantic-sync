#!/bin/bash
# Entrypoint for the Docker-based GitHub Action.
# Filters out empty arguments before passing them to the Python runtime,
# because GitHub Actions' conditional expressions produce empty strings ""
# instead of omitting the argument entirely.

set -e

# Fix "dubious ownership" error: Docker container runs as root but
# /github/workspace is owned by the runner user. Git 2.35+ refuses
# to operate on repos with mismatched ownership.
git config --global --add safe.directory /github/workspace
git config --global --add safe.directory '*'

# Build the argument list, skipping empty strings
ARGS=()
for arg in "$@"; do
  if [ -n "$arg" ]; then
    ARGS+=("$arg")
  fi
done

exec python -m agent.runtime "${ARGS[@]}"
