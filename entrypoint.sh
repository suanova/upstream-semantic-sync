#!/bin/bash
# Entrypoint for the Docker-based GitHub Action.
# Filters out empty arguments before passing them to the Python runtime,
# because GitHub Actions' conditional expressions produce empty strings ""
# instead of omitting the argument entirely.

set -e

# Build the argument list, skipping empty strings
ARGS=()
for arg in "$@"; do
  if [ -n "$arg" ]; then
    ARGS+=("$arg")
  fi
done

exec python -m agent.runtime "${ARGS[@]}"
