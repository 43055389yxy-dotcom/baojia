from app.domain.billing_rules import number
from app.integrations.aws_component_templates.base import (
    BillingOutput,
    ComponentTemplateSpec,
    OfficialSource,
    TemplateField,
)

TEMPLATE = ComponentTemplateSpec(
    service_key="s3",
    display_name="Amazon S3 对象存储",
    aliases=("amazon_s3", "simple_storage_service", "object_storage"),
    primary_variants=("standard",),
    official_sources=(
        OfficialSource(
            "Amazon S3 pricing",
            "https://aws.amazon.com/s3/pricing/",
            "存储、请求、取回与数据传输计费",
        ),
        OfficialSource(
            "Amazon S3 storage classes",
            "https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html",
            "Standard、IA、Intelligent-Tiering 与 Express One Zone 存储级别",
        ),
        OfficialSource(
            "Amazon S3 request metrics",
            "https://docs.aws.amazon.com/AmazonS3/latest/userguide/metrics-dimensions.html",
            "PUT/GET 等请求分类",
        ),
    ),
    fields=(
        TemplateField(
            "storage_gib", "number", "月平均对象存储容量", role="usage", unit="GiB-month"
        ),
        TemplateField(
            "storage_class",
            "string",
            "S3 存储级别",
            allowed_values=(
                "standard",
                "standard_ia",
                "one_zone_ia",
                "intelligent_tiering",
                "express_one_zone",
            ),
        ),
        TemplateField(
            "put_copy_post_list_requests",
            "number",
            "PUT、COPY、POST、LIST 类请求总数",
            role="usage",
            unit="requests/month",
        ),
        TemplateField(
            "get_select_requests",
            "number",
            "GET、SELECT 及其他读取类请求总数",
            role="usage",
            unit="requests/month",
        ),
        TemplateField(
            "data_retrieval_gib",
            "number",
            "需要单独收费的对象取回量",
            role="usage",
            unit="GiB/month",
        ),
        TemplateField(
            "data_transfer_out_gib",
            "number",
            "从 S3 传向公网的数据量",
            role="usage",
            unit="GiB/month",
        ),
    ),
    billing_outputs=(
        BillingOutput(
            "storage",
            "S3 GB-Month",
            ("storage_gib", "storage_class", "quantity"),
            "storage_gib",
            calculation=number("storage_gib"),
            unit="GB-Mo",
            scope_field="storage_gib",
        ),
        BillingOutput(
            "write_requests",
            "S3 Tier 1 Requests",
            ("put_copy_post_list_requests", "storage_class", "quantity"),
            "put_copy_post_list_requests",
            calculation=number("put_copy_post_list_requests", integer=True),
            unit="Requests",
            scope_field="put_copy_post_list_requests",
        ),
        BillingOutput(
            "read_requests",
            "S3 Tier 2 Requests",
            ("get_select_requests", "storage_class", "quantity"),
            "get_select_requests",
            calculation=number("get_select_requests", integer=True),
            unit="Requests",
            scope_field="get_select_requests",
        ),
        BillingOutput(
            "retrieval",
            "S3 Data Retrieval GB",
            ("data_retrieval_gib", "storage_class"),
            "data_retrieval_gib；Standard 无此费用时不得伪造收费行",
        ),
        BillingOutput(
            "internet_transfer_out",
            "Data Transfer Out GB",
            ("data_transfer_out_gib", "quantity"),
            "data_transfer_out_gib",
            calculation=number("data_transfer_out_gib"),
            unit="GB",
            scope_field="data_transfer_out_gib",
        ),
    ),
    guidance="""
本轮优先支持 S3 Standard。容量、写请求、读请求、取回量和公网出站流量必须分别填写，不能把请求量
当对象数或把流量当容量。12 TiB 换算为 12288 GiB。S3 Standard 没有数据取回费；客户仍明确提供
取回量时保留事实，但计价程序必须按存储级别决定是否生成费用。
""",
    critical_rule=(
        "容量写 storage_gib；PUT/COPY/POST/LIST 与 GET/SELECT 请求分开；"
        "公网出站写 data_transfer_out_gib。按量但未给用量时保持 null。"
    ),
    example_customer_text=(
        "东京 S3 Standard 12 TiB，每月 500 万次 PUT、8000 万次 GET，公网出站 2 TiB。"
    ),
)
