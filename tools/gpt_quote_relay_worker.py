#!/usr/bin/env python3
"""Drive the server's logged-in browser for isolated sales quote jobs.

One visible Firefox process owns the administrator session. Each active quote
uses its own tab and conversation. Every queued quote receives an independent
work tab; there is no application-level concurrency cap.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.gpt_browser_navigation import (
    active_quote_poll_order,
    bounded_continuation_attempts,
    canonical_url_path,
    is_new_project_chat,
    is_persistent_permission_action,
    is_project_landing_url,
    is_scroll_to_latest_action,
    is_single_use_permission_action,
    is_tool_permission_prompt,
    is_transient_browser_poll_exception,
)
from app.services.gpt_quote_prompt import (
    build_quote_continuation_prompt,
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
QUOTE_TIMEOUT_SECONDS = int(os.environ.get("ASTRAQUOTE_GPT_QUOTE_TIMEOUT", "1800"))
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
    window_handle: str
    deadline: float
    last_text: str = ""
    stable_since: float = 0.0
    saw_assistant: bool = False
    retry_visible_since: float | None = None
    retry_clicked: bool = False
    minimum_assistant_messages: int = 0


class ChatGptBrowser:
    def __init__(self) -> None:
        self.driver: webdriver.Firefox | None = None
        self.project_url: str | None = None
        self.launcher_handle: str | None = None

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
        self.launcher_handle = self.driver.current_window_handle

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
            self.project_url = None
            self.launcher_handle = None

    def _driver(self) -> webdriver.Firefox:
        if self.driver is None:
            raise RuntimeError("browser is not running")
        return self.driver

    def _switch_to_launcher(self) -> None:
        driver = self._driver()
        handles = driver.window_handles
        if not handles:
            raise RuntimeError("browser has no open window")
        if self.launcher_handle not in handles:
            self.launcher_handle = handles[0]
        driver.switch_to.window(self.launcher_handle)

    def _switch_to_quote(self, quote: ActiveQuote) -> None:
        driver = self._driver()
        if quote.window_handle not in driver.window_handles:
            raise RuntimeError("该报价的独立工作页已关闭。")
        driver.switch_to.window(quote.window_handle)

    def close_quote(self, quote: ActiveQuote) -> None:
        """Close only one quote tab and preserve every other active quote."""

        driver = self._driver()
        if quote.window_handle in driver.window_handles:
            driver.switch_to.window(quote.window_handle)
            if len(driver.window_handles) > 1:
                driver.close()
        self._switch_to_launcher()

    def capture_debug(self, job_id: str) -> None:
        """Keep a local-only screenshot and non-sensitive control inventory."""
        driver = self._driver()
        debug_directory = STATE_PATH.parent / "debug"
        debug_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        driver.save_screenshot(str(debug_directory / f"{job_id}.png"))
        controls: list[dict[str, str]] = []
        for element in driver.find_elements(By.CSS_SELECTOR, "button, a, input, textarea, [role='button']"):
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
            "#prompt-textarea, textarea[data-id='root'], [contenteditable='true'][data-virtualkeyboard]",
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
            "//*[self::a or self::button][contains(normalize-space(.), 'Log in') or contains(normalize-space(.), '登录')]",
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
        """Persistently approve a tool card only inside the active quote tab."""

        driver = self._driver()
        controls = self._visible(
            driver.find_elements(
                By.CSS_SELECTOR,
                "button, [role='button'], [role='menuitem'], [role='option']",
            )
        )

        # The permission card is not consistently marked role=dialog. Start
        # from the approval button that is actually visible, then verify that
        # one of its ancestors contains an explicit ChatGPT tool question.
        for control in controls:
            if not is_persistent_permission_action(self._control_label(control)):
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

        # Some releases hide “始终允许” in the “允许一次” split-button menu.
        # Open only a split control whose ancestor is a tool permission card.
        menu_triggers: list[WebElement] = []
        for control in controls:
            if not is_single_use_permission_action(self._control_label(control)):
                continue
            container = self._tool_permission_container(control)
            if container is None:
                continue
            try:
                has_popup = str(control.get_attribute("aria-haspopup") or "").casefold()
                if has_popup in {"menu", "listbox", "true"}:
                    menu_triggers.append(control)
                    continue
                parent = control.find_element(By.XPATH, "..")
                for sibling in self._visible(
                    parent.find_elements(By.CSS_SELECTOR, "button, [role='button']")
                ):
                    has_popup = str(
                        sibling.get_attribute("aria-haspopup") or ""
                    ).casefold()
                    if has_popup in {"menu", "listbox", "true"}:
                        menu_triggers.append(sibling)
            except WebDriverException:
                continue

        for trigger in menu_triggers:
            self._click(trigger, driver)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                actions = self._visible(
                    driver.find_elements(
                        By.CSS_SELECTOR,
                        "button, [role='button'], [role='menuitem'], [role='option']",
                    )
                )
                for action in actions:
                    if is_persistent_permission_action(self._control_label(action)):
                        self._click(action, driver)
                        return True
                time.sleep(0.2)
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
        driver = self._driver()
        self._switch_to_launcher()
        driver.switch_to.new_window("tab")
        window_handle = driver.current_window_handle
        try:
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
                window_handle=window_handle,
                deadline=now + QUOTE_TIMEOUT_SECONDS,
                stable_since=now,
            )
        except Exception:
            if window_handle in driver.window_handles and len(driver.window_handles) > 1:
                driver.close()
            self._switch_to_launcher()
            raise

    def resume_quote(self, job_id: str, chat_url: str) -> ActiveQuote:
        """Open an already-submitted conversation without resending source text."""

        driver = self._driver()
        self._switch_to_launcher()
        driver.switch_to.new_window("tab")
        window_handle = driver.current_window_handle
        try:
            driver.get(chat_url)
            WebDriverWait(driver, 90).until(
                lambda _: canonical_url_path(driver.current_url)
                == canonical_url_path(chat_url)
            )
            now = time.monotonic()
            return ActiveQuote(
                job_id=job_id,
                chat_url=chat_url,
                window_handle=window_handle,
                deadline=now + QUOTE_TIMEOUT_SECONDS,
                stable_since=now,
            )
        except Exception:
            if window_handle in driver.window_handles and len(driver.window_handles) > 1:
                driver.close()
            self._switch_to_launcher()
            raise

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
            quote.stable_since = time.monotonic()
            return None
        retry = self._text_control(("重试", "Retry"))
        if retry is not None:
            now = time.monotonic()
            if now >= quote.deadline:
                raise TimeoutError(f"ChatGPT quote did not finish in {QUOTE_TIMEOUT_SECONDS} seconds")
            if quote.retry_visible_since is None:
                quote.retry_visible_since = now
            if not quote.retry_clicked and now - quote.retry_visible_since >= 60:
                if completion_check is not None and completion_check():
                    return None
                self._click(retry, driver)
                quote.retry_clicked = True
                quote.retry_visible_since = now
            return None
        quote.retry_visible_since = None
        messages = self._visible(
            driver.find_elements(By.CSS_SELECTOR, "[data-message-author-role='assistant']")
        )
        if len(messages) < quote.minimum_assistant_messages:
            if time.monotonic() >= quote.deadline:
                raise TimeoutError(
                    f"ChatGPT quote did not finish in {QUOTE_TIMEOUT_SECONDS} seconds"
                )
            return None
        current = messages[-1].text.strip() if messages else ""
        now = time.monotonic()
        if current:
            quote.saw_assistant = True
            if current != quote.last_text:
                quote.last_text = current
                quote.stable_since = now
        stop_buttons = self._visible(
            driver.find_elements(
                By.CSS_SELECTOR,
                "button[data-testid='stop-button'], button[aria-label*='Stop'], button[aria-label*='停止']",
            )
        )
        if quote.saw_assistant and not stop_buttons and now - quote.stable_since >= 8:
            return quote.last_text
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
                    "button[data-testid='stop-button'], button[aria-label*='Stop'], button[aria-label*='停止']",
                )
            )
            if stop_buttons:
                self._click(stop_buttons[0], driver)
            for dialog in self._visible(driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")):
                for control in self._visible(dialog.find_elements(By.CSS_SELECTOR, "button, [role='button']")):
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
        quote.saw_assistant = False
        quote.retry_visible_since = None
        quote.retry_clicked = False

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
                    "button[data-testid='stop-button'], button[aria-label*='Stop'], button[aria-label*='停止']",
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
            {"status": "failed", "error": {"code": "source_missing", "message": "待清洗客户需求已不存在。"}},
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
    store.update_if_not_cancelled(
        job_id,
        {"chat_url": active.chat_url, "project_name": PROJECT_NAME},
        stage="submitted",
        message="已在 AstraQuote 项目中创建独立对话并提交需求清洗",
    )
    store.purge_source(job_id)
    return active


def complete_job(store: GptQuoteRelayStore, job_id: str, response: str) -> str:
    current = store.reconcile_delivery_receipt(job_id)
    if current.get("status") in {"cancelled", "completed"}:
        return str(current["status"])
    status, summary = parse_final_response(response)
    if status == "incomplete":
        return "continue"
    # `displayed_on_page` is the current delivery contract. `delivered` is
    # accepted only for conversations started before the contract changed.
    if status in {"displayed_on_page", "delivered"}:
        store.update_if_not_cancelled(
            job_id,
            {
                "status": "completed",
                "result_status": status,
                "result_summary": summary,
                "error": None,
                "lease_expires_at": None,
            },
            stage="completed",
            message="报价文档和可用链接已完成交付",
        )
        return "completed"
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "result_summary": summary,
            "error": {"code": "gpt_quote_blocked", "message": summary},
            "lease_expires_at": None,
        },
        stage="failed",
        message="ChatGPT 未通过最终报价核对，任务已安全停止",
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
                    active = browser.resume_quote(
                        record["job_id"],
                        str(record["chat_url"]),
                    )
                    active_quotes[active.job_id] = active
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
            while logged_in:
                record = store.claim_next(WORKER_ID, lease_minutes=35)
                if record is None:
                    break
                try:
                    active = submit_job(store, browser, record)
                    if active is not None:
                        active_quotes[active.job_id] = active
                except Exception as exc:
                    try:
                        browser.capture_debug(record["job_id"])
                    except Exception:
                        pass
                    fail_job(store, record["job_id"], exc)

            for job_id in active_quote_poll_order(active_quotes):
                active = active_quotes.get(job_id)
                if active is None:
                    continue
                current = store.reconcile_delivery_receipt(job_id)
                if current.get("status") == "completed":
                    try:
                        browser.close_quote(active)
                    finally:
                        active_quotes.pop(job_id, None)
                    continue
                if current.get("status") == "cancelled":
                    try:
                        browser.cancel_quote(active)
                    finally:
                        active_quotes.pop(job_id, None)
                    continue
                try:
                    response = browser.poll_quote(
                        active,
                        completion_check=lambda job_id=job_id: (
                            store.reconcile_delivery_receipt(job_id).get("status")
                            == "completed"
                        ),
                    )
                    if response is None:
                        continue
                    outcome = complete_job(store, job_id, response)
                    if outcome == "continue":
                        latest = store.get(job_id)
                        attempts = int(latest.get("continuation_attempts") or 0)
                        if attempts >= MAX_CONTINUATION_ATTEMPTS:
                            fail_continuation_limit(store, job_id)
                            browser.close_quote(active)
                            active_quotes.pop(job_id, None)
                            continue
                        continuation_prompt = build_quote_continuation_prompt(
                            relay_job_id=job_id,
                            submission_code=str(latest.get("submission_code") or ""),
                        )
                        browser.continue_quote(active, continuation_prompt)
                        store.update_if_not_cancelled(
                            job_id,
                            {"continuation_attempts": attempts + 1},
                            stage="continuing",
                            message="报价尚未完成，已在原对话从保存阶段自动继续",
                        )
                        continue
                    browser.close_quote(active)
                    active_quotes.pop(job_id, None)
                except Exception as exc:
                    if is_transient_browser_poll_exception(exc):
                        # ChatGPT replaces live DOM nodes while generating. The
                        # next polling round must locate fresh elements; the
                        # quote itself is still running and must remain active.
                        active.stable_since = time.monotonic()
                        continue
                    fail_job(store, job_id, exc)
                    try:
                        browser.close_quote(active)
                    except Exception:
                        pass
                    active_quotes.pop(job_id, None)
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
