from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Quote:
    job_id: str
    chat_url: str
    deadline: float
    stable_since: float = 0.0


def test_headless_codex_command_uses_only_private_authenticated_mcp(monkeypatch):
    from tools import codex_cli_agent

    agent = codex_cli_agent.CodexCliAgent(
        active_quote_factory=Quote,
        quote_timeout_seconds=60,
    )
    command = agent._command("astraquote-codex-relay-test")
    rendered = " ".join(command)

    assert "--network caddy-net" in rendered
    assert "--rm -i --name astraquote-codex-relay-test" in rendered
    assert "--env-file /home/ec2-user/astraquote/config/mcp.env" in rendered
    assert 'mcp_servers.astraquote.url="http://astraquote:8200/v2/mcp"' in rendered
    assert "ASTRAQUOTE_INTERNAL_TOKEN" in rendered
    assert "--ignore-user-config" in rendered
    assert "--sandbox read-only" in rendered
    assert "--ask-for-approval never" in rendered


def test_agent_prompt_removes_ui_mention_and_disallows_dynamic_app_proxy():
    from tools.codex_cli_agent import _agent_prompt

    prompt = _agent_prompt("@AstraQuote quote one EC2 instance")

    assert "quote one EC2 instance" in prompt
    assert "codex_apps/astraquote" in prompt
    assert "Never use" in prompt
    assert prompt.count("@AstraQuote") == 0
