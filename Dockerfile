# ── upstream-semantic-sync ───────────────────────────────────────────────────
# Container image for the semantic sync agent.
#
# Build:
#   docker build -t upstream-semantic-sync .
#
# Run:
#   docker run --rm \
#     -e ANTHROPIC_AUTH_TOKEN=<token> \
#     -e ANTHROPIC_BASE_URL=http://127.0.0.1:8080 \
#     -e ANTHROPIC_MODEL=claude-sonnet-5-20250514 \
#     -e GITHUB_TOKEN=<token> \
#     -v /path/to/repo:/repo \
#     upstream-semantic-sync \
#     --repo /repo \
#     --upstream https://github.com/upstream/repo \
#     --commit abc1234
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (includes anthropic SDK)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent code
COPY agent/ agent/
COPY skills/ skills/
COPY knowledge/ knowledge/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "agent.runtime"]
