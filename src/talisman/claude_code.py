"""Call Claude through the Claude Code CLI (`claude -p`).

This uses whatever Claude Code sign-in the `claude` executable finds: the profile
in CLAUDE_CONFIG_DIR, or the default one. No API key is needed. Replies are cached
in SQLite by everything that shapes them, so re-running a prompt is free.
"""

import hashlib
import json
import logging
import os
import sqlite3
import subprocess  # noqa: S404
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_CACHE_DB = ".talisman_claude_cache.db"
MODEL_ALIAS_ENV_VARS = {
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "default": "ANTHROPIC_MODEL",
}


class ClaudeCodeError(Exception):
    """The Claude Code CLI failed or returned an error result."""


@dataclass
class ClaudeCodeClient:
    """Run single prompts through `claude -p`, optionally with MCP tools."""

    model: str = DEFAULT_CLAUDE_MODEL
    command: List[str] = field(default_factory=lambda: ["claude"])
    cache_db: Optional[str] = DEFAULT_CACHE_DB
    """SQLite cache path; None disables caching."""

    timeout: float = 900
    max_budget_usd: Optional[float] = None
    mcp_config: Optional[Dict] = None
    """An MCP config ({"mcpServers": {...}}); only these servers are loaded."""

    allowed_tools: List[str] = field(default_factory=list)
    """MCP tools the model may call without asking, e.g. mcp__amigo3__query_sql."""

    def complete(self, prompt: str, system_prompt: str, json_schema: Dict) -> Dict:
        """Send one prompt and return the structured reply with cost and model metadata."""
        key = self._key(prompt, system_prompt, json_schema)
        cached = self._cache_get(key)
        if cached is not None and self._is_cacheable(cached):
            return {**cached, "cached": True}
        args = [
            *self.command,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--system-prompt",
            system_prompt,
            "--json-schema",
            json.dumps(json_schema),
            # No built-in tools (shell, files, web); only the MCP tools passed in.
            "--tools",
            "",
            "--strict-mcp-config",
        ]
        if self.mcp_config:
            args += ["--mcp-config", json.dumps(self.mcp_config)]
            args += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.max_budget_usd is not None:
            args += ["--max-budget-usd", str(self.max_budget_usd)]
        # An empty working directory keeps project files and CLAUDE.md out of the context.
        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = subprocess.run(  # noqa: S603
                    args,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=cwd,
                )
            except FileNotFoundError as e:
                raise ClaudeCodeError(f"Claude Code CLI not found: {self.command[0]}") from e
            except subprocess.TimeoutExpired as e:
                raise ClaudeCodeError(f"claude did not finish within {self.timeout} s") from e
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise ClaudeCodeError(
                f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:2000]}"
            )
        if proc.returncode != 0 or data.get("is_error"):
            # subtype can read "success" on an API error, so report the error text itself
            reason = data.get("result") or data.get("subtype") or "no result"
            raise ClaudeCodeError(f"claude exited {proc.returncode}: {reason}")
        structured = data.get("structured_output")
        if structured is None:
            try:
                structured = json.loads(data.get("result") or "")
            except ValueError:
                raise ClaudeCodeError(f"No structured output in reply: {data.get('result')}")
        reply = {
            "structured_output": structured,
            "model": next(iter(data.get("modelUsage") or {}), self.model),
            "cost_usd": data.get("total_cost_usd"),
            "num_turns": data.get("num_turns"),
            "permission_denials": data.get("permission_denials") or [],
        }
        if self._is_cacheable(reply):
            self._cache_put(key, reply)
            resolved_model = reply.get("model")
            if resolved_model and resolved_model != self._effective_model():
                resolved_key = self._key(
                    prompt, system_prompt, json_schema, model=str(resolved_model)
                )
                self._cache_put(resolved_key, reply)
        return {**reply, "cached": False}

    def _expects_tool_use(self) -> bool:
        return bool(self.mcp_config)

    def _is_cacheable(self, reply: Dict) -> bool:
        if self._expects_tool_use() and (reply.get("num_turns") or 0) <= 2:
            return False
        return True

    def _effective_model(self, model: Optional[str] = None) -> str:
        chosen = model if model is not None else self.model
        env_var = MODEL_ALIAS_ENV_VARS.get(chosen)
        if env_var:
            override = os.environ.get(env_var, "").strip()
            if override:
                return override
        return chosen

    def _key(
        self, prompt: str, system_prompt: str, json_schema: Dict, model: Optional[str] = None
    ) -> str:
        parts = [
            self._effective_model(model),
            system_prompt,
            prompt,
            json_schema,
            self.mcp_config,
            self.allowed_tools,
        ]
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.cache_db)
        db.execute("CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, reply TEXT)")
        return db

    def _cache_get(self, key: str) -> Optional[Dict]:
        if not self.cache_db:
            return None
        with closing(self._connect()) as db:
            row = db.execute("SELECT reply FROM replies WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_put(self, key: str, reply: Dict) -> None:
        if not self.cache_db:
            return
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO replies VALUES (?, ?)", (key, json.dumps(reply)))
