from app.services.gemini_approval_policy import approval_card_is_safe


def test_gemini_robot_approves_only_known_astraquote_tools() -> None:
    assert approval_card_is_safe(
        "AstraQuote\nTool: build_estimate\nAllow Gemini to use this tool?",
        "Allow",
    )
    assert approval_card_is_safe("AstraQuote 工具：get_prices", "确认授权")
    assert not approval_card_is_safe("Gmail\nTool: send_email", "Allow")
    assert not approval_card_is_safe("AstraQuote\nTool: delete_account", "Allow")
    assert not approval_card_is_safe("AstraQuote\nTool: get_prices", "Deny")
