from app.integrations.calculator_ai_agent import BrowserAction, DeepSeekCalculatorAgent
from app.integrations.calculator_web import AwsCalculatorWebAutomator


def test_page_candidate_below_requested_memory_is_blocked() -> None:
    violation = DeepSeekCalculatorAgent._candidate_goal_violation(
        {
            "name": "cache.t3.small",
            "text": "vCPU: 2 Memory: 1.37 GiB",
            "context": "",
        },
        {"requirements": {"memory_gib": 4}},
        "cache.t3.small",
    )

    assert violation is not None
    assert "below requested" in violation


def test_page_candidate_not_lower_than_requested_is_allowed() -> None:
    violation = DeepSeekCalculatorAgent._candidate_goal_violation(
        {
            "name": "cache.m7g.large",
            "text": "vCPU: 2 Memory: 6.38 GiB",
            "context": "",
        },
        {"requirements": {"memory_gib": 4}},
        "cache.m7g.large",
    )

    assert violation is None


def test_cpu_or_memory_goal_requires_real_model_selection() -> None:
    assert AwsCalculatorWebAutomator._goal_requires_model(
        {"requirements": {"vcpu": 8, "memory_gib": 32}}
    )
    assert not AwsCalculatorWebAutomator._goal_requires_model(
        {"requirements": {"storage_gib": 2048}}
    )
    assert not AwsCalculatorWebAutomator._goal_requires_model(
        {"requirements": {"requested_model": "ALB"}},
        "elasticloadbalancing",
    )


def test_completed_choice_matches_same_page_option() -> None:
    assert DeepSeekCalculatorAgent._same_choice(
        "deployment option multi-az",
        "Deployment option  Multi-AZ",
    )


def test_ai_batch_is_reduced_to_one_action_per_page_observation() -> None:
    decision = DeepSeekCalculatorAgent._safe_parse_decision(
        {
            "actions": [
                {
                    "action": "fill",
                    "control_id": "c1",
                    "value": "500",
                    "reason": "填写存储",
                    "selected_model": None,
                },
                {
                    "action": "click",
                    "control_id": "c2",
                    "value": None,
                    "reason": "选择购买方式",
                    "selected_model": None,
                },
            ]
        }
    )

    assert len(decision.actions) == 1
    assert decision.actions[0].control_id == "c1"


def test_storage_is_downstream_of_instance_selection() -> None:
    assert DeepSeekCalculatorAgent._is_downstream_pricing_control(
        {"name": "Storage amount", "text": "500", "context": "RDS storage"}
    )
    assert not DeepSeekCalculatorAgent._is_downstream_pricing_control(
        {"name": "Search instances", "text": "", "context": "Instance type"}
    )


def test_only_real_form_controls_are_fillable() -> None:
    assert DeepSeekCalculatorAgent._is_fillable_control(
        {"tag": "input", "role": "spinbutton", "fillable": True}
    )
    assert not DeepSeekCalculatorAgent._is_fillable_control(
        {"tag": "div", "role": "spinbutton", "fillable": False}
    )


def test_open_option_panel_blocks_unrelated_fill_but_not_search() -> None:
    assert DeepSeekCalculatorAgent._has_visible_options(
        [{"role": "option", "text": "Asia Pacific"}]
    )
    assert DeepSeekCalculatorAgent._is_search_control(
        {"role": "searchbox", "name": "Search instances"}
    )
    assert not DeepSeekCalculatorAgent._is_search_control({"role": "", "name": "Data transfer out"})


def test_numeric_fill_must_be_grounded_in_customer_requirements() -> None:
    invented = BrowserAction(action="fill", control_id="c1", value="1000", reason="填写流量")
    requested = BrowserAction(action="fill", control_id="c1", value="3", reason="填写3TB流量")
    control = {"name": "Data transfer out", "text": "", "context": "TB/month"}

    assert not DeepSeekCalculatorAgent._numeric_fill_is_grounded(
        invented, control, {"quantity": 1, "requirements": {}}
    )
    assert DeepSeekCalculatorAgent._numeric_fill_is_grounded(
        requested,
        control,
        {"quantity": 1, "requirements": {"data_transfer_out_gib": 3072}},
    )


def test_zero_is_not_a_free_default_for_unrequested_lcu_metric() -> None:
    action = BrowserAction(action="fill", control_id="c1", value="0", reason="填写请求数")
    control = {"name": "Average requests per ALB per second", "context": "LCU"}

    assert not DeepSeekCalculatorAgent._numeric_fill_is_grounded(
        action, control, {"quantity": 1, "requirements": {}}
    )


def test_rds_gp3_keeps_higher_calculator_iops_floor() -> None:
    goal = {
        "requirements": {
            "storage_type": "gp3",
            "storage_gib": 500,
            "storage_iops": 3000,
        }
    }
    control = {
        "id": "c1",
        "tag": "input",
        "fillable": True,
        "name": "通用型 SSD（gp3）– IOPS 输入每卷的 IOPS 数量",
        "value": "12000",
    }

    adjustment = DeepSeekCalculatorAgent._adopt_visible_storage_floor("rds", [control], goal)

    assert adjustment is not None
    assert goal["requirements"]["requested_storage_iops"] == 3000
    assert goal["requirements"]["storage_iops"] == 12000
    assert "AWS 官网值" in adjustment[1]


def test_rds_gp3_does_not_override_valid_customer_iops() -> None:
    goal = {
        "requirements": {
            "storage_type": "gp3",
            "storage_iops": 15000,
        }
    }
    control = {
        "tag": "input",
        "fillable": True,
        "name": "gp3 IOPS",
        "value": "12000",
    }

    assert DeepSeekCalculatorAgent._adopt_visible_storage_floor("rds", [control], goal) is None
    assert goal["requirements"]["storage_iops"] == 15000


def test_lambda_target_control_is_ignored_without_customer_request() -> None:
    control = {"name": "Processed bytes for Lambda targets", "context": "LCU"}

    assert DeepSeekCalculatorAgent._control_is_out_of_scope(
        control, {"requirements": {"load_balancer_type": "application"}}
    )
    assert not DeepSeekCalculatorAgent._control_is_out_of_scope(
        control, {"requirements": {"target_type": "lambda"}}
    )


def test_minimum_alb_processed_bytes_suppresses_other_lcu_dimensions() -> None:
    goal = {
        "requirements": {
            "processed_bytes_ec2_ip_gib_per_hour": 0.01,
            "system_default_assumption": "minimum ALB input",
        }
    }

    assert DeepSeekCalculatorAgent._control_is_out_of_scope(
        {"name": "Average new connections per ALB"}, goal
    )
    assert not DeepSeekCalculatorAgent._control_is_out_of_scope(
        {"name": "Processed bytes for EC2 instances and IP addresses as targets"},
        goal,
    )


def test_visible_tb_unit_is_selected_from_customer_gib_conversion() -> None:
    action = BrowserAction(action="fill", control_id="c1", value="3", reason="填写3TB")
    option = DeepSeekCalculatorAgent._matching_unit_option(
        action,
        [
            {"id": "c2", "role": "option", "text": "GB/月"},
            {"id": "c3", "role": "option", "text": "TB/月"},
        ],
        {"requirements": {"data_transfer_out_gib": 3072}},
    )

    assert option is not None
    assert option["id"] == "c3"


def test_region_option_is_matched_by_stable_aws_region_code() -> None:
    option = DeepSeekCalculatorAgent._matching_region_option(
        [
            {
                "id": "c1",
                "role": "option",
                "text": "美国东部（俄亥俄州） us-east-2",
            },
            {
                "id": "c2",
                "role": "option",
                "text": "亚太地区（东京） ap-northeast-1",
            },
        ],
        "ap-northeast-1",
    )

    assert option is not None
    assert option["id"] == "c2"


def test_region_controls_are_hidden_after_region_is_confirmed() -> None:
    assert DeepSeekCalculatorAgent._is_region_control(
        {"role": "button", "name": "选择一个区域 亚太地区（东京）"}
    )
    assert DeepSeekCalculatorAgent._is_region_control(
        {"role": "option", "text": "亚太地区（新加坡） ap-southeast-1"}
    )
    assert not DeepSeekCalculatorAgent._is_region_control({"role": "option", "text": "8 vCPU"})


def test_location_type_is_locked_to_regular_region() -> None:
    regular = {
        "role": "button",
        "name": "选择位置类型位置类型信息 区域",
        "expanded": None,
    }
    local = {
        "role": "button",
        "name": "选择位置类型位置类型信息 本地区域",
        "expanded": None,
    }
    options = [
        {"id": "c1", "role": "option", "text": "区域"},
        {"id": "c2", "role": "option", "text": "本地区域"},
    ]

    assert DeepSeekCalculatorAgent._location_type_is_region([regular])
    assert not DeepSeekCalculatorAgent._location_type_is_region([local])
    assert DeepSeekCalculatorAgent._matching_region_location_type_option(options) == options[0]
    assert DeepSeekCalculatorAgent._is_location_type_control(local)


def test_elasticache_regular_cluster_scope_excludes_competing_products() -> None:
    controls = [
        {"id": "c1", "section": "Elasticache 无服务器设置"},
        {"id": "c2", "section": "集群设置"},
        {"id": "c3", "section": "数据分层集群设置"},
    ]

    scoped = DeepSeekCalculatorAgent._scope_service_controls(
        controls, "elasticache", {"requirements": {"engine": "redis"}}
    )

    assert [item["id"] for item in scoped] == ["c2"]


def test_redis_alias_uses_elasticache_scope_and_keeps_portal_options() -> None:
    controls = [
        {"id": "c1", "section": "Elasticache 无服务器设置"},
        {"id": "c2", "section": "集群设置"},
        {
            "id": "c3",
            "role": "option",
            "section": "数据分层集群设置",
            "text": "cache.m5.xlarge Memory: 12.93 GiB",
        },
    ]

    scoped = DeepSeekCalculatorAgent._scope_service_controls(
        controls, "redis", {"requirements": {"engine": "redis"}}
    )

    assert [item["id"] for item in scoped] == ["c2", "c3"]


def test_current_elasticache_model_is_confirmed_from_page_evidence() -> None:
    model = DeepSeekCalculatorAgent._selected_model_from_controls(
        [
            {
                "role": "combobox",
                "name": "Select an instance",
                "text": "cache.m5.xlarge",
            }
        ],
        {"requirements": {"memory_gib": 8}},
        "Selected Instance: cache.m5.xlarge vCPU: 4 Memory: 12.93 GiB",
    )

    assert model == "cache.m5.xlarge"


def test_elasticache_candidate_hints_parse_chinese_memory() -> None:
    hints = DeepSeekCalculatorAgent._candidate_hints(
        [
            {
                "id": "c1",
                "role": "option",
                "text": "cache.m5.large 内存: 6.38 GiB",
            },
            {
                "id": "c2",
                "role": "option",
                "text": "cache.m5.xlarge 内存: 12.93 GiB",
            },
        ],
        {"requirements": {"memory_gib": 8}},
    )

    assert [item["model"] for item in hints] == ["cache.m5.xlarge"]


def test_elasticache_completion_requires_nodes_engine_and_pricing() -> None:
    controls = [
        {"name": "缓存引擎 Redis", "text": "Redis"},
        {"name": "定价模型 OnDemand", "text": "OnDemand"},
    ]

    assert DeepSeekCalculatorAgent._elasticache_group_is_complete(
        controls,
        {"quantity": 2, "requirements": {"engine": "redis"}},
        2,
    )


def test_finish_is_blocked_until_explicit_additional_ebs_volume_is_filled() -> None:
    goal = {
        "requirements": {
            "system_disk_gib": 100,
            "additional_ebs_volumes": [
                {"size_gib": 300, "volume_type": "gp3", "count_per_instance": 1}
            ],
        }
    }

    assert DeepSeekCalculatorAgent._missing_additional_ebs_volumes(
        goal, [("EBS storage amount", "100")]
    ) == [300.0]
    assert (
        DeepSeekCalculatorAgent._missing_additional_ebs_volumes(
            goal,
            [("EBS storage amount", "100"), ("Additional volume storage", "300")],
        )
        == []
    )
