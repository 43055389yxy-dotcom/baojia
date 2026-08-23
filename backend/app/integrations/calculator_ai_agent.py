from __future__ import annotations

import asyncio
import json
import random
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.config import Settings
from app.integrations.ai_gateway import AiGateway

ProgressReporter = Callable[[str, str], Awaitable[None]]


class BrowserAction(BaseModel):
    """A deliberately small, auditable action language for Calculator automation."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "fill", "check", "uncheck", "press", "wait", "finish", "fail"]
    control_id: str | None = None
    value: str | None = None
    reason: str = Field(min_length=1, max_length=240)
    selected_model: str | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> BrowserAction:
        if self.action in {"click", "fill", "check", "uncheck", "press"} and not self.control_id:
            raise ValueError("interactive actions require control_id")
        if self.action in {"fill", "press"} and self.value is None:
            raise ValueError("fill/press requires value")
        return self


class BrowserDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # AWS Calculator is a React application. Even a simple fill can replace the
    # surrounding controls, so executing a second action from the same DOM
    # snapshot is unsafe. Always observe again after every action.
    actions: list[BrowserAction] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def validate_batch(self) -> BrowserDecision:
        terminal = [item for item in self.actions if item.action in {"finish", "fail", "wait"}]
        if terminal and len(self.actions) != 1:
            raise ValueError("terminal actions must be returned alone")
        return self


@dataclass(frozen=True, slots=True)
class AgentGroupResult:
    selected_model: str
    steps: tuple[str, ...]
    observations: int


SYSTEM_PROMPT = """你是 AWS Pricing Calculator 的网页操作员，只依据本轮真实可见控件完成本组 goal。
未给出的可选项保留网页默认；网页是字段、依赖和价格的唯一真相。

规则：只返回严格 JSON；每轮仅一个动作；control_id 必须来自 controls。
不能编造型号、价格、selector 或数值。
先选服务/区域/系统或引擎/型号，再填客户明确的数量、购买方式、存储、流量等。下拉或折叠后先观察再继续。
客户指定型号：搜索后必须点击真实候选行。未指定型号：按网页真实候选选择满足规格且最低价的一项。
selected_model、completed_choices 和 completed_controls 代表已完成，禁止重复操作。
只有本组完成才 finish；外层负责保存。
禁止点击保存按钮。页面报错且无可继续控件才 fail；短暂加载才 wait。
fill 仅用于 fillable=true 的输入框，数值仅可来自 goal 或明确换算。
客户未给数值时保留默认；仅 goal 中明确的最小有效默认值可填。
忽略 Lambda 目标，除非 goal 明确要求 Lambda。
ALB 有 processed_bytes_ec2_ip_gib_per_hour 时仅填 EC2/IP 处理字节，不再填其他 LCU 维度。
region_confirmed/location_type_confirmed 为 true 时禁止再次操作位置或区域。
EC2 的 system_disk_gib 已包含同类型系统盘+数据盘总容量。

返回结构：
{"actions":[{"action":"click|fill|check|uncheck|press|wait|finish|fail",
"control_id":"c12或null","value":"文本、按键或null",
"reason":"给销售看的简短中文进度","selected_model":"finish时的真实型号，否则null"}]}
"""


class DeepSeekCalculatorAgent:
    """DeepSeek decides; Playwright only executes a constrained single action."""

    _session_lock = asyncio.Lock()

    def __init__(self, settings: Settings):
        self._settings = settings
        self._gateway = AiGateway(settings)

    async def configure_group(
        self,
        page: Page,
        *,
        service: str,
        goal: dict[str, Any],
        group_index: int,
        require_model: bool = True,
        initial_selected_model: str | None = None,
        reporter: ProgressReporter | None = None,
    ) -> AgentGroupResult:
        steps: list[str] = []
        history: list[dict[str, str]] = []
        action_counts: Counter[str] = Counter()
        observation_counts: Counter[str] = Counter()
        confirmed_model: str | None = initial_selected_model
        confirmed_model_evidence: str | None = (
            "confirmed during the previous save attempt" if initial_selected_model else None
        )
        completed_choices: list[str] = []
        completed_fills: list[tuple[str, str]] = []
        suppressed_controls: set[str] = set()
        recovery_action_completed = not bool(goal.get("save_validation_failed"))
        expected_purchase = str(goal.get("purchase_option") or "on_demand")
        purchase_confirmed = expected_purchase in {"on_demand", "spot"}
        expected_engine = str(goal.get("engine") or "")
        sql_engine_confirmed = not expected_engine.startswith("sql_server_")
        target_region = str(goal.get("region") or "").strip().lower()
        region_confirmed = not bool(target_region)
        location_type_confirmed = not bool(target_region)
        async with self._session_lock:
            for step_number in range(1, self._settings.calculator_ai_max_steps + 1):
                self._assert_calculator_domain(page.url)
                observation = await self._observe(page)

                if target_region and not location_type_confirmed:
                    if self._location_type_is_region(observation["controls"]):
                        location_type_confirmed = True
                    else:
                        location_option = self._matching_region_location_type_option(
                            observation["controls"]
                        )
                        if location_option is not None:
                            if location_option.get("selected") == "true":
                                await page.keyboard.press("Escape")
                            else:
                                await self._execute(
                                    page,
                                    BrowserAction(
                                        action="click",
                                        control_id=str(location_option["id"]),
                                        reason="固定位置类型为区域",
                                    ),
                                    observation["controls"],
                                )
                            location_type_confirmed = True
                            await self._human_pause(navigation=True)
                            continue
                        location_trigger = self._location_type_trigger(observation["controls"])
                        if location_trigger is not None:
                            await self._execute(
                                page,
                                BrowserAction(
                                    action="click",
                                    control_id=str(location_trigger["id"]),
                                    reason="打开位置类型并固定为区域",
                                ),
                                observation["controls"],
                            )
                            await self._human_pause()
                            continue

                if location_type_confirmed:
                    observation["controls"] = [
                        control
                        for control in observation["controls"]
                        if not self._is_location_type_control(control)
                    ]

                # Region selection is uniform across Calculator services and the
                # option label contains the stable AWS region code. Handle this
                # deterministically instead of asking the model to reason about
                # a large, sometimes virtualized listbox.
                if not region_confirmed:
                    region_option = self._matching_region_option(
                        observation["controls"], target_region
                    )
                    if region_option is not None:
                        if region_option.get("selected") == "true":
                            await page.keyboard.press("Escape")
                        else:
                            await self._execute(
                                page,
                                BrowserAction(
                                    action="click",
                                    control_id=str(region_option["id"]),
                                    reason=f"选择客户指定区域 {target_region}",
                                ),
                                observation["controls"],
                            )
                        region_confirmed = True
                        completed_choices.append(f"region {target_region}")
                        history.append(
                            {
                                "action": "click",
                                "control_id": str(region_option["id"]),
                                "value": target_region,
                                "result": "executed deterministic region selection",
                            }
                        )
                        reason = f"选择客户指定区域 {target_region}"
                        steps.append(reason)
                        if reporter:
                            await reporter("browser", reason)
                        await self._human_pause(navigation=True)
                        continue

                if region_confirmed:
                    observation["controls"] = [
                        control
                        for control in observation["controls"]
                        if not self._is_region_control(control)
                    ]
                # This page combines serverless, regular-cluster and
                # data-tiering products. A Redis primary/replica request uses
                # the regular-cluster controls unless tiering was explicit.
                observation["controls"] = self._scope_service_controls(
                    observation["controls"], service, goal
                )
                calculator_adjustment = self._adopt_visible_storage_floor(
                    service, observation["controls"], goal
                )
                if calculator_adjustment is not None:
                    control, notice = calculator_adjustment
                    suppressed_controls.add(self._control_identity(control))
                    completed_fills.append(
                        (
                            self._control_identity(control),
                            str(control.get("value") or ""),
                        )
                    )
                    steps.append(notice)
                    history.append(
                        {
                            "action": "keep",
                            "control_id": str(control.get("id") or ""),
                            "value": str(control.get("value") or ""),
                            "result": "kept Calculator-enforced storage baseline",
                        }
                    )
                    if reporter:
                        await reporter("browser", notice)
                    continue
                is_elasticache = self._is_elasticache_service(service)
                if (require_model or is_elasticache) and confirmed_model is None:
                    page_selected_model = self._selected_model_from_controls(
                        observation["controls"], goal, observation["focused_text"]
                    )
                    if page_selected_model is not None:
                        confirmed_model = page_selected_model
                        confirmed_model_evidence = "Calculator 当前已选实例"
                        completed_choices.append(f"selected model {confirmed_model}")
                        if is_elasticache:
                            for control in observation["controls"]:
                                if (
                                    str(control.get("role") or "").lower() == "combobox"
                                    and confirmed_model.lower() in self._control_identity(control)
                                ):
                                    suppressed_controls.add(self._control_identity(control))
                if suppressed_controls:
                    observation["controls"] = [
                        control
                        for control in observation["controls"]
                        if self._control_identity(control) not in suppressed_controls
                    ]
                observation["candidate_hints"] = self._candidate_hints(
                    observation["controls"], goal
                )
                if is_elasticache and confirmed_model is None and observation["candidate_hints"]:
                    candidate = observation["candidate_hints"][0]
                    candidate_control = next(
                        (
                            item
                            for item in observation["controls"]
                            if item.get("id") == candidate.get("control_id")
                        ),
                        None,
                    )
                    if candidate_control is not None:
                        selected = str(candidate["model"])
                        reason = f"从 Calculator 真实候选中选择不低于需求且规格最接近的 {selected}"
                        await self._execute(
                            page,
                            BrowserAction(
                                action="click",
                                control_id=str(candidate["control_id"]),
                                reason=reason,
                            ),
                            observation["controls"],
                        )
                        confirmed_model = selected
                        confirmed_model_evidence = self._control_identity(candidate_control)
                        completed_choices.append(f"selected model {selected}")
                        steps.append(reason)
                        history.append(
                            {
                                "action": "click",
                                "control_id": str(candidate["control_id"]),
                                "value": selected,
                                "result": "selected deterministic Calculator candidate",
                            }
                        )
                        if reporter:
                            await reporter("browser", reason)
                        await self._human_pause(navigation=True)
                        continue
                if is_elasticache and confirmed_model is not None:
                    node_control = self._elasticache_node_control(observation["controls"])
                    expected_nodes = self._number(goal.get("quantity"))
                    actual_nodes = (
                        self._number(node_control.get("value"))
                        if node_control is not None
                        else None
                    )
                    if (
                        node_control is not None
                        and expected_nodes is not None
                        and actual_nodes != expected_nodes
                    ):
                        value = f"{expected_nodes:g}"
                        reason = f"填写 Redis 标准集群节点总数 {value}"
                        await self._execute(
                            page,
                            BrowserAction(
                                action="fill",
                                control_id=str(node_control["id"]),
                                value=value,
                                reason=reason,
                            ),
                            observation["controls"],
                        )
                        completed_fills.append((self._control_identity(node_control), value))
                        steps.append(reason)
                        history.append(
                            {
                                "action": "fill",
                                "control_id": str(node_control["id"]),
                                "value": value,
                                "result": "filled deterministic cluster node total",
                            }
                        )
                        if reporter:
                            await reporter("browser", reason)
                        await self._human_pause()
                        continue
                    if self._elasticache_group_is_complete(
                        observation["controls"], goal, actual_nodes
                    ):
                        reason = (
                            f"Redis 标准集群已完成：{expected_nodes:g} 个节点，"
                            f"型号 {confirmed_model}"
                        )
                        steps.append(reason)
                        if reporter:
                            await reporter("calculator", reason)
                        return AgentGroupResult(confirmed_model, tuple(steps), step_number)
                state_signature = self._observation_signature(observation)
                observation_counts[state_signature] += 1
                if (
                    observation_counts[state_signature]
                    > self._settings.calculator_ai_repeated_state_limit
                ):
                    raise ValueError(
                        "Calculator page returned to the same scoped control state "
                        "repeatedly; stopped before an AI action loop"
                    )
                observation["agent_state"] = {
                    "location_type_confirmed": location_type_confirmed,
                    "region_confirmed": region_confirmed,
                    "selected_model": confirmed_model,
                    "selected_model_page_evidence": confirmed_model_evidence,
                    "purchase_option_confirmed": purchase_confirmed,
                    "sql_edition_confirmed": sql_engine_confirmed,
                    "completed_choices": completed_choices[-20:],
                    "completed_controls": sorted(suppressed_controls)[-20:],
                }
                decision = await self._decide(
                    service=service,
                    goal=goal,
                    group_index=group_index,
                    step_number=step_number,
                    observation=observation,
                    history=history[-8:],
                )
                for action in decision.actions:
                    action_control = next(
                        (
                            item
                            for item in observation["controls"]
                            if item.get("id") == action.control_id
                        ),
                        {},
                    )
                    action_text = self._action_control_text(action, observation["controls"])
                    if self._control_is_out_of_scope(action_control, goal):
                        suppressed_controls.add(self._control_identity(action_control))
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: optional target type is absent from customer goal"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已忽略客户未要求的可选目标类型",
                            )
                        break
                    if action.action == "fill" and not self._numeric_fill_is_grounded(
                        action, action_control, goal
                    ):
                        suppressed_controls.add(self._control_identity(action_control))
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: numeric value is absent from the customer "
                                    "requirements and is not a neutral default"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已阻止填写客户未提供的数值，保留 Calculator 默认值",
                            )
                        break
                    if action.action == "fill" and not self._is_fillable_control(action_control):
                        suppressed_controls.add(self._control_identity(action_control))
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: this is a display container, not a real "
                                    "fillable input; choose a control with fillable=true"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已跳过不可填写的外层容器，正在定位真实输入框",
                            )
                        break
                    if (
                        action.action == "fill"
                        and self._has_visible_options(observation["controls"])
                        and not self._is_search_control(action_control)
                    ):
                        unit_option = self._matching_unit_option(
                            action, observation["controls"], goal
                        )
                        if unit_option is not None:
                            unit_action = BrowserAction(
                                action="click",
                                control_id=str(unit_option["id"]),
                                reason=("根据客户数值的单位换算选择 Calculator 当前可见单位"),
                            )
                            try:
                                await self._execute(page, unit_action, observation["controls"])
                            except ValueError:
                                pass
                            else:
                                completed_choices.append(
                                    self._action_control_text(unit_action, observation["controls"])[
                                        :300
                                    ]
                                )
                                history.append(
                                    {
                                        "action": "click",
                                        "control_id": str(unit_option["id"]),
                                        "value": str(unit_option.get("text") or ""),
                                        "result": "executed derived unit selection",
                                    }
                                )
                                if reporter:
                                    await reporter(
                                        "browser",
                                        "已按客户数值换算选择当前网页中的计量单位",
                                    )
                                await self._human_pause(navigation=True)
                                break
                        # The open panel is unrelated to the requested numeric
                        # field (for example a destination dropdown reopened
                        # after Internet was already selected). Close it and
                        # re-observe instead of repeating the same blocked fill.
                        await page.keyboard.press("Escape")
                        history.append(
                            {
                                "action": "press",
                                "control_id": "active-dropdown",
                                "value": "Escape",
                                "result": "closed unrelated open option panel",
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已关闭无关的展开选项，继续填写当前字段",
                            )
                        await self._human_pause()
                        break
                    if (
                        action.action in {"click", "check"}
                        and self._is_choice_control(action_control)
                        and any(
                            self._same_choice(action_text, completed)
                            for completed in completed_choices
                        )
                    ):
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": "blocked: this page option was already selected",
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "该选项已经确认，正在继续填写下一项",
                            )
                        break
                    if expected_engine.startswith("sql_server_") and action.action == "click":
                        if action_control.get("role") == "option" and self._is_wrong_sql_edition(
                            action_text, expected_engine
                        ):
                            corrected = self._find_sql_edition_option(
                                observation["controls"], expected_engine
                            )
                            if corrected is None:
                                if await self._click_sql_edition_fallback(page, expected_engine):
                                    sql_engine_confirmed = True
                                    reason = (
                                        "已从当前网页真实选项选择 SQL Server "
                                        f"{self._sql_edition_name(expected_engine)}"
                                    )
                                    steps.append(reason)
                                    history.append(
                                        {
                                            "action": "click",
                                            "control_id": "role=option",
                                            "value": self._sql_edition_name(expected_engine),
                                            "result": "executed semantic fallback",
                                        }
                                    )
                                    if reporter:
                                        await reporter("browser", reason)
                                    await self._human_pause(navigation=True)
                                    break
                                history.append(
                                    {
                                        "action": action.action,
                                        "control_id": action.control_id or "",
                                        "value": "",
                                        "result": (
                                            f"blocked: customer requires {expected_engine}; "
                                            "choose the matching visible SQL Server option"
                                        ),
                                    }
                                )
                                if reporter:
                                    await reporter(
                                        "browser",
                                        "已阻止错误的 SQL Server 版本选择",
                                    )
                                break
                            action = BrowserAction(
                                action="click",
                                control_id=str(corrected["id"]),
                                reason=(
                                    "按客户要求纠正并选择 SQL Server "
                                    f"{self._sql_edition_name(expected_engine)}"
                                ),
                            )
                            action_control = corrected
                            action_text = self._action_control_text(action, observation["controls"])
                        if sql_engine_confirmed and self._is_sql_edition_control(action_text):
                            history.append(
                                {
                                    "action": action.action,
                                    "control_id": action.control_id or "",
                                    "value": "",
                                    "result": (
                                        "blocked: requested SQL Server edition is already "
                                        "confirmed; continue with pricing and storage"
                                    ),
                                }
                            )
                            if reporter:
                                await reporter(
                                    "browser",
                                    "SQL Server 版本已确认，继续填写定价和存储",
                                )
                            break
                    if action.action == "click" and action_control.get("expanded") == "true":
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: this dropdown is already expanded; "
                                    "choose one of the currently visible option controls"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "下拉框已展开，正在从当前可见选项中选择",
                            )
                        break
                    candidate_model = self._model_from_action(action, observation["controls"])
                    candidate_violation = self._candidate_goal_violation(
                        action_control, goal, candidate_model
                    )
                    if candidate_violation:
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": candidate_model or "",
                                "result": f"blocked: {candidate_violation}",
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已阻止不满足客户规格的候选，正在选择不低配型号",
                            )
                        break
                    if (
                        candidate_model
                        and confirmed_model
                        and candidate_model.lower() == confirmed_model.lower()
                    ):
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": candidate_model,
                                "result": "blocked: this exact instance model is already selected",
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                f"{candidate_model} 已选中，继续填写下一项",
                            )
                        break
                    if (
                        require_model
                        and confirmed_model is None
                        and self._is_downstream_pricing_control(action_control)
                    ):
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: select a real instance/node model before "
                                    "editing downstream pricing fields"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "实例型号尚未选中，先从 Calculator 候选中选择型号",
                            )
                        break
                    control_identity = self._action_control_text(action, observation["controls"])[
                        :180
                    ]
                    signature = f"{action.action}:{control_identity}:{action.value}"
                    action_counts[signature] += 1
                    if (
                        action_counts[signature]
                        > self._settings.calculator_ai_repeated_action_limit
                    ):
                        suppressed_controls.add(self._control_identity(action_control))
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: repeated action; this control is now marked "
                                    "complete and unavailable; continue or finish"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已阻止重复点击，正在重新判断当前可见选项",
                            )
                        break
                    if action.action == "fail":
                        raise ValueError(action.reason)
                    if action.action == "finish":
                        if not recovery_action_completed:
                            history.append(
                                {
                                    "action": "finish",
                                    "control_id": "",
                                    "value": "",
                                    "result": (
                                        "blocked: the previous save failed; resolve at least "
                                        "one visible validation field before finishing"
                                    ),
                                }
                            )
                            if reporter:
                                await reporter(
                                    "browser",
                                    "上次保存未成功，正在补齐页面要求的字段",
                                )
                            break
                        missing_ebs = self._missing_additional_ebs_volumes(goal, completed_fills)
                        if missing_ebs:
                            action_counts[signature] -= 1
                            missing_text = ", ".join(f"{size:g} GiB" for size in missing_ebs)
                            history.append(
                                {
                                    "action": "finish",
                                    "control_id": "",
                                    "value": "",
                                    "result": (
                                        "blocked: explicit additional EBS volume has not "
                                        f"been filled on the page: {missing_text}"
                                    ),
                                }
                            )
                            if reporter:
                                await reporter(
                                    "browser",
                                    f"额外数据盘 {missing_text} 尚未实际填写，继续补齐",
                                )
                            break
                        selected = str(
                            action.selected_model
                            or confirmed_model
                            or goal.get("requested_model")
                            or service
                        )
                        if require_model and (
                            confirmed_model is None or selected.lower() != confirmed_model.lower()
                        ):
                            action_counts[signature] -= 1
                            history.append(
                                {
                                    "action": "finish",
                                    "control_id": "",
                                    "value": selected,
                                    "result": (
                                        "blocked: typing a model in search is not selection; "
                                        "click the exact visible model row/radio/option first"
                                    ),
                                }
                            )
                            if reporter:
                                await reporter(
                                    "browser",
                                    "型号尚未真正选中，正在点击 Calculator 的真实候选行",
                                )
                            break
                        full_page_text = await page.locator("body").inner_text()
                        if require_model and selected.lower() not in full_page_text.lower():
                            raise ValueError(
                                f"AI reported model {selected}, but it is not visible "
                                "on the Calculator page"
                            )
                        steps.append(action.reason)
                        if reporter:
                            await reporter("calculator", action.reason)
                        return AgentGroupResult(selected, tuple(steps), step_number)

                    if action.action == "click" and self._is_outer_save_control(
                        action, observation["controls"]
                    ):
                        missing_ebs = self._missing_additional_ebs_volumes(goal, completed_fills)
                        if missing_ebs:
                            action_counts[signature] -= 1
                            missing_text = ", ".join(f"{size:g} GiB" for size in missing_ebs)
                            history.append(
                                {
                                    "action": "click",
                                    "control_id": action.control_id or "",
                                    "value": "",
                                    "result": (
                                        "blocked: explicit additional EBS volume has not "
                                        f"been filled on the page: {missing_text}"
                                    ),
                                }
                            )
                            if reporter:
                                await reporter(
                                    "browser",
                                    f"保存前发现额外数据盘 {missing_text} 未填写，正在补齐",
                                )
                            break
                        selected = confirmed_model
                        if selected is None and require_model:
                            action_counts[signature] -= 1
                            history.append(
                                {
                                    "action": "click",
                                    "control_id": action.control_id or "",
                                    "value": "",
                                    "result": (
                                        "blocked: click the exact visible instance model "
                                        "row/radio/option before saving"
                                    ),
                                }
                            )
                            if reporter:
                                await reporter(
                                    "browser",
                                    "保存前发现型号未选中，正在补选真实候选行",
                                )
                            break
                        reason = "本组字段已填写完成，交由系统统一保存"
                        steps.append(reason)
                        if reporter:
                            await reporter("calculator", reason)
                        return AgentGroupResult(selected or service, tuple(steps), step_number)

                    if (
                        action.action == "click"
                        and expected_purchase
                        in {
                            "standard_reserved",
                            "convertible_reserved",
                            "compute_savings_plan",
                            "ec2_instance_savings_plan",
                        }
                        and self._is_commitment_detail_control(action, observation["controls"])
                        and not purchase_confirmed
                    ):
                        action_counts[signature] -= 1
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "blocked: first select the exact purchase_option "
                                    f"{expected_purchase}"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "已阻止错误的期限/付款操作，先选择客户要求的购买方式",
                            )
                        break

                    try:
                        await self._execute(page, action, observation["controls"])
                    except ValueError as exc:
                        history.append(
                            {
                                "action": action.action,
                                "control_id": action.control_id or "",
                                "value": action.value or "",
                                "result": (
                                    "page control changed while executing; observe the "
                                    f"live page again: {str(exc)[:180]}"
                                ),
                            }
                        )
                        if reporter:
                            await reporter(
                                "browser",
                                "网页控件刚刚发生变化，已重新读取当前页面",
                            )
                        break
                    recovery_action_completed = True
                    if action.action == "fill":
                        completed_fills.append((action_text[:500], action.value or ""))
                    if action.action in {"click", "check"} and self._is_choice_control(
                        action_control
                    ):
                        completed_choices.append(action_text[:300])
                    if self._matches_sql_edition(action_text, expected_engine):
                        sql_engine_confirmed = True
                    if self._matches_purchase_option(
                        action, observation["controls"], expected_purchase
                    ):
                        purchase_confirmed = True
                    if candidate_model:
                        confirmed_model = candidate_model
                        confirmed_model_evidence = action_text[:1200]
                    steps.append(action.reason)
                    history.append(
                        {
                            "action": action.action,
                            "control_id": action.control_id or "",
                            "value": action.value or "",
                            "result": "executed",
                        }
                    )
                    if reporter:
                        await reporter("browser", action.reason)
                    await self._human_pause(navigation=action.action == "click")

        raise ValueError(
            f"AI did not complete Calculator group {group_index} within "
            f"{self._settings.calculator_ai_max_steps} actions"
        )

    async def _decide(
        self,
        *,
        service: str,
        goal: dict[str, Any],
        group_index: int,
        step_number: int,
        observation: dict[str, Any],
        history: list[dict[str, str]],
    ) -> BrowserDecision:
        try:
            payload = await self._gateway.complete_json(
                system_prompt=SYSTEM_PROMPT,
                user_content=json.dumps(
                    {
                        "service": service,
                        "group_index": group_index,
                        "step_number": step_number,
                        "goal": goal,
                        "history": history,
                        "observation": observation,
                    },
                    ensure_ascii=False,
                ),
                expected_keys=("actions",),
            )
            return self._safe_parse_decision(payload)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"AI browser decision failed after retries: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _safe_parse_decision(payload: object) -> BrowserDecision:
        """Keep only the safe prefix when the model over-batches dependent clicks."""

        if not isinstance(payload, dict):
            raise ValueError("AI browser response is not an object")
        raw_actions = payload.get("actions")
        if raw_actions is None and "action" in payload:
            raw_actions = [payload]
        if not isinstance(raw_actions, list) or not raw_actions:
            raise ValueError("AI browser response has no actions")

        # Only the first action is safe against React re-renders. The remaining
        # suggestions, if any, are intentionally discarded and reconsidered
        # against the next live page observation.
        action = BrowserAction.model_validate(raw_actions[0])
        return BrowserDecision(actions=[action])

    async def _observe(self, page: Page) -> dict[str, Any]:
        snapshot_chars = self._settings.calculator_ai_snapshot_chars
        controls = await page.evaluate(
            r"""() => {
              const selectors = [
                'button', 'input', 'textarea', 'select',
                '[role="button"]', '[role="option"]', '[role="radio"]',
                '[role="checkbox"]', '[role="combobox"]', '[role="searchbox"]',
                '[role="spinbutton"]', '[role="textbox"]', '[role="row"]'
              ].join(',');
              const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' &&
                  rect.width > 0 && rect.height > 0 && !el.closest('[aria-hidden="true"]');
              };
              const nameFor = (el) => {
                const labelled = el.getAttribute('aria-labelledby');
                const labelledText = labelled ? labelled.split(/\s+/).map(id =>
                  document.getElementById(id)?.innerText || '').join(' ').trim() : '';
                const ownLabel = el.id
                  ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText
                  : '';
                return (el.getAttribute('aria-label') || labelledText || ownLabel ||
                  el.getAttribute('placeholder') || el.getAttribute('name') || '').trim();
              };
              const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5')]
                .filter(visible);
              const sectionFor = (el) => {
                const top = el.getBoundingClientRect().top;
                const prior = headings.filter(h => h.getBoundingClientRect().top <= top + 2).pop();
                return (prior?.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 80);
              };
              const seen = new Set();
              const candidates = [...document.querySelectorAll(selectors)].filter(el => {
                if (!visible(el) || seen.has(el)) return false;
                seen.add(el); return true;
              });
              const score = (el) => {
                const rect = el.getBoundingClientRect();
                const inViewport = rect.top < innerHeight && rect.bottom > 0;
                const role = el.getAttribute('role') || '';
                return (el.closest('[role="dialog"],[aria-modal="true"]') ? 100 : 0) +
                  (role === 'option' || role === 'radio' ? 60 : 0) +
                  (inViewport ? 30 : 0) +
                  (el.closest('[aria-expanded="true"]') ? 20 : 0);
              };
              return candidates.sort((a, b) => score(b) - score(a)).slice(0, 120)
                .map((el, index) => {
                const id = `c${index + 1}`;
                el.setAttribute('data-astra-agent-id', id);
                return {
                  id,
                  tag: el.tagName.toLowerCase(),
                  type: el.getAttribute('type') || '',
                  role: el.getAttribute('role') || '',
                  name: nameFor(el).slice(0, 100),
                  text: (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 140),
                  value: String(el.value || '').slice(0, 80),
                  context: (el.closest('tr,[role="row"]')?.innerText || '')
                    .trim().replace(/\s+/g, ' ').slice(0, 160),
                  section: sectionFor(el),
                  checked: 'checked' in el ? Boolean(el.checked) : null,
                  selected: el.getAttribute('aria-selected'),
                  expanded: el.getAttribute('aria-expanded'),
                  disabled: Boolean(el.disabled) || el.getAttribute('aria-disabled') === 'true',
                  fillable: ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) ||
                    el.isContentEditable
                };
              });
            }"""
        )
        focused_text = await page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' &&
                  rect.width > 0 && rect.height > 0;
              };
              const focused = [...document.querySelectorAll(
                '[role="dialog"],[aria-modal="true"],[aria-expanded="true"]'
              )].filter(visible).map(el => el.innerText || '').filter(Boolean);
              const viewport = [...document.querySelectorAll('main,section,form')]
                .filter(el => {
                  if (!visible(el)) return false;
                  const rect = el.getBoundingClientRect();
                  return rect.top < innerHeight && rect.bottom > 0;
                }).slice(-4).map(el => el.innerText || '');
              return [...focused, ...viewport].join('\n').replace(/\n{3,}/g, '\n\n');
            }"""
        )
        return {
            "url": page.url,
            "title": await page.title(),
            "controls": controls,
            "focused_text": focused_text[:snapshot_chars],
        }

    async def _execute(
        self,
        page: Page,
        action: BrowserAction,
        controls: list[dict[str, Any]],
    ) -> None:
        ids = {str(item["id"]): item for item in controls if item.get("id")}
        if action.action == "wait":
            await asyncio.sleep(2.0)
            return
        assert action.control_id is not None
        if action.control_id not in ids:
            raise ValueError(
                f"AI selected a control not present in current observation: {action.control_id}"
            )
        if ids[action.control_id].get("disabled"):
            raise ValueError(f"AI selected disabled control {action.control_id}")
        locator = page.locator(f'[data-astra-agent-id="{action.control_id}"]').first
        try:
            if action.action == "click":
                try:
                    await locator.click(timeout=10_000)
                except PlaywrightError:
                    await locator.scroll_into_view_if_needed(timeout=5_000)
                    await self._human_pause()
                    await locator.click(timeout=10_000, force=True)
            elif action.action == "fill":
                await locator.fill(action.value or "", timeout=10_000)
            elif action.action == "check":
                await locator.check(timeout=10_000)
            elif action.action == "uncheck":
                await locator.uncheck(timeout=10_000)
            elif action.action == "press":
                allowed_keys = {"Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space"}
                if action.value not in allowed_keys:
                    raise ValueError(f"key is not allowed: {action.value}")
                await locator.press(action.value, timeout=10_000)
            else:
                raise ValueError(f"unsupported browser action: {action.action}")
        except PlaywrightError as exc:
            control = ids[action.control_id]
            label = " ".join(
                str(control.get(field) or "") for field in ("name", "text", "context")
            ).strip()
            raise ValueError(
                f"Calculator control {action.control_id} ({label[:180]}) could not "
                f"execute {action.action}: {str(exc)[:240]}"
            ) from exc

    async def _human_pause(self, *, navigation: bool = False) -> None:
        if navigation:
            lower = self._settings.calculator_navigation_delay_min_seconds
            upper = self._settings.calculator_navigation_delay_max_seconds
        else:
            lower = self._settings.calculator_action_delay_min_seconds
            upper = self._settings.calculator_action_delay_max_seconds
        await asyncio.sleep(random.uniform(max(0.0, lower), max(lower, upper)))  # noqa: S311

    @staticmethod
    def _assert_calculator_domain(url: str) -> None:
        hostname = (urlparse(url).hostname or "").lower()
        if hostname not in {"calculator.aws", "www.calculator.aws"}:
            raise ValueError(f"browser left the allowed Calculator domain: {hostname or 'unknown'}")

    @staticmethod
    def _is_outer_save_control(action: BrowserAction, controls: list[dict[str, Any]]) -> bool:
        control = next((item for item in controls if item.get("id") == action.control_id), None)
        if control is None:
            return False
        label = f"{control.get('name', '')} {control.get('text', '')}".lower()
        return any(
            marker in label
            for marker in (
                "save and view summary",
                "save and add service",
                "保存并查看摘要",
                "保存并添加服务",
            )
        )

    @staticmethod
    def _is_downstream_pricing_control(control: dict[str, Any]) -> bool:
        label = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        return any(
            marker in label
            for marker in (
                "storage",
                "存储",
                "backup",
                "备份",
                "data transfer",
                "数据传输",
                "purchase option",
                "购买选项",
                "reserved instance",
                "预留实例",
                "iops",
                "throughput",
                "吞吐",
            )
        )

    @staticmethod
    def _is_fillable_control(control: dict[str, Any]) -> bool:
        return control.get("fillable") is True

    @staticmethod
    def _has_visible_options(controls: list[dict[str, Any]]) -> bool:
        return any(control.get("role") == "option" for control in controls)

    @staticmethod
    def _matching_region_option(
        controls: list[dict[str, Any]], target_region: str
    ) -> dict[str, Any] | None:
        if not target_region:
            return None
        region_token = re.compile(rf"(?<![a-z0-9-]){re.escape(target_region.lower())}(?![a-z0-9-])")
        for control in controls:
            if control.get("role") != "option":
                continue
            label = " ".join(
                str(control.get(field) or "") for field in ("name", "text", "context", "value")
            ).lower()
            if region_token.search(label):
                return control
        return None

    @staticmethod
    def _location_type_trigger(
        controls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for control in controls:
            label = " ".join(
                str(control.get(field) or "") for field in ("name", "text", "context")
            ).lower()
            if control.get("role") == "button" and any(
                marker in label for marker in ("选择位置类型", "location type")
            ):
                return control
        return None

    @classmethod
    def _location_type_is_region(cls, controls: list[dict[str, Any]]) -> bool:
        trigger = cls._location_type_trigger(controls)
        if trigger is None or trigger.get("expanded") == "true":
            return False
        label = " ".join(
            str(trigger.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        return (
            ("区域" in label or re.search(r"\bregion\b", label) is not None)
            and "本地区域" not in label
            and "local zone" not in label
            and "wavelength" not in label
        )

    @staticmethod
    def _matching_region_location_type_option(
        controls: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for control in controls:
            if control.get("role") != "option":
                continue
            label = (
                " ".join(str(control.get(field) or "") for field in ("name", "text"))
                .strip()
                .lower()
            )
            if label in {"区域", "region", "区域 区域", "region region"}:
                return control
        return None

    @staticmethod
    def _is_location_type_control(control: dict[str, Any]) -> bool:
        label = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        if any(marker in label for marker in ("选择位置类型", "location type")):
            return True
        if control.get("role") == "option":
            compact = " ".join(label.split())
            return compact in {
                "区域",
                "region",
                "区域 区域",
                "region region",
                "本地区域",
                "local zone",
                "wavelength zone",
            }
        return False

    @staticmethod
    def _missing_additional_ebs_volumes(
        goal: dict[str, Any], completed_fills: list[tuple[str, str]]
    ) -> list[float]:
        requirements = goal.get("requirements")
        if not isinstance(requirements, dict):
            return []
        volumes = requirements.get("additional_ebs_volumes")
        if not isinstance(volumes, list):
            return []
        storage_fills: list[float] = []
        for label, value in completed_fills:
            if not any(
                marker in label.lower()
                for marker in ("ebs", "storage", "volume", "存储", "卷", "磁盘")
            ):
                continue
            try:
                storage_fills.append(float(value.replace(",", "")))
            except ValueError:
                continue
        missing: list[float] = []
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            size = volume.get("size_gib")
            if not isinstance(size, (int, float)) or isinstance(size, bool):
                continue
            numeric_size = float(size)
            if not any(abs(filled - numeric_size) < 0.001 for filled in storage_fills):
                missing.append(numeric_size)
        return missing

    @staticmethod
    def _is_region_control(control: dict[str, Any]) -> bool:
        label = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        if control.get("role") == "option" and re.search(
            r"\b(?:af|ap|ca|eu|il|me|mx|sa|us)(?:-[a-z]+){1,2}-\d\b",
            label,
        ):
            return True
        return any(
            marker in label
            for marker in (
                "选择一个区域",
                "choose a region",
                "select a region",
            )
        )

    @staticmethod
    def _is_search_control(control: dict[str, Any]) -> bool:
        label = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        return control.get("role") == "searchbox" or any(
            marker in label for marker in ("search", "搜索", "筛选")
        )

    @classmethod
    def _matching_unit_option(
        cls,
        action: BrowserAction,
        controls: list[dict[str, Any]],
        goal: dict[str, Any],
    ) -> dict[str, Any] | None:
        raw_value = (action.value or "").strip().replace(",", "")
        if not re.fullmatch(r"\d+(?:\.\d+)?", raw_value):
            return None
        value = float(raw_value)
        customer_numbers = cls._collect_numbers(goal.get("requirements"))
        wants_tb = any(abs(number / 1024 - value) < 0.001 for number in customer_numbers)
        unit_pattern = re.compile(r"\bTB\b|TB/月|TB per month", re.I) if wants_tb else None
        if unit_pattern is None:
            return None
        for control in controls:
            if control.get("role") != "option":
                continue
            label = " ".join(str(control.get(field) or "") for field in ("name", "text", "context"))
            if unit_pattern.search(label):
                return control
        return None

    @classmethod
    def _numeric_fill_is_grounded(
        cls,
        action: BrowserAction,
        control: dict[str, Any],
        goal: dict[str, Any],
    ) -> bool:
        raw_value = (action.value or "").strip().replace(",", "")
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw_value):
            return True
        value = float(raw_value)
        label = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        quantity = goal.get("quantity")
        if (
            quantity is not None
            and value == float(quantity)
            and any(
                marker in label
                for marker in (
                    "quantity",
                    "count",
                    "number of",
                    "instances",
                    "nodes",
                    "数量",
                    "实例数",
                    "节点",
                    "负载均衡器",
                )
            )
        ):
            return True

        requirements = goal.get("requirements")
        customer_numbers = cls._collect_numbers(requirements)
        for number in customer_numbers:
            converted = {number, number / 1024, number * 1024}
            if any(abs(value - item) < 0.001 for item in converted):
                return True

        # Explicit, documented neutral system defaults used only when the page
        # asks for performance/utilization associated with a requested resource.
        if value == 100 and any(marker in label for marker in ("utilization", "利用率")):
            return True
        volume_type = str((requirements or {}).get("volume_type") or "").lower()
        if volume_type == "gp3":
            if value == 3000 and "iops" in label:
                return True
            if value == 125 and any(marker in label for marker in ("throughput", "吞吐")):
                return True
        return False

    @classmethod
    def _adopt_visible_storage_floor(
        cls,
        service: str,
        controls: list[dict[str, Any]],
        goal: dict[str, Any],
    ) -> tuple[dict[str, Any], str] | None:
        if service != "rds":
            return None
        requirements = goal.get("requirements")
        if not isinstance(requirements, dict):
            return None
        if str(requirements.get("storage_type") or "").lower() != "gp3":
            return None

        fields = (
            ("storage_iops", ("iops",), "IOPS"),
            (
                "storage_throughput_mbps",
                ("throughput", "吞吐"),
                "MiBps 吞吐量",
            ),
        )
        for key, markers, display_name in fields:
            if f"requested_{key}" in requirements:
                continue
            requested = cls._number(requirements.get(key))
            if requested is None:
                continue
            for control in controls:
                if not cls._is_fillable_control(control):
                    continue
                label = " ".join(
                    str(control.get(field) or "")
                    for field in ("name", "text", "context", "section")
                ).lower()
                if not any(marker in label for marker in markers):
                    continue
                current = cls._number(control.get("value"))
                if current is None or current <= requested:
                    continue
                actual: int | float = int(current) if current.is_integer() else current
                original: int | float = int(requested) if requested.is_integer() else requested
                requirements[f"requested_{key}"] = original
                requirements[key] = actual
                notice = (
                    f"客户填写的 RDS gp3 {display_name} {original:g} 低于当前 "
                    f"Calculator 页面自动采用的有效值 {actual:g}；本次保留 AWS 官网值继续报价"
                )
                notices = requirements.setdefault("calculator_adjustment_notices", [])
                if isinstance(notices, list) and notice not in notices:
                    notices.append(notice)
                return control, notice
        return None

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
                return float(cleaned)
        return None

    @staticmethod
    def _control_is_out_of_scope(control: dict[str, Any], goal: dict[str, Any]) -> bool:
        label = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()
        source = json.dumps(goal, ensure_ascii=False).lower()
        if "lambda" in label and "lambda" not in source:
            return True
        requirements = goal.get("requirements")
        if (
            isinstance(requirements, dict)
            and requirements.get("processed_bytes_ec2_ip_gib_per_hour") is not None
        ):
            unrelated_lcu_fields = (
                "new connection",
                "connection duration",
                "requests per",
                "rule evaluation",
                "新连接",
                "连接持续",
                "请求数",
                "规则评估",
            )
            return any(marker in label for marker in unrelated_lcu_fields)
        return False

    @staticmethod
    def _scope_service_controls(
        controls: list[dict[str, Any]], service: str, goal: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not DeepSeekCalculatorAgent._is_elasticache_service(service):
            return controls
        requirements = goal.get("requirements")
        tiering_requested = isinstance(requirements, dict) and bool(
            requirements.get("data_tiering")
        )
        if tiering_requested:
            return controls
        excluded = ("serverless", "无服务器", "data tiering", "数据分层")
        return [
            control
            for control in controls
            if str(control.get("role") or "").lower() in {"option", "row", "radio"}
            or not any(marker in str(control.get("section") or "").lower() for marker in excluded)
        ]

    @classmethod
    def _selected_model_from_controls(
        cls,
        controls: list[dict[str, Any]],
        goal: dict[str, Any],
        page_text: str = "",
    ) -> str | None:
        model_pattern = re.compile(r"\b(?:(?:db|cache)\.)?[a-z][a-z0-9-]*\.[a-z0-9-]+\b", re.I)
        for control in controls:
            if str(control.get("role") or "").lower() != "combobox":
                continue
            text = " ".join(
                str(control.get(field) or "") for field in ("name", "text", "value", "context")
            )
            match = model_pattern.search(text)
            if not match:
                continue
            model = match.group()
            evidence = cls._model_evidence(page_text, model)
            requirements = goal.get("requirements")
            requested_memory = (
                requirements.get("memory_gib") if isinstance(requirements, dict) else None
            )
            if requested_memory is not None and not cls._memory_from_text(evidence):
                continue
            candidate_control = dict(control)
            candidate_control["context"] = evidence
            if cls._candidate_goal_violation(candidate_control, goal, model) is None:
                return model
        return None

    @staticmethod
    def _is_elasticache_service(service: str) -> bool:
        return service.lower() in {"elasticache", "redis", "valkey", "memcached"}

    @classmethod
    def _elasticache_node_control(cls, controls: list[dict[str, Any]]) -> dict[str, Any] | None:
        for control in controls:
            if not cls._is_fillable_control(control):
                continue
            label = " ".join(
                str(control.get(field) or "") for field in ("name", "text", "context", "section")
            ).lower()
            if any(marker in label for marker in ("节点", "nodes", "node count")):
                return control
        return None

    @classmethod
    def _elasticache_group_is_complete(
        cls,
        controls: list[dict[str, Any]],
        goal: dict[str, Any],
        actual_nodes: float | None,
    ) -> bool:
        expected_nodes = cls._number(goal.get("quantity"))
        if expected_nodes is None or actual_nodes != expected_nodes:
            return False
        requirements = goal.get("requirements")
        expected_engine = str((requirements or {}).get("engine") or "redis").lower()
        expected_engine = "redis" if expected_engine == "redis_oss" else expected_engine
        labels = [cls._control_identity(control) for control in controls]
        engine_ok = any(
            ("缓存引擎" in label or "cache engine" in label) and expected_engine in label
            for label in labels
        )
        pricing_ok = any(
            ("定价模型" in label or "pricing model" in label)
            and ("ondemand" in label or "on demand" in label)
            for label in labels
        )
        return engine_ok and pricing_ok

    @classmethod
    def _model_evidence(cls, page_text: str, model: str) -> str:
        match = re.search(re.escape(model), page_text, re.I)
        if match is None:
            return ""
        return page_text[match.start() : match.start() + 320]

    @staticmethod
    def _memory_from_text(text: str) -> float | None:
        match = re.search(
            r"(?:Memory|内存)(?:\s*\(GiB\))?\s*[:：]?\s*([0-9.]+)\s*GiB?",
            text,
            re.I,
        )
        return float(match.group(1)) if match else None

    @staticmethod
    def _observation_signature(observation: dict[str, Any]) -> str:
        controls = observation.get("controls")
        if not isinstance(controls, list):
            controls = []
        compact = [
            {
                "role": item.get("role"),
                "name": item.get("name"),
                "text": item.get("text"),
                "value": item.get("value"),
                "checked": item.get("checked"),
                "selected": item.get("selected"),
                "expanded": item.get("expanded"),
            }
            for item in controls
        ]
        return json.dumps(compact, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _collect_numbers(cls, value: Any) -> set[float]:
        if isinstance(value, bool) or value is None:
            return set()
        if isinstance(value, (int, float)):
            return {float(value)}
        if isinstance(value, dict):
            result: set[float] = set()
            for item in value.values():
                result.update(cls._collect_numbers(item))
            return result
        if isinstance(value, list):
            result = set()
            for item in value:
                result.update(cls._collect_numbers(item))
            return result
        return set()

    @classmethod
    def _is_commitment_detail_control(
        cls, action: BrowserAction, controls: list[dict[str, Any]]
    ) -> bool:
        label = cls._action_control_text(action, controls)
        return any(
            marker in label
            for marker in (
                "1 year",
                "3 year",
                "1 年",
                "3 年",
                "upfront",
                "预付",
            )
        )

    @classmethod
    def _matches_purchase_option(
        cls,
        action: BrowserAction,
        controls: list[dict[str, Any]],
        expected: str,
    ) -> bool:
        if action.action != "click":
            return False
        label = cls._action_control_text(action, controls)
        markers = {
            "standard_reserved": ("standard reserved", "标准预留"),
            "convertible_reserved": ("convertible reserved", "可转换预留"),
            "compute_savings_plan": ("compute savings", "计算节省计划"),
            "ec2_instance_savings_plan": ("ec2 instance savings", "ec2 实例节省"),
        }.get(expected, ())
        return any(marker in label for marker in markers)

    @staticmethod
    def _action_control_text(action: BrowserAction, controls: list[dict[str, Any]]) -> str:
        control = next((item for item in controls if item.get("id") == action.control_id), {})
        return " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        ).lower()

    @staticmethod
    def _is_sql_edition_control(label: str) -> bool:
        return any(
            marker in label
            for marker in (
                "database version",
                "数据库版本",
                "enterprise developer",
                "standard",
                "web",
            )
        )

    @classmethod
    def _matches_sql_edition(cls, label: str, engine: str) -> bool:
        expected = {
            "sql_server_standard": "standard",
            "sql_server_web": "web",
            "sql_server_enterprise": "enterprise",
        }.get(engine)
        return bool(expected and expected in label and cls._is_sql_edition_control(label))

    @staticmethod
    def _is_wrong_sql_edition(label: str, engine: str) -> bool:
        edition_words = {"standard", "web", "enterprise", "developer"}
        present = {word for word in edition_words if word in label}
        if not present:
            return False
        expected = {
            "sql_server_standard": "standard",
            "sql_server_web": "web",
            "sql_server_enterprise": "enterprise",
        }.get(engine)
        return expected is not None and expected not in present

    @classmethod
    def _find_sql_edition_option(
        cls, controls: list[dict[str, Any]], engine: str
    ) -> dict[str, Any] | None:
        expected = cls._sql_edition_name(engine).lower()
        for control in controls:
            if control.get("role") != "option":
                continue
            label = " ".join(str(control.get(field) or "") for field in ("name", "text")).lower()
            if expected in label:
                return control
        return None

    @staticmethod
    def _sql_edition_name(engine: str) -> str:
        return {
            "sql_server_standard": "Standard",
            "sql_server_web": "Web",
            "sql_server_enterprise": "Enterprise",
        }.get(engine, "")

    async def _click_sql_edition_fallback(self, page: Page, engine: str) -> bool:
        edition = self._sql_edition_name(engine)
        if not edition:
            return False
        english = re.compile(rf"^{re.escape(edition)}$", re.I)
        chinese = {
            "Standard": re.compile(r"^标准版$"),
            "Web": re.compile(r"^Web$", re.I),
            "Enterprise": re.compile(r"^企业版$"),
        }[edition]
        candidates = (
            page.get_by_role("option", name=english).first,
            page.get_by_role("option", name=chinese).first,
            page.get_by_text(english, exact=True).first,
            page.get_by_text(chinese, exact=True).first,
        )
        for option in candidates:
            try:
                await option.wait_for(state="visible", timeout=1_500)
                await option.click()
                return True
            except PlaywrightError:
                continue
        return False

    @staticmethod
    def _model_from_action(action: BrowserAction, controls: list[dict[str, Any]]) -> str | None:
        if action.action != "click":
            return None
        control = next((item for item in controls if item.get("id") == action.control_id), None)
        if control is None:
            return None
        role = str(control.get("role") or "").lower()
        input_type = str(control.get("type") or "").lower()
        if role not in {"row", "radio", "option"} and input_type != "radio":
            return None
        model_pattern = re.compile(r"\b(?:(?:db|cache)\.)?[a-z][a-z0-9-]*\.[a-z0-9-]+\b", re.I)
        searchable = " ".join(
            str(control.get(field) or "") for field in ("name", "text", "context")
        )
        match = model_pattern.search(searchable)
        return match.group() if match else None

    @staticmethod
    def _candidate_hints(
        controls: list[dict[str, Any]], goal: dict[str, Any]
    ) -> list[dict[str, Any]]:
        requirements = goal.get("requirements")
        source = requirements if isinstance(requirements, dict) else goal
        requested_memory = source.get("memory_gib")
        requested_vcpu = source.get("vcpu")
        if requested_memory is None and requested_vcpu is None:
            return []
        model_pattern = re.compile(r"\b(?:(?:db|cache)\.)?[a-z][a-z0-9-]*\.[a-z0-9-]+\b", re.I)
        memory_pattern = re.compile(
            r"(?:Memory|内存)(?:\s*\(GiB\))?\s*[:：]?\s*([0-9.]+)\s*GiB?",
            re.I,
        )
        vcpu_pattern = re.compile(r"(?:vCPU|vCPU 数)\s*[:：]?\s*([0-9.]+)", re.I)
        price_pattern = re.compile(
            r"(?:每小时成本|hourly cost|cost per hour)\s*[:：]?\s*\$?([0-9.]+)",
            re.I,
        )
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for control in controls:
            role = str(control.get("role") or "").lower()
            if role not in {"option", "row", "radio"}:
                continue
            text = " ".join(str(control.get(field) or "") for field in ("name", "text", "context"))
            model_match = model_pattern.search(text)
            if not model_match:
                continue
            model = model_match.group()
            if model.lower() in seen:
                continue
            memory_match = memory_pattern.search(text)
            vcpu_match = vcpu_pattern.search(text)
            price_match = price_pattern.search(text)
            memory = float(memory_match.group(1)) if memory_match else None
            vcpu = float(vcpu_match.group(1)) if vcpu_match else None
            if requested_memory is not None and (
                memory is None or memory < float(requested_memory)
            ):
                continue
            if requested_vcpu is not None and (vcpu is None or vcpu < float(requested_vcpu)):
                continue
            memory_slack = (
                memory - float(requested_memory)
                if memory is not None and requested_memory is not None
                else 0
            )
            vcpu_slack = (
                vcpu - float(requested_vcpu)
                if vcpu is not None and requested_vcpu is not None
                else 0
            )
            seen.add(model.lower())
            candidates.append(
                {
                    "control_id": control.get("id"),
                    "model": model,
                    "vcpu": vcpu,
                    "memory_gib": memory,
                    "hourly_cost": (float(price_match.group(1)) if price_match else None),
                    "selected": control.get("selected") == "true",
                    "spec_slack": memory_slack + vcpu_slack,
                }
            )
        candidates.sort(
            key=lambda item: (
                item["hourly_cost"] is None,
                item["hourly_cost"] or 0,
                item["spec_slack"],
                item["model"],
            )
        )
        return candidates[:20]

    @staticmethod
    def _candidate_goal_violation(
        control: dict[str, Any], goal: dict[str, Any], model: str | None
    ) -> str | None:
        if model is None:
            return None
        requirements = goal.get("requirements")
        source = requirements if isinstance(requirements, dict) else goal
        requested_model = source.get("requested_model")
        if requested_model and model.lower() != str(requested_model).lower():
            return f"customer requested {requested_model}, but page option is {model}"
        text = " ".join(str(control.get(field) or "") for field in ("name", "text", "context"))
        memory_match = re.search(
            r"(?:Memory|内存)(?:\s*\(GiB\))?\s*[:：]?\s*([0-9.]+)\s*GiB?",
            text,
            re.I,
        )
        vcpu_match = re.search(r"(?:vCPU|vCPU 数)\s*[:：]?\s*([0-9.]+)", text, re.I)
        requested_memory = source.get("memory_gib")
        requested_vcpu = source.get("vcpu")
        if requested_memory is not None and memory_match:
            actual_memory = float(memory_match.group(1))
            if actual_memory < float(requested_memory):
                return (
                    f"{model} has {actual_memory:g} GiB, below requested "
                    f"{float(requested_memory):g} GiB"
                )
        if requested_vcpu is not None and vcpu_match:
            actual_vcpu = float(vcpu_match.group(1))
            if actual_vcpu < float(requested_vcpu):
                return (
                    f"{model} has {actual_vcpu:g} vCPU, below requested "
                    f"{float(requested_vcpu):g} vCPU"
                )
        return None

    @staticmethod
    def _is_choice_control(control: dict[str, Any]) -> bool:
        role = str(control.get("role") or "").lower()
        input_type = str(control.get("type") or "").lower()
        return role in {"option", "radio", "checkbox"} or input_type in {
            "radio",
            "checkbox",
        }

    @staticmethod
    def _same_choice(left: str, right: str) -> bool:
        left_words = " ".join(left.lower().split())
        right_words = " ".join(right.lower().split())
        if not left_words or not right_words:
            return False
        return left_words == right_words or (
            len(left_words) > 12
            and len(right_words) > 12
            and (left_words in right_words or right_words in left_words)
        )

    @staticmethod
    def _control_identity(control: dict[str, Any]) -> str:
        return " ".join(
            " ".join(str(control.get(field) or "").lower().split())
            for field in ("role", "type", "name", "text", "context")
        )[:500]


def calculator_goal(value: object) -> dict[str, Any]:
    """Convert a dataclass input to JSON without exposing Python implementation details."""

    if not hasattr(value, "__dataclass_fields__"):
        raise TypeError("Calculator agent goal must be a dataclass")
    return {key: item for key, item in asdict(value).items() if item is not None}
