from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import threading
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(__file__).resolve().parent
HTML_PATH = ROOT_DIR / "automatic_setup_client.html"
DISCORD_ENV_PATH = ROOT_DIR / "Discord_scrape" / ".env"
STOAT_ENV_PATH = ROOT_DIR / "Stoat_migration" / ".env"

PACKAGES_TO_INSTALL = [
    "discord",
    "aiohttp",
    "python-dotenv",
    "certifi",
    
]

SCRIPT_TARGETS = {
    "setup": ROOT_DIR / "setup.py",
    "bot": ROOT_DIR / "Discord_scrape" / "bot.py",
    "validate": ROOT_DIR / "Discord_scrape" / "validate.py",
    "importer": ROOT_DIR / "Stoat_migration" / "importer.py",
}


class TerminalSession:
    def __init__(self, max_chunks: int = 250000) -> None:
        self._lock = threading.Lock()
        self._chunks: deque[str] = deque()
        self._max_chunks = max_chunks
        self._start_cursor = 0
        self.process: subprocess.Popen[str] | None = None
        self.exit_code: int | None = None
        self.command: list[str] = []

    def _append_output(self, text: str) -> None:
        with self._lock:
            if len(self._chunks) >= self._max_chunks:
                self._chunks.popleft()
                self._start_cursor += 1
            self._chunks.append(text)

    def _stream_output_worker(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None

        while True:
            chunk = proc.stdout.read(1)
            if chunk == "":
                break
            self._append_output(chunk)

        proc.wait()
        with self._lock:
            self.exit_code = proc.returncode
            self.process = None
        self._append_output(f"\n\n[Process exited with code {proc.returncode}]\n")

    def is_running(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None

    def start(self, command: list[str], cwd: Path) -> None:
        with self._lock:
            running = self.process is not None and self.process.poll() is None
        if running:
            raise RuntimeError("A process is already running. Stop it before starting another.")

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        with self._lock:
            self.process = proc
            self.exit_code = None
            self.command = command

        self._append_output("\n$ " + " ".join(command) + "\n\n")

        thread = threading.Thread(target=self._stream_output_worker, args=(proc,), daemon=True)
        thread.start()

    def stop(self) -> bool:
        with self._lock:
            proc = self.process

        if proc is None or proc.poll() is not None:
            return False

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        return True

    def send_input(self, text: str) -> None:
        with self._lock:
            proc = self.process

        if proc is None or proc.poll() is not None:
            raise RuntimeError("No running process.")
        if proc.stdin is None:
            raise RuntimeError("Process has no stdin.")

        proc.stdin.write(text + "\n")
        proc.stdin.flush()
        self._append_output(f"\n> {text}\n")

    def output_since(self, cursor: int) -> dict[str, Any]:
        with self._lock:
            start = self._start_cursor
            if cursor < start:
                cursor = start
                dropped = True
            else:
                dropped = False

            index = cursor - start
            text = "".join(itertools.islice(self._chunks, index, None))
            next_cursor = start + len(self._chunks)
            running = self.process is not None and self.process.poll() is None
            exit_code = self.exit_code

        return {
            "cursor": next_cursor,
            "output": text,
            "running": running,
            "exit_code": exit_code,
            "dropped": dropped,
        }


SESSION = TerminalSession()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(path: Path, values: dict[str, str], header: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_message_limit(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in {"", "none"}:
        return "none"
    if value.isdigit() and int(value) > 0:
        return value
    raise ValueError("DISCORD_MESSAGE_LIMIT must be 'none' or a positive integer.")


def configure_project(payload: dict[str, Any]) -> dict[str, Any]:
    discord_token = str(payload.get("discord_token", "")).strip()
    stoat_token = str(payload.get("stoat_token", "")).strip()
    stoat_server_id = str(payload.get("stoat_server_id", "")).strip()
    message_limit = parse_message_limit(str(payload.get("discord_message_limit", "none")))
    raw_install = payload.get("install_dependencies", False)
    if isinstance(raw_install, bool):
        install_dependencies = raw_install
    elif isinstance(raw_install, str):
        install_dependencies = raw_install.strip().lower() in {"1", "true", "yes", "on"}
    else:
        install_dependencies = bool(raw_install)

    if not discord_token:
        raise ValueError("DISCORD_TOKEN is required.")
    if not stoat_token:
        raise ValueError("STOAT_TOKEN is required.")
    if not stoat_server_id:
        raise ValueError("STOAT_SERVER_ID is required.")

    write_env(
        DISCORD_ENV_PATH,
        {
            "DISCORD_TOKEN": discord_token,
            "DISCORD_MESSAGE_LIMIT": message_limit,
        },
        "# Generated by automatic_setup_server.py",
    )

    write_env(
        STOAT_ENV_PATH,
        {
            "STOAT_TOKEN": stoat_token,
            "STOAT_SERVER_ID": stoat_server_id,
        },
        "# Generated by automatic_setup_server.py",
    )

    result: dict[str, Any] = {
        "saved": True,
        "install_dependencies": install_dependencies,
        "pip_output": "",
        "pip_exit_code": None,
    }

    if install_dependencies:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", *PACKAGES_TO_INSTALL],
            cwd=str(ROOT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        result["pip_exit_code"] = completed.returncode
        result["pip_output"] = completed.stdout

    return result


def get_current_config() -> dict[str, str]:
    discord_values = read_env(DISCORD_ENV_PATH)
    stoat_values = read_env(STOAT_ENV_PATH)
    return {
        "discord_token": discord_values.get("DISCORD_TOKEN", ""),
        "discord_message_limit": discord_values.get("DISCORD_MESSAGE_LIMIT", "none") or "none",
        "stoat_token": stoat_values.get("STOAT_TOKEN", ""),
        "stoat_server_id": stoat_values.get("STOAT_SERVER_ID", ""),
    }


class AutomaticSetupHandler(BaseHTTPRequestHandler):
    server_version = "AutomaticSetup/1.0"

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length", "")
        if not length_header.isdigit():
            return {}
        length = int(length_header)
        data = self.rfile.read(length)
        if not data:
            return {}
        try:
            decoded = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON payload.") from exc
        if not isinstance(decoded, dict):
            raise ValueError("JSON payload must be an object.")
        return decoded

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/":
            if not HTML_PATH.exists():
                self._send_text(
                    "automatic_setup_client.html not found",
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self._send_text(HTML_PATH.read_text(encoding="utf-8"))
            return

        if parsed.path == "/api/config":
            self._send_json({"ok": True, "config": get_current_config()})
            return

        if parsed.path == "/api/process/output":
            qs = parse_qs(parsed.query)
            cursor_raw = qs.get("cursor", ["0"])[0]
            cursor = int(cursor_raw) if cursor_raw.isdigit() else 0
            payload = SESSION.output_since(cursor)
            payload["ok"] = True
            self._send_json(payload)
            return

        self._send_json({"ok": False, "error": "Not found."}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        try:
            payload = self._read_json_body()

            if parsed.path == "/api/configure":
                result = configure_project(payload)
                self._send_json({"ok": True, "result": result})
                return

            if parsed.path == "/api/process/start":
                target = str(payload.get("target", "")).strip()
                script_path = SCRIPT_TARGETS.get(target)
                if script_path is None:
                    self._send_json(
                        {"ok": False, "error": "Unknown target."},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                if not script_path.exists():
                    self._send_json(
                        {"ok": False, "error": f"Script not found: {script_path}"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                SESSION.start([sys.executable, "-u", str(script_path)], ROOT_DIR)
                self._send_json({"ok": True, "message": f"Started {target}."})
                return

            if parsed.path == "/api/process/input":
                text = str(payload.get("text", ""))
                SESSION.send_input(text)
                self._send_json({"ok": True})
                return

            if parsed.path == "/api/process/stop":
                stopped = SESSION.stop()
                self._send_json({"ok": True, "stopped": stopped})
                return

            self._send_json({"ok": False, "error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except RuntimeError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        except Exception as exc:  # pragma: no cover - fallback
            self._send_json(
                {"ok": False, "error": f"Internal error: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Keep server logs concise in terminal.
        print("[HTTP] " + format % args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local automatic setup web UI")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AutomaticSetupHandler)

    print(f"Automatic setup server running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
