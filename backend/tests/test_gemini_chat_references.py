from app.services.gemini_chat_references import (
    is_authenticated_gemini_workspace_url,
)


def test_only_private_spark_task_urls_prove_an_authenticated_workspace() -> None:
    assert is_authenticated_gemini_workspace_url(
        "https://gemini.google.com/spark/chat/046174d2550d9f1c"
    )
    assert not is_authenticated_gemini_workspace_url(
        "https://gemini.google.com/spark"
    )
    assert not is_authenticated_gemini_workspace_url(
        "https://accounts.google.com/signin"
    )
    assert not is_authenticated_gemini_workspace_url(
        "https://example.com/spark/chat/046174d2550d9f1c"
    )
