from app.domain.billing_rules import number, product
from app.integrations.aws_component_templates.base import (
    BillingOutput,
    ComponentTemplateSpec,
    OfficialSource,
    TemplateField,
)

TEMPLATE = ComponentTemplateSpec(
    service_key="rds",
    display_name="Amazon RDS（MySQL / PostgreSQL）",
    aliases=("amazon_rds", "rds_mysql", "rds_postgresql"),
    primary_variants=("mysql", "postgresql"),
    official_sources=(
        OfficialSource(
            "Amazon RDS for MySQL pricing",
            "https://aws.amazon.com/rds/mysql/pricing/",
            "数据库实例、存储、预置 IOPS、备份与传输计费",
        ),
        OfficialSource(
            "Amazon RDS for PostgreSQL pricing",
            "https://aws.amazon.com/rds/postgresql/pricing/",
            "PostgreSQL 数据库实例与存储计费",
        ),
        OfficialSource(
            "Amazon RDS storage types",
            "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Storage.html",
            "gp2、gp3、io1、io2 与磁存储参数",
        ),
        OfficialSource(
            "RDS DB instance classes",
            "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.html",
            "DB 实例类型、vCPU 与内存",
        ),
    ),
    fields=(
        TemplateField("requested_model", "string", "客户指定的 DB 实例类型，例如 db.r7g.large"),
        TemplateField(
            "engine",
            "string",
            "数据库引擎",
            allowed_values=(
                "mysql",
                "postgresql",
                "mariadb",
                "sql_server_standard",
                "sql_server_web",
                "sql_server_enterprise",
                "oracle",
                "db2",
                "aurora_mysql",
                "aurora_postgresql",
            ),
        ),
        TemplateField("engine_version", "string", "客户明确指定的数据库版本"),
        TemplateField("vcpu", "number", "客户要求的最低 vCPU", unit="vCPU"),
        TemplateField("memory_gib", "number", "客户要求的最低内存", unit="GiB"),
        TemplateField(
            "deployment",
            "string",
            "可用性部署",
            allowed_values=("single_az", "multi_az", "multi_az_cluster"),
        ),
        TemplateField(
            "storage_gib",
            "number",
            "每个正式数据库资源的分配存储",
            role="usage",
            unit="GiB/resource",
        ),
        TemplateField(
            "storage_type",
            "string",
            "RDS 存储类型",
            allowed_values=("gp2", "gp3", "io1", "io2", "magnetic"),
        ),
        TemplateField(
            "storage_iops", "number", "预置存储 IOPS", role="usage", unit="IOPS/resource"
        ),
        TemplateField(
            "storage_throughput_mbps",
            "number",
            "gp3 存储吞吐量",
            role="usage",
            unit="MiB/s/resource",
        ),
        TemplateField(
            "purchase_option", "string", "购买方式", allowed_values=("on_demand", "reserved")
        ),
        TemplateField("reserved_term_years", "integer", "预留期限", allowed_values=("1", "3")),
        TemplateField(
            "payment_option",
            "string",
            "预留付款方式",
            allowed_values=("no_upfront", "partial_upfront", "all_upfront"),
        ),
        TemplateField("utilization_percent", "number", "运行利用率", role="usage", unit="percent"),
        TemplateField("license_model", "string", "适用于商业数据库的许可证模式"),
        TemplateField(
            "backup_retention_days", "integer", "自动备份保留天数", role="usage", unit="days"
        ),
        TemplateField(
            "read_replica_count",
            "integer",
            "只读副本数量",
            role="usage",
            unit="replicas/deployment",
        ),
        TemplateField("performance_insights", "boolean", "是否启用 Performance Insights"),
        TemplateField("enhanced_monitoring", "boolean", "是否启用增强监控"),
        TemplateField("aurora_cluster", "boolean", "兼容旧流程的 Aurora 产品身份标记"),
        TemplateField(
            "cluster_members",
            "integer",
            "Aurora 集群内数据库实例数",
            role="usage",
            unit="instances/cluster",
        ),
        TemplateField(
            "instance_count",
            "integer",
            "每套普通 RDS 部署的计费实例数",
            role="usage",
            unit="instances/deployment",
        ),
    ),
    billing_outputs=(
        BillingOutput(
            "db_instance_hours",
            "RDS DB Instance Hours",
            (
                "quantity",
                "hours_per_month",
                "requested_model",
                "engine",
                "engine_version",
                "vcpu",
                "memory_gib",
                "deployment",
                "instance_count",
                "read_replica_count",
                "purchase_option",
                "utilization_percent",
            ),
            "普通 RDS：quantity × hours_per_month；Multi-AZ 单价已包含主备，不能重复乘节点数",
            calculation=product(
                number("quantity", minimum=1, integer=True), number("hours_per_month")
            ),
            unit="Hours",
        ),
        BillingOutput(
            "database_storage",
            "RDS GB-Month",
            ("quantity", "storage_gib", "storage_type", "deployment", "instance_count"),
            "storage_gib × 对应部署的正式存储份数",
            calculation=product(number("quantity", minimum=1, integer=True), number("storage_gib")),
            unit="GB-Mo",
        ),
        BillingOutput(
            "provisioned_iops",
            "RDS Provisioned IOPS-Month",
            ("quantity", "storage_iops", "storage_type", "deployment", "instance_count"),
            "storage_iops × 对应存储份数",
        ),
        BillingOutput(
            "provisioned_throughput",
            "RDS Provisioned Throughput-Month",
            ("quantity", "storage_throughput_mbps", "storage_type", "deployment", "instance_count"),
            "storage_throughput_mbps × 对应存储份数",
        ),
        BillingOutput(
            "backup_storage",
            "RDS Backup GB-Month",
            ("quantity", "storage_gib", "backup_retention_days"),
            "仅在可由客户事实确定超出免费配额时生成",
        ),
    ),
    safe_defaults={
        "storage_type": "gp3",
        "performance_insights": False,
        "enhanced_monitoring": False,
    },
    critical_rule=(
        "db.* 写 requested_model；MySQL/PostgreSQL 引擎、Single-AZ/Multi-AZ、"
        "存储、实例数量分别保留，不能从型号反推客户没写的 CPU 或内存。"
    ),
    guidance="""
本轮主要模板覆盖普通 RDS MySQL 与 PostgreSQL。engine 分别写 mysql、postgresql；客户明确写 db.*
型号时必须保留；db.t3.large 只能是 requested_model。deployment 只能为 single_az、multi_az、
multi_az_cluster，分别描述 AWS 的 Single-AZ、Multi-AZ DB instance 或 Multi-AZ DB cluster，
不能把“主备”当成两套 quantity。普通 RDS 的实例小时、存储、预置 IOPS/吞吐和备份是不同计费维度。
兼容历史 Aurora 输入所需字段仍保留，但 Aurora 不是普通 MySQL/PostgreSQL，不能静默互换产品身份。
""",
    example_customer_text=(
        "东京 2 套 RDS PostgreSQL，db.r7g.large，Multi-AZ，每套 200 GiB gp3，按需全天运行。"
    ),
)
