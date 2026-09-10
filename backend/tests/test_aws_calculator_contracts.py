from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.aws_main as aws_main
from app.integrations.aws_calculator_contracts import (
    AwsCalculatorContractCatalog,
    CalculatorFieldContract,
    CalculatorContractNotFound,
    CalculatorServiceContract,
    CalculatorTemplateContract,
)

MANIFEST = {
    "awsServices": [
        {
            "serviceCode": "sampleCompute",
            "name": "Sample Compute",
            "isActive": "true",
            "serviceDefinitionUrlPath": "/data/sampleCompute/en_US.json",
            "searchKeywords": ["virtual server", "instances"],
        },
        {
            "serviceCode": "sampleStorageGroup",
            "name": "Sample Storage",
            "isActive": "true",
            "subType": "subServiceSelector",
            "serviceDefinitionUrlPath": "/data/sampleStorageGroup/en_US.json",
        },
        {
            "serviceCode": "sampleStandard",
            "name": "Sample Standard",
            "isActive": "true",
            "subType": "subService",
            "serviceDefinitionUrlPath": "/data/sampleStandard/en_US.json",
        },
        {
            "serviceCode": "sampleArchive",
            "name": "Sample Archive",
            "isActive": "true",
            "subType": "subService",
            "serviceDefinitionUrlPath": "/data/sampleArchive/en_US.json",
        },
        {
            "serviceCode": "retiredService",
            "name": "Retired Service",
            "isActive": "false",
            "serviceDefinitionUrlPath": "/data/retiredService/en_US.json",
        },
    ]
}


def test_required_fields_in_an_unselected_official_card_do_not_block_the_active_card() -> None:
    contract = CalculatorServiceContract(
        service_code="sampleMultiCard",
        name="Sample multi-card service",
        definition_url="https://example.invalid/sample.json",
        schema_hash="schema",
        cache_status="fresh",
        templates=(
            CalculatorTemplateContract(
                template_id="combined",
                fields=(
                    CalculatorFieldContract(
                        field_id="standardCount",
                        field_type="numericInput",
                        required=True,
                        section_id="card:0",
                        section_title="Standard",
                    ),
                    CalculatorFieldContract(
                        field_id="standardData",
                        field_type="fileSize",
                        required=True,
                        valid_size_units=("gb",),
                        valid_frequency_units=("month",),
                        section_id="card:0",
                        section_title="Standard",
                    ),
                    CalculatorFieldContract(
                        field_id="regionalCount",
                        field_type="numericInput",
                        required=True,
                        section_id="card:1",
                        section_title="Regional",
                    ),
                    CalculatorFieldContract(
                        field_id="regionalData",
                        field_type="fileSize",
                        required=True,
                        valid_size_units=("gb",),
                        valid_frequency_units=("month",),
                        section_id="card:1",
                        section_title="Regional",
                    ),
                ),
            ),
        ),
    )

    result = AwsCalculatorContractCatalog.validate_configuration(
        contract,
        template_id="combined",
        configuration={
            "standardCount": 2,
            "standardData": {"value": "100", "unit": "gb|month"},
        },
    )

    assert result.valid is True
    assert result.errors == ()


COMPUTE_DEFINITION = {
    "serviceCode": "sampleCompute",
    "serviceName": "Sample Compute",
    "type": "AWSService",
    "version": "3.7.1",
    "templates": [
        {
            "id": "onDemand",
            "title": "On-Demand compute",
            "cards": [
                {
                    "inputSection": {
                        "components": [
                            {
                                "id": "operatingSystem",
                                "type": "input",
                                "subType": "dropdown",
                                "label": "Operating system",
                                "options": [
                                    {"id": "Linux", "label": "Linux"},
                                    {"id": "Windows", "label": "Windows"},
                                ],
                                "defaultDropDownItem": "Linux",
                                "validations": {"required": True},
                            },
                            {
                                "id": "instanceCount",
                                "type": "input",
                                "subType": "numericInput",
                                "label": "Number of instances",
                                "defaultValue": 1,
                                "validations": {
                                    "required": True,
                                    "minValue": 1,
                                    "maxValue": 100,
                                    "allowDecimals": False,
                                },
                            },
                            {
                                "id": "storage",
                                "type": "input",
                                "subType": "fileSize",
                                "label": "Storage",
                                "dropDownSize": [
                                    {"id": "gb", "label": "GB"},
                                    {"id": "tb", "label": "TB"},
                                ],
                                "dropDownFrequency": [
                                    {"id": "month", "label": "month"},
                                ],
                                "outputSize": "gb",
                                "outputFrequency": "month",
                                "validations": {"required": False, "minValue": 0},
                            },
                            {
                                "id": "requests",
                                "type": "input",
                                "subType": "frequency",
                                "label": "Requests",
                                "options": [
                                    {"id": "perHour", "label": "per hour"},
                                    {"id": "perMonth", "label": "per month"},
                                ],
                                "validations": {"required": False, "minValue": 0},
                            },
                            {
                                "id": "advancedMode",
                                "type": "input",
                                "subType": "dropdown",
                                "label": "Advanced mode",
                                "options": [
                                    {"id": "enabled", "label": "Enabled"},
                                    {"id": "disabled", "label": "Disabled"},
                                ],
                                "defaultDropDownItem": "disabled",
                            },
                            {
                                "id": "advancedAmount",
                                "type": "input",
                                "subType": "numericInput",
                                "label": "Advanced amount",
                                "displayIf": {
                                    "==": [
                                        {"type": "component", "id": "advancedMode"},
                                        "enabled",
                                    ]
                                },
                                "validations": {"required": True, "minValue": 1},
                            },
                            {
                                "id": "disabledField",
                                "type": "input",
                                "subType": "numericInput",
                                "isDisabled": True,
                            },
                            {
                                "id": "requestsWithoutFreeTier",
                                "type": "input",
                                "subType": "numericInput",
                            },
                            {
                                "id": "instanceMatrix",
                                "type": "input",
                                "subType": "columnFormIPM",
                                "mappingDefinitionName": "sample-compute-map",
                                "row": [
                                    {
                                        "label": "Nodes",
                                        "selectorId": "Number of Nodes",
                                        "type": "textInput",
                                        "defaultValue": 1,
                                        "validations": {
                                            "required": True,
                                            "minValue": 1,
                                            "allowDecimals": False,
                                        },
                                    },
                                    {
                                        "label": "Instance type",
                                        "selectorId": "Instance Type",
                                        "type": "autoSuggest",
                                        "isInstanceType": True,
                                    },
                                ],
                            },
                        ]
                    }
                }
            ],
        }
    ],
}


GROUP_DEFINITION = {
    "serviceCode": "sampleStorageGroup",
    "serviceName": "Sample Storage",
    "type": "AWSService",
    "subType": "subServiceSelector",
    "version": "1.2.0",
    "templates": ["sampleStandard", "sampleArchive"],
}

STANDARD_DEFINITION = {
    "serviceCode": "sampleStandard",
    "serviceName": "Sample Standard",
    "type": "AWSService",
    "subType": "subService",
    "version": "1.0.0",
    "templates": [{"id": "standard", "cards": []}],
}

ARCHIVE_DEFINITION = {
    "serviceCode": "sampleArchive",
    "serviceName": "Sample Archive",
    "type": "AWSService",
    "subType": "subService",
    "version": "1.0.0",
    "templates": [{"id": "archive", "cards": []}],
}


class FakeFetcher:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self.payloads = payloads
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict[str, object]:
        self.urls.append(url)
        payload = self.payloads.get(url)
        if payload is None:
            raise RuntimeError(f"unexpected url {url}")
        return json.loads(json.dumps(payload))


def _catalog(tmp_path: Path) -> tuple[AwsCalculatorContractCatalog, FakeFetcher]:
    base = "https://official.example"
    fetcher = FakeFetcher(
        {
            f"{base}/manifest/en_US.json": MANIFEST,
            f"{base}/data/sampleCompute/en_US.json": COMPUTE_DEFINITION,
            f"{base}/data/sampleStorageGroup/en_US.json": GROUP_DEFINITION,
            f"{base}/data/sampleStandard/en_US.json": STANDARD_DEFINITION,
            f"{base}/data/sampleArchive/en_US.json": ARCHIVE_DEFINITION,
        }
    )
    return (
        AwsCalculatorContractCatalog(
            cache_path=tmp_path / "calculator_contracts.json",
            base_url=base,
            fetch_json=fetcher,
        ),
        fetcher,
    )


def test_search_uses_live_manifest_identity_and_hides_inactive_services(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)

    by_name = catalog.search("compute")
    by_keyword = catalog.search("virtual server")

    assert [item.service_code for item in by_name] == ["sampleCompute"]
    assert [item.service_code for item in by_keyword] == ["sampleCompute"]
    assert catalog.search("retired") == []
    assert by_name[0].definition_url == (
        "https://official.example/data/sampleCompute/en_US.json"
    )


def test_contract_extracts_template_fields_without_disabled_duplicates(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)

    contract = catalog.get_contract("sampleCompute")

    assert contract.service_code == "sampleCompute"
    assert contract.definition_version == "3.7.1"
    assert len(contract.schema_hash) == 64
    assert contract.field_count == 7
    assert [template.template_id for template in contract.templates] == ["onDemand"]
    fields = {field.field_id: field for field in contract.templates[0].fields}
    assert "disabledField" not in fields
    assert "requestsWithoutFreeTier" not in fields
    assert fields["operatingSystem"].required is True
    assert fields["operatingSystem"].default_value == "Linux"
    assert [option.option_id for option in fields["operatingSystem"].options] == [
        "Linux",
        "Windows",
    ]
    assert fields["storage"].valid_size_units == ("gb", "tb")
    assert fields["storage"].valid_frequency_units == ("month",)
    assert fields["advancedAmount"].display_if == {
        "==": [{"type": "component", "id": "advancedMode"}, "enabled"]
    }
    assert fields["instanceMatrix"].mapping_definition_name == "sample-compute-map"
    assert fields["instanceMatrix"].row_fields[0].selector_id == "Number of Nodes"
    assert fields["instanceMatrix"].row_fields[0].required is True


def test_parent_contract_exposes_official_subservice_codes(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)

    contract = catalog.get_contract("sampleStorageGroup")

    assert contract.sub_type == "subServiceSelector"
    assert contract.subservice_codes == ("sampleStandard", "sampleArchive")
    assert contract.templates == ()
    assert contract.field_count == 0


def test_configuration_validation_is_strict_and_condition_aware(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    contract = catalog.get_contract("sampleCompute")

    valid = catalog.validate_configuration(
        contract,
        template_id="onDemand",
        configuration={
            "region": "ap-northeast-1",
            "description": "production",
            "operatingSystem": {"value": "Linux"},
            "instanceCount": {"value": "4"},
            "storage": {"value": "2", "unit": "tb|month"},
            "requests": {"value": 5000000, "unit": "perMonth"},
            "advancedMode": "disabled",
        },
    )

    assert valid.valid is True
    assert valid.errors == ()
    assert valid.normalized_configuration["instanceCount"] == {"value": "4"}
    assert valid.normalized_configuration["requests"] == {
        "value": "5000000",
        "unit": "perMonth",
    }
    assert "storage" in valid.normalized_configuration

    official_unit_shorthand = catalog.validate_configuration(
        contract,
        template_id="onDemand",
        configuration={
            "operatingSystem": "Linux",
            "instanceCount": "4",
            "storage": {"value": "2", "unit": "tb"},
            "advancedMode": "disabled",
        },
    )

    assert official_unit_shorthand.valid is True
    assert official_unit_shorthand.normalized_configuration["storage"] == {
        "value": "2",
        "unit": "tb|month",
    }

    invalid = catalog.validate_configuration(
        contract,
        template_id="onDemand",
        configuration={
            "region": "ap-northeast-1",
            "operatingSystem": "Solaris",
            "instanceCount": "4.5",
            "storage": {"value": "2", "unit": "PB|month"},
            "requests": {"value": "5", "unit": "perYear"},
            "advancedMode": "enabled",
            "inventedField": "must fail",
        },
    )

    assert invalid.valid is False
    assert any("inventedField" in error for error in invalid.errors)
    assert any("operatingSystem" in error and "Solaris" in error for error in invalid.errors)
    assert any("instanceCount" in error and "integer" in error for error in invalid.errors)
    assert any("storage" in error and "PB" in error for error in invalid.errors)
    assert any("requests" in error and "perYear" in error for error in invalid.errors)
    assert any("advancedAmount" in error and "required" in error for error in invalid.errors)


def test_transient_official_definition_failure_is_retried_before_aborting(
    tmp_path: Path,
) -> None:
    base = "https://official.example"
    definition_url = f"{base}/data/sampleCompute/en_US.json"
    attempts: dict[str, int] = {}

    def flaky_fetcher(url: str) -> dict[str, object]:
        attempts[url] = attempts.get(url, 0) + 1
        if url == definition_url and attempts[url] == 1:
            raise TimeoutError("temporary timeout")
        if url.endswith("/manifest/en_US.json"):
            return json.loads(json.dumps(MANIFEST))
        if url == definition_url:
            return json.loads(json.dumps(COMPUTE_DEFINITION))
        raise RuntimeError(f"unexpected url {url}")

    catalog = AwsCalculatorContractCatalog(
        cache_path=tmp_path / "calculator_contracts.json",
        base_url=base,
        fetch_json=flaky_fetcher,
    )

    contract = catalog.get_contract("sampleCompute")

    assert contract.service_code == "sampleCompute"
    assert attempts[definition_url] == 2


def test_disk_cache_can_serve_stale_official_snapshot_when_network_fails(tmp_path: Path) -> None:
    catalog, fetcher = _catalog(tmp_path)
    expected = catalog.get_contract("sampleCompute")
    assert fetcher.urls

    def unavailable(_: str) -> dict[str, object]:
        raise RuntimeError("offline")

    cached = AwsCalculatorContractCatalog(
        cache_path=tmp_path / "calculator_contracts.json",
        base_url="https://official.example",
        fetch_json=unavailable,
        cache_ttl_seconds=0,
    )

    actual = cached.get_contract("sampleCompute", force_refresh=True)

    assert actual.schema_hash == expected.schema_hash
    assert actual.cache_status == "stale"


def test_forced_refresh_reports_official_schema_drift(tmp_path: Path) -> None:
    catalog, fetcher = _catalog(tmp_path)
    original = catalog.get_contract("sampleCompute")
    changed = json.loads(json.dumps(COMPUTE_DEFINITION))
    changed["version"] = "3.8.0"
    fetcher.payloads["https://official.example/data/sampleCompute/en_US.json"] = changed

    refreshed = catalog.get_contract("sampleCompute", force_refresh=True)
    cached = catalog.get_contract("sampleCompute")

    assert refreshed.schema_changed is True
    assert refreshed.previous_schema_hash == original.schema_hash
    assert refreshed.schema_hash != original.schema_hash
    assert cached.schema_changed is False
    assert cached.previous_schema_hash is None


def test_proxy_selection_does_not_parse_unrelated_no_proxy_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,::1,::1/128")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    catalog = AwsCalculatorContractCatalog(cache_path=tmp_path / "cache.json")

    assert catalog._proxy_url() == "http://127.0.0.1:7890"


def test_unknown_service_is_never_guessed(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)

    with pytest.raises(CalculatorContractNotFound):
        catalog.get_contract("sample")


def test_legacy_contract_api_is_not_exposed_by_the_v2_runtime() -> None:
    client = TestClient(aws_main.app)

    search_response = client.get(
        "/api/aws/calculator-contracts",
        params={"query": "virtual server"},
    )
    contract_response = client.get("/api/aws/calculator-contracts/sampleCompute")
    validation_response = client.post(
        "/api/aws/calculator-contracts/sampleCompute/validate",
        json={
            "template_id": "onDemand",
            "configuration": {
                "region": "ap-northeast-1",
                "operatingSystem": "Linux",
                "instanceCount": "2",
                "advancedMode": "disabled",
            },
        },
    )

    assert search_response.status_code == 404
    assert contract_response.status_code == 404
    assert validation_response.status_code == 404


def test_legacy_contract_integration_status_is_removed() -> None:
    client = TestClient(aws_main.app)

    response = client.get("/api/aws/calculator-contracts/integration-status")

    assert response.status_code == 404


def test_contract_module_has_no_customer_prose_backdoor() -> None:
    module_path = (
        Path(__file__).parents[1]
        / "app"
        / "integrations"
        / "aws_calculator_contracts.py"
    )

    source = module_path.read_text(encoding="utf-8")

    assert "source_text" not in source
    assert "original_source_text" not in source


@pytest.mark.skipif(
    os.environ.get("AWS_CALCULATOR_LIVE_TESTS") != "1",
    reason="requires the live AWS Calculator runtime documents",
)
def test_live_common_service_contracts_are_discoverable(tmp_path: Path) -> None:
    catalog = AwsCalculatorContractCatalog(cache_path=tmp_path / "live-contracts.json")
    minimum_fields = {
        "ec2Enhancement": 20,
        "amazonRDSMySQLDB": 12,
        "amazonRDSPostgreSQLDB": 12,
        "amazonElastiCache": 5,
        "amazonS3Standard": 5,
        "applicationLoadBalancer": 5,
    }

    for service_code, minimum in minimum_fields.items():
        contract = catalog.get_contract(service_code)
        assert contract.source == "aws_calculator_runtime"
        assert contract.field_count >= minimum
        assert contract.schema_hash

    parent = catalog.get_contract("amazonSimpleStorageServiceGroup")
    assert "amazonS3Standard" in parent.subservice_codes
