from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

from app.core.config import Settings
from app.integrations import calculator_selectors as selectors


@dataclass(slots=True)
class Choice:
    label: str
    selected: bool = False


@dataclass(slots=True)
class Control:
    key: str
    name: str
    role: str
    tag: str
    input_type: str | None = None
    required: bool = False
    disabled: bool = False
    expanded: bool | None = None
    value: str | None = None
    minimum: str | None = None
    maximum: str | None = None
    step: str | None = None
    pattern: str | None = None
    placeholder: str | None = None
    dropdown_position: int | None = None
    choices: list[Choice] = field(default_factory=list)


@dataclass(slots=True)
class State:
    fingerprint: str
    path: list[dict[str, Any]]
    controls: list[Control]
    visible_text: list[str]


class CalculatorCapabilityCrawler:
    """Explore Calculator controls and their conditional child-field states."""

    def __init__(
        self,
        settings: Settings,
        *,
        max_depth: int = 8,
        max_states: int = 5000,
        concurrency: int = 4,
    ):
        self.settings = settings
        self.max_depth = max_depth
        self.max_states = max_states
        self.concurrency = concurrency
        self.states: dict[str, State] = {}
        self.branch_attempts: list[dict[str, Any]] = []
        self.truncated = False

    async def discover_services(self, page: Page) -> list[str]:
        await self._open_add_service(page)
        labels = await page.locator('button[aria-label^="配置"]').evaluate_all(
            r"""els => els.map(el => (el.getAttribute('aria-label') || '')
                .replace(/^配置\s+/, '').trim()).filter(Boolean)"""
        )
        if not labels:
            labels = await page.locator('button[aria-label^="Configure"]').evaluate_all(
                r"""els => els.map(el => (el.getAttribute('aria-label') || '')
                    .replace(/^Configure\s+/, '').trim()).filter(Boolean)"""
            )
        return sorted(set(labels))

    async def crawl(self, page: Page, service_name: str) -> dict[str, Any]:
        self.states = {}
        self.branch_attempts = []
        self.truncated = False
        await self._open_service(page, service_name)
        await self._explore(page, [], 0)
        return {
            "schema_version": 1,
            "service": service_name,
            "source": selectors.ADD_SERVICE_URL,
            "observed_at": datetime.now(UTC).isoformat(),
            "state_count": len(self.states),
            "branch_attempt_count": len(self.branch_attempts),
            "truncated": self.truncated,
            "branch_attempts": self.branch_attempts,
            "states": [asdict(state) for state in self.states.values()],
        }

    async def _scan_path(
        self,
        context: BrowserContext,
        service_name: str,
        path: list[dict[str, Any]],
    ) -> dict[str, Any]:
        last_error = "unknown_error"
        for attempt in range(2):
            page = await context.new_page()
            page.set_default_timeout(int(self.settings.calculator_timeout_seconds * 1000))
            try:
                await self._open_service(page, service_name)
                path_error = await self._apply_path(page, path)
                if path_error is not None:
                    last_error = path_error
                else:
                    await self._expand_all_sections(page)
                    return {
                        "success": True,
                        "controls": await self._capture_controls(page),
                        "visible_text": await self._visible_labels(page),
                    }
            except Exception as exc:  # noqa: BLE001 - every failed branch is reported
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            finally:
                await page.close()
            if attempt == 0:
                await asyncio.sleep(0.5)
        return {"success": False, "error": last_error}

    async def _apply_path(
        self, page: Page, path: list[dict[str, Any]]
    ) -> str | None:
        action_timeout = min(
            max(int(self.settings.calculator_timeout_seconds * 1000), 8000), 15000
        )
        for action in path:
            await self._expand_all_sections(page)
            try:
                if action["kind"] == "dropdown":
                    button = page.locator("__never_matches__")
                    if action.get("key"):
                        button = page.locator(f"#{self._css_escape(action['key'])}")
                    if await button.count() != 1:
                        # Calculator generates new timestamped IDs on every page load.
                        # Accessible names are the stable cross-load identity.
                        button = page.get_by_role(
                            "button",
                            name=re.compile(
                                rf"^{re.escape(action['control'])}(?:\s|$)", re.I
                            ),
                        ).and_(page.locator('button[aria-haspopup="listbox"]')).nth(
                            action["occurrence"]
                        )
                    if await button.count() != 1 and action.get("position") is not None:
                        button = page.locator(
                            'main button[aria-haspopup="listbox"]'
                        ).nth(action["position"])
                    if await button.count() != 1:
                        return (
                            f"control_not_found: {action['control']} "
                            f"key={action.get('key')} count={await button.count()}"
                        )
                    await button.click(timeout=action_timeout)
                    options = page.get_by_role("option")
                    try:
                        await options.first.wait_for(state="visible", timeout=5000)
                    except Exception:  # noqa: BLE001 - detailed error is returned below
                        pass
                    labels = await options.all_text_contents()
                    option_index = next(
                        (
                            index
                            for index, label in enumerate(labels)
                            if self._normalize_option_label(label) == action["choice"]
                        ),
                        None,
                    )
                    if option_index is None:
                        await button.press("Escape")
                        return (
                            f"option_not_found: {action['control']} -> {action['choice']}; "
                            f"visible={list(map(self._normalize_option_label, labels))[:20]}"
                        )
                    await options.nth(option_index).click()
                elif action["kind"] == "radio":
                    await (
                        page.get_by_role("radio", name=action["control"], exact=True)
                        .nth(action["occurrence"])
                        .check(timeout=action_timeout)
                    )
                else:
                    await (
                        page.get_by_role("checkbox", name=action["control"], exact=True)
                        .nth(action["occurrence"])
                        .set_checked(action["choice"] == "true", timeout=action_timeout)
                    )
                await page.wait_for_timeout(700)
            except Exception as exc:  # noqa: BLE001 - failed option path is recorded
                return f"{type(exc).__name__}: {str(exc)[:300]}"
        return None

    @staticmethod
    def _branch_actions(
        controls: list[Control], path: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        occurrences: dict[tuple[str, str], int] = {}
        visited = {(item["kind"], item["control"], item["occurrence"]) for item in path}
        for control in controls:
            if control.tag == "button" and control.choices:
                kind = "dropdown"
            elif control.input_type == "radio":
                kind = "radio"
            elif control.input_type == "checkbox":
                kind = "checkbox"
            else:
                continue
            key = (kind, control.name)
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            if (kind, control.name, occurrence) in visited:
                continue
            if kind == "dropdown":
                actions.extend(
                    {
                        "kind": kind,
                        "control": control.name,
                        "occurrence": occurrence,
                        "position": control.dropdown_position,
                        "choice": choice.label,
                    }
                    for choice in control.choices
                    if not choice.selected
                )
            elif kind == "radio" and not control.choices[0].selected:
                actions.append(
                    {
                        "kind": kind,
                        "control": control.name,
                        "occurrence": occurrence,
                        "choice": "true",
                    }
                )
            elif kind == "checkbox":
                actions.append(
                    {
                        "kind": kind,
                        "control": control.name,
                        "occurrence": occurrence,
                        "choice": str(not control.choices[0].selected).lower(),
                    }
                )
        return actions

    async def _open_add_service(self, page: Page) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await page.goto(
                    selectors.ADD_SERVICE_URL, wait_until="domcontentloaded"
                )
                if response is not None and response.status == 403:
                    body = re.sub(r"\s+", " ", (await page.locator("body").inner_text())).strip()
                    raise RuntimeError(
                        "AWS Calculator returned HTTP 403 (CloudFront request blocked): "
                        f"{body[:240]}"
                    )
                await page.get_by_role(
                    "heading", name=re.compile(r"添加服务|Add service", re.I)
                ).wait_for(timeout=30000)
                await page.wait_for_timeout(500)
                return
            except Exception as exc:  # noqa: BLE001 - official page loads are retried
                last_error = exc
                if "HTTP 403" in str(exc):
                    break
                if attempt < 2:
                    await page.wait_for_timeout(1000 * (attempt + 1))
        assert last_error is not None
        raise last_error

    async def _open_service(self, page: Page, service_name: str) -> None:
        await self._open_add_service(page)
        search = page.get_by_role("searchbox", name=selectors.SERVICE_SEARCH)
        await search.fill(service_name)
        button = page.locator(
            f'button[aria-label="配置 {service_name}"], '
            f'button[aria-label="配置  {service_name}"], '
            f'button[aria-label="Configure {service_name}"]'
        ).first
        await button.click()
        await page.wait_for_url(re.compile(r"#/createCalculator/"))
        await page.get_by_role(
            "heading", name=re.compile(r"Create estimate:\s*Configure", re.I)
        ).wait_for()
        await page.wait_for_timeout(800)

    async def _expand_all_sections(self, page: Page) -> None:
        for _ in range(4):
            # Dropdowns also expose aria-expanded=false. Only expand sections here;
            # opening every listbox at once corrupts option-to-field association.
            collapsed = page.locator(
                'main button[aria-expanded="false"]:not([aria-haspopup="listbox"])'
            )
            count = await collapsed.count()
            if not count:
                return
            changed = False
            for index in range(count):
                item = collapsed.nth(index)
                if await item.is_visible() and await item.is_enabled():
                    try:
                        await item.click(timeout=2000)
                        changed = True
                    except Exception:  # noqa: BLE001 - discovery records best-effort UI state
                        continue
            if not changed:
                return
            await page.wait_for_timeout(250)

    async def _explore(self, page: Page, path: list[dict[str, Any]], depth: int) -> None:
        if len(self.states) >= self.max_states:
            self.truncated = True
            return
        await self._expand_all_sections(page)
        controls = await self._capture_controls(page)
        fingerprint = self._fingerprint(controls)
        if fingerprint in self.states:
            return
        self.states[fingerprint] = State(
            fingerprint=fingerprint,
            path=list(path),
            controls=controls,
            visible_text=await self._visible_labels(page),
        )
        if depth >= self.max_depth:
            self.truncated = True
            return

        await self._explore_dropdowns(page, controls, path, depth)
        await self._explore_radios(page, controls, path, depth)
        await self._explore_checkboxes(page, controls, path, depth)

    async def _capture_controls(self, page: Page) -> list[Control]:
        raw = await page.locator(
            "main input, main button, main select, main textarea"
        ).evaluate_all(
            r"""els => els.filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                    s.visibility !== 'hidden';
            }).map((el, index) => {
                const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
                const labelled = ids
                    .map(id => document.getElementById(id)?.innerText || '').join(' ');
                const labels = el.labels ?
                    [...el.labels].map(x => x.innerText || '').join(' ') : '';
                const name = (el.getAttribute('aria-label') || labelled || labels ||
                    el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '')
                    .replace(/\s+/g, ' ').trim();
                return {
                    key: el.id || `${el.tagName.toLowerCase()}:${index}:${name}`,
                    name,
                    role: el.getAttribute('role') || '',
                    tag: el.tagName.toLowerCase(),
                    input_type: el.getAttribute('type'),
                    required: el.required || el.getAttribute('aria-required') === 'true',
                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
                    expanded: el.hasAttribute('aria-expanded') ?
                        el.getAttribute('aria-expanded') === 'true' : null,
                    value: 'value' in el ? String(el.value) : null,
                    minimum: el.getAttribute('min'),
                    maximum: el.getAttribute('max'),
                    step: el.getAttribute('step'),
                    pattern: el.getAttribute('pattern'),
                    placeholder: el.getAttribute('placeholder'),
                    checked: 'checked' in el ? Boolean(el.checked) : null,
                    has_popup: el.getAttribute('aria-haspopup')
                };
            })"""
        )
        controls: list[Control] = []
        for item in raw:
            choices: list[Choice] = []
            if item["input_type"] in {"radio", "checkbox"}:
                choices = [Choice("checked", bool(item.get("checked")))]
            controls.append(
                Control(
                    key=item["key"],
                    name=item["name"],
                    role=item["role"] or self._inferred_role(item),
                    tag=item["tag"],
                    input_type=item["input_type"],
                    required=item["required"],
                    disabled=item["disabled"],
                    expanded=item["expanded"],
                    value=item["value"],
                    minimum=item["minimum"],
                    maximum=item["maximum"],
                    step=item["step"],
                    pattern=item["pattern"],
                    placeholder=item["placeholder"],
                    choices=choices,
                )
            )

        dropdowns = page.locator('main button[aria-haspopup="listbox"]')
        for index in range(await dropdowns.count()):
            button = dropdowns.nth(index)
            if not await button.is_visible() or not await button.is_enabled():
                continue
            key = await button.get_attribute("id") or f"dropdown:{index}"
            try:
                await button.click(timeout=2000)
                option_rows = await page.get_by_role("option").evaluate_all(
                    "els => els.map(el => ({label: (el.innerText || '').trim(), "
                    "selected: el.getAttribute('aria-selected') === 'true'}))"
                )
                await button.press("Escape")
            except Exception:  # noqa: BLE001 - capability scan continues with diagnostics
                option_rows = []
            for control in controls:
                if control.key == key:
                    control.choices = [
                        Choice(self._normalize_option_label(row["label"]), row["selected"])
                        for row in option_rows
                        if row["label"]
                    ]
                    selected = next(
                        (choice.label for choice in control.choices if choice.selected), None
                    )
                    if selected:
                        control.value = selected
                        control.name = self._strip_selected_value(control.name, selected)
                    control.dropdown_position = index
                    break
        return controls

    async def _explore_dropdowns(
        self,
        page: Page,
        controls: list[Control],
        path: list[dict[str, Any]],
        depth: int,
    ) -> None:
        candidates = [
            control for control in controls if control.choices and control.tag == "button"
        ]
        occurrences: dict[str, int] = {}
        visited = {
            (item.get("kind"), item.get("control"), item.get("occurrence"))
            for item in path
        }
        for control in candidates:
            occurrence = occurrences.get(control.name, 0)
            occurrences[control.name] = occurrence + 1
            if ("dropdown", control.name, occurrence) in visited:
                continue
            original = control.value
            for choice in control.choices:
                if choice.selected:
                    continue
                action = {
                    "kind": "dropdown",
                    "control": control.name,
                    "occurrence": occurrence,
                    "position": control.dropdown_position,
                    "choice": choice.label,
                }
                try:
                    error = await self._apply_path(page, [action])
                    if error is not None:
                        self._record_attempt(
                            {"path": [*path, action], "success": False, "error": error}
                        )
                        continue
                    await self._expand_all_sections(page)
                    branch_controls = await self._capture_controls(page)
                    branch_fingerprint = self._fingerprint(branch_controls)
                    self._record_attempt(
                        {
                            "path": [*path, action],
                            "success": True,
                            "fingerprint": branch_fingerprint,
                        }
                    )
                    await self._explore(page, [*path, action], depth + 1)
                except Exception as exc:  # noqa: BLE001 - failure is retained for audit
                    self._record_attempt(
                        {
                            "path": [*path, action],
                            "success": False,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        }
                    )
                finally:
                    if original:
                        restore = {**action, "choice": original}
                        restore_error = await self._apply_path(page, [restore])
                        if restore_error is not None:
                            raise RuntimeError(
                                f"could_not_restore {control.name} to {original}: "
                                f"{restore_error}"
                            )

    async def _explore_radios(
        self,
        page: Page,
        controls: list[Control],
        path: list[dict[str, Any]],
        depth: int,
    ) -> None:
        radios = [control for control in controls if control.input_type == "radio"]
        occurrences: dict[str, int] = {}
        visited = {
            (item.get("kind"), item.get("control"), item.get("occurrence"))
            for item in path
        }
        for control in radios:
            occurrence = occurrences.get(control.name, 0)
            occurrences[control.name] = occurrence + 1
            if ("radio", control.name, occurrence) in visited:
                continue
            radio = page.locator(f"#{self._css_escape(control.key)}")
            if await radio.count() != 1 or await radio.is_checked():
                continue
            radio_group = await radio.get_attribute("name")
            original = (
                page.locator(f'input[type="radio"][name="{radio_group}"]:checked')
                if radio_group
                else None
            )
            original_id = (
                await original.get_attribute("id")
                if original is not None and await original.count() == 1
                else None
            )
            action = {
                "kind": "radio",
                "control": control.name,
                "occurrence": occurrence,
                "choice": "true",
            }
            try:
                await radio.check(timeout=8000)
                await page.wait_for_timeout(700)
                branch_controls = await self._capture_controls(page)
                self._record_attempt(
                    {
                        "path": [*path, action],
                        "success": True,
                        "fingerprint": self._fingerprint(branch_controls),
                    }
                )
                await self._explore(page, [*path, action], depth + 1)
            except Exception as exc:  # noqa: BLE001
                self._record_attempt(
                    {
                        "path": [*path, action],
                        "success": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
            finally:
                if original_id:
                    await page.locator(f"#{self._css_escape(original_id)}").check(
                        timeout=8000
                    )

    async def _explore_checkboxes(
        self,
        page: Page,
        controls: list[Control],
        path: list[dict[str, Any]],
        depth: int,
    ) -> None:
        boxes = [control for control in controls if control.input_type == "checkbox"]
        occurrences: dict[str, int] = {}
        visited = {
            (item.get("kind"), item.get("control"), item.get("occurrence"))
            for item in path
        }
        for control in boxes:
            occurrence = occurrences.get(control.name, 0)
            occurrences[control.name] = occurrence + 1
            if ("checkbox", control.name, occurrence) in visited:
                continue
            box = page.locator(f"#{self._css_escape(control.key)}")
            if await box.count() != 1:
                continue
            before = await box.is_checked()
            action = {
                "kind": "checkbox",
                "control": control.name,
                "occurrence": occurrence,
                "choice": str(not before).lower(),
            }
            try:
                await box.set_checked(not before, timeout=8000)
                await page.wait_for_timeout(700)
                branch_controls = await self._capture_controls(page)
                self._record_attempt(
                    {
                        "path": [*path, action],
                        "success": True,
                        "fingerprint": self._fingerprint(branch_controls),
                    }
                )
                await self._explore(page, [*path, action], depth + 1)
            except Exception as exc:  # noqa: BLE001
                self._record_attempt(
                    {
                        "path": [*path, action],
                        "success": False,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
            finally:
                await box.set_checked(before, timeout=8000)

    def _record_attempt(self, attempt: dict[str, Any]) -> None:
        self.branch_attempts.append(attempt)
        if len(self.branch_attempts) % 10 == 0:
            failures = sum(not item["success"] for item in self.branch_attempts)
            print(
                f"attempted={len(self.branch_attempts)} states={len(self.states)} "
                f"failures={failures}",
                flush=True,
            )

    async def _restore_dropdown(self, page: Page, button: Locator, original: str) -> None:
        try:
            await button.click(timeout=2000)
            options = page.get_by_role("option")
            labels = await options.all_text_contents()
            match = next(
                (
                    index
                    for index, label in enumerate(labels)
                    if self._normalize_option_label(label) == original
                ),
                None,
            )
            if match is not None:
                await options.nth(match).click()
            else:
                await button.press("Escape")
        except Exception:  # noqa: BLE001
            return

    @staticmethod
    def _fingerprint(controls: list[Control]) -> str:
        payload = [
            {
                "name": control.name,
                "role": control.role,
                "required": control.required,
                "disabled": control.disabled,
                "minimum": control.minimum,
                "maximum": control.maximum,
                "step": control.step,
                "pattern": control.pattern,
                "placeholder": control.placeholder,
                "dropdown_position": control.dropdown_position,
                "choices": [choice.label for choice in control.choices],
            }
            for control in controls
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:16]

    @staticmethod
    async def _visible_labels(page: Page) -> list[str]:
        return await page.locator(
            "main label, main legend, main h1, main h2, main h3, main h4"
        ).evaluate_all(
            r"els => [...new Set(els.map(el => (el.innerText || '').replace(/\s+/g, ' ').trim())"
            ".filter(Boolean))]"
        )

    @staticmethod
    def _normalize_option_label(value: str) -> str:
        label = re.sub(r"\s+", " ", value).strip()
        tokens = label.split()
        if len(tokens) % 2 == 0:
            midpoint = len(tokens) // 2
            if tokens[:midpoint] == tokens[midpoint:]:
                return " ".join(tokens[:midpoint])
        if len(label) % 2 == 0:
            midpoint = len(label) // 2
            if label[:midpoint] == label[midpoint:]:
                return label[:midpoint]
        return label

    @staticmethod
    def _strip_selected_value(name: str, selected: str) -> str:
        normalized = re.sub(r"\s+", " ", name).strip()
        if normalized.endswith(selected):
            normalized = normalized[: -len(selected)].strip()
        return normalized

    @staticmethod
    def _inferred_role(item: dict[str, Any]) -> str:
        if item.get("input_type") in {"radio", "checkbox"}:
            return item["input_type"]
        if item["tag"] == "button" and item.get("has_popup") == "listbox":
            return "combobox"
        return item["tag"]

    @staticmethod
    def _css_escape(value: str) -> str:
        return re.sub(r"([^a-zA-Z0-9_-])", lambda match: f"\\{match.group(1)}", value)


async def run(args: argparse.Namespace) -> None:
    settings = Settings()
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    crawler = CalculatorCapabilityCrawler(
        settings,
        max_depth=args.max_depth,
        max_states=args.max_states,
        concurrency=args.concurrency,
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=settings.calculator_browser_channel,
            headless=not args.show_browser,
        )
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()
        page.set_default_timeout(int(settings.calculator_timeout_seconds * 1000))
        service_inventory = output / "services.json"
        if args.all or not service_inventory.exists():
            services = await crawler.discover_services(page)
            service_inventory.write_text(
                json.dumps(services, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            services = json.loads(service_inventory.read_text(encoding="utf-8"))
        selected = services if args.all else args.service
        for service_name in selected:
            capability = await crawler.crawl(page, service_name)
            slug = re.sub(r"[^a-z0-9]+", "-", service_name.lower()).strip("-")
            (output / f"{slug}.json").write_text(
                json.dumps(capability, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                f"{service_name}: {capability['state_count']} states"
                f"{' (truncated)' if capability['truncated'] else ''}",
                flush=True,
            )
        await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", default="artifacts/calculator-capabilities")
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-states", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--show-browser", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.service:
        parser.error("provide --service or --all")
    return args


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
