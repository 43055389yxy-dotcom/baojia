"""Regression contract for numbered-component identity and source ownership.

These tests intentionally exercise the shared inventory/reconciliation boundary,
not a service-specific pricing adapter.  A provider offer code may refine product
identity, but it must never change which numbered customer block owns a component.
"""

from app.domain.models import ParsedIntent, ServiceRequirement
from app.integrations.deepseek import DeepSeekIntentParser

ALB_BLOCK = "Application Load Balancer：新加坡 ap-southeast-1，2个。"
NAT_BLOCK = "AWS NAT Gateway：新加坡 ap-southeast-1，2个。"


def _official_awselb_result(source: str) -> ServiceRequirement:
    """Model a provider-resolved AWSELB result bound to one customer block."""

    return ServiceRequirement(
        # Runtime service identifiers are lower-case; the provider offer code
        # is retained separately as official identity evidence.
        service="awselb",
        calculator_service_name="Elastic Load Balancing",
        quantity=2,
        source_text=source,
        original_source_text=source,
        field_sources={"_official_service_code": "AWSELB"},
    )


def _identity_snapshot(parsed: ParsedIntent) -> list[tuple[str, str, str, str]]:
    return [
        (
            DeepSeekIntentParser._service_key(item.service),
            item.original_source_text or item.source_text or "",
            item.component_key or "",
            item.field_sources.get("_source_block_key", ""),
        )
        for item in parsed.services
    ]


def test_awselb_offer_code_uses_the_elb_runtime_contract() -> None:
    """Provider offer codes are identities, not independent runtime templates."""

    assert DeepSeekIntentParser._service_key("AWSELB") == "elb"


def test_official_awselb_misclassification_cannot_rewrite_nat_numbered_owner() -> None:
    """A conflicting official identity cannot rename an unambiguous NAT block."""

    text = f"1、{NAT_BLOCK}"
    parsed = ParsedIntent(
        customer_summary="NAT 网关",
        services=[_official_awselb_result(NAT_BLOCK)],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert len(parsed.services) == 1
    component = parsed.services[0]
    assert component.service == "nat_gateway"
    assert component.original_source_text == NAT_BLOCK
    assert component.quantity == 2
    assert component.component_key.startswith("cmp_sales_")
    assert component.field_sources["_source_block_key"].startswith("src_")


def test_adjacent_alb_and_nat_do_not_cross_sources_or_create_duplicates() -> None:
    """Reversed classifier output must still join by immutable block ownership."""

    text = f"1、{ALB_BLOCK}\n2、{NAT_BLOCK}"
    parsed = ParsedIntent(
        customer_summary="负载均衡和 NAT 网关",
        # Deliberately reverse the provider results and misclassify NAT as
        # AWSELB.  Position is not a valid ownership join key.
        services=[
            _official_awselb_result(NAT_BLOCK),
            _official_awselb_result(ALB_BLOCK),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._merge_duplicate_service_fragments(parsed)

    assert [
        (DeepSeekIntentParser._service_key(item.service), item.original_source_text)
        for item in parsed.services
    ] == [
        ("elb", ALB_BLOCK),
        ("nat_gateway", NAT_BLOCK),
    ]
    assert len({item.component_key for item in parsed.services}) == 2
    assert len({item.field_sources.get("_source_block_key") for item in parsed.services}) == 2


def test_official_heading_restore_never_replaces_an_immutable_similar_block() -> None:
    """Equal field remainders cannot make ALB inherit the adjacent NAT owner."""

    text = f"1、{ALB_BLOCK}\n2、{NAT_BLOCK}"
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)
    assert parsed is not None

    DeepSeekIntentParser._restore_literal_official_headings(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)

    assert [
        (DeepSeekIntentParser._service_key(item.service), item.original_source_text)
        for item in parsed.services
    ] == [("elb", ALB_BLOCK), ("nat_gateway", NAT_BLOCK)]


def test_numbered_inventory_reconciliation_is_idempotent() -> None:
    """A second reconciliation pass cannot add, remove, or re-own components."""

    text = f"1、{ALB_BLOCK}\n2、{NAT_BLOCK}"
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)
    assert parsed is not None

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    first = _identity_snapshot(parsed)
    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert _identity_snapshot(parsed) == first
    assert [item[0] for item in first] == ["elb", "nat_gateway"]


def test_vpc_offer_container_does_not_duplicate_explicit_nat_gateway() -> None:
    source = (
        "Amazon VPC NAT Gateway：NAT Gateway 固定费 + "
        "200GB/月处理流量，数量 2。"
    )

    assert DeepSeekIntentParser._numbered_block_service_identities(source) == [
        ("nat_gateway", "AWS NAT Gateway")
    ]


def test_two_numbered_rows_for_same_service_keep_independent_owners() -> None:
    """Canonical deduplication must not collapse distinct customer components."""

    singapore = "Application Load Balancer：新加坡 ap-southeast-1，2个。"
    tokyo = "Application Load Balancer：东京 ap-northeast-1，1个。"
    text = f"1、{singapore}\n2、{tokyo}"
    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)
    assert parsed is not None

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert len(parsed.services) == 2
    assert [DeepSeekIntentParser._service_key(item.service) for item in parsed.services] == [
        "elb",
        "elb",
    ]
    assert [item.original_source_text for item in parsed.services] == [
        singapore,
        tokyo,
    ]
    assert len({item.component_key for item in parsed.services}) == 2
    assert len({item.field_sources["_source_block_key"] for item in parsed.services}) == 2
