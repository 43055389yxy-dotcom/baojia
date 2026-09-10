from app.domain.billing_rules import (
    constant,
    divide,
    maximum,
    number,
    product,
    reconcile,
    subtract,
)
from app.integrations.aws_component_templates.base import (
    BillingOutput,
    ComponentTemplateSpec,
    OfficialSource,
    TemplateField,
)

ALB_HOURS = product(number("quantity", minimum=1, integer=True), number("hours_per_month"))
ALB_METRIC_LCU = maximum(
    divide(number("new_connections_per_second"), constant(25)),
    divide(
        reconcile(
            number("active_connections_per_minute"),
            product(
                number("new_connections_per_second"), number("average_connection_duration_seconds")
            ),
        ),
        constant(3000),
    ),
    reconcile(
        number("processed_bytes_ec2_ip_gib_per_hour"),
        divide(
            number("processed_bytes_gib_per_load_balancer"), number("hours_per_month", minimum=1)
        ),
    ),
    divide(
        reconcile(
            number("rule_evaluations_per_second"),
            product(
                number("requests_per_second"),
                maximum(
                    subtract(number("rule_evaluations_per_request"), constant(10)), constant(0)
                ),
            ).when_complete("requests_per_second", "rule_evaluations_per_request"),
        ),
        constant(1000),
    ),
)

TEMPLATE = ComponentTemplateSpec(
    service_key="elb",
    display_name="Application Load Balancer（ALB）",
    aliases=("alb", "application_load_balancer", "elbv2", "elasticloadbalancingv2"),
    primary_variants=("application",),
    official_sources=(
        OfficialSource(
            "Elastic Load Balancing pricing",
            "https://aws.amazon.com/elasticloadbalancing/pricing/",
            "ALB 小时与 LCU 计费",
        ),
        OfficialSource(
            "Elastic Load Balancing FAQs",
            "https://aws.amazon.com/elasticloadbalancing/faqs/",
            "LCU 的新连接、活动连接、处理字节与规则评估维度",
        ),
        OfficialSource(
            "Application Load Balancer CloudWatch metrics",
            "https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html",
            "连接数、处理字节、请求与规则评估指标",
        ),
    ),
    fields=(
        TemplateField(
            "load_balancer_type",
            "string",
            "负载均衡器产品类型",
            allowed_values=("application", "network", "gateway"),
        ),
        TemplateField(
            "scheme", "string", "面向互联网或内部", allowed_values=("internet_facing", "internal")
        ),
        TemplateField(
            "processed_bytes_gib",
            "number",
            "当前 ALB 组件全部负载均衡器每月合计处理的数据；客户未说每个时使用此字段",
            role="usage",
            unit="GiB/component/month",
        ),
        TemplateField(
            "processed_bytes_gib_per_load_balancer",
            "number",
            "客户明确说明每个 ALB 每月处理的数据",
            role="usage",
            unit="GiB/load-balancer/month",
        ),
        TemplateField(
            "processed_bytes_ec2_ip_gib_per_hour",
            "number",
            "每个 ALB 每小时经 EC2/IP 目标处理的数据",
            role="usage",
            unit="GiB/hour",
        ),
        TemplateField(
            "new_connections_per_second",
            "number",
            "每个 ALB 平均每秒新建连接数",
            role="usage",
            unit="connections/second",
        ),
        TemplateField(
            "average_connection_duration_seconds",
            "number",
            "平均连接持续时间",
            role="usage",
            unit="seconds",
        ),
        TemplateField(
            "active_connections_per_minute",
            "number",
            "每个 ALB 每分钟活动连接数",
            role="usage",
            unit="connections/minute",
        ),
        TemplateField(
            "requests_per_second",
            "number",
            "每个 ALB 平均每秒请求数",
            role="usage",
            unit="requests/second",
        ),
        TemplateField(
            "rule_evaluations_per_request",
            "number",
            "每个请求评估的计费规则数",
            role="usage",
            unit="rules/request",
        ),
        TemplateField(
            "rule_evaluations_per_second",
            "number",
            "每个 ALB 每秒规则评估数",
            role="usage",
            unit="evaluations/second",
        ),
        TemplateField(
            "lcu_count",
            "number",
            "客户直接给出的每个 ALB 平均 LCU",
            role="usage",
            unit="LCU/load-balancer/hour",
        ),
        TemplateField("listeners", "integer", "监听器数量；配置展示字段", role="context"),
        TemplateField(
            "requests",
            "number",
            "每月总请求数；不能替代 LCU 所需的每秒业务量",
            role="context",
            unit="requests/month",
        ),
    ),
    billing_outputs=(
        BillingOutput(
            "load_balancer_hours",
            "ALB Hours",
            ("quantity", "hours_per_month", "load_balancer_type"),
            "quantity × hours_per_month",
            calculation=ALB_HOURS,
            unit="Hours",
        ),
        BillingOutput(
            "lcu_hours_direct",
            "Application LCU-Hours",
            ("quantity", "hours_per_month", "lcu_count"),
            "quantity × hours_per_month × lcu_count",
            calculation=product(ALB_HOURS, number("lcu_count")),
            unit="LCU-Hrs",
        ),
        BillingOutput(
            "lcu_hours_component_total_bytes",
            "Application LCU-Hours",
            ("processed_bytes_gib",),
            "组件月合计 GiB ÷ 1 GiB/LCU-hour",
            calculation=number("processed_bytes_gib"),
            unit="LCU-Hrs",
        ),
        BillingOutput(
            "lcu_hours_per_load_balancer_bytes",
            "Application LCU-Hours",
            ("quantity", "processed_bytes_gib_per_load_balancer"),
            "quantity × 每个 ALB 月处理 GiB ÷ 1 GiB/LCU-hour",
            calculation=product(
                number("quantity", minimum=1, integer=True),
                number("processed_bytes_gib_per_load_balancer"),
            ),
            unit="LCU-Hrs",
        ),
        BillingOutput(
            "lcu_hours_calculated_metrics",
            "Application LCU-Hours",
            (
                "quantity",
                "hours_per_month",
                "processed_bytes_ec2_ip_gib_per_hour",
                "processed_bytes_gib_per_load_balancer",
                "new_connections_per_second",
                "average_connection_duration_seconds",
                "active_connections_per_minute",
                "requests_per_second",
                "rule_evaluations_per_request",
                "rule_evaluations_per_second",
            ),
            "按 AWS 四个每 ALB 的 LCU 维度分别计算，取最大值，再乘 quantity 与 hours_per_month",
            calculation=product(ALB_HOURS, ALB_METRIC_LCU),
            unit="LCU-Hrs",
        ),
    ),
    non_pricing_fields=frozenset({"listeners", "requests"}),
    critical_rule=(
        "ALB 写 load_balancer_type=application。客户只说整组月流量时填 processed_bytes_gib；"
        "只有明确说每个 ALB 时才填 processed_bytes_gib_per_load_balancer。"
        "lcu_count 表示每个 ALB 的平均 LCU；"
        "连接、流量和规则指标必须分开填，不能相加。"
    ),
    guidance="""
本轮模板只把 ALB 作为主目标；保留 network/gateway 枚举仅为兼容既有报价。ALB 月费由每个负载均衡器
小时费与 LCU 小时费构成。“2 个 ALB，每月处理 3 TiB”表示组件合计，填 processed_bytes_gib=3072；
只有“每个 ALB 每月 3 TiB”才填 processed_bytes_gib_per_load_balancer=3072。客户直接给“每个 ALB
平均 5 LCU”时只填 lcu_count=5；不要再从请求数反推连接或规则维度。客户给底层指标时，四类维度
必须分别保留，最终 LCU 取最大值而不是求和。
service 必须写 elb，绝不能写 elbv2 或 elasticloadbalancingv2。
""",
    example_customer_text="东京 2 个 ALB，每个每月运行 730 小时，平均使用 5 个 LCU。",
)
