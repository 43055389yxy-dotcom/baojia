#!/usr/bin/env python3
"""Drive one logged-in desktop window across isolated sales quote chats.

The worker never clones browser profiles or opens one tab per quote. It keeps
the stable conversation URL for every active quote and visits those chats in a
round-robin loop; generation continues remotely while another chat is shown.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.gpt_browser_navigation import (
    active_quote_poll_order,
    bounded_continuation_attempts,
    canonical_url_path,
    is_interrupted_response,
    is_new_project_chat,
    is_project_landing_url,
    is_scroll_to_latest_action,
    is_single_use_permission_action,
    is_tool_permission_prompt,
    is_transient_browser_poll_exception,
    should_extend_quote_deadline,
)
from app.services.gpt_quote_batches import (
    build_component_batch_continuation_prompt,
    build_component_batch_prompt,
    build_quote_merge_prompt,
)
from app.services.gpt_quote_prompt import (
    build_quote_context_prompt,
    build_quote_continuation_prompt,
    build_quote_failed_components_retry_prompt,
    build_quote_partial_finalization_prompt,
    build_quote_prompt,
    parse_final_response,
)
from app.services.gpt_quote_relay import GptQuoteRelayStore, utc_now
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

CHATGPT_URL = os.environ.get("ASTRAQUOTE_CHATGPT_URL", "https://chatgpt.com/projects")
PROJECT_NAME = os.environ.get("ASTRAQUOTE_CHATGPT_PROJECT", "baojia")
PROFILE_DIRECTORY = Path(
    os.environ.get(
        "ASTRAQUOTE_FIREFOX_PROFILE",
        "/home/ec2-user/.mozilla/firefox/z97ytnft.default-default",
    )
)
STATE_PATH = Path(
    os.environ.get(
        "ASTRAQUOTE_GPT_RELAY_STATE",
        "/home/ec2-user/astraquote/data/gpt-relay/browser-state.json",
    )
)
POLL_SECONDS = float(os.environ.get("ASTRAQUOTE_GPT_RELAY_POLL_SECONDS", "4"))
QUOTE_TIMEOUT_SECONDS = int(os.environ.get("ASTRAQUOTE_GPT_QUOTE_TIMEOUT", "600"))
MAX_CONTINUATION_ATTEMPTS = bounded_continuation_attempts(
    os.environ.get("ASTRAQUOTE_GPT_MAX_CONTINUATIONS")
)
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_heartbeat(
    store: GptQuoteRelayStore,
    *,
    logged_in: bool,
    message: str,
    browser: str = "Firefox",
) -> None:
    atomic_json(
        store.heartbeat_path,
        {
            "updated_at": utc_now(),
            "worker_id": WORKER_ID,
            "browser": browser,
            "logged_in": logged_in,
            "project_name": PROJECT_NAME,
            "message": message,
        },
    )


@dataclass
class ActiveQuote:
    job_id: str
    chat_url: str
    deadline: float
    last_text: str = ""
    stable_since: float = 0.0
    saw_assistant: bool = False
    retry_visible_since: float | None = None
    retry_clicked: bool = False
    minimum_assistant_messages: int = 0
    generation_grace_used: bool = False
    batch_index: int = 0
    batch_count: int = 1
    role: str = "coordinator"
    component_keys: tuple[str, ...] = ()
    batch_progress_fingerprint: str = ""
    machine_progress_fingerprint: str = ""
    stalled_attempts: int = 0

    @property
    def session_key(self) -> str:
        return f"{self.job_id}:{self.batch_index}"


class ChatGptBrowser:
    def __init__(self) -> None:
        self.driver: webdriver.Firefox | None = None
        self.project_url: str | None = None

    def start(self) -> None:
        if not PROFILE_DIRECTORY.is_dir():
            raise RuntimeError(f"Firefox profile does not exist: {PROFILE_DIRECTORY}")
        options = Options()
        options.add_argument("-profile")
        options.add_argument(str(PROFILE_DIRECTORY))
        options.set_preference("browser.sessionstore.resume_from_crash", False)
        options.set_preference("browser.shell.checkDefaultBrowser", False)
        self.driver = webdriver.Firefox(options=options)
        self.driver.set_page_load_timeout(90)
        self.driver.get(CHATGPT_URL)

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
            self.project_url = None

    def _driver(self) -> webdriver.Firefox:
        if self.driver is None:
            raise RuntimeError("browser is not running")
        return self.driver

    def _switch_to_quote(self, quote: ActiveQuote) -> None:
        driver = self._driver()
        if canonical_url_path(driver.current_url) != canonical_url_path(quote.chat_url):
            driver.get(quote.chat_url)
            WebDriverWait(driver, 90).until(
                lambda _: canonical_url_path(driver.current_url)
                == canonical_url_path(quote.chat_url)
            )

    def close_quote(self, quote: ActiveQuote) -> None:
        """Stop any residual generation without closing the shared window."""

        driver = self._driver()
        self._switch_to_quote(quote)
        stop_buttons = self._visible(
            driver.find_elements(
                By.CSS_SELECTOR,
                "button[data-testid='stop-button'], button[aria-label*='Stop'], "
                "button[aria-label*='停止']",
            )
        )
        if stop_buttons:
            self._click(stop_buttons[0], driver)

    def capture_debug(self, job_id: str) -> None:
        """Keep a local-only screenshot and non-sensitive control inventory."""
        driver = self._driver()
        debug_directory = STATE_PATH.parent / "debug"
        debug_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        driver.save_screenshot(str(debug_directory / f"{job_id}.png"))
        controls: list[dict[str, str]] = []
        for element in driver.find_elements(
            By.CSS_SELECTOR,
            "button, a, input, textarea, [role='button']",
        ):
            if not element.is_displayed():
                continue
            controls.append(
                {
                    "tag": element.tag_name,
                    "text": element.text.strip()[:120],
                    "aria_label": str(element.get_attribute("aria-label") or "")[:120],
                    "href": str(element.get_attribute("href") or "")[:240],
                }
            )
            if len(controls) >= 80:
                break
        atomic_json(
            debug_directory / f"{job_id}.json",
            {"url": driver.current_url, "title": driver.title, "controls": controls},
        )

    def logged_in(self) -> bool:
        driver = self._driver()
        if "/auth/login" in driver.current_url or "/auth/logout" in driver.current_url:
            return False
        # The project landing page can legitimately have no composer, and a
        # slow ChatGPT load can temporarily show only "Retry".  The authenticated
        # browser session is the stable signal; never call that a logout merely
        # because the page body has not rendered yet.
        try:
            cookie_names = {
                str(cookie.get("name") or "") for cookie in driver.get_cookies()
            }
        except WebDriverException:
            cookie_names = set()
        if any(
            name == "_puid"
            or name == "oai-client-auth-signature"
            or "next-auth.session-token" in name
            for name in cookie_names
        ):
            return True
        composers = driver.find_elements(
            By.CSS_SELECTOR,
            "#prompt-textarea, textarea[data-id='root'], "
            "[contenteditable='true'][data-virtualkeyboard]",
        )
        if any(element.is_displayed() for element in composers):
            return True
        messages = driver.find_elements(
            By.CSS_SELECTOR,
            "[data-message-author-role='user'], [data-message-author-role='assistant']",
        )
        if any(element.is_displayed() for element in messages):
            return True
        login_controls = driver.find_elements(
            By.XPATH,
            "//*[self::a or self::button]"
            "[contains(normalize-space(.), 'Log in') "
            "or contains(normalize-space(.), '登录')]",
        )
        return not any(element.is_displayed() for element in login_controls) and bool(
            driver.find_elements(By.CSS_SELECTOR, "a[href*='/projects'], a[href*='/g/g-p-']")
        )

    @staticmethod
    def _visible(elements: list[WebElement]) -> list[WebElement]:
        visible: list[WebElement] = []
        for element in elements:
            try:
                if element.is_displayed():
                    visible.append(element)
            except WebDriverException:
                # ChatGPT frequently replaces live nodes between discovery and
                # visibility checks. The next polling pass will find the new node.
                continue
        return visible

    @staticmethod
    def _control_label(control: WebElement) -> str:
        try:
            return " ".join(
                (control.text or control.get_attribute("aria-label") or "").split()
            )
        except WebDriverException:
            return ""

    def _tool_permission_container(
        self,
        control: WebElement,
    ) -> WebElement | None:
        """Find the permission card owning a visible approval control."""

        current = control
        for _ in range(12):
            try:
                if is_tool_permission_prompt(current.text):
                    return current
                parent = current.find_element(By.XPATH, "..")
                if parent.id == current.id:
                    break
                current = parent
            except WebDriverException:
                return None
        return None

    def _text_control(self, labels: tuple[str, ...]) -> WebElement | None:
        driver = self._driver()
        for label in labels:
            xpath = (
                "//*[self::button or self::a or @role='button']"
                f"[contains(normalize-space(.), {json.dumps(label)})]"
            )
            visible = self._visible(driver.find_elements(By.XPATH, xpath))
            if visible:
                return visible[0]
        return None

    def _approve_tool_if_needed(self) -> bool:
        """Approve AstraQuote once inside the active quote chat."""

        driver = self._driver()
        controls = self._visible(
            driver.find_elements(
                By.CSS_SELECTOR,
                "button, [role='button'], [role='menuitem'], [role='option']",
            )
        )

        # The permission card is not consistently marked role=dialog. Start
        # from the single-use button that is actually visible, then verify that
        # an ancestor names AstraQuote. Never approve another connected tool
        # and never grant account-wide persistent permission from this robot.
        for control in controls:
            if not is_single_use_permission_action(self._control_label(control)):
                continue
            container = self._tool_permission_container(control)
            if container is None:
                continue
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'nearest'})",
                control,
            )
            self._click(control, driver)
            return True
        return False

    def _scroll_to_latest(self) -> bool:
        """Keep the active quote tab following its newest message without screenshots."""

        driver = self._driver()
        # Tool permission cards are rendered below the assistant message and
        # can sit outside the viewport. Move the actual conversation scroller,
        # not merely the browser window, to its bottom before looking for one.
        driver.execute_script(
            """
            const messages = document.querySelectorAll(
              "[data-message-author-role='user'], [data-message-author-role='assistant']"
            );
            let node = messages.length ? messages[messages.length - 1] : null;
            while (node) {
              if (node.scrollHeight > node.clientHeight + 4) {
                node.scrollTop = node.scrollHeight;
              }
              node = node.parentElement;
            }
            if (document.scrollingElement) {
              document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight;
            }
            window.scrollTo(0, document.body.scrollHeight);
            """
        )
        controls = self._visible(
            driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
        )
        for control in controls:
            label = " ".join(
                (control.text or control.get_attribute("aria-label") or "").split()
            )
            if is_scroll_to_latest_action(label):
                self._click(control, driver)
                return True

        messages = driver.find_elements(
            By.CSS_SELECTOR,
            "[data-message-author-role='user'], [data-message-author-role='assistant']",
        )
        if messages:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'end', inline:'nearest'})",
                messages[-1],
            )
            return True
        return False

    @staticmethod
    def _click(element: WebElement, driver: webdriver.Firefox) -> None:
        try:
            element.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click()", element)

    def _project_card(self) -> WebElement | None:
        """Return the project-list row, never a chat whose subtitle is the project name."""

        driver = self._driver()
        exact_targets = self._visible(
            driver.find_elements(
                By.XPATH,
                f"//*[normalize-space(.)={json.dumps(PROJECT_NAME)}]",
            )
        )
        for target in exact_targets:
            if target.find_elements(
                By.XPATH,
                "ancestor::a[contains(@href, '/c/')]",
            ):
                continue
            if target.find_elements(
                By.XPATH,
                "ancestor::*[@role='row' or @role='gridcell']",
            ):
                return target
        return None

    def _wait_for_composer_without_refresh(self, timeout: int = 180) -> WebElement:
        """Let a slow ChatGPT render settle; click Retry at most once after 60 seconds."""

        deadline = time.monotonic() + timeout
        retry_visible_since: float | None = None
        retry_clicked = False
        while time.monotonic() < deadline:
            try:
                return self._composer(2)
            except Exception:
                pass
            retry = self._text_control(("重试", "Retry"))
            now = time.monotonic()
            if retry is not None:
                if retry_visible_since is None:
                    retry_visible_since = now
                elif not retry_clicked and now - retry_visible_since >= 60:
                    self._click(retry, self._driver())
                    retry_clicked = True
            else:
                retry_visible_since = None
            time.sleep(2)
        raise TimeoutError("ChatGPT 项目页等待 180 秒后仍未出现输入框。")

    def ensure_project(self) -> str:
        driver = self._driver()
        driver.get(CHATGPT_URL)
        deadline = time.monotonic() + 180
        retry_visible_since: float | None = None
        retry_clicked = False
        while time.monotonic() < deadline:
            project_card = self._project_card()
            if project_card is not None:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'})",
                    project_card,
                )
                self._click(project_card, driver)
                WebDriverWait(driver, 90).until(
                    lambda _: is_project_landing_url(driver.current_url)
                )
                url = driver.current_url
                self.project_url = url
                return url

            retry = self._text_control(("重试", "Retry"))
            now = time.monotonic()
            if retry is not None:
                if retry_visible_since is None:
                    retry_visible_since = now
                elif not retry_clicked and now - retry_visible_since >= 60:
                    self._click(retry, driver)
                    retry_clicked = True
            else:
                retry_visible_since = None
            time.sleep(2)

        raise RuntimeError(
            f"等待 180 秒后仍没有找到现有 ChatGPT 项目“{PROJECT_NAME}”；程序不会自动创建项目。"
        )

    def open_new_project_chat(self) -> str:
        driver = self._driver()
        project_url = self.ensure_project()
        if not is_project_landing_url(driver.current_url):
            raise RuntimeError("没有进入 ChatGPT 项目的新对话页。")
        self._wait_for_composer_without_refresh()
        return project_url

    def start_quote(self, job_id: str, prompt: str) -> ActiveQuote:
        state = load_state()
        previous_chat_url = str(state.get("last_chat_url") or "") or None
        project_url = self.open_new_project_chat()
        chat_url = self.submit(prompt)
        if not is_new_project_chat(project_url, chat_url, previous_chat_url):
            raise RuntimeError("ChatGPT 没有为本次报价创建新的项目对话。")
        atomic_json(
            STATE_PATH,
            {
                "project_url": project_url,
                "project_name": PROJECT_NAME,
                "last_chat_url": chat_url,
            },
        )
        now = time.monotonic()
        return ActiveQuote(
            job_id=job_id,
            chat_url=chat_url,
            deadline=now + QUOTE_TIMEOUT_SECONDS,
            stable_since=now,
        )

    def start_component_batch(
        self,
        job_id: str,
        prompt: str,
        *,
        batch_index: int,
        batch_count: int,
        component_keys: list[str],
    ) -> ActiveQuote:
        active = self.start_quote(job_id, prompt)
        active.batch_index = batch_index
        active.batch_count = batch_count
        active.role = "component_batch"
        active.component_keys = tuple(component_keys)
        return active

    def resume_quote(
        self,
        job_id: str,
        chat_url: str,
        *,
        batch_index: int = 0,
        batch_count: int = 1,
        role: str = "coordinator",
        component_keys: list[str] | None = None,
    ) -> ActiveQuote:
        """Open an already-submitted conversation without resending source text."""

        driver = self._driver()
        driver.get(chat_url)
        WebDriverWait(driver, 90).until(
            lambda _: canonical_url_path(driver.current_url)
            == canonical_url_path(chat_url)
        )
        now = time.monotonic()
        return ActiveQuote(
            job_id=job_id,
            chat_url=chat_url,
            deadline=now + QUOTE_TIMEOUT_SECONDS,
            stable_since=now,
            batch_index=batch_index,
            batch_count=batch_count,
            role=role,
            component_keys=tuple(component_keys or []),
        )

    def poll_quote(
        self,
        quote: ActiveQuote,
        completion_check: Callable[[], bool] | None = None,
    ) -> str | None:
        driver = self._driver()
        self._switch_to_quote(quote)
        if canonical_url_path(driver.current_url) != canonical_url_path(quote.chat_url):
            raise RuntimeError("报价工作页已离开对应会话。")
        self._scroll_to_latest()
        if self._approve_tool_if_needed():
            now = time.monotonic()
            quote.stable_since = now
            if now >= quote.deadline:
                raise TimeoutError("报价授权后仍未在期限内产生后台进展。")
            return None
        retry = self._text_control(("重试", "Retry"))
        if retry is not None:
            now = time.monotonic()
            if quote.retry_visible_since is None:
                quote.retry_visible_since = now
            if not quote.retry_clicked and now - quote.retry_visible_since >= 60:
                if completion_check is not None and completion_check():
                    return None
                self._click(retry, driver)
                quote.retry_clicked = True
                quote.retry_visible_since = now
            if now >= quote.deadline:
                raise TimeoutError("ChatGPT 重试页面没有恢复，也没有后台进展。")
            return None
        quote.retry_visible_since = None
        messages = self._visible(
            driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='assistant']")
        )
        now = time.monotonic()
        stop_buttons = self._visible(
            driver.find_elements(
                By.CSS_SELECTOR,
                "button[data-testid='stop-button'], button[aria-label*='Stop'], "
                "button[aria-label*='停止']",
            )
        )
        if len(messages) < quote.minimum_assistant_messages:
            if should_extend_quote_deadline(
                deadline_reached=now >= quote.deadline,
                generation_active=bool(stop_buttons),
                retry_visible=False,
            ) and not quote.generation_grace_used:
                quote.deadline = now + QUOTE_TIMEOUT_SECONDS
                quote.generation_grace_used = True
                return None
            if now >= quote.deadline:
                raise TimeoutError(
                    f"ChatGPT quote did not finish in {QUOTE_TIMEOUT_SECONDS} seconds"
                )
            return None
        current = messages[-1].text.strip() if messages else ""
        if current:
            quote.saw_assistant = True
            if current != quote.last_text:
                quote.last_text = current
                quote.stable_since = now
        if current and not stop_buttons and is_interrupted_response(current):
            return current
        if quote.saw_assistant and not stop_buttons and now - quote.stable_since >= 8:
            return quote.last_text
        if should_extend_quote_deadline(
            deadline_reached=now >= quote.deadline,
            generation_active=bool(stop_buttons),
            retry_visible=False,
        ) and not quote.generation_grace_used:
            quote.deadline = now + QUOTE_TIMEOUT_SECONDS
            quote.generation_grace_used = True
            return None
        if now >= quote.deadline:
            raise TimeoutError(f"ChatGPT quote did not finish in {QUOTE_TIMEOUT_SECONDS} seconds")
        return None

    def cancel_quote(self, quote: ActiveQuote) -> None:
        """Stop and close only the cancelled quote's tab."""
        driver = self._driver()
        self._switch_to_quote(quote)
        if canonical_url_path(driver.current_url) == canonical_url_path(quote.chat_url):
            stop_buttons = self._visible(
                driver.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-testid='stop-button'], button[aria-label*='Stop'], "
                    "button[aria-label*='停止']",
                )
            )
            if stop_buttons:
                self._click(stop_buttons[0], driver)
            for dialog in self._visible(
                driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")
            ):
                for control in self._visible(
                    dialog.find_elements(By.CSS_SELECTOR, "button, [role='button']")
                ):
                    label = " ".join(
                        (control.text or control.get_attribute("aria-label") or "").split()
                    ).casefold()
                    if label in {"拒绝", "取消", "deny", "cancel"}:
                        self._click(control, driver)
                        break
        self.close_quote(quote)

    def _composer(self, timeout: int = 30) -> WebElement:
        driver = self._driver()

        def find(_: webdriver.Firefox) -> WebElement | bool:
            selectors = (
                "#prompt-textarea",
                "textarea[data-id='root']",
                "[contenteditable='true'][data-virtualkeyboard]",
                "form [contenteditable='true']",
            )
            for selector in selectors:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed() and element.is_enabled():
                        return element
            return False

        return WebDriverWait(driver, timeout).until(find)

    def submit(self, prompt: str) -> str:
        driver = self._driver()
        self._send_prompt(prompt)
        WebDriverWait(driver, 30).until(lambda _: "/c/" in driver.current_url)
        return driver.current_url

    def _send_prompt(self, prompt: str) -> None:
        driver = self._driver()
        user_message_count = len(
            driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='user']")
        )
        composer = self._composer(45)
        composer.click()
        lines = prompt.splitlines()
        for index, line in enumerate(lines):
            if line:
                composer.send_keys(line)
            if index < len(lines) - 1:
                composer.send_keys(Keys.SHIFT, Keys.ENTER)
        composer.send_keys(Keys.ENTER)
        WebDriverWait(driver, 30).until(
            lambda _: len(
                driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='user']")
            )
            > user_message_count
        )

    def continue_quote(self, quote: ActiveQuote, prompt: str) -> None:
        """Continue an unfinished response in its original isolated quote tab."""

        driver = self._driver()
        self._switch_to_quote(quote)
        if canonical_url_path(driver.current_url) != canonical_url_path(quote.chat_url):
            raise RuntimeError("报价工作页已离开对应会话。")
        assistant_count = len(
            self._visible(
                driver.find_elements(
                    By.CSS_SELECTOR,
                    "[data-message-author-role='assistant']",
                )
            )
        )
        self._send_prompt(prompt)
        quote.minimum_assistant_messages = assistant_count + 1
        quote.last_text = ""
        quote.stable_since = time.monotonic()
        quote.deadline = quote.stable_since + QUOTE_TIMEOUT_SECONDS
        quote.saw_assistant = False
        quote.retry_visible_since = None
        quote.retry_clicked = False
        quote.generation_grace_used = False

    def wait_for_final_response(self, timeout_seconds: int) -> str:
        driver = self._driver()
        deadline = time.monotonic() + timeout_seconds
        last_text = ""
        stable_since = time.monotonic()
        saw_assistant = False
        while time.monotonic() < deadline:
            messages = self._visible(
                driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='assistant']")
            )
            current = messages[-1].text.strip() if messages else ""
            if current:
                saw_assistant = True
                if current != last_text:
                    last_text = current
                    stable_since = time.monotonic()
            stop_buttons = self._visible(
                driver.find_elements(
                    By.CSS_SELECTOR,
                    "button[data-testid='stop-button'], button[aria-label*='Stop'], "
                    "button[aria-label*='停止']",
                )
            )
            if saw_assistant and not stop_buttons and time.monotonic() - stable_since >= 8:
                return last_text
            time.sleep(2)
        raise TimeoutError(f"ChatGPT quote did not finish in {timeout_seconds} seconds")


def mark_needs_login(store: GptQuoteRelayStore, record: dict[str, Any]) -> None:
    store.update_if_not_cancelled(
        record["job_id"],
        {"status": "needs_login", "worker_id": None, "lease_expires_at": None},
        stage="login",
        message="服务器 ChatGPT 登录已失效，等待管理员在运维桌面重新登录",
    )


def submit_job(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    record: dict[str, Any],
) -> ActiveQuote | None:
    job_id = record["job_id"]
    if not browser.logged_in():
        mark_needs_login(store, record)
        return None
    customer_request = str(record.get("customer_request") or "").strip()
    if not customer_request:
        store.update_if_not_cancelled(
            job_id,
            {
                "status": "failed",
                "error": {
                    "code": "source_missing",
                    "message": "待清洗客户需求已不存在。",
                },
            },
            stage="failed",
            message="客户需求缺失，任务已安全停止",
        )
        return None

    latest = store.get(job_id)
    if latest.get("status") == "cancelled":
        return None
    prompt = build_quote_prompt(
        customer_request,
        record.get("quote_options") or {},
        relay_job_id=job_id,
        submission_code=str(record.get("submission_code") or "").strip(),
    )
    active = browser.start_quote(job_id, prompt)
    store.record_chat_session(
        job_id,
        batch_index=0,
        batch_count=1,
        chat_url=active.chat_url,
        role="coordinator",
        component_keys=[],
    )
    store.update_if_not_cancelled(
        job_id, {"project_name": PROJECT_NAME}, stage="submitted",
        message="已在 AstraQuote 项目中创建总控对话并提交需求清洗",
    )
    store.purge_source(job_id)
    return active


def batch_progress_fingerprint(batch: dict[str, Any], previous: str = "") -> str:
    try:
        prior = json.loads(previous or "[]")
    except ValueError:
        prior = []
    if isinstance(prior, dict):
        prior = [key for key, state in prior.items() if state == "completed"]
    completed = {
        key for key, state in (batch.get("component_states") or {}).items()
        if state == "completed"
    }
    return json.dumps(
        sorted(set(prior) | completed),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def batch_is_finished(batch: dict[str, Any]) -> bool:
    states = list((batch.get("component_states") or {}).values())
    return bool(states) and all(state in {"completed", "failed"} for state in states)


def active_batch(
    store: GptQuoteRelayStore,
    active: ActiveQuote,
) -> dict[str, Any] | None:
    return next(
        (
            batch
            for batch in store.quote_chat_batches(active.job_id)
            if int(batch["batch_index"]) == active.batch_index
        ),
        None,
    )


def refresh_progress_deadline(store: GptQuoteRelayStore, active: ActiveQuote) -> None:
    """Only saved successful work extends the no-progress deadline."""

    batch = active_batch(store, active) if active.batch_count > 1 else None
    if batch is not None and active.role != "merge":
        fingerprint = batch_progress_fingerprint(batch, active.machine_progress_fingerprint)
    else:
        fingerprint = store._merge_progress(
            active.machine_progress_fingerprint,
            store.progress_fingerprint(active.job_id),
        )
    if fingerprint != active.machine_progress_fingerprint:
        active.machine_progress_fingerprint = fingerprint
        active.deadline = time.monotonic() + QUOTE_TIMEOUT_SECONDS
        active.generation_grace_used = False


def continue_component_batch(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    active: ActiveQuote,
) -> bool:
    """Continue a child batch twice only while its machine state is unchanged."""

    batch = active_batch(store, active)
    if batch is None:
        return False
    fingerprint = batch_progress_fingerprint(batch, active.batch_progress_fingerprint)
    if fingerprint != active.batch_progress_fingerprint:
        active.batch_progress_fingerprint = fingerprint
        active.stalled_attempts = 0
    if batch_is_finished(batch):
        store.update_chat_session(active.job_id, active.batch_index, status="saved")
        return False
    if active.stalled_attempts >= MAX_CONTINUATION_ATTEMPTS:
        store.update_chat_session(
            active.job_id,
            active.batch_index,
            status="stalled",
            stalled_attempts=active.stalled_attempts,
        )
        return False
    active.stalled_attempts += 1
    # Reserve before sending. An uncertain send or worker restart must not
    # restore the same allowance and repeat the instruction forever.
    store.update_chat_session(
        active.job_id,
        active.batch_index,
        status="running",
        stalled_attempts=active.stalled_attempts,
        progress_fingerprint=fingerprint,
    )
    latest = store.get(active.job_id)
    browser.continue_quote(
        active,
        build_component_batch_continuation_prompt(
            relay_job_id=active.job_id,
            submission_code=str(latest.get("submission_code") or ""),
            price_batch_id=str(batch["price_batch_id"]),
            batch_index=active.batch_index,
            batch_count=int(batch["batch_count"]),
            component_keys=list(batch["component_keys"]),
        ),
    )
    return True


def create_missing_component_chats(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    active_quotes: dict[str, ActiveQuote],
    job_id: str,
) -> None:
    """Open pending component chats through the one shared desktop window."""

    batches = store.quote_chat_batches(job_id)
    if len(batches) <= 1:
        return
    record = store.get(job_id)
    if record.get("status") != "processing":
        return
    sessions = {
        int(item.get("batch_index", -1)): item
        for item in (record.get("chat_sessions") or [])
    }
    coordinator = sessions.get(0)
    if coordinator:
        store.update_chat_session(
            job_id, 0,
            batch_count=len(batches),
            component_keys=list(batches[0]["component_keys"]),
        )
        current = active_quotes.get(f"{job_id}:0")
        if current is not None:
            current.batch_count = len(batches)
            current.component_keys = tuple(batches[0]["component_keys"])

    for batch in batches[1:]:
        batch_index = int(batch["batch_index"])
        if batch_is_finished(batch) or batch_index in sessions:
            continue
        if len(active_quotes) >= store.max_concurrent_quotes:
            break
        prompt = build_component_batch_prompt(
            relay_job_id=job_id,
            submission_code=str(record.get("submission_code") or ""),
            price_batch_id=str(batch["price_batch_id"]),
            batch_index=batch_index,
            batch_count=int(batch["batch_count"]),
            components=list(batch["components"]),
            quote_context=build_quote_context_prompt(record.get("quote_options") or {}),
        )
        active = browser.start_component_batch(
            job_id,
            prompt,
            batch_index=batch_index,
            batch_count=int(batch["batch_count"]),
            component_keys=list(batch["component_keys"]),
        )
        store.record_chat_session(
            job_id,
            batch_index=batch_index,
            batch_count=int(batch["batch_count"]),
            chat_url=active.chat_url,
            role="component_batch",
            component_keys=list(batch["component_keys"]),
        )
        active.batch_progress_fingerprint = batch_progress_fingerprint(batch)
        active_quotes[active.session_key] = active


def maybe_start_final_merge(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    active_quotes: dict[str, ActiveQuote],
    job_id: str,
) -> None:
    """Return to the coordinator only after every component chat has stopped."""

    batches = store.quote_chat_batches(job_id)
    if len(batches) <= 1:
        return
    record = store.get(job_id)
    if record.get("status") != "processing":
        return
    if any(active.job_id == job_id for active in active_quotes.values()):
        return
    if len(active_quotes) >= store.max_concurrent_quotes:
        return
    sessions = {
        int(item.get("batch_index", -1)): item
        for item in (record.get("chat_sessions") or [])
    }
    for batch in batches:
        if batch_is_finished(batch):
            continue
        session = sessions.get(int(batch["batch_index"]))
        if not session or session.get("status") != "stalled":
            return
    if any(item.get("status") == "merging" for item in sessions.values()):
        return
    if any(item.get("status") == "running" for item in sessions.values()):
        return
    coordinator = sessions.get(0)
    if not coordinator:
        return
    active = active_quotes.get(f"{job_id}:0")
    if active is None:
        active = browser.resume_quote(
            job_id,
            str(coordinator["chat_url"]),
            batch_index=0,
            batch_count=len(batches),
            role="merge",
            component_keys=list(batches[0]["component_keys"]),
        )
    else:
        active.role = "merge"
    browser.continue_quote(
        active,
        build_quote_merge_prompt(
            relay_job_id=job_id,
            submission_code=str(record.get("submission_code") or ""),
            price_batch_id=str(batches[0]["price_batch_id"]),
        ),
    )
    store.update_chat_session(job_id, 0, status="merging")
    active_quotes[active.session_key] = active


def complete_job(store: GptQuoteRelayStore, job_id: str, response: str) -> str:
    current = store.reconcile_delivery_receipt(job_id)
    if current.get("status") in {"cancelled", "completed", "partial", "failed"}:
        return str(current["status"])
    status, summary = parse_final_response(response)
    # A completion sentence is not delivery evidence. Only the receipt above
    # may complete the job. A confirmed blocker skips futile retry messages.
    if current.get("partial_finalization_requested"):
        summary = "部分报价收口仍未产生可用交付回执。"
    elif store.has_unrecoverable_failure(job_id):
        if store.request_partial_finalization(job_id):
            return "partial_finalize"
        summary = summary or "报价遇到当前无法继续的阻塞。"
    elif status in {"incomplete", "displayed_on_page", "delivered", "blocked"}:
        return "continue"
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "result_summary": summary,
            "error": {"code": "gpt_quote_blocked", "message": summary},
            "lease_expires_at": None,
        },
        stage="failed",
        message="报价无法继续，已保存当前处理结果",
    )
    return "failed"


def fail_continuation_limit(store: GptQuoteRelayStore, job_id: str) -> None:
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "result_summary": "ChatGPT 多次只返回阶段进度，未给出最终完成或明确阻塞状态。",
            "error": {
                "code": "gpt_quote_continuation_limit",
                "message": "报价在限定续跑次数内仍未产生最终状态。",
            },
            "lease_expires_at": None,
        },
        stage="failed",
        message="报价多次续跑后仍未产生最终状态",
    )


def continue_from_saved_stage(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    active: ActiveQuote,
    *,
    message: str,
) -> bool:
    """Resume one unfinished quote in the same conversation without source text."""

    latest = store.reconcile_delivery_receipt(active.job_id)
    if latest.get("status") != "processing":
        return False
    if latest.get("partial_finalization_requested"):
        fail_continuation_limit(store, active.job_id)
        return False
    if store.has_unrecoverable_failure(active.job_id):
        if store.request_partial_finalization(active.job_id):
            browser.continue_quote(
                active,
                build_quote_partial_finalization_prompt(
                    relay_job_id=active.job_id,
                    submission_code=str(latest.get("submission_code") or ""),
                ),
            )
            return True
        complete_job(store, active.job_id, "")
        return False
    if not store.reserve_continuation(
        active.job_id,
        maximum=MAX_CONTINUATION_ATTEMPTS,
    ):
        if store.request_partial_finalization(active.job_id):
            partial_prompt = build_quote_partial_finalization_prompt(
                relay_job_id=active.job_id,
                submission_code=str(latest.get("submission_code") or ""),
            )
            browser.continue_quote(active, partial_prompt)
            return True
        fail_continuation_limit(store, active.job_id)
        return False
    continuation_prompt = build_quote_continuation_prompt(
        relay_job_id=active.job_id,
        submission_code=str(latest.get("submission_code") or ""),
    )
    browser.continue_quote(active, continuation_prompt)
    store.update_if_not_cancelled(
        active.job_id,
        {},
        stage="continuing",
        message=message,
    )
    return True


def fail_job(store: GptQuoteRelayStore, job_id: str, exc: Exception) -> None:
    current = store.get(job_id)
    if current.get("status") == "cancelled":
        return
    source_was_submitted = bool(current.get("source_purged_at"))
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "customer_request": "" if source_was_submitted else current.get("customer_request", ""),
            "error": {
                "code": "gpt_browser_automation_failed",
                "message": str(exc)[:1200],
            },
            "lease_expires_at": None,
        },
        stage="failed",
        message="服务器 ChatGPT 浏览器自动化失败，任务已安全停止",
    )


def handle_no_progress_timeout(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    active: ActiveQuote,
    active_quotes: dict[str, ActiveQuote],
) -> None:
    """Use the same durable budget for timeouts and repeated DOM failures."""

    current = store.reconcile_delivery_receipt(active.job_id)
    if current.get("status") in {"completed", "partial", "cancelled", "failed"}:
        browser.close_quote(active)
        active_quotes.pop(active.session_key, None)
        return
    batches = store.quote_chat_batches(active.job_id)
    if len(batches) > 1 and active.role != "merge":
        if not continue_component_batch(store, browser, active):
            active_quotes.pop(active.session_key, None)
        maybe_start_final_merge(store, browser, active_quotes, active.job_id)
        return
    if not continue_from_saved_stage(
        store,
        browser,
        active,
        message="报价等待超时，已在原对话从保存阶段自动继续",
    ):
        browser.close_quote(active)
        active_quotes.pop(active.session_key, None)


def reattach_job_chats(
    store: GptQuoteRelayStore,
    browser: ChatGptBrowser,
    record: dict[str, Any],
    active_quotes: dict[str, ActiveQuote],
) -> None:
    """Restore every running logical chat after the desktop worker restarts."""

    sessions = list(record.get("chat_sessions") or [])
    if not sessions and record.get("chat_url"):
        sessions = [{
            "batch_index": 0,
            "batch_count": 1,
            "chat_url": record["chat_url"],
            "role": "coordinator",
            "component_keys": [],
            "status": "running",
        }]
    for session in sessions:
        if record.get("partial_retry_pending"):
            continue
        if session.get("status") not in {"running", "merging"}:
            continue
        batch_index = int(session.get("batch_index") or 0)
        active = browser.resume_quote(
            record["job_id"],
            str(session["chat_url"]),
            batch_index=batch_index,
            batch_count=int(session.get("batch_count") or 1),
            role=("merge" if session.get("status") == "merging" else str(
                session.get("role") or "coordinator"
            )),
            component_keys=list(session.get("component_keys") or []),
        )
        active.stalled_attempts = int(session.get("stalled_attempts") or 0)
        active.batch_progress_fingerprint = str(
            session.get("progress_fingerprint") or ""
        )
        active_quotes[active.session_key] = active

    if record.get("partial_retry_pending"):
        coordinator = next(
            (item for item in sessions if int(item.get("batch_index") or 0) == 0),
            None,
        )
        if coordinator:
            active = active_quotes.get(f"{record['job_id']}:0")
            if active is None:
                active = browser.resume_quote(
                    record["job_id"],
                    str(coordinator["chat_url"]),
                    batch_index=0,
                    batch_count=int(coordinator.get("batch_count") or 1),
                    role="merge",
                    component_keys=list(coordinator.get("component_keys") or []),
                )
            browser.continue_quote(
                active,
                build_quote_failed_components_retry_prompt(
                    relay_job_id=record["job_id"],
                    submission_code=str(record.get("submission_code") or ""),
                ),
            )
            active.role = "merge"
            active_quotes[active.session_key] = active
            store.update_chat_session(record["job_id"], 0, status="merging")
            store.update_if_not_cancelled(
                record["job_id"],
                {"partial_retry_pending": False},
                stage="retry",
                message="已在总控对话中仅重试未完成组件",
            )


def stop_terminal_job_chats(
    browser: ChatGptBrowser,
    active_quotes: dict[str, ActiveQuote],
    job_id: str,
    *,
    cancelled: bool = False,
) -> None:
    """Stop every still-running chat before releasing its shared slot."""

    for session_key, quote in list(active_quotes.items()):
        if quote.job_id != job_id:
            continue
        try:
            if cancelled:
                browser.cancel_quote(quote)
            else:
                browser.close_quote(quote)
        except (RuntimeError, WebDriverException):
            # Keep the slot and retry on the next poll.  Forgetting a chat on
            # an uncertain stop is what allowed old batches to run overnight.
            continue
        active_quotes.pop(session_key, None)


def main() -> int:
    store = GptQuoteRelayStore()
    browser = ChatGptBrowser()
    active_quotes: dict[str, ActiveQuote] = {}
    while True:
        try:
            if browser.driver is None:
                browser.start()
                for record in store.claim_submitted_for_monitoring(
                    WORKER_ID,
                    limit=None,
                    lease_minutes=35,
                ):
                    reattach_job_chats(store, browser, record, active_quotes)
                    create_missing_component_chats(
                        store, browser, active_quotes, record["job_id"],
                    )
                    maybe_start_final_merge(
                        store, browser, active_quotes, record["job_id"],
                    )
            logged_in = browser.logged_in()
            if logged_in:
                store.resume_login_waiting()
            elif not active_quotes:
                store.mark_queued_needs_login()
            write_heartbeat(
                store,
                logged_in=logged_in,
                message=(
                    f"正在并行处理 {len(active_quotes)} 个报价"
                    if active_quotes
                    else "等待销售报价任务"
                ) if logged_in else "等待管理员登录 ChatGPT",
            )
            if logged_in:
                for record in store.claim_submitted_for_monitoring(
                    WORKER_ID,
                    limit=None,
                    lease_minutes=35,
                    exclude_job_ids={quote.job_id for quote in active_quotes.values()},
                ):
                    reattach_job_chats(store, browser, record, active_quotes)
                    create_missing_component_chats(
                        store, browser, active_quotes, record["job_id"],
                    )
                    maybe_start_final_merge(
                        store, browser, active_quotes, record["job_id"],
                    )
            while logged_in:
                record = store.claim_next(WORKER_ID, lease_minutes=35)
                if record is None:
                    break
                try:
                    if record.get("partial_retry_pending") and record.get("chat_url"):
                        reattach_job_chats(store, browser, record, active_quotes)
                    else:
                        active = submit_job(store, browser, record)
                        if active is not None:
                            active_quotes[active.session_key] = active
                except Exception as exc:
                    try:
                        browser.capture_debug(record["job_id"])
                    except Exception:
                        pass
                    fail_job(store, record["job_id"], exc)

            processing_job_ids = {
                quote.job_id for quote in active_quotes.values()
            }
            for job_id in processing_job_ids:
                create_missing_component_chats(
                    store, browser, active_quotes, job_id,
                )
                maybe_start_final_merge(store, browser, active_quotes, job_id)

            for session_key in active_quote_poll_order(active_quotes):
                active = active_quotes.get(session_key)
                if active is None:
                    continue
                job_id = active.job_id
                current = store.reconcile_delivery_receipt(job_id)
                if current.get("status") in {"completed", "partial", "failed"}:
                    stop_terminal_job_chats(browser, active_quotes, job_id)
                    continue
                if current.get("status") == "cancelled":
                    stop_terminal_job_chats(
                        browser,
                        active_quotes,
                        job_id,
                        cancelled=True,
                    )
                    continue
                try:
                    store.renew_lease(job_id, WORKER_ID, lease_minutes=35)
                    refresh_progress_deadline(store, active)
                    response = browser.poll_quote(
                        active,
                        completion_check=lambda job_id=job_id: (
                            store.reconcile_delivery_receipt(job_id).get("status")
                            in {"completed", "partial"}
                        ),
                    )
                    if response is None:
                        continue
                    batches = store.quote_chat_batches(job_id)
                    if len(batches) > 1 and active.role != "merge":
                        if not continue_component_batch(store, browser, active):
                            active_quotes.pop(session_key, None)
                        maybe_start_final_merge(
                            store, browser, active_quotes, job_id,
                        )
                        continue
                    outcome = complete_job(store, job_id, response)
                    if outcome == "partial_finalize":
                        latest = store.get(job_id)
                        browser.continue_quote(
                            active,
                            build_quote_partial_finalization_prompt(
                                relay_job_id=job_id,
                                submission_code=str(
                                    latest.get("submission_code") or ""
                                ),
                            ),
                        )
                        continue
                    if outcome == "continue":
                        if not continue_from_saved_stage(
                            store,
                            browser,
                            active,
                            message="报价尚未完成，已在原对话从保存阶段自动继续",
                        ):
                            browser.close_quote(active)
                            active_quotes.pop(session_key, None)
                        continue
                    browser.close_quote(active)
                    active_quotes.pop(session_key, None)
                except TimeoutError:
                    handle_no_progress_timeout(store, browser, active, active_quotes)
                except Exception as exc:
                    if is_transient_browser_poll_exception(exc):
                        # ChatGPT replaces live DOM nodes while generating. The
                        # next polling round must locate fresh elements; the
                        # quote itself is still running and must remain active.
                        active.stable_since = time.monotonic()
                        if active.stable_since >= active.deadline:
                            handle_no_progress_timeout(store, browser, active, active_quotes)
                        continue
                    fail_job(store, job_id, exc)
                    try:
                        browser.close_quote(active)
                    except Exception:
                        pass
                    active_quotes.pop(session_key, None)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            browser.close()
            return 0
        except Exception as exc:
            write_heartbeat(store, logged_in=False, message=f"浏览器工作进程异常：{str(exc)[:500]}")
            # Submitted ChatGPT conversations continue server-side if Firefox
            # restarts. Leave them processing so the next browser session can
            # reattach by chat_url instead of reporting a false failure.
            active_quotes.clear()
            browser.close()
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
