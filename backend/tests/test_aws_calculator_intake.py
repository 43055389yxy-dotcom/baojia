from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.models import ServiceRequirement
from app.integrations.aws_calculator_contracts import (
    AwsCalculatorContractCatalog,
    CalculatorContractNotFound,
    CalculatorServiceContract,
    CalculatorServiceSummary,
    CalculatorTemplateContract,
)
from app.integrations.aws_calculator_intake import (
    OfficialCalculatorIntakeResolver,
    OfficialCalculatorTemplateSelectionError,
    official_child_identity_field,
)
from tests.test_aws_calculator_contracts import _catalog


def _component(name: str) -> ServiceRequirement:
    return ServiceRequirement(
        service="sample_storage",
        calculator_service_name=name,
        source_text="Sample Storage 需要 500 GB",
    )


def test_official_parent_contract_requires_one_of_its_published_children(
    tmp_path: Path,
) -> None:
    catalog, _ = _catalog(tmp_path)
    resolver = OfficialCalculatorIntakeResolver(catalog)

    resolution = resolver.resolve(_component("Sample Storage"))

    assert resolution.status == "selection_required"
    assert resolution.parent_service_code == "sampleStorageGroup"
    assert [option.service_code for option in resolution.options] == [
        "sampleStandard",
        "sampleArchive",
    ]
    assert [option.name for option in resolution.options] == [
        "Sample Standard",
        "Sample Archive",
    ]
    assert resolution.contract is None


def test_selected_official_child_becomes_the_only_intake_contract(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    resolver = OfficialCalculatorIntakeResolver(catalog)

    resolution = resolver.resolve(
        _component("Sample Storage"),
        selected_service_code="sampleStandard",
    )

    assert resolution.status == "ready"
    assert resolution.parent_service_code == "sampleStorageGroup"
    assert resolution.contract is not None
    assert resolution.contract.service_code == "sampleStandard"
    assert resolution.contract.name == "Sample Standard"


def test_unpublished_child_cannot_be_injected_into_an_official_parent(
    tmp_path: Path,
) -> None:
    catalog, _ = _catalog(tmp_path)
    resolver = OfficialCalculatorIntakeResolver(catalog)

    with pytest.raises(OfficialCalculatorTemplateSelectionError):
        resolver.resolve(
            _component("Sample Storage"),
            selected_service_code="sampleCompute",
        )


def test_direct_service_uses_live_official_fields_without_a_local_field_list(
    tmp_path: Path,
) -> None:
    catalog, _ = _catalog(tmp_path)
    resolver = OfficialCalculatorIntakeResolver(catalog)

    resolution = resolver.resolve(_component("Sample Compute"))

    assert resolution.status == "ready"
    assert resolution.contract is not None
    assert resolution.contract.service_code == "sampleCompute"
    assert resolution.contract.field_count == 7
    assert resolver.prompt_payload(resolution.contract)["templates"][0]["fields"][0] == {
        "field_id": "operatingSystem",
        "label": "Operating system",
        "field_type": "dropdown",
        "required": True,
        "default_value": "Linux",
        "options": [
            {"id": "Linux", "label": "Linux"},
            {"id": "Windows", "label": "Windows"},
        ],
        "valid_size_units": [],
        "valid_frequency_units": [],
        "section_id": "card:0",
        "section_title": None,
    }


def test_official_backup_child_becomes_product_identity_not_discardable_context() -> None:
    assert official_child_identity_field(
        "backup",
        "amazonEfsBackup",
        "EFS Backup",
    ) == ("protected_service", "EFS")


class _NamespaceCatalog:
    def __init__(self, namespace: str, rows: tuple[tuple[str, str], ...]) -> None:
        self.namespace = namespace
        self.rows = rows
        self.contracts = {
            code: CalculatorServiceContract(
                service_code=code,
                name=name,
                definition_url=f"https://official.example/{code}.json",
                schema_hash=(str(index + 1) * 64)[:64],
                cache_status="fresh",
                templates=(CalculatorTemplateContract(template_id="template"),),
            )
            for index, (code, name) in enumerate(rows)
        }

    def get_contract(self, service: str) -> CalculatorServiceContract:
        contract = self.contracts.get(service)
        if contract is None:
            raise CalculatorContractNotFound(service)
        return contract

    def search(
        self,
        query: str,
        *,
        limit: int = 30,
    ) -> list[CalculatorServiceSummary]:
        assert query == self.namespace
        return [
            CalculatorServiceSummary(
                service_code=code,
                name=name,
                definition_url=f"https://official.example/{code}.json",
            )
            for code, name in self.rows[:limit]
        ]


def test_generic_official_product_namespace_requires_an_exact_leaf_selection() -> None:
    catalog = _NamespaceCatalog(
        "Amazon FSx",
        (
            ("amazonFSx", "Amazon FSx for Windows File Server"),
            ("amazonFSxForLustre", "Amazon FSx for Lustre"),
            ("amazonFSxForNetAppOntap", "Amazon FSx for NetApp ONTAP"),
            ("amazonFSxForOpenZfs", "Amazon FSx for OpenZFS"),
        ),
    )
    resolver = OfficialCalculatorIntakeResolver(catalog)
    component = ServiceRequirement(
        service="fsx",
        calculator_service_name="Amazon FSx",
    )

    pending = resolver.resolve(component)

    assert pending.status == "selection_required"
    assert pending.parent_service_code is None
    assert [option.service_code for option in pending.options] == [
        "amazonFSx",
        "amazonFSxForLustre",
        "amazonFSxForNetAppOntap",
        "amazonFSxForOpenZfs",
    ]

    selected = resolver.resolve(
        component,
        selected_service_code="amazonFSxForOpenZfs",
    )

    assert selected.status == "ready"
    assert selected.contract is not None
    assert selected.contract.service_code == "amazonFSxForOpenZfs"


def test_generic_name_with_parenthetical_description_uses_official_namespace() -> None:
    catalog = _NamespaceCatalog(
        "Amazon RDS",
        (
            ("amazonRDSMySQLDB", "Amazon RDS for MySQL"),
            ("amazonRDSPostgreSQLDB", "Amazon RDS for PostgreSQL"),
        ),
    )
    resolver = OfficialCalculatorIntakeResolver(catalog)
    component = ServiceRequirement(
        service="rds",
        calculator_service_name="Amazon RDS（MySQL / PostgreSQL）",
    )

    resolution = resolver.resolve(component)

    assert resolution.status == "selection_required"
    assert [option.service_code for option in resolution.options] == [
        "amazonRDSMySQLDB",
        "amazonRDSPostgreSQLDB",
    ]


def test_official_acronym_resolves_to_expanded_manifest_name() -> None:
    class AcronymCatalog(_NamespaceCatalog):
        def search(
            self,
            query: str,
            *,
            limit: int = 30,
        ) -> list[CalculatorServiceSummary]:
            if query == "AWS WAF":
                return []
            assert query.casefold() == "waf"
            return [
                CalculatorServiceSummary(
                    service_code=code,
                    name=name,
                    definition_url=f"https://official.example/{code}.json",
                )
                for code, name in self.rows[:limit]
            ]

    catalog = AcronymCatalog(
        "AWS WAF",
        (
            ("awsFirewallManager", "AWS Firewall Manager"),
            ("awsWebApplicationFirewall", "AWS Web Application Firewall (WAF)"),
        ),
    )
    resolver = OfficialCalculatorIntakeResolver(catalog)

    resolution = resolver.resolve(
        ServiceRequirement(service="waf", calculator_service_name="AWS WAF")
    )

    assert resolution.status == "ready"
    assert resolution.contract is not None
    assert resolution.contract.service_code == "awsWebApplicationFirewall"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("amazonFSx", "windows"),
        ("amazonFSxForLustre", "lustre"),
        ("amazonFSxForNetAppOntap", "ontap"),
        ("amazonFSxForOpenZfs", "openzfs"),
    ],
)
def test_official_fsx_child_becomes_typed_product_identity(
    code: str,
    expected: str,
) -> None:
    assert official_child_identity_field("fsx", code, "unused") == (
        "file_system_type",
        expected,
    )


@pytest.mark.parametrize(
    ("service", "requirements", "expected"),
    [
        ("ec2", {}, "ec2Enhancement"),
        ("rds", {"engine": "mysql"}, "amazonRDSMySQLDB"),
        ("rds", {"engine": "postgresql"}, "amazonRDSPostgreSQLDB"),
        ("elasticache", {"engine": "redis"}, "amazonElastiCache"),
        ("s3", {}, "amazonSimpleStorageServiceGroup"),
        ("elb", {"load_balancer_type": "application"}, "elasticLoadBalancing"),
    ],
)
def test_common_component_identity_uses_exact_official_entrypoint(
    service: str,
    requirements: dict[str, object],
    expected: str,
) -> None:
    from app.integrations.aws_calculator_intake import official_calculator_entrypoint

    component = ServiceRequirement(service=service, requirements=requirements)

    assert official_calculator_entrypoint(component) == expected
