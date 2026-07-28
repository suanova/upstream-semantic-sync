"""
upstream-semantic-sync — Executor

Runs individual skills by loading their prompt templates, interpolating
inputs, and invoking the LLM (or a local function for deterministic skills).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import anthropic
import yaml

log = logging.getLogger("sync.executor")

DEFAULT_MODEL = "claude-sonnet-5"


class SkillError(Exception):
    """Raised when a skill fails to execute."""


class Executor:
    """Executes skills by rendering prompts and running them."""

    def __init__(self, skills_dir: Path, knowledge_dir: Path) -> None:
        self.skills_dir = skills_dir
        self.knowledge_dir = knowledge_dir

        # Initialize the Anthropic client.
        #
        # Env vars:
        #   ANTHROPIC_AUTH_TOKEN  — API key (required)
        #   ANTHROPIC_BASE_URL    — custom endpoint, e.g. http://127.0.0.1:8080
        #   ANTHROPIC_MODEL       — model ID (default: claude-sonnet-5)
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not auth_token:
            raise SkillError(
                "ANTHROPIC_AUTH_TOKEN is not set. "
                "Export it or add it as a repository secret in your Action workflow."
            )

        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        client_kwargs: dict[str, Any] = {
            "api_key": auth_token,
            # Disable SDK-level retries — our _run_llm method handles
            # retries with custom backoff and logging. Without this,
            # the SDK retries 504s silently before our code gets a chance,
            # which wastes time and produces confusing logs.
            "max_retries": 0,
        }
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = anthropic.Anthropic(**client_kwargs)
        self.model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    # ── Public API ───────────────────────────────────────────────────────────

    def run_skill(self, skill_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run a named skill with the given inputs. Returns parsed JSON output."""

        skill_dir = self.skills_dir / skill_name
        if not skill_dir.exists():
            raise SkillError(f"Unknown skill: {skill_name}")

        # Load skill metadata
        meta = self._load_skill_meta(skill_dir)

        log.info(
            "Running skill %s (version %s)",
            meta.get("name", skill_name),
            meta.get("version", "unknown"),
        )

        # Determine execution mode
        handler = meta.get("handler", "llm")

        if handler == "local":
            return self._run_local(skill_name, inputs)

        # LLM handler — need a prompt template
        prompt = self._render_prompt(skill_dir, inputs)
        return self._run_llm(prompt, meta, skill_name)

    # ── LLM execution ───────────────────────────────────────────────────────

    def _run_llm(self, prompt: str, meta: dict[str, Any], name: str = "") -> dict[str, Any]:
        """Invoke Claude via the Anthropic Python SDK and parse structured JSON output."""

        import time

        model = meta.get("model", self.model)
        max_tokens = meta.get("max_tokens", 8192)
        timeout = meta.get("timeout", 300)
        max_retries = meta.get("max_retries", 3)

        # Human-readable request log — the SDK's own DEBUG dump is a single
        # unreadable line with the whole prompt escaped, so we emit our own
        # (and the SDK loggers are quieted in runtime.main).
        log.debug(
            "─── LLM request: %s (model=%s, max_tokens=%d, timeout=%ds, prompt=%d chars) ───\n%s",
            name or "?", model, max_tokens, timeout, len(prompt), prompt,
        )

        last_exc = None
        for attempt in range(max_retries):
            try:
                response = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    messages=[
                        {"role": "user", "content": prompt},
                    ],
                )
                break  # success
            except anthropic.InternalServerError as exc:
                # 500/502/503/504 — server-side transient errors, worth retrying
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    log.warning(
                        "Server error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    raise SkillError(f"Anthropic API error after {max_retries} retries: {exc}") from exc
            except anthropic.RateLimitError as exc:
                # 429 — rate limited, back off longer
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 4 * (2 ** attempt)  # 4s, 8s, 16s
                    log.warning(
                        "Rate limited (attempt %d/%d), retrying in %ds",
                        attempt + 1, max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    raise SkillError(f"Rate limited after {max_retries} retries: {exc}") from exc
            except anthropic.APIError as exc:
                # Other API errors (400, 401, 403, etc.) — not retryable
                raise SkillError(f"Anthropic API error: {exc}") from exc
            except anthropic.APIConnectionError as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log.warning(
                        "Connection error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    raise SkillError(f"Cannot reach Anthropic API after {max_retries} retries: {exc}") from exc

        # Extract text from the response
        text_blocks = [
            block.text for block in response.content if block.type == "text"
        ]
        raw = "\n".join(text_blocks)
        log.debug("─── LLM response: %s (%d chars) ───\n%s", name or "?", len(raw), raw)

        # Parse JSON from the LLM output, which may contain markdown
        # fencing or extra text after the JSON object.
        parsed = self._extract_json(raw)
        if parsed is not None:
            return parsed

        # The LLM sometimes returns prose instead of JSON (especially
        # build_fix, which used to prompt the model to "read files" and
        # "run builds" — actions it cannot take).  For skills that
        # tolerate a fallback, return a structured default so the
        # pipeline can continue; otherwise raise.
        if name == "build_fix":
            log.warning(
                "build_fix LLM returned non-JSON — returning fail fallback. "
                "Raw (first 300 chars): %s", raw[:300],
            )
            return {
                "fixes_applied": [],
                "unresolved": [],
                "build_status": "fail",
                "iterations_used": 0,
                "_parse_error": f"LLM returned non-JSON: {raw[:200]}",
            }

        raise SkillError(f"LLM output was not valid JSON.\nRaw output:\n{raw[:500]}")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Extract the first JSON object from text that may have extra content.

        Handles:
        - Raw JSON
        - JSON wrapped in ```json ... ```
        - JSON followed by explanatory text
        """
        text = text.strip()

        # Try 1: strip markdown fencing
        if text.startswith("```"):
            inner = re.sub(r"^```\w*\n?", "", text)
            inner = re.sub(r"\n?```$", "", inner.strip())
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass

        # Try 2: find the first { ... } using brace counting
        start = text.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start : i + 1])
                        except json.JSONDecodeError:
                            break

        # Try 3: the whole thing
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # ── Local (deterministic) execution ──────────────────────────────────────

    def _run_local(self, skill_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run a skill as a local Python function (no LLM needed).

        Used for deterministic skills like architecture_mapping and
        create_pr where the logic is better expressed as code than as
        an LLM prompt (e.g. git operations, API calls).
        """

        if skill_name == "architecture_mapping":
            return self._run_architecture_mapping(inputs)

        if skill_name == "create_pr":
            return self._run_create_pr(inputs)

        raise SkillError(f"No local handler for skill: {skill_name}")

    def _run_architecture_mapping(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Apply architecture mapping rules deterministically."""

        upstream_paths = inputs.get("upstream_paths", [])
        change_type = inputs.get("change_type", "unknown")

        # Load mapping rules
        rules_path = self.skills_dir / "architecture_mapping" / "rules.yaml"
        rules_data = yaml.safe_load(rules_path.read_text()) if rules_path.exists() else {}
        rules = rules_data.get("rules", [])

        # Load known mappings
        mappings_path = self.knowledge_dir / "mappings.yaml"
        mappings_data = yaml.safe_load(mappings_path.read_text()) if mappings_path.exists() else {}

        downstream_targets = []
        unmapped = []
        new_mappings = []

        module_maps = mappings_data.get("modules", {})
        surface_maps = mappings_data.get("surfaces", {})

        for path in upstream_paths:
            # Check direct module mapping
            if path in module_maps:
                target = module_maps[path]
                downstream_targets.append({
                    "upstream": path,
                    "downstream": target.get("downstream", path),
                    "confidence": target.get("confidence", "medium"),
                })
            else:
                # Try prefix matching
                matched = False
                for prefix, mapping in module_maps.items():
                    if path.startswith(prefix):
                        downstream = path.replace(prefix, mapping.get("downstream", prefix), 1)
                        downstream_targets.append({
                            "upstream": path,
                            "downstream": downstream,
                            "confidence": mapping.get("confidence", "medium"),
                        })
                        matched = True
                        break

                if not matched:
                    # Default: passthrough — assume same path exists downstream
                    # when no explicit mapping is configured
                    downstream_targets.append({
                        "upstream": path,
                        "downstream": path,
                        "confidence": "medium",
                    })

        return {
            "downstream_targets": downstream_targets,
            "unmapped": unmapped,
            "new_mappings": new_mappings,
        }

    def _run_create_pr(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Create a PR using the local Python handler (git + GitHub API)."""

        from agent.pr import create_pr

        return create_pr(
            repo_path=inputs.get("repo_path", ""),
            branch_name=inputs.get("branch_name", ""),
            upstream_refs=inputs.get("upstream_refs", []),
            analyses=inputs.get("analyses", []),
            transformations=inputs.get("transformations", []),
            build_result=inputs.get("build_result", {}),
        )

    # ── Prompt rendering ─────────────────────────────────────────────────────

    def _render_prompt(self, skill_dir: Path, inputs: dict[str, Any]) -> str:
        """Load the prompt template and interpolate {{variable}} placeholders."""

        prompt_path = skill_dir / "prompt.md"
        if not prompt_path.exists():
            raise SkillError(f"No prompt.md in {skill_dir}")

        template = prompt_path.read_text()

        # Interpolate {{key}} placeholders
        def replace_match(match: re.Match) -> str:
            key = match.group(1)
            value = inputs.get(key, "")
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)
            return str(value)

        rendered = re.sub(r"\{\{(\w+)\}\}", replace_match, template)

        # Also interpolate nested dot-notation like {{analysis.intent}}
        def replace_dot_match(match: re.Match) -> str:
            key_path = match.group(1)
            parts = key_path.split(".")
            value: Any = inputs
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part, "")
                else:
                    value = ""
                    break
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)
            return str(value)

        rendered = re.sub(r"\{\{([\w.]+)\}\}", replace_dot_match, rendered)

        return rendered

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_skill_meta(skill_dir: Path) -> dict[str, Any]:
        meta_path = skill_dir / "skill.yaml"
        if not meta_path.exists():
            return {}
        with open(meta_path) as f:
            return yaml.safe_load(f) or {}
