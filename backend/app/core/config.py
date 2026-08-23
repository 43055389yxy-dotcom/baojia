from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The API may be launched from the repository root, a service manager,
        # or a container.  Resolve the backend dotenv file explicitly instead
        # of relying on the process working directory.
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    ai_provider: str = "auto"
    bedrock_api_key: str = ""
    bedrock_base_url: str = "https://bedrock-mantle.us-east-1.api.aws/v1"
    bedrock_model: str = "zai.glm-4.7-flash"
    # Compatibility override for local tests and deployments that previously
    # supplied a single AI_API_KEY instead of a provider-specific key.
    ai_api_key_override: str = Field(
        default="", validation_alias=AliasChoices("ai_api_key", "AI_API_KEY")
    )

    @property
    def ai_api_key(self) -> str:
        if self.ai_api_key_override:
            return self.ai_api_key_override
        if self.ai_provider == "bedrock" or (self.ai_provider == "auto" and self.bedrock_api_key):
            return self.bedrock_api_key
        return self.deepseek_api_key

    @property
    def ai_base_url(self) -> str:
        if self.ai_provider == "bedrock" or (self.ai_provider == "auto" and self.bedrock_api_key):
            return self.bedrock_base_url
        return self.deepseek_base_url

    @property
    def ai_model(self) -> str:
        if self.ai_provider == "bedrock" or (self.ai_provider == "auto" and self.bedrock_api_key):
            return self.bedrock_model
        return self.deepseek_model

    @property
    def ai_display_name(self) -> str:
        if self.ai_model == "zai.glm-4.7-flash":
            return "Amazon Bedrock GLM 4.7 Flash"
        if self.ai_model == "deepseek.v3.2":
            return "Amazon Bedrock DeepSeek V3.2"
        if self.ai_model == "openai.gpt-5.6-luna":
            return "Amazon Bedrock GPT-5.6 Luna"
        if self.ai_model.startswith("openai.gpt-oss"):
            size = self.ai_model.rsplit("-", 1)[-1].upper()
            return f"Amazon Bedrock GPT OSS {size}"
        return "DeepSeek"

    aws_default_region: str = "us-east-1"
    aws_pricing_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    bcm_workload_estimate_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    bcm_allow_estimate_create: bool = True
    bcm_rate_type: str = "BEFORE_DISCOUNTS"
    bcm_poll_interval_seconds: float = 1.0
    bcm_poll_timeout_seconds: float = 30.0

    calculator_browser_channel: str = "chrome"
    calculator_enabled: bool = True
    # Public estimate links are intentionally disabled. The UI exports the
    # current result to Excel and the application does not retain quote links.
    calculator_generate_share_link: bool = False
    calculator_headless: bool = True
    calculator_timeout_seconds: float = 90.0
    # Keep a modest, irregular pace.  Long fixed pauses make a multi-service
    # estimate unnecessarily slow, while zero-delay bursts are fragile.
    calculator_action_delay_min_seconds: float = 0.35
    calculator_action_delay_max_seconds: float = 0.85
    calculator_navigation_delay_min_seconds: float = 1.1
    calculator_navigation_delay_max_seconds: float = 2.1
    calculator_ai_agent_enabled: bool = True
    calculator_ai_max_steps: int = 48
    calculator_ai_snapshot_chars: int = 5000
    calculator_ai_repeated_action_limit: int = 2
    calculator_ai_repeated_state_limit: int = 4

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # A developer machine can have unrelated AWS credentials exported.
        # For this local project, explicit backend/.env values must win; in
        # deployments without a dotenv file the normal process environment
        # remains the source of truth (for example IAM-role configuration).
        return init_settings, dotenv_settings, env_settings, file_secret_settings

    @field_validator("bcm_workload_estimate_ids", mode="before")
    @classmethod
    def parse_estimate_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
