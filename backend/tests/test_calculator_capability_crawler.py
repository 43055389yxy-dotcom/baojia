from scripts.calculator_capability_crawler import (
    CalculatorCapabilityCrawler,
    Choice,
    Control,
)


def test_normalize_option_label_removes_aws_duplicate_text() -> None:
    assert CalculatorCapabilityCrawler._normalize_option_label("区域 区域") == "区域"
    assert CalculatorCapabilityCrawler._normalize_option_label("每天每天") == "每天"
    assert CalculatorCapabilityCrawler._normalize_option_label("Multi-AZ") == "Multi-AZ"


def test_branch_actions_enumerate_every_unselected_dropdown_option() -> None:
    control = Control(
        key="generated-id",
        name="存储类型",
        role="combobox",
        tag="button",
        dropdown_position=4,
        choices=[
            Choice("gp2", selected=True),
            Choice("gp3"),
            Choice("io1"),
            Choice("io2"),
        ],
    )

    actions = CalculatorCapabilityCrawler._branch_actions([control], [])

    assert [action["choice"] for action in actions] == ["gp3", "io1", "io2"]
    assert all(action["position"] == 4 for action in actions)


def test_branch_actions_do_not_revisit_a_control_in_the_same_path() -> None:
    control = Control(
        key="generated-id",
        name="存储类型",
        role="combobox",
        tag="button",
        dropdown_position=4,
        choices=[Choice("gp2", selected=True), Choice("gp3")],
    )
    path = [
        {
            "kind": "dropdown",
            "control": "存储类型",
            "occurrence": 0,
            "position": 4,
            "choice": "gp3",
        }
    ]

    assert CalculatorCapabilityCrawler._branch_actions([control], path) == []


def test_fingerprint_changes_when_a_numeric_constraint_changes() -> None:
    base = Control(
        key="iops",
        name="IOPS",
        role="spinbutton",
        tag="input",
        input_type="number",
        minimum="1000",
        maximum="64000",
    )
    changed = Control(
        key="iops",
        name="IOPS",
        role="spinbutton",
        tag="input",
        input_type="number",
        minimum="1000",
        maximum="256000",
    )

    assert CalculatorCapabilityCrawler._fingerprint([base]) != (
        CalculatorCapabilityCrawler._fingerprint([changed])
    )
