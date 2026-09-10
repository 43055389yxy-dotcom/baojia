from __future__ import annotations

import pytest

from app.domain.quote_compiler import RequirementFactIR, RequirementIR, ResourceIR
from app.integrations.aws_calculator_binding import (
    AwsCalculatorConfigurationCompiler,
    AwsCalculatorConfigurationPlanner,
    CalculatorConfigurationCandidate,
    CalculatorConfigurationMappingError,
    calculator_configuration_prompt,
)
from app.integrations.aws_calculator_contracts import (
    AwsCalculatorContractCatalog,
    CalculatorFieldContract,
    CalculatorFieldOption,
    CalculatorServiceContract,
    CalculatorTemplateContract,
)


def _requirement() -> RequirementIR:
    return RequirementIR(
        component_id="component-7",
        component_key="ec2-backend",
        service_intent="ec2",
        product_identity="AmazonEC2",
        region="ap-northeast-1",
        facts=(
            RequirementFactIR(
                fact_id="fact-count",
                path="quantity",
                value=4,
                unit="count",
                scope="component",
                source_kind="customer_text",
                evidence="客户原句不应进入映射提示词",
            ),
            RequirementFactIR(
                fact_id="fact-os",
                path="requirements.operating_system",
                value="Linux",
                scope="component",
                source_kind="customer_text",
                evidence="另一段客户原句",
            ),
        ),
        evidence_fingerprint="f" * 64,
    )


def _contract() -> CalculatorServiceContract:
    return CalculatorServiceContract(
        service_code="sampleCompute",
        name="Sample Compute",
        definition_version="1.0",
        definition_url="https://official.example/data/sampleCompute/en_US.json",
        schema_hash="a" * 64,
        cache_status="fresh",
        templates=(
            CalculatorTemplateContract(
                template_id="onDemand",
                fields=(
                    CalculatorFieldContract(
                        field_id="instanceCount",
                        field_type="numericInput",
                        required=True,
                        min_value=1,
                        max_value=100,
                        allow_decimals=False,
                    ),
                    CalculatorFieldContract(
                        field_id="operatingSystem",
                        field_type="dropdown",
                        required=True,
                        options=(
                            CalculatorFieldOption(option_id="Linux", label="Linux"),
                            CalculatorFieldOption(option_id="Windows", label="Windows"),
                        ),
                    ),
                ),
            ),
        ),
    )


def _resource() -> ResourceIR:
    return ResourceIR(
        component_id="component-7",
        service="ec2",
        region="ap-northeast-1",
        model="c7g.large",
        quantity=4,
        pricing_status="priced",
        official_specifications={"vCPU": 2, "memoryGiB": 4},
    )


def _candidate(**updates: object) -> CalculatorConfigurationCandidate:
    payload = {
        "component_id": "component-7",
        "service_code": "sampleCompute",
        "template_id": "onDemand",
        "schema_hash": "a" * 64,
        "bindings": [
            {
                "field_id": "instanceCount",
                "value": "4",
                "source_fact_ids": ["fact-count"],
            },
            {
                "field_id": "operatingSystem",
                "value": "Linux",
                "source_fact_ids": ["fact-os"],
            },
        ],
    }
    payload.update(updates)
    return CalculatorConfigurationCandidate.model_validate(payload)


def test_compiler_binds_each_official_field_to_one_customer_fact() -> None:
    compiled = AwsCalculatorConfigurationCompiler.compile(
        requirement=_requirement(),
        contract=_contract(),
        candidate=_candidate(),
    )

    assert compiled.status == "manifest_schema_validated"
    assert compiled.configuration == {
        "region": "ap-northeast-1",
        "instanceCount": {"value": "4"},
        "operatingSystem": {"value": "Linux"},
    }
    assert compiled.consumed_fact_ids == ("fact-count", "fact-os")
    assert compiled.unconsumed_fact_ids == ()


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (_candidate(schema_hash="b" * 64), "schema hash"),
        (_candidate(component_id="component-99"), "component identity"),
        (_candidate(service_code="anotherService"), "service identity"),
        (
            _candidate(
                bindings=[
                    {
                        "field_id": "instanceCount",
                        "value": "4",
                        "source_fact_ids": ["missing-fact"],
                    },
                    {
                        "field_id": "operatingSystem",
                        "value": "Linux",
                        "source_fact_ids": ["fact-os"],
                    },
                ]
            ),
            "unknown facts",
        ),
        (
            _candidate(
                bindings=[
                    {
                        "field_id": "instanceCount",
                        "value": "4",
                        "source_fact_ids": ["fact-count"],
                    },
                    {
                        "field_id": "operatingSystem",
                        "value": "Linux",
                        "source_fact_ids": ["fact-count"],
                    },
                ]
            ),
            "multiple official fields",
        ),
        (
            _candidate(
                bindings=[
                    {
                        "field_id": "instanceCount",
                        "value": "4",
                        "source_fact_ids": ["fact-count"],
                    },
                    {
                        "field_id": "operatingSystem",
                        "value": "Solaris",
                        "source_fact_ids": ["fact-os"],
                    },
                ]
            ),
            "not an official option",
        ),
    ],
)
def test_compiler_rejects_untraceable_or_stale_ai_candidates(
    candidate: CalculatorConfigurationCandidate,
    message: str,
) -> None:
    with pytest.raises(CalculatorConfigurationMappingError, match=message):
        AwsCalculatorConfigurationCompiler.compile(
            requirement=_requirement(),
            contract=_contract(),
            candidate=candidate,
        )


def test_compiler_rejects_a_value_that_does_not_match_its_customer_fact() -> None:
    candidate = _candidate(
        bindings=[
            {
                "field_id": "instanceCount",
                "value": "6",
                "source_fact_ids": ["fact-count"],
            },
            {
                "field_id": "operatingSystem",
                "value": "Linux",
                "source_fact_ids": ["fact-os"],
            },
        ]
    )

    with pytest.raises(CalculatorConfigurationMappingError, match="does not equal customer fact"):
        AwsCalculatorConfigurationCompiler.compile(
            requirement=_requirement(),
            contract=_contract(),
            candidate=candidate,
        )


def test_compiler_accepts_selected_model_only_from_verified_resource_ir() -> None:
    contract = _contract().model_copy(
        update={
            "templates": (
                _contract().templates[0].model_copy(
                    update={
                        "fields": (
                            *_contract().templates[0].fields,
                            CalculatorFieldContract(
                                field_id="instanceType",
                                field_type="input",
                                required=True,
                            ),
                        )
                    }
                ),
            )
        }
    )
    candidate = _candidate(
        bindings=[
            *_candidate().model_dump(mode="json")["bindings"],
            {
                "field_id": "instanceType",
                "value": "c7g.large",
                "source_fact_ids": [],
                "source_resource_paths": ["model"],
            },
        ]
    )

    compiled = AwsCalculatorConfigurationCompiler.compile(
        requirement=_requirement(),
        resource=_resource(),
        contract=contract,
        candidate=candidate,
    )

    assert compiled.configuration["instanceType"] == {"value": "c7g.large"}
    assert compiled.consumed_resource_paths == ("model",)


def test_compiler_rejects_ai_model_different_from_resource_ir() -> None:
    contract = _contract().model_copy(
        update={
            "templates": (
                _contract().templates[0].model_copy(
                    update={
                        "fields": (
                            *_contract().templates[0].fields,
                            CalculatorFieldContract(
                                field_id="instanceType",
                                field_type="input",
                                required=True,
                            ),
                        )
                    }
                ),
            )
        }
    )
    candidate = _candidate(
        bindings=[
            *_candidate().model_dump(mode="json")["bindings"],
            {
                "field_id": "instanceType",
                "value": "m7i.large",
                "source_fact_ids": [],
                "source_resource_paths": ["model"],
            },
        ]
    )

    with pytest.raises(CalculatorConfigurationMappingError, match="does not equal ResourceIR"):
        AwsCalculatorConfigurationCompiler.compile(
            requirement=_requirement(),
            resource=_resource(),
            contract=contract,
            candidate=candidate,
        )


def test_compiler_traces_each_leaf_of_a_structured_calculator_field() -> None:
    contract = _contract().model_copy(
        update={
            "templates": (
                CalculatorTemplateContract(
                    template_id="matrix",
                    fields=(
                        CalculatorFieldContract(
                            field_id="instanceMatrix",
                            field_type="columnFormIPM",
                            row_fields=(),
                        ),
                    ),
                ),
            )
        }
    )
    candidate = CalculatorConfigurationCandidate.model_validate(
        {
            "component_id": "component-7",
            "service_code": "sampleCompute",
            "template_id": "matrix",
            "schema_hash": "a" * 64,
            "bindings": [
                {
                    "field_id": "instanceMatrix",
                    "value": {
                        "value": [
                            {
                                "Number of Nodes": {"value": "4"},
                                "Instance Type": {"value": "c7g.large"},
                            }
                        ]
                    },
                    "sources": [
                        {
                            "target_pointer": "/value/0/Number of Nodes/value",
                            "fact_id": "fact-count",
                        },
                        {
                            "target_pointer": "/value/0/Instance Type/value",
                            "resource_path": "model",
                        },
                    ],
                }
            ],
        }
    )

    compiled = AwsCalculatorConfigurationCompiler.compile(
        requirement=_requirement(),
        resource=_resource(),
        contract=contract,
        candidate=candidate,
    )

    assert compiled.consumed_fact_ids == ("fact-count",)
    assert compiled.consumed_resource_paths == ("model",)


def test_compiler_rejects_structured_source_pointer_with_a_different_value() -> None:
    contract = _contract().model_copy(
        update={
            "templates": (
                CalculatorTemplateContract(
                    template_id="matrix",
                    fields=(
                        CalculatorFieldContract(
                            field_id="instanceMatrix",
                            field_type="columnFormIPM",
                        ),
                    ),
                ),
            )
        }
    )
    candidate = CalculatorConfigurationCandidate.model_validate(
        {
            "component_id": "component-7",
            "service_code": "sampleCompute",
            "template_id": "matrix",
            "schema_hash": "a" * 64,
            "bindings": [
                {
                    "field_id": "instanceMatrix",
                    "value": {"value": [{"Number of Nodes": {"value": "9"}}]},
                    "sources": [
                        {
                            "target_pointer": "/value/0/Number of Nodes/value",
                            "fact_id": "fact-count",
                        }
                    ],
                }
            ],
        }
    )

    with pytest.raises(CalculatorConfigurationMappingError, match="does not equal customer fact"):
        AwsCalculatorConfigurationCompiler.compile(
            requirement=_requirement(),
            resource=_resource(),
            contract=contract,
            candidate=candidate,
        )


def test_compiler_accepts_only_a_declared_official_enum_translation() -> None:
    requirement = _requirement().model_copy(
        update={
            "service_intent": "rds",
            "facts": (
                RequirementFactIR(
                    fact_id="fact-storage",
                    path="requirements.storage_type",
                    value="gp3",
                    unit=None,
                    scope="component",
                    source_kind="customer_text",
                    evidence="gp3",
                ),
            ),
        }
    )
    contract = CalculatorServiceContract(
        service_code="amazonRDSMySQLDB",
        name="RDS MySQL",
        definition_url="https://official.example/rds.json",
        schema_hash="a" * 64,
        cache_status="fresh",
        templates=(
            CalculatorTemplateContract(
                template_id="mysql",
                fields=(
                    CalculatorFieldContract(
                        field_id="storageType",
                        field_type="dropdown",
                        options=(
                            CalculatorFieldOption(
                                option_id="General Purpose-GP3",
                                label="General Purpose SSD (gp3)",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    candidate = CalculatorConfigurationCandidate.model_validate(
        {
            "component_id": "component-7",
            "service_code": "amazonRDSMySQLDB",
            "template_id": "mysql",
            "schema_hash": "a" * 64,
            "bindings": [
                {
                    "field_id": "storageType",
                    "value": "General Purpose-GP3",
                    "source_fact_ids": ["fact-storage"],
                }
            ],
        }
    )

    compiled = AwsCalculatorConfigurationCompiler.compile(
        requirement=requirement,
        contract=contract,
        candidate=candidate,
    )

    assert compiled.configuration["storageType"] == {
        "value": "General Purpose-GP3"
    }


def test_mapping_prompt_contains_typed_facts_but_not_customer_evidence() -> None:
    prompt = calculator_configuration_prompt(_requirement(), _contract(), resource=_resource())

    assert "fact-count" in prompt
    assert '"value":4' in prompt
    assert "instanceCount" in prompt
    assert "c7g.large" in prompt
    assert "客户原句不应进入映射提示词" not in prompt
    assert "另一段客户原句" not in prompt


class FakeCompleter:
    def __init__(self) -> None:
        self.user_content = ""

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.user_content = str(kwargs["user_content"])
        return _candidate().model_dump(mode="json")


@pytest.mark.asyncio
async def test_planner_accepts_only_a_compiled_structured_candidate() -> None:
    completer = FakeCompleter()
    planner = AwsCalculatorConfigurationPlanner(completer)

    compiled = await planner.plan(requirement=_requirement(), contract=_contract())

    assert compiled.service_code == "sampleCompute"
    assert compiled.schema_hash == "a" * 64
    assert "客户原句不应进入映射提示词" not in completer.user_content


class RepairingCompleter:
    def __init__(self) -> None:
        self.calls = 0
        self.system_prompts: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.system_prompts.append(str(kwargs["system_prompt"]))
        if self.calls == 1:
            return _candidate(
                bindings=[
                    {
                        "field_id": "instanceCount",
                        "value": "6",
                        "source_fact_ids": ["fact-count"],
                    }
                ]
            ).model_dump(mode="json")
        return _candidate().model_dump(mode="json")


@pytest.mark.asyncio
async def test_planner_retries_once_with_compiler_feedback() -> None:
    completer = RepairingCompleter()
    planner = AwsCalculatorConfigurationPlanner(completer)

    compiled = await planner.plan(requirement=_requirement(), contract=_contract())

    assert compiled.consumed_fact_ids == ("fact-count", "fact-os")
    assert completer.calls == 2
    assert "does not equal customer fact" in completer.system_prompts[1]


class CoverageCompleter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            return _candidate(
                bindings=[
                    {
                        "field_id": "operatingSystem",
                        "value": "Linux",
                        "source_fact_ids": ["fact-os"],
                    }
                ]
            ).model_dump(mode="json")
        return _candidate().model_dump(mode="json")


@pytest.mark.asyncio
async def test_planner_retries_when_a_required_typed_source_was_omitted() -> None:
    completer = CoverageCompleter()
    compiled = await AwsCalculatorConfigurationPlanner(completer).plan(
        requirement=_requirement(),
        contract=_contract(),
        required_fact_ids=("fact-count",),
    )

    assert completer.calls == 2
    assert compiled.consumed_fact_ids == ("fact-count", "fact-os")


@pytest.mark.asyncio
async def test_planner_discards_fields_proposed_for_service_identity_facts() -> None:
    class _IdentityCompleter:
        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            return _candidate().model_dump(mode="json")

    contract = _contract().model_copy(
        update={
            "templates": (
                _contract().templates[0].model_copy(
                    update={
                        "fields": (
                            _contract().templates[0].fields[0],
                            _contract().templates[0].fields[1].model_copy(
                                update={"default_value": "Linux"}
                            ),
                        )
                    }
                ),
            )
        }
    )
    compiled = await AwsCalculatorConfigurationPlanner(_IdentityCompleter()).plan(
        requirement=_requirement(),
        contract=contract,
        identity_fact_ids=("fact-os",),
    )

    assert compiled.consumed_fact_ids == ("fact-count",)
    assert "operatingSystem" not in compiled.configuration


def test_binding_module_has_no_customer_prose_fields() -> None:
    module_source = __import__(
        "inspect"
    ).getsource(__import__("app.integrations.aws_calculator_binding", fromlist=["*"]))

    assert "source_text" not in module_source
    assert "original_source_text" not in module_source


def test_validation_uses_contract_catalog_rules() -> None:
    result = AwsCalculatorContractCatalog.validate_configuration(
        _contract(),
        template_id="onDemand",
        configuration={
            "region": "ap-northeast-1",
            "instanceCount": "4",
            "operatingSystem": "Linux",
        },
    )

    assert result.valid is True


def test_compiler_checks_file_size_unit_against_normalized_gib_fact() -> None:
    requirement = _requirement().model_copy(
        update={
            "service_intent": "backup",
            "facts": (
                RequirementFactIR(
                    fact_id="fact-storage",
                    path="requirements.backup_storage_gib",
                    value=5120,
                    unit="GiB",
                    scope="component_total",
                    source_kind="customer_text",
                    evidence="备份容量5TB",
                ),
            ),
        }
    )
    contract = CalculatorServiceContract(
        service_code="amazonEfsBackup",
        name="EFS Backup",
        definition_url="https://official.example/efs-backup.json",
        schema_hash="a" * 64,
        cache_status="fresh",
        templates=(
            CalculatorTemplateContract(
                template_id="efsBackup",
                fields=(
                    CalculatorFieldContract(
                        field_id="dataSize",
                        field_type="fileSize",
                        valid_size_units=("gb", "tb"),
                        default_unit="gb|NA",
                    ),
                ),
            ),
        ),
    )

    def candidate(value: int, unit: str) -> CalculatorConfigurationCandidate:
        return CalculatorConfigurationCandidate.model_validate(
            {
                "component_id": "component-7",
                "service_code": "amazonEfsBackup",
                "template_id": "efsBackup",
                "schema_hash": "a" * 64,
                "bindings": [
                    {
                        "field_id": "dataSize",
                        "value": {"value": value, "unit": unit},
                        "source_fact_ids": ["fact-storage"],
                    }
                ],
            }
        )

    compiled = AwsCalculatorConfigurationCompiler.compile(
        requirement=requirement,
        contract=contract,
        candidate=candidate(5, "tb"),
    )
    assert compiled.configuration["dataSize"] == {"value": "5", "unit": "tb|NA"}

    with pytest.raises(CalculatorConfigurationMappingError, match="does not equal customer fact"):
        AwsCalculatorConfigurationCompiler.compile(
            requirement=requirement,
            contract=contract,
            candidate=candidate(5120, "tb"),
        )
