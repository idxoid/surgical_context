"""CLI bridge helpers for benchmark LLM judge (Claude Code + Codex).

Adapted from synto.bridge_providers but without Synto-specific system preamble.
Uses SYNTO_* / DEV_FACTORY_* env vars when set for timeouts and bridge dirs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from QA.bridge_output import normalize_bridge_stdout


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _env(
    synto_name: str,
    dev_factory_name: str | None = None,
    fallback: str | None = None,
) -> str | None:
    val = os.environ.get(synto_name)
    if val:
        return val
    if dev_factory_name:
        val = os.environ.get(dev_factory_name)
        if val:
            return val
    return fallback


def _env_bool(
    synto_name: str, dev_factory_name: str | None = None, *, default: bool = False
) -> bool:
    raw = _env(synto_name, dev_factory_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_cli_model(env_model: str | None, request_model: str | None) -> str | None:
    if request_model and request_model.strip():
        return request_model.strip()
    if env_model and env_model.strip():
        return env_model.strip()
    return None


@contextmanager
def _bridge_run_dir(base: Path) -> Iterator[Path]:
    run_dir = base / "runs" / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield run_dir
    finally:
        if not _env_bool("SYNTO_BRIDGE_KEEP_RUNS"):
            shutil.rmtree(run_dir, ignore_errors=True)


@dataclass(frozen=True)
class BridgeRequest:
    system: str
    prompt: str
    model: str | None = None


@dataclass(frozen=True)
class BridgeResponse:
    provider: str
    text: str
    model: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class BridgeProvider(ABC):
    name: str

    @abstractmethod
    def complete(self, request: BridgeRequest) -> BridgeResponse: ...

    def healthcheck(self) -> bool:
        return bool(shutil.which(self._cli_name()))

    @abstractmethod
    def _cli_name(self) -> str: ...


class ClaudeCodeBridgeProvider(BridgeProvider):
    name = "claude-code"

    def __init__(
        self,
        bridge_dir: str | Path | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        default_dir = Path(__file__).resolve().parent / ".bridge" / "claude"
        self.bridge_dir = Path(
            bridge_dir
            or _env("SYNTO_CLAUDE_BRIDGE_DIR", "DEV_FACTORY_CLAUDE_BRIDGE_DIR")
            or default_dir
        )
        timeout_raw = _env("SYNTO_CLAUDE_TIMEOUT_S", "DEV_FACTORY_CLAUDE_TIMEOUT_S")
        self.timeout_s = float(timeout_raw if timeout_raw is not None else timeout_s)
        self.no_tools = _env_bool("SYNTO_CLAUDE_NO_TOOLS", "DEV_FACTORY_CLAUDE_NO_TOOLS")
        self.bare = _env_bool("SYNTO_CLAUDE_BARE", "DEV_FACTORY_CLAUDE_BARE")
        self.no_session_persistence = _env_bool(
            "SYNTO_CLAUDE_NO_SESSION", "DEV_FACTORY_CLAUDE_NO_SESSION", default=True
        )
        self.model = _env("SYNTO_CLAUDE_MODEL", "DEV_FACTORY_CLAUDE_MODEL")
        self.effort = _env("SYNTO_CLAUDE_EFFORT", "DEV_FACTORY_CLAUDE_EFFORT")

    def _cli_name(self) -> str:
        return "claude"

    def complete(self, request: BridgeRequest) -> BridgeResponse:
        if not self.healthcheck():
            raise FileNotFoundError("Claude Code CLI `claude` not found on PATH")
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        user_prompt = request.prompt.strip()
        system_prompt = request.system.strip()
        cli_model = _resolve_cli_model(self.model, request.model)

        with _bridge_run_dir(self.bridge_dir) as run_dir:
            system_path = run_dir / "system.md"
            outbox = run_dir / "outbox.md"
            system_path.write_text(system_prompt, encoding="utf-8")
            command = self._command(system_path, cli_model)
            try:
                result = subprocess.run(
                    command,
                    input=user_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"claude -p timed out after {self.timeout_s}s") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"claude -p failed with exit {result.returncode}: {detail}")
            raw = (result.stdout or "").strip()
            if not raw:
                raise RuntimeError("claude -p returned empty stdout")
            text = normalize_bridge_stdout(raw, self.name)
            if not text:
                raise RuntimeError("claude -p returned empty payload after normalization")
            outbox.write_text(text + "\n", encoding="utf-8")
            return BridgeResponse(
                provider=self.name,
                text=text,
                model=cli_model,
                metadata={"run_dir": str(run_dir)},
            )

    def _command(self, system_path: Path, cli_model: str | None) -> list[str]:
        command = ["claude", "-p", "--add-dir", str(self.bridge_dir)]
        command.extend(["--system-prompt-file", str(system_path)])
        if self.no_tools:
            command.extend(["--tools", ""])
        if self.bare:
            command.append("--bare")
        if self.no_session_persistence:
            command.append("--no-session-persistence")
        if cli_model:
            command.extend(["--model", cli_model])
        if self.effort:
            command.extend(["--effort", self.effort])
        return command


class CodexBridgeProvider(BridgeProvider):
    name = "codex"

    def __init__(
        self,
        bridge_dir: str | Path | None = None,
        timeout_s: float = 600.0,
    ) -> None:
        default_dir = Path(__file__).resolve().parent / ".bridge" / "codex"
        self.bridge_dir = Path(
            bridge_dir
            or _env("SYNTO_CODEX_BRIDGE_DIR", "DEV_FACTORY_CODEX_BRIDGE_DIR")
            or default_dir
        )
        cwd_raw = _env("SYNTO_CODEX_CWD", "DEV_FACTORY_CODEX_CWD")
        self.cwd = Path(cwd_raw) if cwd_raw else self.bridge_dir
        timeout_raw = _env("SYNTO_CODEX_TIMEOUT_S", "DEV_FACTORY_CODEX_TIMEOUT_S")
        self.timeout_s = float(timeout_raw if timeout_raw is not None else timeout_s)
        self.model = _env("SYNTO_CODEX_MODEL", "DEV_FACTORY_CODEX_MODEL")
        self.profile = _env("SYNTO_CODEX_PROFILE", "DEV_FACTORY_CODEX_PROFILE")
        sandbox_raw = _env("SYNTO_CODEX_SANDBOX", "DEV_FACTORY_CODEX_SANDBOX", "read-only")
        self.sandbox = sandbox_raw or "read-only"
        self.ephemeral = _env_bool(
            "SYNTO_CODEX_EPHEMERAL", "DEV_FACTORY_CODEX_EPHEMERAL", default=True
        )
        self.ignore_rules = _env_bool("SYNTO_CODEX_IGNORE_RULES", "DEV_FACTORY_CODEX_IGNORE_RULES")

    def _cli_name(self) -> str:
        return "codex"

    def complete(self, request: BridgeRequest) -> BridgeResponse:
        if not self.healthcheck():
            raise FileNotFoundError("Codex CLI `codex` not found on PATH")
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        stdin_prompt = f"{request.system.strip()}\n\n---\n\n{request.prompt.strip()}"
        cli_model = _resolve_cli_model(self.model, request.model)

        with _bridge_run_dir(self.bridge_dir) as run_dir:
            outbox = run_dir / "outbox.md"
            command = self._command(outbox, cli_model)
            try:
                result = subprocess.run(
                    command,
                    input=stdin_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"codex exec timed out after {self.timeout_s}s") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"codex exec failed with exit {result.returncode}: {detail}")
            raw = (
                outbox.read_text(encoding="utf-8").strip()
                if outbox.exists()
                else (result.stdout or "").strip()
            )
            if not raw:
                raise RuntimeError("codex exec returned empty output")
            text = normalize_bridge_stdout(raw, self.name)
            if not text:
                raise RuntimeError("codex exec returned empty payload after normalization")
            return BridgeResponse(
                provider=self.name,
                text=text,
                model=cli_model,
                metadata={"run_dir": str(run_dir), "cwd": str(self.cwd)},
            )

    def _command(self, outbox: Path, cli_model: str | None) -> list[str]:
        command = [
            "codex",
            "exec",
            "--cd",
            str(self.cwd),
            "--add-dir",
            str(self.bridge_dir),
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--output-last-message",
            str(outbox),
        ]
        if self.ephemeral:
            command.append("--ephemeral")
        if self.ignore_rules:
            command.append("--ignore-rules")
        command.extend(["--sandbox", self.sandbox])
        if cli_model:
            command.extend(["--model", cli_model])
        if self.profile:
            command.extend(["--profile", self.profile])
        command.append("-")
        return command


def build_bridge_provider(
    name: str,
    *,
    bridge_dir: str | Path | None = None,
    timeout_s: float = 600.0,
) -> BridgeProvider:
    key = name.strip().lower()
    aliases = {
        "claude": "claude-code",
        "claude-code": "claude-code",
        "codex": "codex",
        "codex-cli": "codex",
    }
    normalized = aliases.get(key, key)
    if normalized == "claude-code":
        return ClaudeCodeBridgeProvider(bridge_dir=bridge_dir, timeout_s=timeout_s)
    if normalized == "codex":
        return CodexBridgeProvider(bridge_dir=bridge_dir, timeout_s=timeout_s)
    raise ValueError(f"Unknown bridge provider: {name!r} (expected claude-code or codex)")
