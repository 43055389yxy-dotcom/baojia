from __future__ import annotations

import logging
from typing import Any, Callable

from app.core.errors import ManualConfirmationRequired
from app.domain.customer_facts import field_scope, scoped_amount
from app.domain.models import (
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.integrations.aws import PricingCatalog
from app.services.plugins.base import ServicePlugin, required_float

logger = logging.getLogger(__name__)


def _one_matching(
    catalog: PricingCatalog,
    service_code: str,
    filters: dict[str, str],
    predicate: Callable[[dict[str, str]], bool],
    context: str,
    *,
    fallback_filters: dict[str, str] | None = None,
    fallback_predicate: Callable[[dict[str, str]], bool] | None = None,
) -> dict[str, Any]:
    """Resolve one official product and recover from stale catalog schemas.

    AWS product names and usage-type labels are not a stable API contract.  A
    cached response or a renamed billing label must therefore not make a
    healthy official API look unavailable.  Try the cached narrow query first,
    refresh it once, then (when supplied by the adapter) discover candidates
    through broader stable business attributes.
    """

    def matching(
        query_filters: dict[str, str],
        selector: Callable[[dict[str, str]], bool],
        *,
        refresh: bool,
    ) -> list[dict[str, Any]]:
        try:
            products = catalog.products(
                service_code,
                query_filters,
                max_pages=4,
                refresh=refresh,
            )
        except TypeError as exc:
            # Lightweight test catalogs and third-party read-only catalog
            # implementations may predate the refresh keyword. Production's
            # PricingCatalog always supports it.
            if "refresh" not in str(exc):
                raise
            products = catalog.products(
                service_code,
                query_filters,
                max_pages=4,
            )
        return [
            product
            for product in products
            if selector(catalog.attributes(product))
        ]

    candidates = matching(filters, predicate, refresh=False)
    if candidates:
        try:
            return PricingCatalog.require_unique(candidates, context=context)
        except ManualConfirmationRequired as exc:
            if exc.code != "ambiguous_billing_dimensions":
                raise

    refreshed = matching(filters, predicate, refresh=True)
    if refreshed:
        try:
            return PricingCatalog.require_unique(refreshed, context=context)
        except ManualConfirmationRequired as exc:
            if exc.code != "ambiguous_billing_dimensions":
                raise

    if fallback_filters is not None:
        selector = fallback_predicate or predicate
        discovered = matching(fallback_filters, selector, refresh=True)
        if discovered:
            logger.warning(
                "%s recovered after AWS billing schema discovery; narrow filters=%s",
                context,
                filters,
            )
            return PricingCatalog.require_unique(discovered, context=context)

    return PricingCatalog.require_unique(refreshed, context=context)


def _usage(
    product: dict[str, Any],
    *,
    key: str,
    amount: float,
    group: str,
    source_fields: tuple[str, ...] = (),
) -> UsageLine:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    return UsageLine(
        key=key,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
        amount=amount,
        group=group,
        source_fields=list(source_fields),
    )


def _reference(product: dict[str, Any], *, description: str) -> ReferenceRate:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    priced = PricingCatalog.on_demand_unit_rate(product)
    if priced is None:
        raise ManualConfirmationRequired(
            "AWS 官方目录暂时没有返回该项目的单位价格",
            code="reference_unit_rate_not_found",
            service_code=service_code,
            usage_type=usage_type,
        )
    unit_price, unit = priced
    return ReferenceRate(
        description=description,
        unit=unit,
        unit_price=unit_price,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
    )


class _MinimumAssumptionPlugin(ServicePlugin):
    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        preview = super().preview(requirement, default_region)
        return preview.model_copy(
            update={"requires_confirmation": False, "confirmation_reason": None}
        )


class Route53Plugin(_MinimumAssumptionPlugin):
    kind = ServiceKind.ROUTE53
    display_name = "Amazon Route 53"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        hosted_zones = required_float(requirement.requirements, "hosted_zones") or 1.0
        product = PricingCatalog.require_unique(
            self.catalog.products("AmazonRoute53", {"usagetype": "HostedZone"}, max_pages=1),
            context="Route 53 Hosted Zone",
        )
        line = _usage(
            product,
            key="rt53",
            amount=hosted_zones,
            group="route53",
            source_fields=("hosted_zones",),
        )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region="Global",
            model="Public Hosted Zone",
            architecture=f"{hosted_zones:g} 个域名托管区",
            specifications={"hostedZones": hosted_zones},
            official_product={"source": "AWS Price List", "usageType": line.usage_type},
            rationale="使用 Route 53 公共托管区月度官方计费维度。",
            substitution_notice="当前包含 1 个 Hosted Zone 的官方最小单位单价；DNS 查询费用按实际用量计算。",
            usage_lines=[line],
        )


class WafPlugin(_MinimumAssumptionPlugin):
    kind = ServiceKind.WAF
    display_name = "AWS WAF"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        requested_region = requirement.region or default_region
        # WAF attached to CloudFront is priced with AWS' global/edge records.
        # ``global`` is a scope, not an AWS Region code, so it must never be
        # sent to the SSM region directory.  Regional WAF (for ALB/API Gateway)
        # continues to use the normal Region -> Price List location mapping.
        is_global = requested_region.strip().casefold() in {
            "global",
            "aws-global",
            "cloudfront",
            "any",
        }
        region = "Global" if is_global else requested_region
        scope_filter = (
            {"location": "Any"} if is_global else {"regionCode": requested_region}
        )
        web_acls = required_float(requirement.requirements, "web_acls") or 1.0
        rules = required_float(requirement.requirements, "rules") or 1.0
        requests = required_float(requirement.requirements, "requests")
        billed_rules = scoped_amount(
            requirement,
            "rules",
            rules,
            resource_count=web_acls,
        )
        billed_requests = (
            scoped_amount(
                requirement,
                "requests",
                requests,
                resource_count=web_acls,
            )
            if requests is not None
            else None
        )
        acl = _one_matching(
            self.catalog,
            "awswaf",
            {**scope_filter, "group": "Web ACL"},
            lambda attrs: attrs.get("usagetype", "").endswith("-WebACLV2"),
            f"AWS WAF Web ACL ({region})",
        )
        rule = _one_matching(
            self.catalog,
            "awswaf",
            {**scope_filter, "group": "Rule"},
            lambda attrs: attrs.get("usagetype", "").endswith("-RuleV2"),
            f"AWS WAF Rule ({region})",
        )
        request = _one_matching(
            self.catalog,
            "awswaf",
            {**scope_filter, "group": "Request"},
            lambda attrs: attrs.get("usagetype", "").endswith("-RequestV2-Tier0"),
            f"AWS WAF Request ({region})",
        )
        lines = [
            _usage(
                acl,
                key="wafacl",
                amount=web_acls,
                group="waf",
                source_fields=("web_acls",),
            ),
            _usage(
                rule,
                key="wafrule",
                amount=billed_rules,
                group="waf",
                source_fields=("web_acls", "rules"),
            ),
        ]
        if billed_requests is not None:
            lines.append(
                _usage(
                    request,
                    key="wafreq",
                    amount=billed_requests,
                    group="waf",
                    source_fields=("web_acls", "requests"),
                )
            )
        reference_rates = (
            [_reference(request, description="AWS WAF 请求单价")]
            if requests is None
            else []
        )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="WAF Basic Protection",
            architecture=(
                f"{web_acls:g} 个 Web ACL · 每个 {rules:g} 条规则"
                if field_scope(requirement, "rules") == "per_resource"
                else f"{web_acls:g} 个 Web ACL · {rules:g} 条规则"
            ),
            specifications={
                "webACLs": web_acls,
                "rules": billed_rules,
                **(
                    {"rulesPerWebACL": rules}
                    if field_scope(requirement, "rules") == "per_resource"
                    else {}
                ),
                **(
                    {"requests": billed_requests}
                    if billed_requests is not None
                    else {}
                ),
                **(
                    {"requestsPerWebACL": requests}
                    if requests is not None
                    and field_scope(requirement, "requests") == "per_resource"
                    else {}
                ),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="基础防护按 Web ACL、规则和请求三个官方计费维度提交 BCM。",
            substitution_notice=(
                "客户未提供 WAF 请求量；请求费仅展示 AWS 官方单位价，不计入月费合计。"
                if requests is None
                else None
            ),
            usage_lines=lines,
            reference_rates=reference_rates,
        )


class SqsPlugin(_MinimumAssumptionPlugin):
    kind = ServiceKind.SQS
    display_name = "Amazon SQS"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requests = required_float(requirement.requirements, "requests")
        product = _one_matching(
            self.catalog,
            "AWSQueueService",
            {"regionCode": region, "group": "SQS-APIRequest-Tier1"},
            lambda attrs: attrs.get("queueType", "").casefold() == "standard",
            f"SQS Standard 请求 ({region})",
            # ``group`` and ``usagetype`` are catalog labels and have changed
            # over time.  If the narrow query stops returning data, discover
            # the Standard queue product from the region and stable queueType.
            fallback_filters={"regionCode": region},
            fallback_predicate=lambda attrs: (
                attrs.get("queueType", "").casefold() == "standard"
                and "sqs" in (
                    f"{attrs.get('group', '')} "
                    f"{attrs.get('groupDescription', '')}"
                ).casefold()
                and "request" in (
                    f"{attrs.get('group', '')} "
                    f"{attrs.get('groupDescription', '')}"
                ).casefold()
            ),
        )
        line = (
            _usage(
                product,
                key="sqs",
                amount=requests,
                group="sqs",
                source_fields=("requests", "payload_size_kib", "queue_type"),
            )
            if requests is not None
            else None
        )
        reference_rates = (
            [_reference(product, description="SQS Standard API 请求单价")]
            if requests is None
            else []
        )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="SQS Standard",
            architecture=(f"{requests:g} 次标准队列 API 请求" if requests is not None else "未提供请求量，仅展示官方单位参考价"),
            specifications={"queueType": "Standard", **({"requests": requests} if requests is not None else {})},
            official_product={"source": "AWS Price List", "usageType": (line.usage_type if line else reference_rates[0].usage_type)},
            rationale="使用 SQS Standard Tier 1 官方请求计费维度。",
            substitution_notice=("客户未提供 SQS 请求量；仅展示 AWS 官方单位价，不计入月费合计。" if requests is None else None),
            usage_lines=([line] if line else []),
            reference_rates=reference_rates,
        )


class SesPlugin(_MinimumAssumptionPlugin):
    kind = ServiceKind.SES
    display_name = "Amazon SES"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        messages = required_float(requirement.requirements, "outbound_messages")
        product = _one_matching(
            self.catalog,
            "AmazonSES",
            {"regionCode": region, "operation": "Send"},
            lambda attrs: attrs.get("usagetype", "").endswith("-Recipients"),
            f"SES 出站邮件 ({region})",
        )
        line = (
            _usage(
                product,
                key="ses",
                amount=messages,
                group="ses",
                source_fields=("outbound_messages",),
            )
            if messages is not None
            else None
        )
        reference_rates = (
            [_reference(product, description="SES 出站邮件单价")]
            if messages is None
            else []
        )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="SES Outbound Email",
            architecture=(f"{messages:g} 封普通出站邮件" if messages is not None else "未提供邮件量，仅展示官方单位参考价"),
            specifications=({"outboundMessages": messages} if messages is not None else {}),
            official_product={"source": "AWS Price List", "usageType": (line.usage_type if line else reference_rates[0].usage_type)},
            rationale="使用 SES 每收件人出站邮件官方计费维度。",
            substitution_notice=("客户未提供邮件发送量；仅展示 AWS 官方单位价，不计入月费合计。" if messages is None else None),
            usage_lines=([line] if line else []),
            reference_rates=reference_rates,
        )


class CloudWatchPlugin(_MinimumAssumptionPlugin):
    kind = ServiceKind.CLOUDWATCH
    display_name = "Amazon CloudWatch"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = requirement.requirements
        include_logs = requested.get("include_logs") is not False
        include_metrics = requested.get("include_metrics") is not False
        lines: list[UsageLine] = []
        reference_rates: list[ReferenceRate] = []
        specs: dict[str, float] = {}
        if include_logs:
            logs = required_float(requested, "log_ingestion_gib")
            product = _one_matching(
                self.catalog,
                "AmazonCloudWatch",
                {"regionCode": region, "group": "Ingested Logs"},
                lambda attrs: attrs.get("usagetype", "").endswith("-DataProcessing-Bytes")
                and attrs.get("operation") == "PutLogEvents",
                f"CloudWatch Logs 写入 ({region})",
            )
            if logs is None:
                reference_rates.append(_reference(product, description="CloudWatch Logs 写入单价"))
            else:
                lines.append(
                    _usage(
                        product,
                        key="cwlog",
                        amount=logs,
                        group="cloudwatch",
                        source_fields=("log_ingestion_gib", "include_logs"),
                    )
                )
                specs["logIngestionGiB"] = logs
        if include_metrics:
            metrics = required_float(requested, "custom_metrics")
            product = PricingCatalog.require_unique(
                self.catalog.products(
                    "AmazonCloudWatch", {"regionCode": region, "group": "Metric"}, max_pages=2
                ),
                context=f"CloudWatch 自定义指标 ({region})",
            )
            if metrics is None:
                reference_rates.append(_reference(product, description="CloudWatch 自定义指标单价"))
            else:
                lines.append(
                    _usage(
                        product,
                        key="cwmet",
                        amount=metrics,
                        group="cloudwatch",
                        source_fields=("custom_metrics", "include_metrics"),
                    )
                )
                specs["customMetrics"] = metrics
        if not lines and not reference_rates:
            raise ManualConfirmationRequired(
                "CloudWatch 日志和指标均被关闭，无法形成报价",
                code="empty_cloudwatch_requirement",
            )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="CloudWatch Logs & Metrics",
            architecture=("按客户提供的日志与指标用量计费" if lines else "未提供用量，仅展示官方单位参考价"),
            specifications=specs,
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="按 CloudWatch Logs 写入和自定义指标官方计费维度提交 BCM。",
            substitution_notice=(
                "客户未提供 CloudWatch 用量；仅展示 AWS 官方单位价，不计入月费合计。"
                if reference_rates
                else None
            ),
            usage_lines=lines,
            reference_rates=reference_rates,
        )
