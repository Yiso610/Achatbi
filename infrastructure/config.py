from __future__ import annotations

import ipaddress
import os
from typing import Final
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


# 将 .env 变量加载到进程环境中。默认不覆盖部署环境已经注入的值。
load_dotenv()


SUPPORTED_LLM_PROVIDERS: Final[tuple[str, ...]] = (
    "qwen",
    "deepseek",
    "openai",
    "gemini",
    "anthropic",
)

LLM_ROLES: Final[tuple[str, ...]] = (
    "router",
    "sql_generator",
    "reflector",
    "visualizer",
)

_PROVIDER_ALIASES: Final[dict[str, str]] = {
    "qwen": "qwen",
    "dashscope": "qwen",
    "aliyun": "qwen",
    "deepseek": "deepseek",
    "openai": "openai",
    "gemini": "gemini",
    "google": "gemini",
    "claude": "anthropic",
    "anthropic": "anthropic",
}

_PLACEHOLDER_API_KEYS: Final[set[str]] = {
    "",
    "your_api_key_here",
    "your api key here",
    "your_dashscope_api_key_here",
    "sk-your-dashscope-api-key",
    "sk-your-deepseek-api-key",
    "sk-your-openai-api-key",
    "your-google-api-key",
    "sk-ant-your-anthropic-api-key",
}


def normalize_provider(value: str) -> str:
    """Return the canonical provider name used by the model factory."""
    normalized = str(value or "").strip().lower()
    provider = _PROVIDER_ALIASES.get(normalized)
    if provider is None:
        supported = ", ".join(SUPPORTED_LLM_PROVIDERS)
        raise ValueError(
            f"Unsupported LLM provider '{value}'. Supported providers: {supported}."
        )
    return provider


class ModelTarget(BaseModel):
    """One provider/model target in an ordered role-specific model chain."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        return normalize_provider(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        model = str(value or "").strip()
        if not model:
            raise ValueError("Model name cannot be empty.")
        return model


class ProviderSettings(BaseModel):
    """Credential and endpoint settings for one configured provider."""

    model_config = ConfigDict(frozen=True)

    provider: str
    api_key: SecretStr
    base_url: str | None = None


def parse_model_chain(value: str) -> tuple[ModelTarget, ...]:
    """
    Parse an ordered chain such as
    ``qwen:qwen-plus,deepseek:deepseek-chat``.
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        return ()

    targets: list[ModelTarget] = []
    seen: set[tuple[str, str]] = set()
    for raw_target in raw_value.split(","):
        target_text = raw_target.strip()
        if not target_text:
            continue
        if ":" not in target_text:
            raise ValueError(
                "Each model-chain entry must use the 'provider:model' format; "
                f"received '{target_text}'."
            )
        provider, model = target_text.split(":", 1)
        target = ModelTarget(provider=provider, model=model)
        identity = (target.provider, target.model)
        if identity not in seen:
            targets.append(target)
            seen.add(identity)

    if not targets:
        raise ValueError("A configured model chain cannot be empty.")
    return tuple(targets)


def _configured_api_key(secret: SecretStr) -> bool:
    value = secret.get_secret_value().strip().strip('"').strip("'")
    normalized = value.lower()
    if normalized in _PLACEHOLDER_API_KEYS:
        return False
    return not (
        "your-" in normalized
        or "your_" in normalized
        or ("your " in normalized and "key" in normalized)
    )


class Config(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        validate_assignment=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

    # 默认供应商。未设置新的 *_MODEL_CHAIN 时，与原有 *_MODEL 组合使用。
    llm_provider: str = Field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", "qwen"),
        description="Default LLM provider for legacy single-model settings",
    )

    # 各供应商凭据。只有被模型链实际引用的供应商才会被要求提供 Key。
    dashscope_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(
            os.getenv("DASHSCOPE_API_KEY", "")
        ),
        description="Alibaba Cloud Model Studio API key",
    )

    dashscope_base_url: str = Field(
        default_factory=lambda: os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        description="OpenAI-compatible endpoint for Qwen",
    )

    deepseek_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(
            os.getenv("DEEPSEEK_API_KEY", "")
        ),
        description="DeepSeek API key",
    )

    deepseek_base_url: str = Field(
        default_factory=lambda: os.getenv(
            "DEEPSEEK_BASE_URL",
            "https://api.deepseek.com",
        ),
        description="OpenAI-compatible endpoint for DeepSeek",
    )

    openai_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(
            os.getenv("OPENAI_API_KEY", "")
        ),
        description="OpenAI API key",
    )

    openai_base_url: str = Field(
        default_factory=lambda: os.getenv(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        ),
        description="OpenAI API endpoint",
    )

    google_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(
            os.getenv("GOOGLE_API_KEY")
            or os.getenv("GEMINI_API_KEY", "")
        ),
        description="Google AI / Gemini API key",
    )

    anthropic_api_key: SecretStr = Field(
        default_factory=lambda: SecretStr(
            os.getenv("ANTHROPIC_API_KEY", "")
        ),
        description="Anthropic / Claude API key",
    )

    anthropic_base_url: str = Field(
        default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL", ""),
        description="Optional Anthropic-compatible endpoint override",
    )

    # 原有模型变量继续保留，用于单供应商部署和向后兼容。
    router_model: str = Field(
        default_factory=lambda: os.getenv("ROUTER_MODEL", "qwen-plus"),
        description="Model used for intent routing",
    )

    sql_generator_model: str = Field(
        default_factory=lambda: os.getenv("SQL_GENERATOR_MODEL", "qwen-plus"),
        description="Model used for SQL generation",
    )

    reflector_model: str = Field(
        default_factory=lambda: os.getenv("REFLECTOR_MODEL", "qwen-plus"),
        description="Model used for error correction and reflection",
    )

    visualizer_model: str = Field(
        default_factory=lambda: os.getenv("VISUALIZER_MODEL", "qwen-plus"),
        description="Model used for chart recommendations",
    )

    # 可选的有序模型链。第一个模型为主模型，其余模型为瞬态故障时的备用。
    router_model_chain: str = Field(
        default_factory=lambda: os.getenv("ROUTER_MODEL_CHAIN", ""),
        description="Ordered provider:model chain for intent routing",
    )

    sql_generator_model_chain: str = Field(
        default_factory=lambda: os.getenv("SQL_GENERATOR_MODEL_CHAIN", ""),
        description="Ordered provider:model chain for SQL generation",
    )

    reflector_model_chain: str = Field(
        default_factory=lambda: os.getenv("REFLECTOR_MODEL_CHAIN", ""),
        description="Ordered provider:model chain for reflection",
    )

    visualizer_model_chain: str = Field(
        default_factory=lambda: os.getenv("VISUALIZER_MODEL_CHAIN", ""),
        description="Ordered provider:model chain for visualization",
    )

    # 应用设置
    max_retry_count: int = Field(
        default_factory=lambda: int(os.getenv("MAX_RETRY_COUNT", "3")),
        description="Maximum number of retry attempts for SQL generation",
        ge=1,
        le=5,
    )

    max_result_rows: int = Field(
        default_factory=lambda: int(os.getenv("MAX_RESULT_ROWS", "50")),
        description="Maximum number of rows to return from queries",
        ge=1,
        le=1000,
    )

    max_result_bytes: int = Field(
        default_factory=lambda: int(
            os.getenv("MAX_RESULT_BYTES", str(5 * 1024 * 1024))
        ),
        description="Maximum approximate in-memory query result size",
        ge=1024,
        le=100 * 1024 * 1024,
    )

    sql_query_timeout_seconds: int = Field(
        default_factory=lambda: int(
            os.getenv("SQL_QUERY_TIMEOUT_SECONDS", "15")
        ),
        description="Maximum SQLite execution time for one generated query",
        ge=1,
        le=120,
    )

    max_sql_query_bytes: int = Field(
        default_factory=lambda: int(
            os.getenv("MAX_SQL_QUERY_BYTES", "100000")
        ),
        description="Maximum generated SQLite SQL text size",
        ge=1000,
        le=1_000_000,
    )

    max_result_columns: int = Field(
        default_factory=lambda: int(
            os.getenv("MAX_RESULT_COLUMNS", "200")
        ),
        description="Maximum number of columns in a SQLite result",
        ge=1,
        le=1000,
    )

    # LLM 设置
    temperature: float = Field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.0")),
        description="Temperature for LLM generation (0 for deterministic)",
        ge=0.0,
        le=1.0,
    )

    llm_timeout: int = Field(
        default_factory=lambda: int(os.getenv("LLM_TIMEOUT", "60")),
        description="Timeout for each provider request in seconds",
        ge=1,
        le=300,
    )

    llm_max_retries: int = Field(
        default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "2")),
        description="Maximum retries within one provider before failover",
        ge=0,
        le=5,
    )

    @field_validator("llm_provider", mode="before")
    @classmethod
    def validate_default_provider(cls, value: str) -> str:
        return normalize_provider(value)

    @field_validator(
        "router_model",
        "sql_generator_model",
        "reflector_model",
        "visualizer_model",
    )
    @classmethod
    def validate_legacy_model_name(cls, value: str) -> str:
        model = str(value or "").strip()
        if not model:
            raise ValueError("Model name cannot be empty.")
        return model

    @field_validator(
        "dashscope_base_url",
        "deepseek_base_url",
        "openai_base_url",
        "anthropic_base_url",
    )
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = str(value or "").strip().rstrip("/")
        if not normalized:
            return normalized

        parsed = urlparse(normalized)
        if parsed.scheme == "https" and parsed.hostname:
            return normalized

        is_loopback = parsed.hostname == "localhost"
        if parsed.hostname and not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                is_loopback = False

        if parsed.scheme != "http" or not is_loopback:
            raise ValueError(
                "Provider base URLs must use HTTPS; plain HTTP is allowed "
                "only for localhost or a loopback IP address."
            )
        return normalized

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> "Config":
        implicit_qwen_defaults = [
            f"{role.upper()}_MODEL"
            for role in LLM_ROLES
            if (
                self.llm_provider != "qwen"
                and not getattr(self, f"{role}_model_chain").strip()
                and getattr(self, f"{role}_model") == "qwen-plus"
            )
        ]
        if implicit_qwen_defaults:
            variables = ", ".join(implicit_qwen_defaults)
            raise ValueError(
                f"LLM_PROVIDER is '{self.llm_provider}', but these roles still "
                f"use the Qwen default model 'qwen-plus': {variables}. "
                "Set provider-appropriate model names or configure the "
                "corresponding *_MODEL_CHAIN variables."
            )

        missing_providers: list[str] = []
        missing_base_urls: list[str] = []
        for provider in sorted(self.configured_providers()):
            settings = self.get_provider_settings(provider)
            if not _configured_api_key(settings.api_key):
                missing_providers.append(provider)
            if (
                provider in {"qwen", "deepseek", "openai"}
                and not settings.base_url
            ):
                missing_base_urls.append(provider)

        if missing_providers:
            providers = ", ".join(missing_providers)
            raise ValueError(
                "Missing API key for configured LLM provider(s): "
                f"{providers}. Configure the corresponding environment variable."
            )
        if missing_base_urls:
            providers = ", ".join(missing_base_urls)
            raise ValueError(
                "Missing base URL for configured OpenAI-compatible provider(s): "
                f"{providers}."
            )
        return self

    def get_model_chain(self, role: str) -> tuple[ModelTarget, ...]:
        """Return the ordered model chain for one Agent role."""
        if role not in LLM_ROLES:
            raise ValueError(f"Unknown LLM role '{role}'.")

        configured_chain = getattr(self, f"{role}_model_chain")
        parsed_chain = parse_model_chain(configured_chain)
        if parsed_chain:
            return parsed_chain

        return (
            ModelTarget(
                provider=self.llm_provider,
                model=getattr(self, f"{role}_model"),
            ),
        )

    def configured_providers(self) -> set[str]:
        """Return providers referenced by any active role model chain."""
        return {
            target.provider
            for role in LLM_ROLES
            for target in self.get_model_chain(role)
        }

    def get_provider_settings(self, provider: str) -> ProviderSettings:
        """Resolve one canonical provider to its credential settings."""
        canonical = normalize_provider(provider)
        if canonical == "qwen":
            return ProviderSettings(
                provider=canonical,
                api_key=self.dashscope_api_key,
                base_url=self.dashscope_base_url,
            )
        if canonical == "deepseek":
            return ProviderSettings(
                provider=canonical,
                api_key=self.deepseek_api_key,
                base_url=self.deepseek_base_url,
            )
        if canonical == "openai":
            return ProviderSettings(
                provider=canonical,
                api_key=self.openai_api_key,
                base_url=self.openai_base_url,
            )
        if canonical == "gemini":
            return ProviderSettings(
                provider=canonical,
                api_key=self.google_api_key,
            )
        return ProviderSettings(
            provider=canonical,
            api_key=self.anthropic_api_key,
            base_url=self.anthropic_base_url or None,
        )


try:
    config = Config()
except Exception as e:
    print(f"[ERROR] Configuration Error: {e}")
    print("\n[INFO] Quick Fix:")
    print("1. Copy .env.example to .env")
    print(
        "2. Configure an API key for every provider referenced by "
        "LLM_PROVIDER or a *_MODEL_CHAIN"
    )
    raise


def get_config() -> Config:
    return config
