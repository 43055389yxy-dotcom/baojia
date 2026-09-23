"""Run authenticated AstraQuote conversations with the headless Codex CLI.

Each turn runs in a short-lived container on the private Caddy Docker network.
The mounted Codex home preserves login and thread state; the AstraQuote MCP is
addressed directly and authenticated with the existing internal service token.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CODEX_CHAT_REFERENCE_PREFIX = "codex-chat://conversations/"
CODEX_PENDING_REFERENCE_PREFIX = "codex-chat://pending/"
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
RELAY_JOB_ID_PATTERN = re.compile(r"^gpt-[0-9a-f]{32}$", re.IGNORECASE)


def split_astraquote_prompt(prompt: str) -> str:
    match = re.match(r"^@AstraQuote(?:[ \t]+)?", str(prompt))
    if match is None:
        raise ValueError("quote prompt must begin with @AstraQuote")
    remainder = str(prompt)[match.end() :]
    if not remainder.strip():
        raise ValueError("quote prompt is empty")
    return remainder


def codex_chat_reference(thread_id: str) -> str:
    normalized = str(thread_id).strip()
    if not UUID_PATTERN.fullmatch(normalized):
        raise ValueError("Codex thread id is invalid")
    return f"{CODEX_CHAT_REFERENCE_PREFIX}{normalized}"


def conversation_id_from_reference(reference: str) -> str:
    value = str(reference).strip()
    if not value.startswith(CODEX_CHAT_REFERENCE_PREFIX):
        raise ValueError("Only Codex conversation references are allowed")
    return codex_chat_reference(value.removeprefix(CODEX_CHAT_REFERENCE_PREFIX)).rsplit(
        "/", 1
    )[-1]


def pending_chat_reference(job_id: str) -> str:
    normalized = str(job_id).strip()
    if not RELAY_JOB_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Codex pending relay job id is invalid")
    return f"{CODEX_PENDING_REFERENCE_PREFIX}{normalized}"

CODEX_IMAGE = os.environ.get(
    "ASTRAQUOTE_CODEX_IMAGE", "astraquote/chatgpt-desktop:26.908.40834-zh"
)
CODEX_HOME = os.environ.get(
    "ASTRAQUOTE_CODEX_HOME", "/home/ec2-user/.chatgpt-desktop-home"
)
MCP_ENV_FILE = os.environ.get(
    "ASTRAQUOTE_MCP_ENV_FILE", "/home/ec2-user/astraquote/config/mcp.env"
)
MCP_URL = os.environ.get(
    "ASTRAQUOTE_DIRECT_MCP_URL", "http://astraquote:8200/v2/mcp"
)
MCP_READY_URL = os.environ.get(
    "ASTRAQUOTE_DIRECT_MCP_READY_URL", "http://astraquote:8200/readyz"
)
DOCKER_NETWORK = os.environ.get("ASTRAQUOTE_DOCKER_NETWORK", "caddy-net")
STATE_PATH = Path(
    os.environ.get(
        "ASTRAQUOTE_GPT_RELAY_STATE",
        "/home/ec2-user/astraquote/data/gpt-relay/codex-cli-state.json",
    )
)
CONTAINER_LABEL = "com.astraquote.codex-relay=true"


@dataclass
class _CodexRun:
    process: subprocess.Popen[str]
    container_name: str
    quote: Any
    thread_id: str = ""
    last_message: str = ""
    error_lines: deque[str] = field(default_factory=lambda: deque(maxlen=80))
    completed: threading.Event = field(default_factory=threading.Event)
    thread_ready: threading.Event = field(default_factory=threading.Event)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _agent_prompt(prompt: str) -> str:
    try:
        content = split_astraquote_prompt(prompt)
    except ValueError:
        content = str(prompt).strip()
    return (
        "You are the AstraQuote quote engine. Use only the MCP server named "
        "`astraquote` for quote operations. Never use `codex_apps/astraquote`, "
        "browser tools, shell commands, local files, or any other MCP server. "
        "Follow the AstraQuote MCP instructions exactly and complete the request.\n\n"
        f"{content}"
    )


class CodexCliAgent:
    """Adapter matching the quote worker's browser interface without a GUI."""

    def __init__(
        self,
        *,
        active_quote_factory: Callable[..., Any],
        quote_timeout_seconds: int,
    ) -> None:
        self.active_quote_factory = active_quote_factory
        self.quote_timeout_seconds = quote_timeout_seconds
        self.driver: Any | None = None
        self._runs: dict[int, _CodexRun] = {}
        self._ready = False

    @staticmethod
    def _run_checked(command: list[str], timeout: int = 30) -> None:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1200:]
            raise RuntimeError(detail or f"Command failed: {command[0]}")

    def start(self) -> None:
        self._run_checked(["docker", "image", "inspect", CODEX_IMAGE])
        self._run_checked(
            [
                "docker", "run", "--rm", "--network", DOCKER_NETWORK,
                "--entrypoint", "/usr/bin/curl", "astraquote:production",
                "-fsS", "--max-time", "5", MCP_READY_URL,
            ]
        )
        self._run_checked(
            [
                "docker", "run", "--rm", "--user", "1000:1000",
                "-e", "HOME=/home/chatgpt", "-v", f"{CODEX_HOME}:/home/chatgpt",
                "--entrypoint", "/usr/lib/chatgpt/resources/codex", CODEX_IMAGE,
                "login", "status",
            ],
            timeout=45,
        )
        self._ready = True
        self.driver = self

    def close(self) -> None:
        for run in list(self._runs.values()):
            self._stop_run(run)
        self._runs.clear()
        self.driver = None
        self._ready = False

    def reconnect(self) -> None:
        self.start()

    def logged_in(self) -> bool:
        return self._ready and self.driver is self

    def recover_if_unavailable(self) -> bool:
        try:
            self.start()
        except Exception:
            self.driver = None
            self._ready = False
        return self.logged_in()

    @staticmethod
    def _container_name() -> str:
        return f"astraquote-codex-relay-{uuid.uuid4().hex[:12]}"

    def _command(self, container_name: str, thread_id: str = "") -> list[str]:
        command = [
            "docker", "run", "--rm", "--name", container_name,
            "--label", CONTAINER_LABEL, "--network", DOCKER_NETWORK,
            "--user", "1000:1000", "--env-file", MCP_ENV_FILE,
            "-e", "HOME=/home/chatgpt", "-v", f"{CODEX_HOME}:/home/chatgpt",
            "--entrypoint", "/usr/lib/chatgpt/resources/codex", CODEX_IMAGE,
            "--ask-for-approval", "never", "--sandbox", "read-only",
            "-c", f'mcp_servers.astraquote.url="{MCP_URL}"',
            "-c", 'mcp_servers.astraquote.bearer_token_env_var="ASTRAQUOTE_INTERNAL_TOKEN"',
            "exec", "--ignore-user-config", "--ignore-rules", "--json",
            "--skip-git-repo-check", "-C", "/tmp",
        ]
        if thread_id:
            command.extend(["resume", thread_id, "-"])
        else:
            command.append("-")
        return command

    def _read_stdout(self, run: _CodexRun) -> None:
        assert run.process.stdout is not None
        for line in run.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                run.error_lines.append(line.strip()[-500:])
                continue
            event_type = str(event.get("type") or "")
            if event_type == "thread.started":
                thread_id = str(event.get("thread_id") or "").strip()
                try:
                    run.quote.chat_url = codex_chat_reference(thread_id)
                except ValueError:
                    run.error_lines.append("Codex returned an invalid thread id")
                else:
                    run.thread_id = thread_id
                    run.thread_ready.set()
                    _atomic_json(
                        STATE_PATH,
                        {
                            "surface": "Codex CLI",
                            "last_chat_reference": run.quote.chat_url,
                            "updated_at": time.time(),
                        },
                    )
            elif event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    text = str(item.get("text") or "").strip()
                    if text:
                        run.last_message = text
            elif event_type == "error":
                run.error_lines.append(str(event.get("message") or event)[-1000:])
        run.process.wait()
        run.completed.set()

    @staticmethod
    def _read_stderr(run: _CodexRun) -> None:
        assert run.process.stderr is not None
        for line in run.process.stderr:
            run.error_lines.append(line.strip()[-1000:])

    def _spawn(self, quote: Any, prompt: str, *, thread_id: str = "") -> _CodexRun:
        existing = self._runs.get(id(quote))
        if existing is not None and existing.process.poll() is None:
            raise RuntimeError("This quote already has an active Codex turn")
        container_name = self._container_name()
        process = subprocess.Popen(
            self._command(container_name, thread_id),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        run = _CodexRun(process=process, container_name=container_name, quote=quote)
        self._runs[id(quote)] = run
        assert process.stdin is not None
        process.stdin.write(_agent_prompt(prompt))
        process.stdin.close()
        threading.Thread(target=self._read_stdout, args=(run,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(run,), daemon=True).start()
        return run

    def start_quote(self, job_id: str, prompt: str) -> Any:
        now = time.monotonic()
        quote = self.active_quote_factory(
            job_id=job_id,
            chat_url=pending_chat_reference(job_id),
            deadline=now + self.quote_timeout_seconds,
            stable_since=now,
        )
        run = self._spawn(quote, prompt)
        run.thread_ready.wait(timeout=15)
        return quote

    def start_component_batch(
        self,
        job_id: str,
        prompt: str,
        *,
        batch_index: int,
        batch_count: int,
        component_keys: list[str],
    ) -> Any:
        quote = self.start_quote(job_id, prompt)
        quote.batch_index = batch_index
        quote.batch_count = batch_count
        quote.role = "component_batch"
        quote.component_keys = tuple(component_keys)
        return quote

    def resume_quote(
        self,
        job_id: str,
        chat_url: str,
        *,
        batch_index: int = 0,
        batch_count: int = 1,
        role: str = "coordinator",
        component_keys: list[str] | None = None,
        previous_conversation_ids: list[str] | None = None,
        conversation_index: int | None = None,
        wave_index: int = 0,
        wave_count: int = 1,
    ) -> Any:
        now = time.monotonic()
        quote = self.active_quote_factory(
            job_id=job_id,
            chat_url=chat_url,
            deadline=now + self.quote_timeout_seconds,
            stable_since=now,
            batch_index=batch_index,
            batch_count=batch_count,
            role=role,
            component_keys=tuple(component_keys or []),
            previous_conversation_ids=tuple(previous_conversation_ids or []),
            conversation_index=conversation_index,
            wave_index=wave_index,
            wave_count=wave_count,
        )
        self._spawn(
            quote,
            "Continue this AstraQuote task from its persisted MCP state. Do not restart completed work.",
            thread_id=conversation_id_from_reference(chat_url),
        )
        return quote

    def promote_pending_reference(self, quote: Any) -> bool:
        run = self._runs.get(id(quote))
        if run is None or not run.thread_id:
            return False
        quote.chat_url = codex_chat_reference(run.thread_id)
        return True

    def poll_quote(
        self,
        quote: Any,
        completion_check: Callable[[], bool] | None = None,
    ) -> str | None:
        del completion_check
        run = self._runs.get(id(quote))
        if run is None:
            raise RuntimeError("Codex run state is missing for this quote")
        if run.process.poll() is None:
            if time.monotonic() >= quote.deadline:
                self._stop_run(run)
                raise TimeoutError(
                    f"Codex CLI quote did not finish in {self.quote_timeout_seconds} seconds"
                )
            return None
        if run.process.returncode != 0:
            detail = "\n".join(run.error_lines)[-2500:]
            raise RuntimeError(detail or f"Codex CLI exited with {run.process.returncode}")
        if not run.last_message:
            raise RuntimeError("Codex CLI completed without an assistant response")
        quote.last_text = run.last_message
        quote.saw_assistant = True
        return run.last_message

    def ready_for_next_turn(self, quote: Any) -> bool:
        run = self._runs.get(id(quote))
        return run is None or run.process.poll() is not None

    def continue_quote(self, quote: Any, prompt: str) -> None:
        thread_id = conversation_id_from_reference(quote.chat_url)
        now = time.monotonic()
        quote.last_text = ""
        quote.stable_since = now
        quote.deadline = now + self.quote_timeout_seconds
        quote.saw_assistant = False
        quote.retry_visible_since = None
        quote.retry_clicked = False
        quote.generation_grace_used = False
        self._spawn(quote, prompt, thread_id=thread_id)

    @staticmethod
    def _stop_run(run: _CodexRun) -> None:
        if run.process.poll() is None:
            subprocess.run(
                ["docker", "rm", "-f", run.container_name],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            try:
                run.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                run.process.kill()

    def close_quote(self, quote: Any) -> None:
        run = self._runs.get(id(quote))
        if run is not None:
            self._stop_run(run)

    def cancel_quote(self, quote: Any) -> None:
        self.close_quote(quote)

    def capture_debug(self, job_id: str) -> None:
        matching = next(
            (run for run in self._runs.values() if run.quote.job_id == job_id), None
        )
        payload = {
            "surface": "Codex CLI",
            "job_id": job_id,
            "active": bool(matching and matching.process.poll() is None),
            "exit_code": None if matching is None else matching.process.poll(),
            "errors": [] if matching is None else list(matching.error_lines),
            "updated_at": time.time(),
        }
        _atomic_json(STATE_PATH.parent / "debug" / f"{job_id}.json", payload)
