import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def gemini_browser(monkeypatch):
    modules = {
        "selenium": {"webdriver": Mock()},
        "selenium.common": {},
        "selenium.common.exceptions": {"WebDriverException": RuntimeError},
        "selenium.webdriver": {},
        "selenium.webdriver.common": {},
        "selenium.webdriver.common.by": {
            "By": SimpleNamespace(CSS_SELECTOR="css", XPATH="xpath")
        },
        "selenium.webdriver.common.keys": {"Keys": Mock()},
        "selenium.webdriver.firefox": {},
        "selenium.webdriver.firefox.options": {"Options": Mock()},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[2] / "tools/gemini_chat_browser.py"
    spec = importlib.util.spec_from_file_location("gemini_browser_behavior", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.GeminiChatBrowser


def test_each_poll_scrolls_the_active_quote_before_scanning_approval(
    gemini_browser,
) -> None:
    browser = gemini_browser(
        active_quote_factory=Mock(),
        quote_timeout_seconds=600,
    )
    events: list[str] = []
    browser._switch_to_quote = lambda _quote: events.append("switch")
    browser._scroll_to_latest = lambda: events.append("scroll")
    browser._approve_tool_if_needed = lambda: events.append("approve") or True
    quote = SimpleNamespace(stable_since=0)

    assert browser.poll_quote(quote) is None
    assert events == ["switch", "scroll", "approve"]
