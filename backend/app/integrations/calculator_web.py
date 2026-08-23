from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field, replace
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.integrations import calculator_selectors as selectors
from app.integrations.calculator_ai_agent import (
    DeepSeekCalculatorAgent,
    ProgressReporter,
    calculator_goal,
)


@dataclass(frozen=True, slots=True)
class Ec2CalculatorInput:
    region: str
    instance_type: str | None
    quantity: int
    requested_vcpu: float | None = None
    requested_memory_gib: float | None = None
    operating_system: str = "linux"
    tenancy: str = "shared"
    purchase_option: str = "on_demand"
    term_years: int | None = None
    payment_option: str | None = None
    utilization_percent: float = 100
    spot_discount_percent: float | None = None
    ebs_gib_per_instance: float | None = None
    ebs_volume_type: str = "gp3"
    ebs_iops: int | None = None
    ebs_throughput_mbps: float | None = None
    snapshot_frequency: str = "none"
    snapshot_changed_gib: float | None = None
    detailed_monitoring: bool = False
    data_transfer_in_gib: float | None = None
    data_transfer_regional_gib: float | None = None
    data_transfer_out_gib: float | None = None
    additional_monthly_cost: float | None = None


@dataclass(frozen=True, slots=True)
class RdsCalculatorInput:
    region: str
    engine: str
    instance_type: str | None
    quantity: int
    requested_vcpu: float | None = None
    requested_memory_gib: float | None = None
    deployment: str = "single_az"
    purchase_option: str = "on_demand"
    term_years: int | None = None
    payment_option: str | None = None
    utilization_percent: float = 100
    storage_gib_per_instance: float | None = None
    storage_type: str = "gp3"
    storage_iops: int | None = None
    storage_throughput_mbps: float | None = None
    license_model: str | None = None


@dataclass(frozen=True, slots=True)
class GenericCalculatorInput:
    """A service-neutral goal; fields come from the customer, not a local template."""

    service: str
    calculator_service_name: str
    region: str | None
    quantity: int
    requirements: dict[str, Any]
    source_text: str = ""


@dataclass(frozen=True, slots=True)
class CalculatorGenericGroupResult:
    service: str
    calculator_service_name: str
    selected_model: str


@dataclass(frozen=True, slots=True)
class CalculatorEc2GroupResult:
    instance_type: str
    vcpu: float | None = None
    memory_gib: float | None = None


@dataclass(frozen=True, slots=True)
class CalculatorRdsGroupResult:
    instance_type: str
    engine: str
    vcpu: float | None = None
    memory_gib: float | None = None


@dataclass(frozen=True, slots=True)
class CalculatorWebResult:
    monthly_total: float
    upfront_total: float = 0
    selected_instance_type: str | None = None
    selected_vcpu: float | None = None
    selected_memory_gib: float | None = None
    currency: str = "USD"
    source_url: str = "https://calculator.aws/"
    share_url: str | None = None
    details: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    groups: list[CalculatorEc2GroupResult] = field(default_factory=list)
    rds_groups: list[CalculatorRdsGroupResult] = field(default_factory=list)
    generic_groups: list[CalculatorGenericGroupResult] = field(default_factory=list)


class AwsCalculatorWebAutomator:
    def __init__(
        self,
        settings: Settings,
        ai_agent: DeepSeekCalculatorAgent | None = None,
    ):
        self._settings = settings
        self._ai_agent = ai_agent

    async def _human_pause(self, multiplier: float = 1.0) -> None:
        lower = max(0.0, self._settings.calculator_action_delay_min_seconds)
        upper = max(lower, self._settings.calculator_action_delay_max_seconds)
        await asyncio.sleep(random.uniform(lower, upper) * multiplier)  # noqa: S311

    async def quote_ec2(
        self,
        quote_input: Ec2CalculatorInput,
        reporter: ProgressReporter | None = None,
    ) -> CalculatorWebResult:
        return await self.quote_ec2_groups([quote_input], reporter)

    async def quote_ai_groups(
        self,
        quote_inputs: list[GenericCalculatorInput],
        reporter: ProgressReporter | None = None,
    ) -> CalculatorWebResult:
        """Quote any Calculator services through the observe/decide/act agent loop.

        This method deliberately knows nothing about service-specific fields. It
        only owns the Calculator's universal add/save/summary lifecycle.
        """

        if not quote_inputs:
            raise ValueError("at least one Calculator group is required")
        if self._ai_agent is None:
            raise ManualConfirmationRequired(
                "通用 Calculator 报价需要启用 AI 浏览器代理",
                code="calculator_ai_agent_unavailable",
            )
        steps: list[str] = []
        groups: list[CalculatorGenericGroupResult] = []
        current_step = "启动浏览器"
        page: Page | None = None
        timeout_ms = int(self._settings.calculator_timeout_seconds * 1000)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    channel=self._settings.calculator_browser_channel,
                    headless=self._settings.calculator_headless,
                )
                context = await browser.new_context(locale="zh-CN")
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)
                current_step = "打开 AWS Pricing Calculator"
                response = await page.goto(selectors.ADD_SERVICE_URL, wait_until="domcontentloaded")
                if response is not None and response.status == 403:
                    raise ValueError("AWS Calculator returned HTTP 403")
                await page.get_by_role(
                    "heading", name=re.compile(r"添加服务|Add service", re.I)
                ).wait_for()
                await self._human_pause(2)
                steps.append("已打开 AWS Pricing Calculator 添加服务页面")
                if reporter:
                    await reporter("browser", "已打开 AWS Pricing Calculator 官方网页")

                for index, quote_input in enumerate(quote_inputs, start=1):
                    current_step = f"AI 操作第 {index} 项 {quote_input.calculator_service_name}"
                    goal = {
                        "calculator_service_name": quote_input.calculator_service_name,
                        "quantity": quote_input.quantity,
                        "requirements": quote_input.requirements,
                        "customer_source_text": quote_input.source_text,
                    }
                    if quote_input.region:
                        goal["region"] = quote_input.region
                    result = await self._ai_agent.configure_group(
                        page,
                        service=quote_input.service,
                        goal=goal,
                        group_index=index,
                        require_model=self._goal_requires_model(goal, quote_input.service),
                        reporter=reporter,
                    )
                    steps.extend(result.steps)
                    if index < len(quote_inputs):
                        current_step = f"保存第 {index} 项并添加下一项"
                        saved = False
                        for save_attempt in range(3):
                            await page.get_by_role("button", name=selectors.SAVE_ADD_SERVICE).click(
                                timeout=15_000
                            )
                            await self._human_pause(2)
                            try:
                                await page.get_by_role(
                                    "heading",
                                    name=re.compile(r"添加服务|Add service", re.I),
                                ).wait_for(timeout=12_000)
                                saved = True
                                break
                            except PlaywrightTimeoutError:
                                if save_attempt == 2:
                                    break
                                if reporter:
                                    await reporter(
                                        "browser",
                                        "Calculator 未接受保存，正在读取页面提示并补齐必填项",
                                    )
                                goal["save_validation_failed"] = True
                                recovery = await self._ai_agent.configure_group(
                                    page,
                                    service=quote_input.service,
                                    goal=goal,
                                    group_index=index,
                                    require_model=self._goal_requires_model(
                                        goal, quote_input.service
                                    ),
                                    initial_selected_model=result.selected_model,
                                    reporter=reporter,
                                )
                                steps.extend(recovery.steps)
                                result = recovery
                        if not saved:
                            raise ValueError(
                                "Calculator rejected Save and add service after "
                                "AI attempted to resolve visible validation fields"
                            )
                        steps.append(f"第 {index} 项已保存到同一个 Estimate")
                        if reporter:
                            await reporter(
                                "calculator",
                                f"第 {index} 项已保存，正在处理下一项",
                            )
                    groups.append(
                        CalculatorGenericGroupResult(
                            service=quote_input.service,
                            calculator_service_name=quote_input.calculator_service_name,
                            selected_model=result.selected_model,
                        )
                    )

                current_step = "保存整份估算并读取官方明细"
                await page.get_by_role("button", name=selectors.SAVE_SUMMARY).click(timeout=30_000)
                await page.get_by_role("table", name=selectors.ESTIMATE_SERVICES_TABLE).wait_for(
                    timeout=30_000
                )
                monthly_total, upfront_total = await self._read_summary_totals(page)
                details = await self._read_summary_details(page)
                steps.append(f"已从 Calculator 摘要读取总月费 {monthly_total:.2f} USD")
                if reporter:
                    await reporter("result", "已读取合并报价总额和各项官方明细")
                share_url = None
                if self._settings.calculator_generate_share_link:
                    current_step = "生成 Calculator 分享链接"
                    share_url = await self._create_share_link(page)
                    steps.append("已生成 Calculator 公共分享链接")
                await browser.close()
        except (PlaywrightTimeoutError, PlaywrightError, ValueError) as exc:
            visible_fields: list[dict[str, str]] = []
            if page is not None:
                try:
                    visible_fields = await asyncio.wait_for(
                        self._visible_controls(page), timeout=3.0
                    )
                except (TimeoutError, PlaywrightError):
                    # Diagnostics must never hide the original failure behind
                    # another full Calculator timeout.
                    visible_fields = []
            raise ManualConfirmationRequired(
                f"AWS Pricing Calculator AI 自动操作失败，停在：{current_step}",
                code="calculator_web_automation_failed",
                step=current_step,
                failed_control=current_step,
                visible_fields=visible_fields,
                error_type=type(exc).__name__,
                technical_message=str(exc)[:300],
            ) from exc

        return CalculatorWebResult(
            monthly_total=monthly_total,
            upfront_total=upfront_total,
            share_url=share_url,
            details=details,
            steps=steps,
            generic_groups=groups,
        )

    @staticmethod
    def _goal_requires_model(goal: dict[str, Any], service: str | None = None) -> bool:
        requirements = goal.get("requirements")
        if not isinstance(requirements, dict):
            return False
        if service is not None:
            modeled_services = {
                "ec2",
                "rds",
                "elasticache",
                "redis",
                "valkey",
                "memcached",
                "memorydb",
                "opensearch",
                "documentdb",
                "neptune",
            }
            if service.lower() not in modeled_services:
                return False
        return any(
            requirements.get(field) is not None
            for field in ("requested_model", "vcpu", "memory_gib")
        )

    async def quote_ec2_groups(
        self,
        quote_inputs: list[Ec2CalculatorInput],
        reporter: ProgressReporter | None = None,
    ) -> CalculatorWebResult:
        last_error: ManualConfirmationRequired | None = None
        attempts = 1 if self._ai_agent is not None else 2
        for attempt in range(attempts):
            try:
                result = await self._quote_ec2_groups_once(quote_inputs, reporter)
                if attempt:
                    return replace(
                        result,
                        steps=[
                            "Calculator 页面首次未完成跳转，系统已自动安全重试",
                            *result.steps,
                        ],
                    )
                return result
            except ManualConfirmationRequired as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def quote_rds_groups(
        self,
        quote_inputs: list[RdsCalculatorInput],
        reporter: ProgressReporter | None = None,
    ) -> CalculatorWebResult:
        last_error: ManualConfirmationRequired | None = None
        attempts = 1 if self._ai_agent is not None else 2
        for attempt in range(attempts):
            try:
                result = await self._quote_rds_groups_once(quote_inputs, reporter)
                if attempt:
                    return replace(
                        result,
                        steps=[
                            "Calculator 页面首次未完成跳转，系统已自动安全重试",
                            *result.steps,
                        ],
                    )
                return result
            except ManualConfirmationRequired as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _quote_rds_groups_once(
        self,
        quote_inputs: list[RdsCalculatorInput],
        reporter: ProgressReporter | None = None,
    ) -> CalculatorWebResult:
        if not quote_inputs:
            raise ValueError("at least one RDS group is required")
        steps: list[str] = []
        groups: list[CalculatorRdsGroupResult] = []
        current_step = "启动浏览器"
        page: Page | None = None
        timeout_ms = int(self._settings.calculator_timeout_seconds * 1000)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    channel=self._settings.calculator_browser_channel,
                    headless=self._settings.calculator_headless,
                )
                context = await browser.new_context(locale="zh-CN")
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)
                current_step = "打开 AWS Pricing Calculator"
                response = await page.goto(selectors.ADD_SERVICE_URL, wait_until="domcontentloaded")
                if response is not None and response.status == 403:
                    raise ValueError("AWS Calculator returned HTTP 403")
                await page.get_by_role(
                    "heading", name=re.compile(r"添加服务|Add service", re.I)
                ).wait_for()
                await self._human_pause(2)
                steps.append("已打开 AWS Pricing Calculator 添加服务页面")
                if reporter:
                    await reporter("browser", "已打开 AWS Pricing Calculator 官方网页")

                for index, quote_input in enumerate(quote_inputs, start=1):
                    current_step = f"配置第 {index} 组 RDS"
                    group = await self._configure_rds(page, quote_input, steps, index, reporter)
                    groups.append(group)
                    if index < len(quote_inputs):
                        current_step = f"保存第 {index} 组 RDS 并添加下一组"
                        await page.get_by_role("button", name=selectors.SAVE_ADD_SERVICE).click(
                            timeout=30_000
                        )
                        await self._human_pause(2)
                        await page.get_by_role(
                            "heading", name=re.compile(r"添加服务|Add service", re.I)
                        ).wait_for(timeout=30_000)
                        steps.append(f"第 {index} 组 RDS 已保存到同一个 Estimate")
                        if reporter:
                            await reporter("calculator", f"第 {index} 组数据库已保存到同一份报价")

                current_step = "保存 RDS 估算并读取明细"
                await page.get_by_role("button", name=selectors.SAVE_SUMMARY).click()
                await page.get_by_role("table", name=selectors.ESTIMATE_SERVICES_TABLE).wait_for(
                    timeout=30_000
                )
                monthly_total, upfront_total = await self._read_summary_totals(page)
                details = await self._read_summary_details(page)
                self._validate_rds_summary(details, quote_inputs)
                steps.append(f"已从 Calculator 摘要读取 RDS 总月费 {monthly_total:.2f} USD")
                if reporter:
                    await reporter("result", "已读取数据库报价总额和明细")
                share_url = None
                if self._settings.calculator_generate_share_link:
                    current_step = "生成 Calculator 分享链接"
                    share_url = await self._create_share_link(page)
                    steps.append("已生成有效期一年的 Calculator 公共分享链接")
                await browser.close()
        except (PlaywrightTimeoutError, PlaywrightError, ValueError) as exc:
            visible_fields = await self._visible_controls(page) if page is not None else []
            raise ManualConfirmationRequired(
                f"AWS Pricing Calculator RDS 网页自动化失败，停在：{current_step}",
                code="calculator_web_automation_failed",
                step=current_step,
                failed_control=current_step,
                visible_fields=visible_fields,
                error_type=type(exc).__name__,
                technical_message=str(exc)[:300],
            ) from exc

        return CalculatorWebResult(
            monthly_total=monthly_total,
            upfront_total=upfront_total,
            share_url=share_url,
            details=details,
            steps=steps,
            rds_groups=groups,
        )

    async def _quote_ec2_groups_once(
        self,
        quote_inputs: list[Ec2CalculatorInput],
        reporter: ProgressReporter | None = None,
    ) -> CalculatorWebResult:
        if not quote_inputs:
            raise ValueError("at least one EC2 group is required")
        steps: list[str] = []
        groups: list[CalculatorEc2GroupResult] = []
        current_step = "启动浏览器"
        page: Page | None = None
        timeout_ms = int(self._settings.calculator_timeout_seconds * 1000)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    channel=self._settings.calculator_browser_channel,
                    headless=self._settings.calculator_headless,
                )
                context = await browser.new_context(locale="zh-CN")
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)

                current_step = "打开 AWS Pricing Calculator"
                await page.goto(selectors.ADD_SERVICE_URL, wait_until="domcontentloaded")
                await page.get_by_role(
                    "heading", name=re.compile(r"添加服务|Add service", re.I)
                ).wait_for()
                await self._human_pause(2)
                steps.append("已打开 AWS Pricing Calculator 添加服务页面")
                if reporter:
                    await reporter("browser", "已打开 AWS Pricing Calculator 官方网页")

                for index, quote_input in enumerate(quote_inputs, start=1):
                    current_step = f"配置第 {index} 组 EC2"
                    group = await self._configure_ec2(page, quote_input, steps, index, reporter)
                    groups.append(group)
                    if index < len(quote_inputs):
                        current_step = f"保存第 {index} 组并添加下一组"
                        await page.get_by_role("button", name=selectors.SAVE_ADD_SERVICE).click(
                            timeout=30_000
                        )
                        await self._human_pause(2)
                        await page.get_by_role(
                            "heading", name=re.compile(r"添加服务|Add service", re.I)
                        ).wait_for(timeout=30_000)
                        steps.append(f"第 {index} 组已保存到同一个 Estimate")
                        if reporter:
                            await reporter("calculator", f"第 {index} 组服务器已保存到同一份报价")

                current_step = "保存估算并读取明细"
                await page.get_by_role("button", name=selectors.SAVE_SUMMARY).click()
                await page.get_by_role("table", name=selectors.ESTIMATE_SERVICES_TABLE).wait_for(
                    timeout=30_000
                )
                monthly_total, upfront_total = await self._read_summary_totals(page)
                details = await self._read_summary_details(page)
                self._validate_ec2_summary(details, quote_inputs)
                steps.append(f"已保存估算并从摘要读取总月费 {monthly_total:.2f} USD")
                if reporter:
                    await reporter("result", "已读取服务器报价总额和明细")
                if upfront_total:
                    steps.append(f"摘要显示总预付费用 {upfront_total:.2f} USD")
                share_url = None
                if self._settings.calculator_generate_share_link:
                    current_step = "生成 Calculator 分享链接"
                    share_url = await self._create_share_link(page)
                    steps.append("已生成有效期一年的 Calculator 公共分享链接")
                await browser.close()
        except (PlaywrightTimeoutError, PlaywrightError, ValueError) as exc:
            visible_fields = await self._visible_controls(page) if page is not None else []
            raise ManualConfirmationRequired(
                f"AWS Pricing Calculator 网页自动化失败，停在：{current_step}",
                code="calculator_web_automation_failed",
                step=current_step,
                failed_control=current_step,
                visible_fields=visible_fields,
                error_type=type(exc).__name__,
                technical_message=str(exc)[:300],
            ) from exc

        return CalculatorWebResult(
            monthly_total=monthly_total,
            upfront_total=upfront_total,
            selected_instance_type=groups[0].instance_type,
            selected_vcpu=groups[0].vcpu,
            selected_memory_gib=groups[0].memory_gib,
            share_url=share_url,
            details=details,
            steps=steps,
            groups=groups,
        )

    async def _configure_rds(
        self,
        page: Page,
        quote_input: RdsCalculatorInput,
        steps: list[str],
        index: int,
        reporter: ProgressReporter | None = None,
    ) -> CalculatorRdsGroupResult:
        if self._ai_agent is not None:
            result = await self._ai_agent.configure_group(
                page,
                service="rds",
                goal=calculator_goal(quote_input),
                group_index=index,
                reporter=reporter,
            )
            steps.extend(result.steps)
            return CalculatorRdsGroupResult(
                result.selected_model,
                quote_input.engine,
                quote_input.requested_vcpu,
                quote_input.requested_memory_gib,
            )
        service_name = self._rds_service_name(quote_input.engine)
        search = page.get_by_role("searchbox", name=selectors.SERVICE_SEARCH)
        await search.fill(service_name)
        await self._human_pause()
        await page.get_by_role(
            "button",
            name=re.compile(
                rf"配置\s+{re.escape(service_name)}|Configure\s+{re.escape(service_name)}",
                re.I,
            ),
        ).first.click()
        await self._human_pause(1.5)
        await page.get_by_role(
            "heading", name=re.compile(rf"Configure\s+{re.escape(service_name)}", re.I)
        ).wait_for()
        await self._select_region(page, quote_input.region)
        await self._human_pause()
        await page.get_by_role("textbox", name=selectors.RDS_NODES).fill(str(quote_input.quantity))
        await self._human_pause()
        deployment = page.get_by_role("button", name=selectors.RDS_DEPLOYMENT).first
        if await self._is_visible(deployment):
            await self._select_dropdown(
                page,
                selectors.RDS_DEPLOYMENT,
                (
                    selectors.RDS_MULTI_AZ
                    if quote_input.deployment in {"multi_az", "multi_az_cluster"}
                    else selectors.RDS_SINGLE_AZ
                ),
            )
        if quote_input.instance_type:
            model, vcpu, memory = await self._select_rds_instance(page, quote_input.instance_type)
            steps.append(f"第 {index} 组 RDS 使用客户指定型号 {model}")
        else:
            model, vcpu, memory = await self._select_lowest_rds_instance(
                page,
                quote_input.requested_vcpu,
                quote_input.requested_memory_gib,
            )
            steps.append(f"第 {index} 组由 Calculator 选择满足规格的最低价 RDS 型号 {model}")
        if quote_input.engine.startswith("sql_server_"):
            await self._fill_rds_sql_server_edition(page, quote_input)
        await self._fill_rds_purchase_option(page, quote_input)
        if quote_input.storage_gib_per_instance is not None:
            storage_type = page.get_by_role("button", name=selectors.RDS_STORAGE_TYPE).first
            if await self._is_visible(storage_type):
                await self._select_dropdown(
                    page,
                    selectors.RDS_STORAGE_TYPE,
                    self._rds_storage_label(quote_input.storage_type),
                )
            await page.get_by_role("spinbutton", name=selectors.RDS_STORAGE_AMOUNT).fill(
                f"{quote_input.storage_gib_per_instance:g}"
            )
            unit = page.get_by_role("button", name=selectors.RDS_STORAGE_UNIT).first
            if await self._is_visible(unit):
                await unit.click()
                await page.get_by_role("option", name=re.compile(r"^GB$", re.I)).click()
            await self._fill_optional_rds_storage_performance(page, quote_input)
        await self._select_rds_default_off(page, selectors.RDS_PROXY)
        await self._select_rds_default_off(page, selectors.RDS_DATABASE_INSIGHTS)
        await self._select_rds_default_off(page, selectors.RDS_EXTENDED_SUPPORT)
        return CalculatorRdsGroupResult(model, quote_input.engine, vcpu, memory)

    async def _fill_rds_sql_server_edition(self, page: Page, value: RdsCalculatorInput) -> None:
        license_option = (
            selectors.RDS_BRING_YOUR_OWN_MEDIA
            if value.license_model == "bring_your_own_media"
            else selectors.RDS_LICENSE_INCLUDED
        )
        await self._select_dropdown(page, selectors.RDS_LICENSE, license_option)
        version = {
            "sql_server_standard": selectors.RDS_SQL_STANDARD,
            "sql_server_web": selectors.RDS_SQL_WEB,
            "sql_server_enterprise": selectors.RDS_SQL_ENTERPRISE,
        }.get(value.engine)
        if version is None:
            raise ValueError(f"unsupported SQL Server edition: {value.engine}")
        await self._select_dropdown(page, selectors.RDS_DATABASE_VERSION, version)

    async def _select_rds_default_off(self, page: Page, field: re.Pattern) -> None:
        control = (
            page.get_by_role("button", name=field)
            .and_(page.locator('button[aria-haspopup="listbox"]'))
            .first
        )
        if not await self._is_visible(control):
            return
        await control.click()
        no_option = page.get_by_role("option", name=selectors.RDS_NO).first
        if await self._is_visible(no_option):
            await no_option.click()
            await self._human_pause()
        else:
            await control.press("Escape")

    async def _select_rds_instance(
        self, page: Page, instance_type: str
    ) -> tuple[str, float | None, float | None]:
        control = page.get_by_role("combobox", name=selectors.RDS_INSTANCE).first
        await control.fill(instance_type)
        option = page.get_by_role("option", name=re.compile(re.escape(instance_type), re.I)).first
        await option.wait_for(state="visible")
        await option.click()
        vcpu, memory = await self._selected_rds_specs(page)
        return instance_type, vcpu, memory

    async def _select_lowest_rds_instance(
        self,
        page: Page,
        requested_vcpu: float | None,
        requested_memory_gib: float | None,
    ) -> tuple[str, float | None, float | None]:
        control = page.get_by_role("combobox", name=selectors.RDS_INSTANCE).first
        await control.fill("")
        await control.click()
        options = page.get_by_role("option")
        await options.first.wait_for(state="visible")
        texts = await options.all_text_contents()
        candidates: list[tuple[int, str, float, float]] = []
        for option_index, text in enumerate(texts):
            model_match = re.search(r"\bdb\.[a-z0-9-]+\.[a-z0-9-]+(?=vCPU|\s|$)", text, re.I)
            if model_match is None:
                continue
            vcpu_match = re.search(r"(?:vCPU|vCPU 数)\D*(\d+(?:\.\d+)?)", text, re.I)
            memory_match = re.search(r"(?:Memory|内存)\D*(\d+(?:\.\d+)?)\s*(?:GiB|GB)", text, re.I)
            vcpu = float(vcpu_match.group(1)) if vcpu_match else None
            memory = float(memory_match.group(1)) if memory_match else None
            if requested_vcpu is not None and (vcpu is None or vcpu < requested_vcpu):
                continue
            if requested_memory_gib is not None and (
                memory is None or memory < requested_memory_gib
            ):
                continue
            if vcpu is not None and memory is not None:
                candidates.append((option_index, model_match.group(), vcpu, memory))
        if not candidates:
            await control.press("Escape")
            raise ValueError(
                "Calculator instance chooser did not expose a candidate satisfying "
                f"vCPU={requested_vcpu}, memory={requested_memory_gib}"
            )
        requested_cpu = requested_vcpu or min(candidate[2] for candidate in candidates)
        requested_memory = requested_memory_gib or min(candidate[3] for candidate in candidates)
        nearest_distance = min(
            (candidate[2] - requested_cpu, candidate[3] - requested_memory)
            for candidate in candidates
        )
        nearest = [
            candidate
            for candidate in candidates
            if (candidate[2] - requested_cpu, candidate[3] - requested_memory) == nearest_distance
        ]
        await control.press("Escape")
        priced: list[tuple[float, str, float, float]] = []
        for _, model, vcpu, memory in nearest[:24]:
            await control.fill(model)
            await self._human_pause()
            option = page.get_by_role(
                "option", name=re.compile(rf"^{re.escape(model)}\s", re.I)
            ).first
            await option.wait_for(state="visible")
            await option.click()
            await self._human_pause()
            monthly = await self._read_editor_monthly_total(page)
            priced.append((monthly, model, vcpu, memory))
        monthly, model, vcpu, memory = min(priced)
        del monthly
        await control.fill(model)
        await self._human_pause()
        await page.get_by_role(
            "option", name=re.compile(rf"^{re.escape(model)}\s", re.I)
        ).first.click()
        selected_vcpu, selected_memory = await self._selected_rds_specs(page)
        return model, selected_vcpu or vcpu, selected_memory or memory

    async def _selected_rds_specs(self, page: Page) -> tuple[float | None, float | None]:
        text = await page.locator("main").inner_text()
        selected = text.split("Selected Instance:", 1)[-1]
        vcpu_match = re.search(r"vCPU\s*:\s*(\d+(?:\.\d+)?)", selected, re.I)
        memory_match = re.search(r"Memory\s*:\s*(\d+(?:\.\d+)?)\s*GiB", selected, re.I)
        return (
            float(vcpu_match.group(1)) if vcpu_match else None,
            float(memory_match.group(1)) if memory_match else None,
        )

    async def _read_editor_monthly_total(self, page: Page) -> float:
        text = await page.locator("body").inner_text()
        match = re.search(
            r"(?:月度总成本|Total monthly cost)\s*:\s*([0-9,]+(?:\.\d+)?)\s*USD",
            text,
            re.I,
        )
        if match is None:
            raise ValueError("Calculator editor monthly total not found")
        return float(match.group(1).replace(",", ""))

    async def _fill_rds_purchase_option(self, page: Page, value: RdsCalculatorInput) -> None:
        option = (
            selectors.RDS_ON_DEMAND
            if value.purchase_option == "on_demand"
            else selectors.RDS_RESERVED
        )
        await self._select_dropdown(page, selectors.RDS_PRICING_MODEL, option)
        if value.purchase_option == "on_demand":
            utilization = page.get_by_role("spinbutton", name=selectors.RDS_UTILIZATION)
            if await self._is_visible(utilization):
                await utilization.fill(f"{value.utilization_percent:g}")
            return
        # Calculator redraws the commitment controls after changing from
        # OnDemand to Reserved.  Test visibility after a human-like pause,
        # rather than treating the transient redraw as a missing field.
        await self._human_pause(1.5)
        await self._select_rds_reservation_value(
            page,
            selectors.RDS_TERM_FIELD,
            selectors.RDS_TERM_1_YEAR if value.term_years == 1 else selectors.RDS_TERM_3_YEAR,
        )
        payment = {
            "no_upfront": selectors.RDS_NO_UPFRONT,
            "partial_upfront": selectors.RDS_PARTIAL_UPFRONT,
            "all_upfront": selectors.RDS_ALL_UPFRONT,
        }.get(value.payment_option or "no_upfront")
        if payment is None:
            raise ValueError(f"unsupported RDS payment option: {value.payment_option}")
        await self._select_rds_reservation_value(page, selectors.RDS_PURCHASE_FIELD, payment)

    async def _select_rds_reservation_value(
        self, page: Page, field_name: re.Pattern, option_name: re.Pattern
    ) -> None:
        radio = page.get_by_role("radio", name=option_name).first
        if await self._is_visible(radio):
            await radio.click()
            return
        button = page.get_by_role("button", name=field_name).first
        try:
            await button.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise ValueError(
                f"Calculator did not expose RDS reservation field {field_name.pattern}"
            ) from exc
        await button.click()
        await page.get_by_role("option", name=option_name).first.click()
        await self._human_pause()

    async def _fill_optional_rds_storage_performance(
        self, page: Page, value: RdsCalculatorInput
    ) -> None:
        if value.storage_iops is not None:
            iops = page.get_by_role(
                "spinbutton", name=re.compile(r"IOPS.*值|IOPS.*Value", re.I)
            ).first
            if not await self._is_visible(iops):
                raise ValueError("Calculator did not expose RDS IOPS for selected storage type")
            await iops.fill(str(value.storage_iops))
        if value.storage_throughput_mbps is not None:
            throughput = page.get_by_role(
                "spinbutton", name=re.compile(r"吞吐量.*值|Throughput.*Value", re.I)
            ).first
            if not await self._is_visible(throughput):
                raise ValueError(
                    "Calculator did not expose RDS throughput for selected storage type"
                )
            await throughput.fill(f"{value.storage_throughput_mbps:g}")

    async def _configure_ec2(
        self,
        page: Page,
        quote_input: Ec2CalculatorInput,
        steps: list[str],
        index: int,
        reporter: ProgressReporter | None = None,
    ) -> CalculatorEc2GroupResult:
        if self._ai_agent is not None:
            result = await self._ai_agent.configure_group(
                page,
                service="ec2",
                goal=calculator_goal(quote_input),
                group_index=index,
                reporter=reporter,
            )
            steps.extend(result.steps)
            return CalculatorEc2GroupResult(
                result.selected_model,
                quote_input.requested_vcpu,
                quote_input.requested_memory_gib,
            )
        await self._add_ec2(page)
        await self._select_region(page, quote_input.region)
        await self._select_dropdown(
            page,
            re.compile(r"租赁|Tenancy", re.I),
            self._tenancy_label(quote_input.tenancy),
        )
        await self._select_dropdown(
            page,
            re.compile(r"操作系统|Operating system", re.I),
            self._os_label(quote_input.operating_system),
        )
        await page.get_by_role("spinbutton", name=selectors.INSTANCE_QUANTITY).fill(
            str(quote_input.quantity)
        )
        if quote_input.instance_type:
            model, vcpu, memory = await self._select_instance(page, quote_input.instance_type)
            steps.append(f"第 {index} 组使用客户指定型号 {model}")
        else:
            model, vcpu, memory = await self._select_first_filtered_instance(
                page, quote_input.requested_vcpu, quote_input.requested_memory_gib
            )
            steps.append(f"第 {index} 组由 Calculator 自动选择最低价型号 {model}")
        await self._fill_purchase_option(page, quote_input)
        if quote_input.ebs_gib_per_instance is not None:
            await self._fill_ebs(page, quote_input)
        if quote_input.detailed_monitoring:
            section = page.get_by_role("button", name=selectors.DETAILED_MONITORING_SECTION).first
            if await section.get_attribute("aria-expanded") != "true":
                await section.click()
            await page.get_by_role("checkbox", name=selectors.ENABLE_MONITORING).check()
        if any(
            value not in (None, 0)
            for value in (
                quote_input.data_transfer_in_gib,
                quote_input.data_transfer_regional_gib,
                quote_input.data_transfer_out_gib,
            )
        ):
            await self._fill_data_transfer(page, quote_input)
        if quote_input.additional_monthly_cost is not None:
            await self._fill_additional_cost(page, quote_input.additional_monthly_cost)
        return CalculatorEc2GroupResult(model, vcpu, memory)

    async def _add_ec2(self, page: Page) -> None:
        search = page.get_by_role("searchbox", name=selectors.SERVICE_SEARCH)
        await search.fill("Amazon EC2")
        await page.get_by_role("button", name=selectors.CONFIGURE_EC2).click()
        await page.get_by_role("heading", name=re.compile(r"Configure Amazon EC2", re.I)).wait_for()

    async def _select_region(self, page: Page, region: str) -> None:
        await page.get_by_role("button", name=selectors.REGION_BUTTON).click()
        await page.get_by_role("option", name=re.compile(re.escape(region), re.I)).click()

    async def _select_instance(
        self, page: Page, instance_type: str
    ) -> tuple[str, float | None, float | None]:
        await page.get_by_role("searchbox", name=selectors.INSTANCE_SEARCH).fill(instance_type)
        row = page.get_by_role("row", name=re.compile(rf"\b{re.escape(instance_type)}\b")).first
        await row.get_by_role("radio").click()
        vcpu, memory = await self._row_specs(row)
        return instance_type, vcpu, memory

    async def _select_first_filtered_instance(
        self,
        page: Page,
        requested_vcpu: float | None,
        requested_memory_gib: float | None,
    ) -> tuple[str, float | None, float | None]:
        if requested_vcpu is not None:
            await self._select_numeric_filter(
                page,
                selectors.VCPU_FILTER,
                requested_vcpu,
            )
        if requested_memory_gib is not None:
            await self._select_numeric_filter(
                page,
                selectors.MEMORY_FILTER,
                requested_memory_gib,
            )
        table = page.get_by_role("table", name=selectors.EC2_SELECTION_TABLE)
        first = (
            table.get_by_role("row")
            .filter(has_text=re.compile(r"\b[a-z][a-z0-9-]*\.[a-z0-9-]+\b", re.I))
            .first
        )
        await first.wait_for(state="visible")
        text = await first.inner_text()
        model = text.strip().split()[0]
        if not re.fullmatch(r"[a-z0-9-]+(?:[a-z0-9-]*)?\.[a-z0-9-]+", model, re.I):
            raise ValueError(f"first filtered EC2 row has no instance type: {text[:160]}")
        await first.get_by_role("radio").click()
        vcpu, memory = await self._row_specs(first)
        return model, vcpu, memory

    @staticmethod
    async def _row_specs(row: Locator) -> tuple[float | None, float | None]:
        cells = await row.get_by_role("cell").all_text_contents()
        try:
            vcpu = float(cells[4].strip())
            memory_match = re.search(r"\d+(?:\.\d+)?", cells[6].replace(",", ""))
            memory = float(memory_match.group()) if memory_match else None
            return vcpu, memory
        except (IndexError, ValueError):
            return None, None

    async def _select_numeric_filter(
        self,
        page: Page,
        button_name: re.Pattern,
        minimum: float,
    ) -> None:
        button = page.get_by_role("button", name=button_name).first
        await button.click()
        options = page.get_by_role("option")
        texts = await options.all_text_contents()
        choices: list[tuple[float, int]] = []
        for index, text in enumerate(texts):
            match = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
            if match:
                value = float(match.group())
                if value >= minimum:
                    choices.append((value, index))
        if not choices:
            await button.press("Escape")
            raise ValueError(f"Calculator filter has no value >= {minimum:g}")
        _, index = min(choices)
        await options.nth(index).click()

    async def _select_dropdown(
        self, page: Page, button_name: re.Pattern, option_name: re.Pattern
    ) -> None:
        await page.get_by_role("button", name=button_name).first.click()
        await page.get_by_role("option", name=option_name).first.click()

    async def _fill_purchase_option(self, page: Page, value: Ec2CalculatorInput) -> None:
        purchase_locators = {
            "compute_savings_plan": selectors.COMPUTE_SAVINGS_PLAN,
            "ec2_instance_savings_plan": selectors.EC2_SAVINGS_PLAN,
            "on_demand": selectors.ON_DEMAND,
            "spot": selectors.SPOT,
            "standard_reserved": selectors.STANDARD_RESERVED,
            "convertible_reserved": selectors.CONVERTIBLE_RESERVED,
        }
        if value.purchase_option not in purchase_locators:
            raise ValueError(f"unsupported purchase option: {value.purchase_option}")
        if value.purchase_option in {"standard_reserved", "convertible_reserved"}:
            other = page.get_by_role("button", name=selectors.OTHER_PURCHASE_OPTIONS)
            if await other.get_attribute("aria-expanded") != "true":
                await other.click()
        await page.get_by_role("radio", name=purchase_locators[value.purchase_option]).first.click()
        if value.purchase_option == "on_demand":
            await page.get_by_role("spinbutton", name=selectors.UTILIZATION).fill(
                f"{value.utilization_percent:g}"
            )
            return
        if value.purchase_option == "spot":
            if value.spot_discount_percent is not None:
                await page.get_by_role(
                    "spinbutton", name=re.compile(r"Assume percentage discount", re.I)
                ).fill(f"{value.spot_discount_percent:g}")
            return
        purchase_index = {
            "compute_savings_plan": 0,
            "ec2_instance_savings_plan": 1,
            "standard_reserved": 2,
            "convertible_reserved": 3,
        }[value.purchase_option]
        term_pattern = selectors.TERM_1_YEAR if value.term_years == 1 else selectors.TERM_3_YEAR
        await page.get_by_role("radio", name=term_pattern).nth(purchase_index).click()
        payment_pattern = {
            "no_upfront": selectors.NO_UPFRONT,
            "partial_upfront": selectors.PARTIAL_UPFRONT,
            "all_upfront": selectors.ALL_UPFRONT,
        }.get(value.payment_option or "no_upfront")
        if payment_pattern is None:
            raise ValueError(f"unsupported payment option: {value.payment_option}")
        await page.get_by_role("radio", name=payment_pattern).nth(purchase_index).click()

    async def _fill_ebs(self, page: Page, value: Ec2CalculatorInput) -> None:
        section = page.get_by_role("button", name=selectors.EBS_SECTION).first
        if await section.get_attribute("aria-expanded") != "true":
            await section.click()
        await self._select_dropdown(
            page,
            selectors.EBS_VOLUME_TYPE,
            self._volume_label(value.ebs_volume_type),
        )
        if value.ebs_iops is not None:
            await page.get_by_role("textbox", name=selectors.EBS_IOPS).fill(str(value.ebs_iops))
        if value.ebs_throughput_mbps is not None:
            await page.get_by_role("spinbutton", name=selectors.EBS_THROUGHPUT).fill(
                f"{value.ebs_throughput_mbps:g}"
            )
        await page.get_by_role("spinbutton", name=selectors.EBS_STORAGE).fill(
            f"{value.ebs_gib_per_instance:g}"
        )
        if value.snapshot_frequency != "none":
            await self._select_dropdown(
                page,
                selectors.SNAPSHOT_FREQUENCY,
                self._snapshot_label(value.snapshot_frequency),
            )
            changed = page.get_by_role("spinbutton", name=selectors.SNAPSHOT_CHANGED)
            if value.snapshot_changed_gib is not None and await self._is_visible(changed):
                await changed.fill(f"{value.snapshot_changed_gib:g}")

    async def _fill_data_transfer(self, page: Page, value: Ec2CalculatorInput) -> None:
        section = page.get_by_role("button", name=selectors.DATA_TRANSFER_SECTION).first
        if await section.get_attribute("aria-expanded") != "true":
            await section.click()
        group = page.get_by_role("group", name=selectors.DATA_TRANSFER_SECTION)
        amounts = group.get_by_role("spinbutton")
        if value.data_transfer_in_gib not in (None, 0) and await amounts.count() >= 1:
            source = group.get_by_role("button", name=selectors.INBOUND_SOURCE)
            if await self._is_visible(source):
                await source.click()
                internet = page.get_by_role("option", name=selectors.INTERNET_OPTION)
                if await self._is_visible(internet):
                    await internet.first.click()
            await self._select_transfer_gb_unit(page, group, selectors.INBOUND_TRANSFER_UNIT)
            await amounts.nth(0).fill(f"{value.data_transfer_in_gib:g}")
            await amounts.nth(0).press("Tab")
        if value.data_transfer_regional_gib not in (None, 0) and await amounts.count() >= 2:
            await self._select_transfer_gb_unit(page, group, selectors.REGIONAL_TRANSFER_UNIT)
            await amounts.nth(1).fill(f"{value.data_transfer_regional_gib:g}")
            await amounts.nth(1).press("Tab")
        if value.data_transfer_out_gib not in (None, 0) and await amounts.count() >= 3:
            destination = group.get_by_role("button", name=selectors.OUTBOUND_DESTINATION)
            if await self._is_visible(destination):
                await destination.click()
                internet = page.get_by_role("option", name=selectors.INTERNET_OPTION)
                if await self._is_visible(internet):
                    await internet.first.click()
            await self._select_transfer_gb_unit(page, group, selectors.OUTBOUND_TRANSFER_UNIT)
            await amounts.nth(2).fill(f"{value.data_transfer_out_gib:g}")
            await amounts.nth(2).press("Tab")

    async def _select_transfer_gb_unit(
        self, page: Page, group: Locator, button_name: re.Pattern
    ) -> None:
        button = group.get_by_role("button", name=button_name).first
        await button.click()
        options = page.get_by_role("option")
        labels = await options.all_text_contents()
        for index, label in enumerate(labels):
            if label.strip().lower() in selectors.GB_PER_MONTH_LABELS:
                await options.nth(index).click()
                return
        raise ValueError("Calculator data-transfer unit has no GB/month option")

    async def _fill_additional_cost(self, page: Page, amount: float) -> None:
        section = page.get_by_role("button", name=selectors.ADDITIONAL_COST_SECTION).first
        if await section.get_attribute("aria-expanded") != "true":
            await section.click()
        control = page.get_by_role("textbox", name=selectors.ADDITIONAL_COST_INPUT)
        if await self._is_visible(control):
            await control.fill(f"{amount:g}")

    @staticmethod
    async def _is_visible(locator: Locator) -> bool:
        try:
            return await locator.count() > 0 and await locator.first.is_visible()
        except PlaywrightError:
            return False

    @staticmethod
    async def _visible_controls(page: Page) -> list[dict[str, str]]:
        try:
            controls = page.locator("input, button, select, textarea")
            return await controls.evaluate_all(
                """elements => elements.filter(el => {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none' &&
                           rect.width > 0 && rect.height > 0;
                }).slice(0, 120).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    name: el.getAttribute('aria-label') || el.getAttribute('name') || '',
                    text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 160)
                }))"""
            )
        except PlaywrightError:
            return []

    @staticmethod
    def _tenancy_label(value: str) -> re.Pattern:
        labels = {
            "shared": r"共享实例|Shared",
            "dedicated_instance": r"专用实例|Dedicated Instance",
            "dedicated_host": r"专用主机|Dedicated Host",
        }
        if value not in labels:
            raise ValueError(f"unsupported tenancy: {value}")
        return re.compile(labels[value], re.I)

    @staticmethod
    def _os_label(value: str) -> re.Pattern:
        labels = {
            "linux": r"^Linux$",
            "windows": r"Windows Server$",
            "windows_sql_standard": (
                r"配有 SQL Server Standard 的 Windows|Windows.*SQL Server Standard"
            ),
            "windows_sql_web": r"配有 SQL Server Web 的 Windows|Windows.*SQL Server Web",
            "windows_sql_enterprise": (
                r"配有 SQL Server Enterprise 的 Windows|Windows.*SQL Server Enterprise"
            ),
            "rhel": r"^Red Hat Enterprise Linux$",
            "suse": r"SUSE Linux Enterprise Server",
            "linux_sql_standard": r"配有 SQL Server Standard 的 Linux|Linux.*SQL Server Standard",
            "linux_sql_web": r"配有 SQL Server Web 的 Linux|Linux.*SQL Server Web",
            "linux_sql_enterprise": (
                r"配有 SQL Server Enterprise 的 Linux|Linux.*SQL Server Enterprise"
            ),
            "rhel_ha": r"Red Hat Enterprise Linux with HA|配有 HA 的 Red Hat",
            "rhel_sql_web": r"SQL Server Web.*Red Hat|Red Hat.*SQL Server Web",
            "rhel_sql_standard": r"SQL Server Standard.*Red Hat|Red Hat.*SQL Server Standard",
            "rhel_sql_enterprise": r"SQL Server Enterprise.*Red Hat|Red Hat.*SQL Server Enterprise",
            "rhel_ha_sql_standard": (
                r"HA.*SQL Server Standard.*Red Hat|Red Hat.*HA.*SQL Server Standard"
            ),
            "rhel_ha_sql_enterprise": (
                r"HA.*SQL Server Enterprise.*Red Hat|Red Hat.*HA.*SQL Server Enterprise"
            ),
            "ubuntu_pro": r"Ubuntu Pro",
        }
        if value not in labels:
            raise ValueError(f"unsupported operating system: {value}")
        return re.compile(labels[value], re.I)

    @staticmethod
    def _volume_label(value: str) -> re.Pattern:
        labels = {
            "gp3": r"gp3",
            "gp2": r"gp2",
            "io1": r"io1",
            "io2": r"io2",
            "st1": r"st\s*1",
            "sc1": r"sc1",
            "magnetic": r"磁介质|Magnetic",
        }
        if value not in labels:
            raise ValueError(f"unsupported EBS volume type: {value}")
        return re.compile(labels[value], re.I)

    @staticmethod
    def _rds_service_name(engine: str) -> str:
        services = {
            "postgresql": "Amazon RDS for PostgreSQL",
            "mysql": "Amazon RDS for MySQL",
            "mariadb": "Amazon RDS for MariaDB",
            "aurora_mysql": "Amazon Aurora MySQL-Compatible",
            "aurora_postgresql": "Amazon Aurora PostgreSQL-Compatible DB",
            "sql_server_standard": "Amazon RDS for SQL Server",
            "sql_server_web": "Amazon RDS for SQL Server",
            "sql_server_enterprise": "Amazon RDS for SQL Server",
            "oracle": "Amazon RDS for Oracle",
            "db2": "Amazon RDS for Db2",
        }
        try:
            return services[engine]
        except KeyError as exc:
            raise ValueError(f"unsupported RDS engine: {engine}") from exc

    @staticmethod
    def _rds_storage_label(value: str) -> re.Pattern:
        labels = {
            "gp3": r"gp3",
            "gp2": r"gp2",
            "io1": r"io1",
            "io2": r"io2",
            "magnetic": r"磁介质|Magnetic",
        }
        try:
            return re.compile(labels[value], re.I)
        except KeyError as exc:
            raise ValueError(f"unsupported RDS storage type: {value}") from exc

    @staticmethod
    def _snapshot_label(value: str) -> re.Pattern:
        labels = {
            "none": r"无快照存储|No snapshot",
            "hourly": r"^每小时$|Hourly",
            "daily": r"^每日$|^Daily$",
            "twice_daily": r"2 倍每日|Twice daily",
            "three_times_daily": r"3 倍每日|Three times daily",
            "four_times_daily": r"4 倍每日|Four times daily",
            "six_times_daily": r"6 倍每日|Six times daily",
            "weekly": r"^每周$|Weekly",
            "monthly": r"^每月$|Monthly",
        }
        if value not in labels:
            raise ValueError(f"unsupported snapshot frequency: {value}")
        return re.compile(labels[value], re.I)

    async def _read_summary_totals(self, page: Page) -> tuple[float, float]:
        body = await page.locator("body").inner_text()
        monthly_match = selectors.SUMMARY_MONTHLY_TOTAL.search(body)
        if monthly_match is None:
            raise ValueError("estimate summary monthly total not found")
        upfront_match = selectors.SUMMARY_UPFRONT_TOTAL.search(body)
        monthly = float(monthly_match.group(1).replace(",", ""))
        upfront = (
            float(upfront_match.group(1).replace(",", "")) if upfront_match is not None else 0.0
        )
        return monthly, upfront

    async def _read_summary_details(self, page: Page) -> list[str]:
        table = page.get_by_role("table", name=selectors.ESTIMATE_SERVICES_TABLE)
        rows = table.get_by_role("row")
        if await rows.count() < 2:
            raise ValueError("estimate summary row not found")
        return [
            (await rows.nth(index).inner_text()).strip() for index in range(1, await rows.count())
        ]

    @staticmethod
    def _validate_ec2_summary(details: list[str], quote_inputs: list[Ec2CalculatorInput]) -> None:
        if len(details) != len(quote_inputs):
            raise ValueError("Calculator summary group count does not match EC2 inputs")
        purchase_markers = {
            "compute_savings_plan": ("compute savings plans",),
            "ec2_instance_savings_plan": ("ec2 instance savings plans",),
            "on_demand": ("on-demand", "ondemand"),
            "spot": ("spot",),
            "standard_reserved": ("standard reserved", "标准预留"),
            "convertible_reserved": ("convertible reserved", "可转换预留"),
        }
        for index, (detail, value) in enumerate(zip(details, quote_inputs, strict=True), start=1):
            normalized = detail.lower()
            if value.instance_type and value.instance_type.lower() not in normalized:
                raise ValueError(
                    f"Calculator summary group {index} does not contain requested model "
                    f"{value.instance_type}"
                )
            markers = purchase_markers.get(value.purchase_option, ())
            if markers and not any(marker in normalized for marker in markers):
                raise ValueError(
                    f"Calculator summary group {index} purchase option does not match "
                    f"{value.purchase_option}"
                )

    @staticmethod
    def _validate_rds_summary(details: list[str], quote_inputs: list[RdsCalculatorInput]) -> None:
        if len(details) != len(quote_inputs):
            raise ValueError("Calculator summary group count does not match RDS inputs")
        engine_markers = {
            "postgresql": ("postgresql",),
            "mysql": ("mysql",),
            "mariadb": ("mariadb",),
            "aurora_mysql": ("aurora mysql",),
            "aurora_postgresql": ("aurora postgresql",),
            "sql_server_standard": ("数据库版本 (standard)", "database version (standard)"),
            "sql_server_web": ("数据库版本 (web)", "database version (web)"),
            "sql_server_enterprise": (
                "数据库版本 (enterprise)",
                "database version (enterprise)",
            ),
        }
        for index, (detail, value) in enumerate(zip(details, quote_inputs, strict=True), start=1):
            normalized = detail.lower()
            if value.instance_type and value.instance_type.lower() not in normalized:
                raise ValueError(
                    f"Calculator summary group {index} does not contain requested model "
                    f"{value.instance_type}"
                )
            markers = engine_markers.get(value.engine, ())
            if markers and not any(marker in normalized for marker in markers):
                raise ValueError(
                    f"Calculator summary group {index} database engine/version does not "
                    f"match {value.engine}"
                )
            deployment_marker = "multi-az" if value.deployment != "single_az" else "single-az"
            if deployment_marker not in normalized:
                raise ValueError(
                    f"Calculator summary group {index} deployment does not match {value.deployment}"
                )
            pricing_marker = "reserved" if value.purchase_option == "reserved" else "ondemand"
            if pricing_marker not in normalized.replace("-", ""):
                raise ValueError(
                    f"Calculator summary group {index} pricing model does not match "
                    f"{value.purchase_option}"
                )

    async def _create_share_link(self, page: Page) -> str:
        await page.get_by_role("button", name=selectors.SHARE).click()
        agree = page.get_by_role("button", name=selectors.AGREE_SHARE)
        if await agree.count() > 0:
            await agree.click()
        link = page.locator(selectors.PUBLIC_LINK_INPUT).first
        await link.wait_for()
        value = await link.input_value()
        if not value.startswith("https://calculator.aws/#/estimate?id="):
            raise ValueError("public share link not found")
        return value
