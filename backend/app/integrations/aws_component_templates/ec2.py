from app.domain.billing_rules import constant, divide, each, number, product, reconcile
from app.integrations.aws_component_templates.base import (
    BillingOutput,
    ComponentTemplateSpec,
    OfficialSource,
    TemplateField,
)

TEMPLATE = ComponentTemplateSpec(
    service_key="ec2",
    display_name="Amazon EC2 云服务器",
    aliases=("amazon_ec2", "elastic_compute_cloud"),
    primary_variants=("linux", "windows", "arm64", "x86_64"),
    official_sources=(
        OfficialSource(
            "Amazon EC2 启动实例参数",
            "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-launch-parameters.html",
            "实例类型、操作系统映像、存储、网络与购买配置",
        ),
        OfficialSource(
            "Amazon EC2 On-Demand Pricing",
            "https://aws.amazon.com/ec2/pricing/on-demand/",
            "实例小时、操作系统、EBS 与数据传输计费边界",
        ),
        OfficialSource(
            "Amazon EBS volume types",
            "https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volume-types.html",
            "卷类型、容量、IOPS 与吞吐量",
        ),
    ),
    fields=(
        TemplateField("requested_model", "string", "客户明确指定的 EC2 实例类型，例如 c7g.large"),
        TemplateField("vcpu", "number", "客户要求的最低 vCPU", unit="vCPU"),
        TemplateField("memory_gib", "number", "客户要求的最低内存", unit="GiB"),
        TemplateField(
            "operating_system",
            "string",
            "AWS EC2 计价操作系统",
            allowed_values=("linux", "windows", "rhel", "suse"),
        ),
        TemplateField("architecture", "string", "处理器架构", allowed_values=("arm64", "x86_64")),
        TemplateField(
            "tenancy",
            "string",
            "实例租用方式",
            allowed_values=("shared", "dedicated_instance", "dedicated_host"),
        ),
        TemplateField("business_type", "string", "选型用途上下文", role="context"),
        TemplateField(
            "system_disk_gib", "number", "每台实例的系统盘容量", role="usage", unit="GiB/resource"
        ),
        TemplateField(
            "total_system_disk_gib",
            "number",
            "本组件全部系统盘总容量",
            role="usage",
            unit="GiB/component",
        ),
        TemplateField(
            "volume_type",
            "string",
            "EBS 卷类型",
            allowed_values=("gp3", "gp2", "io1", "io2", "st1", "sc1", "standard"),
        ),
        TemplateField("ebs_iops", "number", "每卷预置 IOPS", role="usage", unit="IOPS/volume"),
        TemplateField(
            "ebs_throughput_mbps", "number", "每卷预置吞吐量", role="usage", unit="MiB/s/volume"
        ),
        TemplateField(
            "additional_ebs_volumes",
            "array<object>",
            "额外数据盘；对象字段为 size_gib、volume_type、count_per_instance、"
            "iops、throughput_mbps",
            role="usage",
        ),
        TemplateField(
            "purchase_option",
            "string",
            "购买方式",
            allowed_values=(
                "on_demand",
                "spot",
                "standard_reserved",
                "convertible_reserved",
                "compute_savings_plan",
                "ec2_instance_savings_plan",
            ),
        ),
        TemplateField("reserved_term_years", "integer", "预留期限", allowed_values=("1", "3")),
        TemplateField(
            "payment_option",
            "string",
            "预留付款方式",
            allowed_values=("no_upfront", "partial_upfront", "all_upfront"),
        ),
        TemplateField(
            "utilization_percent", "number", "月运行时长之外的利用率", role="usage", unit="percent"
        ),
        TemplateField("detailed_monitoring", "boolean", "是否启用 EC2 详细监控"),
        TemplateField("snapshot_frequency", "string", "EBS 快照频率", role="usage"),
        TemplateField(
            "snapshot_changed_gib",
            "number",
            "每次快照变化数据量",
            role="usage",
            unit="GiB/snapshot",
        ),
        TemplateField(
            "snapshot_retention_days", "integer", "快照保留天数", role="usage", unit="days"
        ),
        TemplateField(
            "data_transfer_in_gib", "number", "组件合计公网入站流量", role="usage", unit="GiB/month"
        ),
        TemplateField(
            "data_transfer_regional_gib",
            "number",
            "组件合计区域内跨可用区流量",
            role="usage",
            unit="GiB/month",
        ),
        TemplateField(
            "data_transfer_out_gib",
            "number",
            "组件合计公网出站流量",
            role="usage",
            unit="GiB/month",
        ),
        TemplateField(
            "data_transfer_in_gib_per_instance",
            "number",
            "每台公网入站流量",
            role="usage",
            unit="GiB/resource/month",
        ),
        TemplateField(
            "data_transfer_regional_gib_per_instance",
            "number",
            "每台区域内跨可用区流量",
            role="usage",
            unit="GiB/resource/month",
        ),
        TemplateField(
            "data_transfer_out_gib_per_instance",
            "number",
            "每台公网出站流量",
            role="usage",
            unit="GiB/resource/month",
        ),
        TemplateField("purpose", "string", "客户描述的服务器用途", role="context"),
    ),
    billing_outputs=(
        BillingOutput(
            "instance_hours",
            "EC2 Instance Hours",
            (
                "quantity",
                "hours_per_month",
                "requested_model",
                "vcpu",
                "memory_gib",
                "operating_system",
                "architecture",
                "tenancy",
                "purchase_option",
                "utilization_percent",
            ),
            "quantity × hours_per_month × utilization_percent / 100",
            calculation=product(
                number("quantity", minimum=1, integer=True),
                number("hours_per_month"),
                divide(number("utilization_percent", default=100, maximum=100), constant(100)),
            ),
            unit="Hours",
        ),
        BillingOutput(
            "system_ebs_gib_month",
            "EBS GB-Month",
            (
                "quantity",
                "system_disk_gib",
                "total_system_disk_gib",
                "volume_type",
                "ebs_iops",
                "ebs_throughput_mbps",
            ),
            "system_disk_gib × quantity，或客户明确的 total_system_disk_gib",
            calculation=reconcile(
                number("total_system_disk_gib"),
                product(number("system_disk_gib"), number("quantity", minimum=1, integer=True)),
            ),
            unit="GB-Mo",
        ),
        BillingOutput(
            "additional_ebs_gib_month",
            "EBS GB-Month",
            ("quantity", "additional_ebs_volumes"),
            "Σ(size_gib × count_per_instance × quantity)",
            calculation=each(
                "additional_ebs_volumes",
                product(
                    number("size_gib", minimum=1),
                    number("count_per_instance", default=1, minimum=1, integer=True),
                    number("quantity", minimum=1, integer=True),
                ),
                local_fields=("size_gib", "count_per_instance"),
            ),
            unit="GB-Mo",
        ),
        BillingOutput(
            "internet_data_transfer_out",
            "Data Transfer Out GB",
            ("quantity", "data_transfer_out_gib", "data_transfer_out_gib_per_instance"),
            "总量，或 per_instance × quantity",
            calculation=reconcile(
                number("data_transfer_out_gib"),
                product(
                    number("data_transfer_out_gib_per_instance"),
                    number("quantity", minimum=1, integer=True),
                ),
            ),
            unit="GB",
        ),
        BillingOutput(
            "regional_data_transfer",
            "Regional Data Transfer GB",
            ("quantity", "data_transfer_regional_gib", "data_transfer_regional_gib_per_instance"),
            "总量，或 per_instance × quantity",
            calculation=reconcile(
                number("data_transfer_regional_gib"),
                product(
                    number("data_transfer_regional_gib_per_instance"),
                    number("quantity", minimum=1, integer=True),
                ),
            ),
            unit="GB",
        ),
        BillingOutput(
            "ebs_snapshot_storage",
            "EBS Snapshot GB-Month",
            ("quantity", "snapshot_frequency", "snapshot_changed_gib", "snapshot_retention_days"),
            "由变化量、频率与保留期生成；缺任何必要客户事实时不猜量",
        ),
    ),
    safe_defaults={"operating_system": "Linux", "detailed_monitoring": False, "volume_type": "gp3"},
    # operating_system_version is a legacy/context input that is deliberately
    # accepted only for removal; it is not exposed as an AI pricing field.
    non_pricing_fields=frozenset({"operating_system_version", "business_type", "purpose"}),
    critical_rule=(
        "型号写 requested_model；CPU、内存、系统盘、数据盘、操作系统、架构与数量分别填写，"
        "不能互相替代。Arm/Graviton 归一为 arm64，Intel/AMD 归一为 x86_64。"
    ),
    guidance="""
普通 Ubuntu、Amazon Linux、CentOS 归一为 linux。客户指定 c7g/m7g 等 Graviton 型号时保留型号和
architecture=arm64；指定 m7i/c7i 等 Intel 型号时保留 architecture=x86_64。额外数据盘写成
additional_ebs_volumes 对象数组。“每台”流量写 *_per_instance，“合计”流量写总量字段。
紧凑写法“m6i.xlarge (4C16G) + gp3 500GB”中，16G 是内存，500GB 才是磁盘。
客户只写 Nacos、XXL-JOB、应用服务器等 EC2 承载工作负载且完全没给运行规格时，只允许调用公共的
最低可运行配置规则补下限，仍不得虚构 requested_model。
""",
    example_customer_text=(
        "东京 3 台 c7g.large ARM64 Linux，每台 100 GiB gp3，全天运行，每月公网出站 2 TiB。"
    ),
)
