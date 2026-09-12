"""Control one authenticated Codex desktop Chat window through Chromium CDP.

The adapter deliberately exposes only Codex Chat conversation references.  It
cannot navigate to chatgpt.com and it never starts a browser process.  A quote
prompt is submitted only after the composer contains a real AstraQuote plugin
mention produced by the application's own suggestion menu.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import subprocess
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.services.gpt_browser_navigation import (
    is_interrupted_response,
    should_extend_quote_deadline,
)

CODEX_CONTAINER = os.environ.get(
    "ASTRAQUOTE_CODEX_CONTAINER", "astraquote-chatgpt-desktop"
)
CODEX_CDP = os.environ.get("ASTRAQUOTE_CODEX_CDP", "http://127.0.0.1:9222").rstrip(
    "/"
)
CODEX_STATE_PATH = Path(
    os.environ.get(
        "ASTRAQUOTE_GPT_RELAY_STATE",
        "/home/ec2-user/astraquote/data/gpt-relay/codex-state.json",
    )
)
CODEX_NEW_CHAT_LINK = "codex://threads/new?mode=chat"
CODEX_CHAT_REFERENCE_PREFIX = "codex-chat://conversations/"
CODEX_PENDING_REFERENCE_PREFIX = "codex-chat://pending/"
COMPOSER_SELECTOR = (
    '[contenteditable="true"][aria-label="给 ChatGPT 发消息"],'
    '[contenteditable="true"][aria-label*="ChatGPT"],'
    '[contenteditable="true"][data-virtualkeyboard="true"]'
)
RICH_MENTION_SELECTOR = '[plugin-mention-display-name="AstraQuote"]'
ASSISTANT_SELECTOR = '[data-content-search-unit-key$=":assistant"]'
USER_SELECTOR = '[data-content-search-unit-key$=":user"]'
SIDEBAR_REFERENCE_ATTRIBUTE = "data-sidebar-chatgpt-conversation-key"
THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
RELAY_JOB_ID_PATTERN = re.compile(r"^gpt-[0-9a-f]{32}$", re.IGNORECASE)


class PendingConversationReferenceError(RuntimeError):
    """The prompt is running but Codex has not listed its stable chat id yet."""


def split_astraquote_prompt(prompt: str) -> str:
    """Return prompt text after an exact leading AstraQuote mention."""

    match = re.match(r"^@AstraQuote(?:[ \t]+)?", str(prompt))
    if match is None:
        raise ValueError("报价消息必须以 @AstraQuote 开头。")
    remainder = str(prompt)[match.end() :]
    if not remainder.strip():
        raise ValueError("@AstraQuote 后缺少报价内容。")
    return remainder


def codex_chat_reference(thread_id: str) -> str:
    normalized = str(thread_id).strip()
    if not THREAD_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Codex Chat conversation id is invalid")
    return f"{CODEX_CHAT_REFERENCE_PREFIX}{normalized}"


def conversation_id_from_reference(reference: str) -> str:
    value = str(reference).strip()
    if not value.startswith(CODEX_CHAT_REFERENCE_PREFIX):
        raise ValueError("Only Codex Chat conversation references are allowed")
    return codex_chat_reference(value.removeprefix(CODEX_CHAT_REFERENCE_PREFIX)).rsplit(
        "/", 1
    )[-1]


def pending_chat_reference(job_id: str) -> str:
    normalized = str(job_id).strip()
    if not RELAY_JOB_ID_PATTERN.fullmatch(normalized):
        raise ValueError("Codex Chat pending relay job id is invalid")
    return f"{CODEX_PENDING_REFERENCE_PREFIX}{normalized}"


def is_pending_chat_reference(reference: str) -> bool:
    value = str(reference).strip()
    return value.startswith(CODEX_PENDING_REFERENCE_PREFIX) and bool(
        RELAY_JOB_ID_PATTERN.fullmatch(
            value.removeprefix(CODEX_PENDING_REFERENCE_PREFIX)
        )
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class CodexChatDesktop:
    """Drive ChatGPT-mode conversations in one visible Codex desktop app."""

    def __init__(
        self,
        *,
        active_quote_factory: Callable[..., Any],
        quote_timeout_seconds: int,
        container_name: str = CODEX_CONTAINER,
        cdp_base_url: str = CODEX_CDP,
    ) -> None:
        self.active_quote_factory = active_quote_factory
        self.quote_timeout_seconds = quote_timeout_seconds
        self.container_name = container_name
        self.cdp_base_url = cdp_base_url.rstrip("/")
        self._websocket: Any | None = None
        self._request_id = 0
        # The relay loop historically tests ``driver is None``.  Keep this
        # compatibility sentinel while the actual control transport is CDP.
        self.driver: Any | None = None

    def start(self) -> None:
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001 - the desktop may be closed manually
            self._recover_desktop(exc)
            self._connect()
        self.driver = self
        if not self._chat_surface_ready():
            self._open_deep_link(CODEX_NEW_CHAT_LINK)
            self._wait_until(self._chat_surface_ready, timeout=90)

    def close(self) -> None:
        if self._websocket is not None:
            with suppress(Exception):
                self._websocket.close()
        self._websocket = None
        self.driver = None

    def reconnect(self) -> None:
        """Reconnect CDP without closing or replacing the desktop app."""

        if self._websocket is not None:
            with suppress(Exception):
                self._websocket.close()
        self._websocket = None
        self._connect()
        self.driver = self

    def _cdp_target_available(self) -> bool:
        """Return whether the visible Codex desktop renderer is controllable."""

        try:
            targets = json.load(
                urllib.request.urlopen(f"{self.cdp_base_url}/json/list", timeout=5)
            )
        except Exception:  # noqa: BLE001 - a missing renderer is the expected signal
            return False
        return any(item.get("url") == "app://-/index.html" for item in targets)

    def _recover_desktop(self, original_error: Exception) -> None:
        """Start or restart Codex while preserving its mounted login profile."""

        self.close()
        try:
            inspection = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    self.container_name,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            action = (
                "restart" if inspection.stdout.strip().lower() == "true" else "start"
            )
            subprocess.run(
                ["docker", action, self.container_name],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
            )
            self._wait_until(
                self._cdp_target_available,
                timeout=90,
                interval=2,
            )
        except Exception as recovery_error:  # noqa: BLE001 - retain both causes
            raise RuntimeError(
                "Codex 桌面已退出，自动重新启动失败："
                f"{str(recovery_error)[:500]}"
            ) from original_error

    def _connect(self) -> None:
        targets = json.load(
            urllib.request.urlopen(f"{self.cdp_base_url}/json/list", timeout=5)
        )
        target = next(
            (item for item in targets if item.get("url") == "app://-/index.html"),
            None,
        )
        if target is None:
            raise RuntimeError("没有找到 Codex 桌面主窗口。")
        websocket_module = importlib.import_module("websocket")
        self._websocket = websocket_module.create_connection(
            target["webSocketDebuggerUrl"], timeout=8, suppress_origin=True
        )
        self._call("Runtime.enable")
        self._call("Page.enable")

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._websocket is None:
            raise RuntimeError("Codex desktop control is not connected")
        self._request_id += 1
        request_id = self._request_id
        self._websocket.send(
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            )
        )
        while True:
            response = json.loads(self._websocket.recv())
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise RuntimeError(
                    f"Codex desktop control failed: {response['error'].get('message', method)}"
                )
            return response

    def _evaluate(self, expression: str) -> Any:
        response = self._call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        result = response.get("result", {})
        if result.get("exceptionDetails"):
            description = (
                result.get("exceptionDetails", {})
                .get("exception", {})
                .get("description", "Codex page evaluation failed")
            )
            raise RuntimeError(str(description))
        return result.get("result", {}).get("value")

    def _wait_until(
        self,
        check: Callable[[], Any],
        *,
        timeout: float,
        interval: float = 0.25,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                value = check()
                if value:
                    return value
            except Exception as exc:  # noqa: BLE001 - live renderer can replace any node
                last_error = exc
            time.sleep(interval)
        if last_error is not None:
            raise TimeoutError(str(last_error)) from last_error
        raise TimeoutError("Codex 桌面等待操作超时。")

    def _open_deep_link(self, deep_link: str) -> None:
        if not deep_link.startswith("codex://"):
            raise ValueError("Only Codex desktop deep links are allowed")
        subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "1000:1000",
                "-e",
                "HOME=/home/chatgpt",
                "-e",
                "DISPLAY=:1",
                self.container_name,
                "/usr/bin/chatgpt",
                "--no-sandbox",
                deep_link,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )

    def _chat_surface_ready(self) -> bool:
        return bool(
            self._evaluate(
                f"""
                (() => {{
                  const composer = document.querySelector({json.dumps(COMPOSER_SELECTOR)});
                  const mode = [...document.querySelectorAll('button')].find(
                    x => (x.getAttribute('aria-label') || '').includes('ChatGPT')
                  );
                  const chat = [...document.querySelectorAll('button')].find(
                    x => ['聊天', 'Chat'].includes(x.innerText.trim())
                      && x.getAttribute('aria-pressed') === 'true'
                  );
                  const work = [...document.querySelectorAll('button')].find(
                    x => ['工作', 'Work'].includes(x.innerText.trim())
                      && x.getAttribute('aria-pressed') === 'true'
                  );
                  return Boolean(composer && mode && chat && !work);
                }})()
                """
            )
        )

    def logged_in(self) -> bool:
        try:
            if self._chat_surface_ready():
                return True
            body = str(self._evaluate("document.body?.innerText || ''") or "")
            return not any(
                marker in body
                for marker in ("Log in", "登录 ChatGPT", "Continue with Google")
            ) and bool(self._evaluate("Boolean(document.querySelector('main'))"))
        except Exception:  # noqa: BLE001 - authentication check must remain non-fatal
            return False

    def _sidebar_ids(self) -> list[str]:
        values = self._evaluate(
            f"""
            [...document.querySelectorAll('[{SIDEBAR_REFERENCE_ATTRIBUTE}]')]
              .map(x => x.getAttribute('{SIDEBAR_REFERENCE_ATTRIBUTE}'))
              .filter(Boolean)
              .map(x => x.replace('chatgpt:conversation:', ''))
            """
        )
        return [value for value in (values or []) if THREAD_ID_PATTERN.fullmatch(value)]

    def _open_new_chat(self) -> list[str]:
        previous = self._sidebar_ids()
        self._open_deep_link(CODEX_NEW_CHAT_LINK)

        def new_chat_loaded() -> bool:
            return bool(
                self._evaluate(
                    f"""
                    (() => {{
                      const composer = document.querySelector({json.dumps(COMPOSER_SELECTOR)});
                      return Boolean(composer)
                        && document.querySelectorAll('[data-turn-key]').length === 0;
                    }})()
                    """
                )
            )

        self._wait_until(new_chat_loaded, timeout=90)
        draft_present = bool(
            self._evaluate(
                f"""
                (() => {{
                  const composer = document.querySelector({json.dumps(COMPOSER_SELECTOR)});
                  composer.focus();
                  return Boolean(composer.innerText.trim());
                }})()
                """
            )
        )
        if draft_present:
            # Codex can preserve an unsent draft when the new-chat deep link is
            # opened twice.  Clear only the empty conversation's composer.
            self._dispatch_key("a", "KeyA", modifiers=2)
            self._dispatch_key("Backspace", "Backspace")

        def blank_chat_ready() -> bool:
            if not self._chat_surface_ready():
                return False
            return bool(
                self._evaluate(
                    f"""
                    (() => {{
                      const composer = document.querySelector({json.dumps(COMPOSER_SELECTOR)});
                      return document.querySelectorAll('[data-turn-key]').length === 0
                        && composer && !composer.innerText.trim();
                    }})()
                    """
                )
            )

        self._wait_until(blank_chat_ready, timeout=90)
        return previous

    def _dispatch_key(
        self,
        key: str,
        code: str,
        *,
        modifiers: int = 0,
    ) -> None:
        self._call(
            "Input.dispatchKeyEvent",
            {
                "type": "rawKeyDown",
                "key": key,
                "code": code,
                "modifiers": modifiers,
            },
        )
        self._call(
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": key, "code": code, "modifiers": modifiers},
        )

    def _user_message_count(self) -> int:
        return int(
            self._evaluate(
                f"document.querySelectorAll({json.dumps(USER_SELECTOR)}).length"
            )
            or 0
        )

    def _send_prompt(self, prompt: str) -> None:
        remainder = split_astraquote_prompt(prompt)
        user_message_count = self._user_message_count()
        focused = self._evaluate(
            f"""
            (() => {{
              const composer = document.querySelector({json.dumps(COMPOSER_SELECTOR)});
              if (!composer || composer.innerText.trim()) return false;
              composer.focus();
              return true;
            }})()
            """
        )
        if not focused:
            raise RuntimeError("Codex Chat 输入框未就绪或含有未发送内容。")
        self._call("Input.insertText", {"text": "@"})

        def choose_astraquote() -> bool:
            return bool(
                self._evaluate(
                    """
                    (() => {
                      const option = [...document.querySelectorAll(
                        '[data-list-navigation-item="true"]'
                      )].find(x => x.innerText.split('\\n')[0].trim() === 'AstraQuote');
                      if (!option) return false;
                      option.click();
                      return true;
                    })()
                    """
                )
            )

        self._wait_until(choose_astraquote, timeout=15)
        self._wait_until(
            lambda: bool(
                self._evaluate(
                    f"Boolean(document.querySelector({json.dumps(RICH_MENTION_SELECTOR)}))"
                )
            ),
            timeout=10,
        )
        self._call("Input.insertText", {"text": remainder})
        if not self._evaluate(
            f"Boolean(document.querySelector({json.dumps(RICH_MENTION_SELECTOR)}))"
        ):
            raise RuntimeError("AstraQuote 插件标记在发送前丢失。")
        self._dispatch_key("Enter", "Enter")
        self._wait_until(
            lambda: self._user_message_count() > user_message_count,
            timeout=45,
        )
        valid_user_mention = self._evaluate(
            f"""
            (() => {{
              const messages = document.querySelectorAll({json.dumps(USER_SELECTOR)});
              const latest = messages[messages.length - 1];
              return Boolean(latest && latest.querySelector(
                '[data-prompt-link-href^="plugin://"]'
              ) && latest.innerText.includes('AstraQuote'));
            }})()
            """
        )
        if not valid_user_mention:
            raise RuntimeError("已发送消息没有绑定 AstraQuote 插件。")

    def _new_conversation_reference(
        self,
        previous_ids: list[str],
        *,
        timeout: float = 3,
    ) -> str | None:
        previous = set(previous_ids)

        def find_new() -> str | None:
            return next((value for value in self._sidebar_ids() if value not in previous), None)

        try:
            thread_id = self._wait_until(find_new, timeout=timeout)
        except TimeoutError:
            return None
        return codex_chat_reference(str(thread_id))

    def promote_pending_reference(self, quote: Any) -> bool:
        """Replace a temporary sent-message handle once Codex lists the chat."""

        if not is_pending_chat_reference(quote.chat_url):
            return False
        previous = set(getattr(quote, "previous_conversation_ids", ()) or ())
        thread_id = next(
            (value for value in self._sidebar_ids() if value not in previous),
            None,
        )
        if thread_id is None:
            return False
        quote.chat_url = codex_chat_reference(thread_id)
        quote.previous_conversation_ids = ()
        _atomic_json(
            CODEX_STATE_PATH,
            {
                "surface": "Codex Chat",
                "last_chat_reference": quote.chat_url,
                "updated_at": time.time(),
            },
        )
        return True

    def start_quote(self, job_id: str, prompt: str) -> Any:
        previous_ids = self._open_new_chat()
        self._send_prompt(prompt)
        chat_reference = self._new_conversation_reference(previous_ids) or (
            pending_chat_reference(job_id)
        )
        now = time.monotonic()
        _atomic_json(
            CODEX_STATE_PATH,
            {
                "surface": "Codex Chat",
                "last_chat_reference": chat_reference,
                "updated_at": time.time(),
            },
        )
        return self.active_quote_factory(
            job_id=job_id,
            chat_url=chat_reference,
            deadline=now + self.quote_timeout_seconds,
            stable_since=now,
            previous_conversation_ids=tuple(previous_ids),
        )

    def start_component_batch(
        self,
        job_id: str,
        prompt: str,
        *,
        batch_index: int,
        batch_count: int,
        component_keys: list[str],
    ) -> Any:
        active = self.start_quote(job_id, prompt)
        active.batch_index = batch_index
        active.batch_count = batch_count
        active.role = "component_batch"
        active.component_keys = tuple(component_keys)
        return active

    def _switch_to_quote(self, quote: Any) -> None:
        def current_quote_visible() -> bool:
            return bool(
                self._evaluate(
                    f"""
                    [...document.querySelectorAll({json.dumps(USER_SELECTOR)})]
                      .some(x => x.innerText.includes({json.dumps(quote.job_id)}))
                    """
                )
            )

        if current_quote_visible():
            self.promote_pending_reference(quote)
            return
        if (
            is_pending_chat_reference(quote.chat_url)
            and not self.promote_pending_reference(quote)
        ):
            raise PendingConversationReferenceError(
                "Codex 报价已发送，正在等待最近对话生成标题。"
            )
        thread_id = conversation_id_from_reference(quote.chat_url)
        clicked = self._evaluate(
            f"""
            (() => {{
              const row = document.querySelector(
                '[{SIDEBAR_REFERENCE_ATTRIBUTE}="chatgpt:conversation:{thread_id}"]'
              );
              if (!row) return false;
              (row.querySelector('[role="button"]') || row).click();
              return true;
            }})()
            """
        )
        if not clicked:
            raise RuntimeError("Codex 最近对话中找不到本报价会话。")
        self._wait_until(current_quote_visible, timeout=45)

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
    ) -> Any:
        now = time.monotonic()
        active = self.active_quote_factory(
            job_id=job_id,
            chat_url=chat_url,
            deadline=now + self.quote_timeout_seconds,
            stable_since=now,
            batch_index=batch_index,
            batch_count=batch_count,
            role=role,
            component_keys=tuple(component_keys or []),
            previous_conversation_ids=tuple(previous_conversation_ids or []),
        )
        try:
            self._switch_to_quote(active)
        except PendingConversationReferenceError:
            # The submitted remote turn is still valid. Keep it monitored and
            # promote its reference as soon as the sidebar title appears.
            pass
        return active

    def _scroll_to_latest(self) -> None:
        self._evaluate(
            """
            (() => {
              const units = document.querySelectorAll('[data-content-search-unit-key]');
              let node = units.length ? units[units.length - 1] : null;
              while (node) {
                if (node.scrollHeight > node.clientHeight + 4) {
                  node.scrollTop = node.scrollHeight;
                }
                node = node.parentElement;
              }
              if (document.scrollingElement) {
                document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight;
              }
            })()
            """
        )

    def _assistant_messages(self) -> list[str]:
        values = self._evaluate(
            f"""
            [...document.querySelectorAll({json.dumps(ASSISTANT_SELECTOR)})]
              .map(x => x.innerText.trim()).filter(Boolean)
            """
        )
        return list(values or [])

    def _generation_active(self) -> bool:
        return bool(
            self._evaluate(
                """
                [...document.querySelectorAll('button')].some(x => {
                  const label = `${x.getAttribute('aria-label') || ''} ${x.innerText || ''}`;
                  const visible = Boolean(x.offsetWidth || x.offsetHeight || x.getClientRects().length);
                  return visible && /停止|Stop/i.test(label);
                })
                """
            )
        )

    def _click_text_control(self, labels: tuple[str, ...]) -> bool:
        return bool(
            self._evaluate(
                f"""
                (() => {{
                  const labels = {json.dumps(list(labels), ensure_ascii=False)};
                  const target = [...document.querySelectorAll('button,[role="button"]')]
                    .find(x => labels.includes((x.innerText || x.getAttribute('aria-label') || '').trim())
                      && Boolean(x.offsetWidth || x.offsetHeight || x.getClientRects().length));
                  if (!target) return false;
                  target.click();
                  return true;
                }})()
                """
            )
        )

    def _text_control_visible(self, labels: tuple[str, ...]) -> bool:
        return bool(
            self._evaluate(
                f"""
                (() => {{
                  const labels = {json.dumps(list(labels), ensure_ascii=False)};
                  return [...document.querySelectorAll('button,[role="button"]')]
                    .some(x => labels.includes((x.innerText || x.getAttribute('aria-label') || '').trim())
                      && Boolean(x.offsetWidth || x.offsetHeight || x.getClientRects().length));
                }})()
                """
            )
        )

    def _approve_tool_if_needed(self) -> bool:
        return bool(
            self._evaluate(
                """
                (() => {
                  const allowed = new Set([
                    '允许一次', 'Allow once', '这次允许', 'Allow this time'
                  ]);
                  for (const control of document.querySelectorAll('button,[role="button"]')) {
                    const label = (control.innerText || control.getAttribute('aria-label') || '').trim();
                    if (!allowed.has(label)) continue;
                    let owner = control;
                    let belongsToAstraQuote = false;
                    for (let depth = 0; owner && depth < 12; depth += 1, owner = owner.parentElement) {
                      if ((owner.innerText || '').includes('AstraQuote')) {
                        belongsToAstraQuote = true;
                        break;
                      }
                    }
                    if (!belongsToAstraQuote) continue;
                    control.click();
                    return true;
                  }
                  return false;
                })()
                """
            )
        )

    def poll_quote(
        self,
        quote: Any,
        completion_check: Callable[[], bool] | None = None,
    ) -> str | None:
        self._switch_to_quote(quote)
        self._scroll_to_latest()
        now = time.monotonic()
        if self._approve_tool_if_needed():
            quote.stable_since = now
            if now >= quote.deadline:
                raise TimeoutError("报价授权后仍未在期限内产生后台进展。")
            return None
        retry_labels = ("重试", "Retry")
        if self._text_control_visible(retry_labels):
            if quote.retry_visible_since is None:
                quote.retry_visible_since = now
            elif (
                not quote.retry_clicked
                and now - quote.retry_visible_since >= 60
                and (completion_check is None or not completion_check())
            ):
                self._click_text_control(retry_labels)
                quote.retry_clicked = True
                quote.retry_visible_since = now
            if now >= quote.deadline:
                raise TimeoutError("Codex Chat 重试页面没有恢复，也没有后台进展。")
            return None
        quote.retry_visible_since = None
        messages = self._assistant_messages()
        generation_active = self._generation_active()
        if len(messages) < quote.minimum_assistant_messages:
            if should_extend_quote_deadline(
                deadline_reached=now >= quote.deadline,
                generation_active=generation_active,
                retry_visible=False,
            ) and not quote.generation_grace_used:
                quote.deadline = now + self.quote_timeout_seconds
                quote.generation_grace_used = True
                return None
            if now >= quote.deadline:
                raise TimeoutError(
                    f"Codex Chat quote did not finish in {self.quote_timeout_seconds} seconds"
                )
            return None
        current = messages[-1].strip() if messages else ""
        if current:
            quote.saw_assistant = True
            if current != quote.last_text:
                quote.last_text = current
                quote.stable_since = now
        if current and not generation_active and is_interrupted_response(current):
            return current
        if quote.saw_assistant and not generation_active and now - quote.stable_since >= 8:
            return quote.last_text
        if should_extend_quote_deadline(
            deadline_reached=now >= quote.deadline,
            generation_active=generation_active,
            retry_visible=False,
        ) and not quote.generation_grace_used:
            quote.deadline = now + self.quote_timeout_seconds
            quote.generation_grace_used = True
            return None
        if now >= quote.deadline:
            raise TimeoutError(
                f"Codex Chat quote did not finish in {self.quote_timeout_seconds} seconds"
            )
        return None

    def continue_quote(self, quote: Any, prompt: str) -> None:
        self._switch_to_quote(quote)
        assistant_count = len(self._assistant_messages())
        self._send_prompt(prompt)
        quote.minimum_assistant_messages = assistant_count + 1
        quote.last_text = ""
        quote.stable_since = time.monotonic()
        quote.deadline = quote.stable_since + self.quote_timeout_seconds
        quote.saw_assistant = False
        quote.retry_visible_since = None
        quote.retry_clicked = False
        quote.generation_grace_used = False

    def _stop_generation(self) -> bool:
        return bool(
            self._evaluate(
                """
                (() => {
                  const target = [...document.querySelectorAll('button')].find(x => {
                    const label = `${x.getAttribute('aria-label') || ''} ${x.innerText || ''}`;
                    return /停止|Stop/i.test(label)
                      && Boolean(x.offsetWidth || x.offsetHeight || x.getClientRects().length);
                  });
                  if (!target) return false;
                  target.click();
                  return true;
                })()
                """
            )
        )

    def close_quote(self, quote: Any) -> None:
        self._switch_to_quote(quote)
        self._stop_generation()

    def cancel_quote(self, quote: Any) -> None:
        self._switch_to_quote(quote)
        self._stop_generation()
        self._click_text_control(("拒绝", "Deny", "取消", "Cancel"))

    def capture_debug(self, job_id: str) -> None:
        debug_directory = CODEX_STATE_PATH.parent / "debug"
        debug_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        screenshot = self._call("Page.captureScreenshot", {"format": "png"})
        data = screenshot.get("result", {}).get("data")
        if data:
            (debug_directory / f"{job_id}.png").write_bytes(base64.b64decode(data))
        inventory = self._evaluate(
            """
            (() => ({
              surface: 'Codex Chat',
              body: (document.body?.innerText || '').slice(-6000),
              controls: [...document.querySelectorAll('button,[role="button"]')]
                .filter(x => Boolean(x.offsetWidth || x.offsetHeight || x.getClientRects().length))
                .slice(0, 100)
                .map(x => ({
                  text: (x.innerText || '').slice(0, 120),
                  aria_label: (x.getAttribute('aria-label') || '').slice(0, 120)
                }))
            }))()
            """
        )
        _atomic_json(debug_directory / f"{job_id}.json", inventory or {})
