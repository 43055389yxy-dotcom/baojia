"""Drive one authenticated, visible Gemini Spark browser for quote tasks.

The browser owns one persistent Firefox profile.  Navigation and approvals use
DOM semantics; no fixed screen coordinates are used.  Only AstraQuote tool
permission cards whose tool name is on the public allowlist are approved.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.services.gemini_approval_policy import (
    APPROVE_LABELS,
    approval_card_is_safe,
)
from app.services.gemini_chat_references import (
    gemini_chat_reference,
    is_authenticated_gemini_workspace_url,
    task_id_from_gemini_reference,
)
from app.services.gemini_composer_selection import (
    choose_composer_candidate,
    is_gemini_send_label,
)
from app.services.gpt_browser_navigation import (
    is_interrupted_response,
    should_extend_quote_deadline,
)
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options

GEMINI_URL = os.environ.get(
    "ASTRAQUOTE_GEMINI_URL",
    "https://gemini.google.com/spark/chat/046174d2550d9f1c",
)
GEMINI_PROFILE = Path(
    os.environ.get(
        "ASTRAQUOTE_GEMINI_PROFILE",
        "/home/ec2-user/.mozilla/firefox/astraquote-gemini",
    )
)
GEMINI_STATE_PATH = Path(
    os.environ.get(
        "ASTRAQUOTE_GPT_RELAY_STATE",
        "/home/ec2-user/astraquote/data/gpt-relay/gemini-state.json",
    )
)
RETRY_LABELS = {"retry", "重试"}
LOGICAL_TASK_PATTERN = re.compile(
    r"^(gpt-[0-9a-f]{32})-batch-([0-9]{1,3})$", re.IGNORECASE
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


class GeminiChatBrowser:
    """Use Gemini Spark tasks through one visible, persistent Firefox window."""

    def __init__(
        self,
        *,
        active_quote_factory: Callable[..., Any],
        quote_timeout_seconds: int,
        start_url: str = GEMINI_URL,
        profile_path: Path = GEMINI_PROFILE,
    ) -> None:
        self.active_quote_factory = active_quote_factory
        self.quote_timeout_seconds = quote_timeout_seconds
        self.start_url = start_url
        self.profile_path = Path(profile_path)
        self.driver: Any | None = None

    @property
    def surface_name(self) -> str:
        return "Gemini Spark"

    def start(self) -> None:
        if self.driver is not None:
            return
        self.profile_path.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.add_argument("-profile")
        options.add_argument(str(self.profile_path))
        options.set_preference("browser.sessionstore.resume_from_crash", False)
        options.set_preference("browser.shell.checkDefaultBrowser", False)
        self.driver = webdriver.Firefox(options=options)
        self.driver.set_page_load_timeout(90)
        self.driver.get(self.start_url)

    def close(self) -> None:
        if self.driver is not None:
            with suppress(Exception):
                self.driver.quit()
        self.driver = None

    def reconnect(self) -> None:
        self.close()
        self.start()

    def _execute(self, script: str, *args: Any) -> Any:
        if self.driver is None:
            raise RuntimeError("Gemini browser is not running")
        return self.driver.execute_script(script, *args)

    def _wait_until(
        self,
        predicate: Callable[[], Any],
        *,
        timeout: float,
        interval: float = 0.5,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (WebDriverException, RuntimeError) as exc:
                last_error = exc
            time.sleep(interval)
        if last_error:
            raise TimeoutError(str(last_error)) from last_error
        raise TimeoutError("Gemini 页面没有在限定时间内就绪。")

    def logged_in(self) -> bool:
        if self.driver is None:
            return False
        try:
            url = str(self.driver.current_url or "")
            if "accounts.google.com" in url:
                return False
            # A private Spark task URL is issued only after the Google account
            # and the associated app workspace have loaded successfully.
            if is_authenticated_gemini_workspace_url(url):
                return True
            return bool(
                self._execute(
                    """
                    const visible = node => Boolean(
                      node.offsetWidth || node.offsetHeight || node.getClientRects().length
                    );
                    const signIn = [...document.querySelectorAll('a,button,[role="button"]')]
                      .some(node => {
                        const label = `${node.innerText || ''} ${node.getAttribute('aria-label') || ''}`.trim();
                        return visible(node) && /^(登录|登入|Sign in)$/i.test(label);
                      });
                    if (signIn) return false;
                    const pageText = document.body?.innerText || '';
                    return /描述任务|Describe a task|接下来要做些什么|Ask Gemini/i.test(pageText);
                    """
                )
            )
        except Exception:  # noqa: BLE001 - browser may be on the login transition
            return False

    def _composer_candidates(self) -> tuple[list[dict[str, Any]], float, float]:
        candidates = list(
            self._execute(
            """
            const nodes = [...document.querySelectorAll(
              'textarea,[contenteditable="true"],[role="textbox"]'
            )];
            return nodes.map(node => {
              const rect = node.getBoundingClientRect();
              return {
                element: node,
                left: rect.left,
                top: rect.top,
                width: rect.width,
                height: rect.height,
                label: `${node.getAttribute('aria-label') || ''} ${node.getAttribute('placeholder') || ''}`.trim()
              };
            });
            """
            )
            or []
        )
        if self.driver is None:
            raise RuntimeError("Gemini browser is not running")
        window_size = self.driver.get_window_size()
        return (
            candidates,
            float(window_size["width"]),
            float(window_size["height"]),
        )

    def _composer(self, *, new_task: bool) -> Any:
        candidates, width, height = self._composer_candidates()
        selected = choose_composer_candidate(
            candidates,
            new_task=new_task,
            viewport_width=width,
            viewport_height=height,
        )
        return selected.get("element") if selected is not None else None

    def _new_task_composer(self) -> Any:
        return self._composer(new_task=True)

    def _current_composer(self) -> Any:
        return self._composer(new_task=False)

    @staticmethod
    def _write_multiline(element: Any, text: str) -> None:
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
        lines = str(text).splitlines() or [str(text)]
        for index, line in enumerate(lines):
            if index:
                element.send_keys(Keys.SHIFT, Keys.ENTER)
            if line:
                element.send_keys(line)

    def _bind_astraquote_mention(self, composer: Any, prompt: str) -> None:
        match = re.match(r"^@AstraQuote(?:[ \t]+)?", str(prompt))
        if match is None:
            raise ValueError("Gemini 报价消息必须以 @AstraQuote 开头。")
        composer.click()
        composer.send_keys("@AstraQuote")

        def mention_option() -> Any:
            return self._execute(
                """
                const nodes = [...document.querySelectorAll(
                  '[role="option"],[role="menuitem"],[role="listbox"] button,[class*="autocomplete"] button'
                )];
                return nodes.find(node => {
                  const text = (node.innerText || node.getAttribute('aria-label') || '').trim();
                  const rect = node.getBoundingClientRect();
                  return rect.width > 10 && rect.height > 10 && /AstraQuote/i.test(text);
                }) || null;
                """
            )

        try:
            option = self._wait_until(mention_option, timeout=5)
            option.click()
            remainder = str(prompt)[match.end() :]
            if remainder:
                composer.send_keys(" ")
                lines = remainder.splitlines()
                for index, line in enumerate(lines):
                    if index:
                        composer.send_keys(Keys.SHIFT, Keys.ENTER)
                    if line:
                        composer.send_keys(line)
        except TimeoutError:
            # Associated Gemini apps sometimes render the mention chip only
            # after submit. Preserve the exact leading mention in that case.
            self._write_multiline(composer, prompt)

    def _send_prompt(self, prompt: str, *, new_task: bool) -> None:
        if self.driver is None:
            raise RuntimeError("Gemini browser is not running")
        if new_task:
            self.driver.get(self.start_url)
            composer = self._wait_until(self._new_task_composer, timeout=60)
        else:
            composer = self._wait_until(self._current_composer, timeout=45)
        self._write_multiline(composer, "")
        self._bind_astraquote_mention(composer, prompt)
        self._submit_composer(composer)

    def _submit_composer(self, composer: Any) -> None:
        container = composer
        for _ in range(10):
            for button in container.find_elements(
                By.CSS_SELECTOR,
                "button,[role='button']",
            ):
                with suppress(Exception):
                    label = (
                        button.get_attribute("aria-label") or button.text or ""
                    ).strip()
                    if (
                        is_gemini_send_label(label)
                        and button.is_displayed()
                        and button.is_enabled()
                    ):
                        button.click()
                        return
            if str(container.tag_name or "").lower() in {"html", "body"}:
                break
            container = container.find_element(By.XPATH, "..")
        raise RuntimeError("Gemini 输入框旁没有可用的发送按钮。")

    def _page_contains_marker(self, marker: str) -> bool:
        return bool(
            self._execute(
                """
                const marker = arguments[0];
                const main = document.querySelector('main,[role="main"]');
                return Boolean((main?.innerText || document.body?.innerText || '').includes(marker));
                """,
                marker,
            )
        )

    def _confirm_new_task_started(self, job_id: str) -> None:
        self._wait_until(
            lambda: self._page_contains_marker(job_id),
            timeout=45,
        )

    def _current_task_contains(self, quote: Any) -> bool:
        text = str(
            self._execute(
                """
                const messages = [...document.querySelectorAll(
                  'user-query,[data-message-author-role="user"],[data-test-id*="user-query"],[class*="user-query"]'
                )].map(node => node.innerText || '').filter(Boolean);
                if (messages.length) return messages.join('\\n');
                const main = document.querySelector('main,[role="main"]');
                return main?.innerText || '';
                """
            )
            or ""
        )
        if quote.job_id not in text:
            return False
        if int(getattr(quote, "batch_count", 1) or 1) <= 1 or str(
            getattr(quote, "role", "coordinator")
        ) == "merge":
            return True
        batch_index = int(getattr(quote, "batch_index", 0) or 0)
        batch_count = int(getattr(quote, "batch_count", 1) or 1)
        markers = (
            f"relay_batch_index：{batch_index}",
            f"relay_batch_index: {batch_index}",
            f"当前为第 {batch_index + 1}/{batch_count} 批",
            f"第 {batch_index + 1}/{batch_count} 个组件批次",
        )
        return any(marker in text for marker in markers)

    def _switch_to_quote(self, quote: Any) -> None:
        if self._current_task_contains(quote):
            return
        # Gemini task URLs are not guaranteed to expose a durable conversation
        # id. Search only semantic navigation rows and verify the private relay
        # job marker after every click.
        candidates = self._execute(
            """
            const roots = [...document.querySelectorAll('nav,aside')];
            const scope = roots.length ? roots : [document.body];
            const nodes = scope.flatMap(root => [...root.querySelectorAll('a,button,[role="button"],[tabindex="0"]')]);
            const visible = nodes.filter(node => {
              const rect = node.getBoundingClientRect();
              const text = (node.innerText || '').trim();
              return rect.width > 30 && rect.height > 20 && rect.left < window.innerWidth * .48
                && text && text.length < 240;
            });
            const needsInput = node => /需要输入内容|Needs input|Action required/i.test(
              node.innerText || ''
            );
            return visible.sort((left, right) =>
              Number(needsInput(right)) - Number(needsInput(left))
            );
            """
        ) or []
        for candidate in candidates[:80]:
            with suppress(Exception):
                candidate.click()
                time.sleep(0.4)
                if self._current_task_contains(quote):
                    return
        raise RuntimeError("Gemini 最近任务中找不到本报价会话。")

    def _new_active(
        self,
        job_id: str,
        *,
        batch_index: int = 0,
        batch_count: int = 1,
        role: str = "coordinator",
        component_keys: list[str] | None = None,
    ) -> Any:
        now = time.monotonic()
        reference = gemini_chat_reference(f"{job_id}-batch-{batch_index}")
        _atomic_json(
            GEMINI_STATE_PATH,
            {
                "surface": self.surface_name,
                "last_chat_reference": reference,
                "updated_at": time.time(),
            },
        )
        return self.active_quote_factory(
            job_id=job_id,
            chat_url=reference,
            deadline=now + self.quote_timeout_seconds,
            stable_since=now,
            batch_index=batch_index,
            batch_count=batch_count,
            role=role,
            component_keys=tuple(component_keys or []),
            previous_conversation_ids=(),
        )

    def start_quote(self, job_id: str, prompt: str) -> Any:
        self._send_prompt(prompt, new_task=True)
        self._confirm_new_task_started(job_id)
        return self._new_active(job_id)

    def start_component_batch(
        self,
        job_id: str,
        prompt: str,
        *,
        batch_index: int,
        batch_count: int,
        component_keys: list[str],
    ) -> Any:
        self._send_prompt(prompt, new_task=True)
        self._confirm_new_task_started(job_id)
        return self._new_active(
            job_id,
            batch_index=batch_index,
            batch_count=batch_count,
            role="component_batch",
            component_keys=component_keys,
        )

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
        reference_id = task_id_from_gemini_reference(chat_url)
        match = LOGICAL_TASK_PATTERN.fullmatch(reference_id)
        if match is None or match.group(1).lower() != job_id.lower() or int(
            match.group(2)
        ) != batch_index:
            raise ValueError("Gemini 报价会话与任务编号不一致。")
        active = self._new_active(
            job_id,
            batch_index=batch_index,
            batch_count=batch_count,
            role=role,
            component_keys=component_keys,
        )
        self._switch_to_quote(active)
        return active

    def _assistant_messages(self) -> list[str]:
        values = self._execute(
            """
            const selectors = [
              'model-response', '.model-response-text', '[data-message-author-role="assistant"]',
              '[data-test-id*="model-response"]', '[class*="model-response"]'
            ];
            const seen = new Set();
            const result = [];
            for (const node of document.querySelectorAll(selectors.join(','))) {
              const text = (node.innerText || '').trim();
              if (!text || seen.has(text)) continue;
              seen.add(text);
              result.push(text);
            }
            return result;
            """
        )
        return list(values or [])

    def _generation_active(self) -> bool:
        return bool(
            self._execute(
                """
                return [...document.querySelectorAll('button,[role="button"]')].some(node => {
                  const text = `${node.innerText || ''} ${node.getAttribute('aria-label') || ''}`;
                  const rect = node.getBoundingClientRect();
                  return rect.width > 10 && rect.height > 10 && /停止生成|Stop generating|Stop response/i.test(text);
                });
                """
            )
        )

    def _scroll_to_latest(self) -> None:
        self._execute(
            """
            const root = document.querySelector('main,[role="main"]') || document.body;
            const scrollables = [root, ...root.querySelectorAll('*')].filter(node =>
              node.scrollHeight > node.clientHeight + 24
            );
            for (const node of scrollables) node.scrollTop = node.scrollHeight;
            window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'});
            """
        )

    def _approve_tool_if_needed(self) -> bool:
        buttons = self.driver.find_elements(By.CSS_SELECTOR, "button,[role='button']")
        for button in buttons:
            with suppress(Exception):
                label = (button.text or button.get_attribute("aria-label") or "").strip()
                if label.casefold() not in APPROVE_LABELS or not button.is_displayed():
                    continue
                card = button
                card_text = label
                for _ in range(8):
                    if str(card.tag_name or "").lower() in {"html", "body", "main"}:
                        break
                    card_text = str(card.text or "")
                    if approval_card_is_safe(card_text, label):
                        button.click()
                        return True
                    card = card.find_element(By.XPATH, "..")
        return False

    def _retry_visible(self) -> Any | None:
        for button in self.driver.find_elements(By.CSS_SELECTOR, "button,[role='button']"):
            with suppress(Exception):
                label = (button.text or button.get_attribute("aria-label") or "").strip()
                if label.casefold() in RETRY_LABELS and button.is_displayed():
                    return button
        return None

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
            return None
        retry = self._retry_visible()
        if retry is not None:
            if quote.retry_visible_since is None:
                quote.retry_visible_since = now
            elif (
                not quote.retry_clicked
                and now - quote.retry_visible_since >= 60
                and (completion_check is None or not completion_check())
            ):
                retry.click()
                quote.retry_clicked = True
            if now >= quote.deadline:
                raise TimeoutError("Gemini 重试页面没有恢复，也没有后台进展。")
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
                raise TimeoutError("Gemini 报价未在限定时间内完成。")
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
        if now >= quote.deadline:
            raise TimeoutError("Gemini 报价未在限定时间内产生真实后台进展。")
        return None

    def continue_quote(self, quote: Any, prompt: str) -> None:
        self._switch_to_quote(quote)
        assistant_count = len(self._assistant_messages())
        self._send_prompt(prompt, new_task=False)
        now = time.monotonic()
        quote.minimum_assistant_messages = assistant_count + 1
        quote.last_text = ""
        quote.stable_since = now
        quote.deadline = now + self.quote_timeout_seconds
        quote.saw_assistant = False
        quote.retry_visible_since = None
        quote.retry_clicked = False
        quote.generation_grace_used = False

    def _stop_generation(self) -> None:
        if self.driver is None:
            return
        for button in self.driver.find_elements(By.CSS_SELECTOR, "button,[role='button']"):
            with suppress(Exception):
                label = f"{button.text} {button.get_attribute('aria-label') or ''}"
                if button.is_displayed() and re.search(
                    r"停止生成|Stop generating|Stop response", label, re.IGNORECASE
                ):
                    button.click()
                    return

    def close_quote(self, quote: Any) -> None:
        self._switch_to_quote(quote)
        self._stop_generation()

    def cancel_quote(self, quote: Any) -> None:
        self.close_quote(quote)

    def capture_debug(self, job_id: str) -> None:
        if self.driver is None:
            return
        debug_directory = GEMINI_STATE_PATH.parent / "debug"
        debug_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.driver.save_screenshot(str(debug_directory / f"gemini-{job_id}.png"))
        _atomic_json(
            debug_directory / f"gemini-{job_id}.json",
            {
                "surface": self.surface_name,
                "url": str(self.driver.current_url or ""),
                "body": str(self.driver.find_element(By.TAG_NAME, "body").text or "")[-6000:],
            },
        )
