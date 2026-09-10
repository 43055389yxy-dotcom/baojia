from copy import deepcopy
import json

import pytest

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.models import ParsedIntent, ServiceRequirement
from app.integrations.aws_calculator_contracts import CalculatorContractNotFound, CalculatorContractUnavailable
from app.integrations.deepseek import DeepSeekIntentParser


class MissingForms:
    def get_contract(self, service):
        raise CalculatorContractNotFound(service)

    def search(self, query, *, limit=30):
        return []


class Profiles:
    def __init__(self):
        self.profile = {
            "status": "verified", "service_key": "sample_meter",
            "service_code": "AWSSampleMeter", "region": "ap-northeast-1",
            "profile_schema_version": 22,
            "dimensions": [{"usage_type": "Meter-Hours", "operation": "Run",
                            "unit": "Hrs", "price": 0.025}],
            "field_bindings": [{"field": "hours_per_month", "usage_type": "Meter-Hours",
                                "operation": "Run", "unit": "Hrs"}],
        }

    def ensure_profile(self, **kwargs):
        return deepcopy(self.profile)


def setup_parser():
    profiles = Profiles()
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid"),
        calculator_contract_catalog=MissingForms(), auto_discovery=profiles,
    )

    async def keep_identity(*args, **kwargs):
        pass

    parser._resolve_unknown_component_service = keep_identity
    component = ServiceRequirement(service="sample_meter", calculator_service_name="AWS Sample Meter",
                                   region="ap-northeast-1", requirements={"resource_count": 2})
    return parser, profiles, ParsedIntent(customer_summary="test", services=[component])


@pytest.mark.asyncio
async def test_missing_form_uses_verified_official_price_list_without_faking_a_form():
    parser, profiles, intent = setup_parser()
    assert await parser._prepare_official_calculator_intake(intent, reporter=None) is False
    item = intent.services[0]
    assert item.official_calculator_service_code is None
    assert item.field_sources["_official_calculator_status"] == "price_list_ready"
    assert item.requirements == {"resource_count": 2}
    reference = json.loads(item.field_sources["_official_price_list_intake_contract"])
    assert reference["schema_hash"]
    assert reference["billing_schema_hash"]
    assert "meters" not in reference  # Do not copy a whole price catalog into every draft/event.
    parser.validate_official_calculator_configurations(intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["failed", "empty", "region", "identity", "bindings"])
async def test_unverified_or_wrong_profile_cannot_bypass_missing_form(change):
    parser, profiles, intent = setup_parser()
    if change == "failed": profiles.profile["status"] = "failed"
    if change == "empty": profiles.profile["dimensions"] = []
    if change == "region": profiles.profile["region"] = "us-east-1"
    if change == "identity": profiles.profile["service_key"] = "other"
    if change == "bindings": profiles.profile["field_bindings"][0]["usage_type"] = "Invented"
    with pytest.raises(ManualConfirmationRequired):
        await parser._prepare_official_calculator_intake(intent, reporter=None)


@pytest.mark.asyncio
async def test_profile_identity_is_revalidated_before_final_pricing():
    parser, profiles, intent = setup_parser()
    await parser._prepare_official_calculator_intake(intent, reporter=None)
    profiles.profile["service_code"] = "OtherProduct"
    with pytest.raises(ManualConfirmationRequired):
        parser.validate_official_calculator_configurations(intent)


@pytest.mark.asyncio
async def test_network_failure_is_not_treated_as_confirmed_missing_form():
    parser, profiles, intent = setup_parser()

    class Offline(MissingForms):
        def get_contract(self, service):
            raise CalculatorContractUnavailable("offline")

    from app.integrations.aws_calculator_intake import OfficialCalculatorIntakeResolver
    parser._calculator_intake = OfficialCalculatorIntakeResolver(Offline())
    with pytest.raises(ManualConfirmationRequired):
        await parser._prepare_official_calculator_intake(intent, reporter=None)
    assert intent.services[0].field_sources.get("_official_calculator_status") != "price_list_ready"


@pytest.mark.asyncio
async def test_published_form_with_missing_definition_cannot_use_price_list_fallback():
    from app.integrations.aws_calculator_contracts import CalculatorServiceSummary
    from app.integrations.aws_calculator_intake import OfficialCalculatorIntakeResolver

    class BrokenForm(MissingForms):
        def search(self, query, *, limit=30):
            return [CalculatorServiceSummary(service_code="sampleMeter", name="AWS Sample Meter",
                                             definition_url="https://example.invalid/form.json")]

    parser, _, intent = setup_parser()
    parser._calculator_intake = OfficialCalculatorIntakeResolver(BrokenForm())
    with pytest.raises(ManualConfirmationRequired):
        await parser._prepare_official_calculator_intake(intent, reporter=None)
    assert intent.services[0].field_sources.get("_official_calculator_status") != "price_list_ready"


@pytest.mark.asyncio
async def test_cleaning_region_change_rebinds_only_an_equivalent_official_contract():
    parser, profiles, intent = setup_parser()
    await parser._prepare_official_calculator_intake(intent, reporter=None)
    intent.services[0].region = "global"
    profiles.profile["region"] = "global"
    await parser._rebind_price_list_intake_regions(intent)
    parser.validate_official_calculator_configurations(intent)


@pytest.mark.asyncio
async def test_cleaning_region_change_cannot_rebind_different_meters():
    parser, profiles, intent = setup_parser()
    await parser._prepare_official_calculator_intake(intent, reporter=None)
    intent.services[0].region = "global"
    profiles.profile["region"] = "global"
    profiles.profile["dimensions"][0]["description"] = "different billing meaning"
    with pytest.raises(ManualConfirmationRequired):
        await parser._rebind_price_list_intake_regions(intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, "", "global", "Global", " GLOBAL "])
@pytest.mark.parametrize("cached", [None, "global", "Global"])
async def test_catalog_scope_aliases_survive_intake_cache_and_final_validation(requested, cached):
    parser, profiles, intent = setup_parser()
    component = intent.services[0]
    component.region = requested
    profiles.profile["region"] = cached
    original = component.model_dump()
    await parser._prepare_official_calculator_intake(intent, reporter=None)
    parser.validate_official_calculator_configurations(intent)
    reference = json.loads(component.field_sources["_official_price_list_intake_contract"])
    assert reference["region"] == "global"
    assert component.region == original["region"]  # Catalog scope is not a customer region default.
    assert component.requirements == original["requirements"]
    component.region = "Global"
    await parser._rebind_price_list_intake_regions(intent)
    parser.validate_official_calculator_configurations(intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,cached", [(None,"us-east-1"), ("global","us-east-1"),
                                             ("us-east-1","global"), ("us-east-1","us-west-2")])
async def test_distinct_catalog_scopes_remain_blocked(requested, cached):
    parser, profiles, intent = setup_parser()
    intent.services[0].region = requested
    profiles.profile["region"] = cached
    with pytest.raises(ManualConfirmationRequired):
        await parser._prepare_official_calculator_intake(intent, reporter=None)


def test_region_case_does_not_change_official_contract_fingerprint():
    from app.integrations.official_price_list_intake import verified_price_list_contract
    _, profiles, intent = setup_parser()
    first = verified_price_list_contract(intent.services[0], profiles.profile)
    intent.services[0].region = " AP-NORTHEAST-1 "
    assert verified_price_list_contract(intent.services[0], profiles.profile) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_region", [None, "Global", " GLOBAL "])
async def test_legacy_reference_can_upgrade_only_equivalent_region_spelling(legacy_region):
    import hashlib
    parser, profiles, intent = setup_parser()
    component = intent.services[0]
    component.region = "global"
    profiles.profile["region"] = "global"
    await parser._prepare_official_calculator_intake(intent, reporter=None)
    reference = json.loads(component.field_sources["_official_price_list_intake_contract"])
    # Reconstruct the previous unversioned fingerprint, not a fabricated new signature.
    payload = {key: reference[key] for key in ("source", "service_code", "service_key", "region", "profile_schema_version")}
    payload["region"] = legacy_region
    payload["meters"] = [{key: profiles.profile["dimensions"][0].get(key) or "" for key in
                          ("usage_type", "operation", "unit", "description", "instance_type")}]
    payload["field_bindings"] = profiles.profile["field_bindings"]
    reference.pop("contract_version", None)
    reference["region"] = legacy_region
    reference["schema_hash"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    component.field_sources["_official_price_list_intake_contract"] = json.dumps(reference)
    parser.validate_official_calculator_configurations(intent)
    migrated = json.loads(component.field_sources["_official_price_list_intake_contract"])
    assert migrated["region"] == "global"
    assert migrated["contract_version"] == 2
    # A genuine drift is still rejected, even if the legacy region spelling matches.
    component.field_sources["_official_price_list_intake_contract"] = json.dumps(reference)
    profiles.profile["dimensions"][0]["description"] = "changed semantics"
    with pytest.raises(ManualConfirmationRequired):
        parser.validate_official_calculator_configurations(intent)
