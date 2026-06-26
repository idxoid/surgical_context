#!/usr/bin/env python3
"""Local development helper for Surgical Context.

This script keeps the local daily-driver path in one place:

- prepare ignored data/log directories
- install and compile the VS Code extension
- start local Neo4j through Docker Compose
- run the FastAPI context_engine
- launch VS Code with the extension development path
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXTENSION_DIR = ROOT / "extension"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
SMOKE_PROJECT_DIR = ROOT / "context_engine" / "axis"
SMOKE_DOCS_PATH = ROOT / "docs" / "local_development.md"

LOCAL_DIRS = [
    ROOT / "data" / "lancedb",
    ROOT / "data" / "history",
    ROOT / "data" / "neo4j",
    ROOT / "logs" / "context_engine",
    ROOT / "logs" / "neo4j",
    ROOT / "import" / "neo4j",
    ROOT / "plugins" / "neo4j",
]


def _display_cmd(cmd: list[str]) -> str:
    return " ".join(cmd)


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    dry_run: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {_display_cmd(cmd)}")
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, env=env)


def _run_checked(cmd: list[str], *, hint: str, dry_run: bool = False) -> None:
    try:
        _run(cmd, dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        message = [
            f"Command failed with exit code {exc.returncode}: {_display_cmd(cmd)}",
            hint,
        ]
        raise SystemExit("\n".join(message)) from exc


def _tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _compose_cmd() -> list[str] | None:
    if _tool_exists("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    if _tool_exists("docker-compose"):
        return ["docker-compose"]
    return None


def _python_cmd() -> list[str]:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return [str(venv_python)]
    return [sys.executable]


def _context_engine_env(*, default_workspace_id: str | None = None) -> dict[str, str]:
    from context_engine.env_loader import load_repo_dotenv

    load_repo_dotenv(path=ENV_FILE)
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(ROOT))
    env.setdefault("LANCEDB_PATH", str(ROOT / "data" / "lancedb"))
    env.setdefault(
        "DEFAULT_WORKSPACE_ID",
        default_workspace_id or "local/surgical_context@main",
    )
    return env


def _resolve_smoke_workspace_id(project_path: Path, explicit: str) -> str:
    from context_engine.workspace import WorkspaceResolver

    return (
        WorkspaceResolver()
        .from_project_path(
            str(project_path),
            value=explicit.strip() or None,
        )
        .id
    )


_LOCAL_SIDECAR_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _default_sidecar_scheme(host: str) -> str:
    return "http" if host in _LOCAL_SIDECAR_HOSTS else "https"


def _sidecar_base_url(host: str, port: int, scheme: str | None = None) -> str:
    resolved = scheme or _default_sidecar_scheme(host)
    return f"{resolved}://{host}:{port}"


def _api_url(base_url: str, path: str, query: dict[str, str] | None = None) -> str:
    url = base_url.rstrip("/") + path
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return url


def _request(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    workspace_id: str,
    timeout: float,
) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Workspace": workspace_id,
            "X-User-Id": "local-smoke",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc


def _request_json(
    method: str,
    base_url: str,
    path: str,
    *,
    payload: dict | None = None,
    workspace_id: str,
    timeout: float,
    query: dict[str, str] | None = None,
) -> dict:
    _, body = _request(
        method,
        _api_url(base_url, path, query),
        payload=payload,
        workspace_id=workspace_id,
        timeout=timeout,
    )
    return json.loads(body or "{}")


def _request_text(
    method: str,
    base_url: str,
    path: str,
    *,
    workspace_id: str,
    timeout: float,
) -> str:
    _, body = _request(
        method,
        _api_url(base_url, path),
        workspace_id=workspace_id,
        timeout=timeout,
    )
    return body


def _wait_for_health(
    *,
    base_url: str,
    workspace_id: str,
    timeout: float,
    request_timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            health = _request_json(
                "GET",
                base_url,
                "/health",
                workspace_id=workspace_id,
                timeout=request_timeout,
            )
            if health.get("status") == "ok":
                return health
            last_error = f"unexpected health response: {health}"
        except RuntimeError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(
        f"Sidecar did not become healthy at {base_url} within {timeout:.1f}s. "
        f"Last error: {last_error}"
    )


def _stop_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_for_tcp(host: str, port: int, *, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(
        f"{label} did not become reachable at {host}:{port} within {timeout:.1f}s. "
        f"Last error: {last_error}"
    )


def prepare_local_dirs(*, dry_run: bool = False) -> None:
    for path in LOCAL_DIRS:
        if dry_run:
            print(f"Would create {path.relative_to(ROOT)}")
            continue
        path.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        print("Prepared local data/log directories.")


def ensure_env_file(*, dry_run: bool = False) -> None:
    if ENV_FILE.exists():
        print(".env already exists.")
        return
    if not ENV_EXAMPLE.exists():
        print("No .env.example found; skipped .env creation.")
        return
    if dry_run:
        print("Would create .env from .env.example.")
        return
    shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
    print("Created .env from .env.example.")


def install_extension(args: argparse.Namespace) -> None:
    if args.skip_npm:
        print("Skipped extension dependency install.")
        return

    if not _tool_exists("npm") and not args.dry_run:
        raise SystemExit("npm is required to install/compile the extension.")

    node_modules = EXTENSION_DIR / "node_modules"
    deps_ready = node_modules.exists() and (node_modules / "esbuild").exists()
    if deps_ready and not args.force_npm:
        print("extension/node_modules already exists.")
    else:
        if node_modules.exists() and not args.force_npm:
            print("extension/node_modules is incomplete; running npm install.")
        _run(["npm", "install"], cwd=EXTENSION_DIR, dry_run=args.dry_run)

    if not args.skip_compile:
        _run(["npm", "run", "compile"], cwd=EXTENSION_DIR, dry_run=args.dry_run)


def start_storage(args: argparse.Namespace) -> None:
    if args.skip_storage:
        print("Skipped Neo4j Docker startup.")
        return

    compose = _compose_cmd()
    if compose is None and args.dry_run:
        compose = ["docker", "compose"]
    if compose is None:
        raise SystemExit("Docker Compose is required to start local Neo4j.")

    _run_checked(
        [*compose, "up", "-d", "neo4j"],
        dry_run=args.dry_run,
        hint=(
            "Docker Compose could not start Neo4j. If this mentions an old "
            "`surgical-network`, remove that stale network with "
            "`docker network rm surgical-network`, or rerun with --skip-storage "
            "if Neo4j is already running. If this mentions Docker socket "
            "permission denied, start Docker or run from a user allowed to "
            "access /var/run/docker.sock."
        ),
    )
    if not args.dry_run:
        _wait_for_tcp(
            "127.0.0.1",
            7687,
            timeout=getattr(args, "storage_start_timeout", 90.0),
            label="Neo4j Bolt",
        )


def context_engine_command(args: argparse.Namespace) -> list[str]:
    host = args.host or "127.0.0.1"
    port = str(args.port or 8000)
    cmd = [
        *_python_cmd(),
        "-m",
        "uvicorn",
        "context_engine.main:app",
        "--host",
        host,
        "--port",
        port,
    ]
    if args.reload:
        cmd.append("--reload")
    return cmd


def code_command() -> list[str]:
    return [
        "code",
        f"--extensionDevelopmentPath={EXTENSION_DIR}",
        str(ROOT),
    ]


def print_next_steps(args: argparse.Namespace) -> None:
    context_engine = context_engine_command(args)
    code = code_command()
    print("\nNext terminals:")
    print(f"  1. {_display_cmd(context_engine)}")
    print(f"  2. {_display_cmd(code)}")
    print("\nUseful checks:")
    print("  python scripts/local_dev.py doctor")
    print("  curl http://127.0.0.1:8000/health")
    print("  curl http://127.0.0.1:8000/status/cloud")


def doctor(_: argparse.Namespace) -> int:
    checks = [
        ("Python", True, sys.version.split()[0]),
        ("Docker Compose", _compose_cmd() is not None, "docker compose or docker-compose"),
        ("npm", _tool_exists("npm"), "extension dependency install"),
        ("VS Code CLI", _tool_exists("code"), "extension dev host launch"),
        (".env", ENV_FILE.exists(), str(ENV_FILE)),
        ("extension/node_modules", (EXTENSION_DIR / "node_modules").exists(), "npm install"),
    ]

    ok = True
    for label, passed, detail in checks:
        mark = "ok" if passed else "missing"
        print(f"{label:24} {mark:8} {detail}")
        ok = ok and passed

    for path in LOCAL_DIRS:
        exists = path.exists()
        mark = "ok" if exists else "missing"
        print(f"{path.relative_to(ROOT)!s:24} {mark:8} local path")
        ok = ok and exists

    return 0 if ok else 1


def bootstrap(args: argparse.Namespace) -> int:
    ensure_env_file(dry_run=args.dry_run)
    prepare_local_dirs(dry_run=args.dry_run)
    start_storage(args)
    install_extension(args)
    print_next_steps(args)
    return 0


def run_context_engine(args: argparse.Namespace) -> int:
    prepare_local_dirs(dry_run=args.dry_run)
    cmd = context_engine_command(args)
    return _run(cmd, dry_run=args.dry_run, env=_context_engine_env()).returncode


def launch_code(args: argparse.Namespace) -> int:
    if not _tool_exists("code") and not args.dry_run:
        raise SystemExit("VS Code CLI 'code' is required to launch the extension dev host.")
    return _run(code_command(), dry_run=args.dry_run).returncode


def _smoke_step(label: str, action) -> object:
    print(f"[smoke] {label} ... ", end="", flush=True)
    try:
        result = action()
    except Exception:
        print("failed", flush=True)
        raise
    print("ok", flush=True)
    return result


def _assert_path(path: Path, label: str) -> None:
    if not path.exists():
        raise RuntimeError(f"Missing {label}: {path}")


def _ensure_context_engine_for_smoke(
    args: argparse.Namespace,
    *,
    base_url: str,
    workspace_id: str,
) -> tuple[dict, subprocess.Popen | None]:
    try:
        health = _request_json(
            "GET",
            base_url,
            "/health",
            workspace_id=workspace_id,
            timeout=min(args.timeout, 2.0),
        )
        return health, None
    except RuntimeError as exc:
        if args.no_start_context_engine:
            raise RuntimeError(
                f"Sidecar is not reachable at {base_url}. Start it with "
                "`python scripts/local_dev.py context_engine --reload`, or run smoke "
                "without --no-start-context_engine to let the smoke test start a "
                "temporary context_engine."
            ) from exc

        print(
            f"\n[smoke] context_engine is not reachable at {base_url}; starting temporary context_engine"
        )
        cmd = context_engine_command(args)
        print(f"$ {_display_cmd(cmd)}")
        process = subprocess.Popen(
            cmd, cwd=ROOT, env=_context_engine_env(default_workspace_id=workspace_id)
        )
        try:
            health = _wait_for_health(
                base_url=base_url,
                workspace_id=workspace_id,
                timeout=args.context_engine_start_timeout,
                request_timeout=min(args.timeout, 2.0),
            )
        except Exception:
            _stop_process(process)
            raise
        return health, process


def _smoke_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    return _sidecar_base_url(args.host, args.port, args.scheme or None)


def _smoke_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    default_project_path = ROOT if args.full_repo else SMOKE_PROJECT_DIR
    default_docs_path = ROOT / "docs" if args.full_repo else SMOKE_DOCS_PATH
    project_path = Path(args.project_path or default_project_path).resolve()
    docs_path = Path(args.docs_path or default_docs_path).resolve()
    return project_path, docs_path


def _validate_smoke_workspace_id(project_path: Path, workspace_id: str, args: argparse.Namespace) -> None:
    if not args.workspace_id.strip():
        return
    from context_engine.workspace import assert_workspace_repo_matches_project_root

    try:
        assert_workspace_repo_matches_project_root(project_path, workspace_id)
    except ValueError as exc:
        raise RuntimeError(
            f"{exc}. Use --workspace-id local/{project_path.name}@main, "
            "or index the full repo with --full-repo."
        ) from exc


def _smoke_dry_run(
    args: argparse.Namespace,
    *,
    base_url: str,
    project_path: Path,
    docs_path: Path,
    workspace_id: str,
) -> int:
    print(
        "Smoke would check extension assets, local dirs, context_engine health, indexes, ask, impact, and metrics."
    )
    if not args.skip_storage:
        compose = _compose_cmd() or ["docker", "compose"]
        print(f"$ {_display_cmd([*compose, 'up', '-d', 'neo4j'])}")
    if not args.no_start_context_engine:
        print(f"$ {_display_cmd(context_engine_command(args))}")
    print(f"Project path: {project_path}")
    print(f"Docs path: {docs_path}")
    print(f"Base URL: {base_url}")
    print(f"Workspace: {workspace_id}")
    return 0


def _run_smoke_preflight(args: argparse.Namespace) -> None:
    _smoke_step(
        "extension assets",
        lambda: [
            _assert_path(EXTENSION_DIR / "dist" / "extension.js", "extension host bundle"),
            _assert_path(EXTENSION_DIR / "media" / "main.js", "main webview bundle"),
            _assert_path(EXTENSION_DIR / "media" / "styles.css", "webview stylesheet"),
        ],
    )
    _smoke_step(
        "local dirs",
        lambda: [_assert_path(path, path.relative_to(ROOT).as_posix()) for path in LOCAL_DIRS],
    )
    _smoke_step("Neo4j storage", lambda: start_storage(args))


def _run_smoke_api_checks(
    args: argparse.Namespace,
    *,
    base_url: str,
    workspace_id: str,
    project_path: Path,
    docs_path: Path,
) -> None:
    _smoke_step(
        "cloud/provider status",
        lambda: _request_json(
            "GET",
            base_url,
            "/status/cloud",
            workspace_id=workspace_id,
            timeout=args.timeout,
        ),
    )

    if not args.skip_index:
        _smoke_step(
            "index code",
            lambda: _request_json(
                "POST",
                base_url,
                "/index",
                payload={"project_path": str(project_path), "queue": False},
                workspace_id=workspace_id,
                timeout=args.long_timeout,
            ),
        )

    if not args.skip_docs:
        _smoke_step(
            "index docs",
            lambda: _request_json(
                "POST",
                base_url,
                "/index/docs",
                payload={"docs_path": str(docs_path)},
                workspace_id=workspace_id,
                timeout=args.long_timeout,
            ),
        )

    search = _smoke_step(
        "unified search",
        lambda: _request_json(
            "POST",
            base_url,
            "/search/unified",
            payload={
                "query": args.question,
                "symbol": args.symbol,
                "limit": 3,
                "include_graph": True,
                "token_budget": args.token_budget,
            },
            workspace_id=workspace_id,
            timeout=args.timeout,
        ),
    )
    if "results" not in search:
        raise RuntimeError(f"Unexpected search response: {search}")

    ask = _smoke_step(
        "ask",
        lambda: _request_json(
            "POST",
            base_url,
            "/ask",
            payload={
                "symbol": args.symbol,
                "question": args.question,
                "token_budget": args.token_budget,
            },
            workspace_id=workspace_id,
            timeout=args.long_timeout,
        ),
    )
    if not ask.get("context") or not ask.get("trace_id"):
        raise RuntimeError(f"Unexpected ask response keys: {sorted(ask)}")

    _smoke_step(
        "impact",
        lambda: _request_json(
            "GET",
            base_url,
            "/impact",
            workspace_id=workspace_id,
            timeout=args.timeout,
            query={"symbol": args.symbol},
        ),
    )

    metrics = _smoke_step(
        "dashboard metrics",
        lambda: _request_text(
            "GET",
            base_url,
            "/metrics",
            workspace_id=workspace_id,
            timeout=args.timeout,
        ),
    )
    if "context_engine_" not in metrics:
        raise RuntimeError("Metrics response did not contain context_engine metrics.")


def smoke(args: argparse.Namespace) -> int:
    """Run a local product smoke test against a running context_engine."""
    base_url = _smoke_base_url(args)
    project_path, docs_path = _smoke_paths(args)
    workspace_id = _resolve_smoke_workspace_id(project_path, args.workspace_id)
    _validate_smoke_workspace_id(project_path, workspace_id, args)

    if args.dry_run:
        return _smoke_dry_run(
            args,
            base_url=base_url,
            project_path=project_path,
            docs_path=docs_path,
            workspace_id=workspace_id,
        )

    _run_smoke_preflight(args)

    context_engine_process = None
    try:
        health, context_engine_process = _smoke_step(
            "context_engine health",
            lambda: _ensure_context_engine_for_smoke(
                args,
                base_url=base_url,
                workspace_id=workspace_id,
            ),
        )
        if health.get("status") != "ok":
            raise RuntimeError(f"Unexpected health response: {health}")

        _run_smoke_api_checks(
            args,
            base_url=base_url,
            workspace_id=workspace_id,
            project_path=project_path,
            docs_path=docs_path,
        )

        print("\nLocal smoke test passed.")
        return 0
    finally:
        if context_engine_process is not None:
            print("[smoke] stopping temporary context_engine")
            _stop_process(context_engine_process)


def up(args: argparse.Namespace) -> int:
    bootstrap(args)
    context_engine_cmd = context_engine_command(args)

    if args.dry_run:
        print(f"$ {_display_cmd(context_engine_cmd)}")
        if args.launch_code:
            print(f"$ {_display_cmd(code_command())}")
        return 0

    print("\nStarting context_engine. Press Ctrl+C here to stop it.")
    context_engine = subprocess.Popen(context_engine_cmd, cwd=ROOT, env=_context_engine_env())
    try:
        time.sleep(args.launch_delay)
        if args.launch_code:
            launch_code(args)
        return context_engine.wait()
    except KeyboardInterrupt:
        print("\nStopping context_engine...")
        context_engine.terminate()
        try:
            return context_engine.wait(timeout=5)
        except subprocess.TimeoutExpired:
            context_engine.kill()
            return context_engine.wait()


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without running them"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Sidecar host for printed/run commands")
    parser.add_argument("--port", type=int, default=8000, help="Sidecar port")
    parser.add_argument("--reload", action="store_true", help="Run uvicorn with --reload")


def add_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--skip-storage", action="store_true", help="Do not start Neo4j")
    parser.add_argument(
        "--storage-start-timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for Neo4j Bolt to become reachable",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Surgical Context local development helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Check local developer prerequisites")
    doctor_parser.set_defaults(func=doctor)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="Prepare local dirs, storage, and extension build"
    )
    add_common_args(bootstrap_parser)
    add_storage_args(bootstrap_parser)
    bootstrap_parser.add_argument("--skip-npm", action="store_true", help="Do not run npm install")
    bootstrap_parser.add_argument(
        "--force-npm", action="store_true", help="Run npm install even if node_modules exists"
    )
    bootstrap_parser.add_argument(
        "--skip-compile", action="store_true", help="Do not compile extension"
    )
    bootstrap_parser.set_defaults(func=bootstrap)

    context_engine_parser = subparsers.add_parser(
        "context_engine", help="Run the FastAPI context_engine"
    )
    add_common_args(context_engine_parser)
    context_engine_parser.set_defaults(func=run_context_engine)

    code_parser = subparsers.add_parser("code", help="Launch VS Code extension dev host")
    code_parser.add_argument(
        "--dry-run", action="store_true", help="Print command without running it"
    )
    code_parser.set_defaults(func=launch_code)

    up_parser = subparsers.add_parser(
        "up", help="Bootstrap, run context_engine, optionally launch VS Code"
    )
    add_common_args(up_parser)
    add_storage_args(up_parser)
    up_parser.add_argument("--skip-npm", action="store_true", help="Do not run npm install")
    up_parser.add_argument(
        "--force-npm", action="store_true", help="Run npm install even if node_modules exists"
    )
    up_parser.add_argument("--skip-compile", action="store_true", help="Do not compile extension")
    up_parser.add_argument(
        "--launch-code", action="store_true", help="Open VS Code after context_engine starts"
    )
    up_parser.add_argument(
        "--launch-delay", type=float, default=2.0, help="Seconds to wait before launching VS Code"
    )
    up_parser.set_defaults(func=up)

    smoke_parser = subparsers.add_parser("smoke", help="Run local ask/search/impact smoke test")
    add_common_args(smoke_parser)
    add_storage_args(smoke_parser)
    smoke_parser.add_argument(
        "--base-url",
        default="",
        help="Existing context_engine URL. Defaults to <scheme>://<host>:<port>.",
    )
    smoke_parser.add_argument(
        "--scheme",
        choices=("http", "https"),
        default="",
        help=(
            "URL scheme when --base-url is omitted. Defaults to http for localhost "
            "and https otherwise."
        ),
    )
    smoke_parser.add_argument(
        "--workspace-id",
        default="",
        help=(
            "X-Workspace header. Defaults to local/<project-dir-basename>@<git-ref> "
            "derived from --project-path (required for sandbox registration)."
        ),
    )
    smoke_parser.add_argument(
        "--project-path",
        default="",
        help="Code path to index. Defaults to context_engine/axis for a fast smoke test.",
    )
    smoke_parser.add_argument(
        "--docs-path",
        default="",
        help="Docs path to index. Defaults to docs/local_development.md for a fast smoke test.",
    )
    smoke_parser.add_argument(
        "--full-repo",
        action="store_true",
        help="Use the full repo and docs/ as smoke index targets.",
    )
    smoke_parser.add_argument("--symbol", default="run_axis_retrieval")
    smoke_parser.add_argument(
        "--question",
        default="How does the axis retrieval pipeline assemble context?",
    )
    smoke_parser.add_argument("--token-budget", type=int, default=2000)
    smoke_parser.add_argument("--timeout", type=float, default=10.0)
    smoke_parser.add_argument("--long-timeout", type=float, default=180.0)
    smoke_parser.add_argument(
        "--context_engine-start-timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for a temporary context_engine to become healthy",
    )
    smoke_parser.add_argument(
        "--no-start-context_engine",
        action="store_true",
        help="Fail if no context_engine is already running",
    )
    smoke_parser.add_argument("--skip-index", action="store_true")
    smoke_parser.add_argument("--skip-docs", action="store_true")
    smoke_parser.set_defaults(func=smoke)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
