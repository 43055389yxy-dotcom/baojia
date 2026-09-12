from app.services.gemini_composer_selection import (
    choose_composer_candidate,
    is_gemini_send_label,
)


def test_new_task_uses_upper_left_prompt_box() -> None:
    candidates = [
        {
            "left": 161,
            "top": 71,
            "width": 292,
            "height": 24,
            "label": "为 Gemini 输入提示",
            "element": "new-task",
        },
        {
            "left": 680,
            "top": 751,
            "width": 490,
            "height": 24,
            "label": "为 Gemini 输入提示",
            "element": "current-chat",
        },
    ]

    assert (
        choose_composer_candidate(
            candidates,
            new_task=True,
            viewport_width=1365,
            viewport_height=900,
        )["element"]
        == "new-task"
    )


def test_current_chat_uses_lower_main_prompt_box() -> None:
    candidates = [
        {
            "left": 161,
            "top": 71,
            "width": 292,
            "height": 24,
            "label": "为 Gemini 输入提示",
            "element": "new-task",
        },
        {
            "left": 680,
            "top": 751,
            "width": 490,
            "height": 24,
            "label": "为 Gemini 输入提示",
            "element": "current-chat",
        },
    ]

    assert (
        choose_composer_candidate(
            candidates,
            new_task=False,
            viewport_width=1365,
            viewport_height=900,
        )["element"]
        == "current-chat"
    )


def test_new_task_does_not_fall_back_to_current_chat_box() -> None:
    candidates = [
        {
            "left": 419,
            "top": 804,
            "width": 490,
            "height": 24,
            "label": "为 Gemini 输入提示",
            "element": "current-chat",
        }
    ]

    assert (
        choose_composer_candidate(
            candidates,
            new_task=True,
            viewport_width=1170,
            viewport_height=900,
        )
        is None
    )


def test_hidden_quill_clipboards_are_ignored() -> None:
    candidates = [
        {
            "left": -100000,
            "top": 50,
            "width": 1,
            "height": 1,
            "label": "",
            "element": "quill-clipboard",
        },
        {
            "left": 150,
            "top": 60,
            "width": 300,
            "height": 24,
            "label": "Enter a prompt for Gemini",
            "element": "new-task",
        },
    ]

    assert (
        choose_composer_candidate(
            candidates,
            new_task=True,
            viewport_width=1365,
            viewport_height=900,
        )["element"]
        == "new-task"
    )


def test_only_the_semantic_send_button_is_accepted() -> None:
    assert is_gemini_send_label("发送")
    assert is_gemini_send_label("Send")
    assert is_gemini_send_label("Send message")
    assert not is_gemini_send_label("语音输入 (^⇧D)")
    assert not is_gemini_send_label("上传和工具")
