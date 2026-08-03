from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import (
    Runnable,
    RunnableConfig,
    RunnableLambda,
)
from langchain_openai import ChatOpenAI

from infrastructure.config import (
    Config,
    LLM_ROLES,
    ModelTarget,
    get_config,
)


logger = logging.getLogger(__name__)

ChatRunnable = Runnable[Any, BaseMessage]
ModelCacheKey = tuple[str, str, float, tuple[tuple[str, str], ...]]


class RetryableProviderError(RuntimeError):
    """A sanitized transient provider error that may trigger failover."""


def _iter_exception_chain(error: BaseException):
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        yield current
        visited.add(id(current))
        current = current.__cause__ or current.__context__


def is_retryable_provider_error(error: BaseException) -> bool:
    """
    Return whether a provider failure is suitable for cross-provider failover.

    Authentication, invalid request, content policy, and application parsing
    errors are intentionally not considered retryable.
    """
    retryable_name_fragments = (
        "apiconnection",
        "apitimeout",
        "connecterror",
        "connectionerror",
        "deadlineexceeded",
        "internalserver",
        "ratelimit",
        "readtimeout",
        "resourceexhausted",
        "servererror",
        "serviceunavailable",
        "temporarilyunavailable",
        "timeout",
        "timeouterror",
        "toomanyrequests",
    )

    for cause in _iter_exception_chain(error):
        if isinstance(
            cause,
            (
                asyncio.TimeoutError,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return True

        status_code = getattr(cause, "status_code", None)
        if hasattr(status_code, "value"):
            status_code = status_code.value
        try:
            numeric_status = int(status_code)
        except (TypeError, ValueError):
            numeric_status = None

        if numeric_status in {408, 409, 425, 429}:
            return True
        if numeric_status is not None and 500 <= numeric_status <= 599:
            return True

        compact_name = cause.__class__.__name__.replace("_", "").lower()
        if any(fragment in compact_name for fragment in retryable_name_fragments):
            return True

    return False


def extract_response_text(response: Any) -> str:
    """Normalize plain text and provider content blocks into one string."""
    content = getattr(response, "content", response)

    if isinstance(content, str):
        return content
    if content is None:
        return ""

    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""

    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return str(content)


class LLMManager:
    """Create and cache provider-neutral model chains for each Agent role."""

    _instances: dict[ModelCacheKey, ChatRunnable] = {}
    _active_config_fingerprint: str | None = None

    @staticmethod
    def _config_fingerprint(config: Config) -> str:
        """
        Build a non-plaintext identity for cache-safe config/key rotation.

        API keys are included only in the one-way digest input and are never
        stored in the cache key or logs.
        """
        providers = {}
        for provider in sorted(config.configured_providers()):
            settings = config.get_provider_settings(provider)
            providers[provider] = {
                "api_key": settings.api_key.get_secret_value(),
                "base_url": settings.base_url,
            }

        payload = {
            "llm_timeout": config.llm_timeout,
            "llm_max_retries": config.llm_max_retries,
            "roles": {
                role: [
                    (target.provider, target.model)
                    for target in config.get_model_chain(role)
                ]
                for role in LLM_ROLES
            },
            "providers": providers,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _create_model(
        config: Config,
        target: ModelTarget,
        temperature: float,
    ) -> BaseChatModel:
        settings = config.get_provider_settings(target.provider)
        api_key = settings.api_key

        if target.provider in {"qwen", "deepseek", "openai"}:
            return ChatOpenAI(
                model=target.model,
                api_key=api_key,
                base_url=settings.base_url,
                temperature=temperature,
                timeout=config.llm_timeout,
                max_retries=config.llm_max_retries,
            )

        if target.provider == "gemini":
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
            except ImportError as error:
                raise RuntimeError(
                    "Gemini is configured but langchain-google-genai is not "
                    "installed. Run: pip install -r requirements.txt"
                ) from error

            return ChatGoogleGenerativeAI(
                model=target.model,
                google_api_key=api_key,
                temperature=temperature,
                timeout=config.llm_timeout,
                max_retries=config.llm_max_retries,
            )

        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as error:
            raise RuntimeError(
                "Claude is configured but langchain-anthropic is not installed. "
                "Run: pip install -r requirements.txt"
            ) from error

        anthropic_options: dict[str, Any] = {}
        if settings.base_url:
            anthropic_options["base_url"] = settings.base_url
        return ChatAnthropic(
            model=target.model,
            api_key=api_key,
            temperature=temperature,
            timeout=config.llm_timeout,
            max_retries=config.llm_max_retries,
            **anthropic_options,
        )

    @staticmethod
    def _wrap_fallback_candidate(
        model: BaseChatModel,
        target: ModelTarget,
    ) -> ChatRunnable:
        def invoke(
            model_input: Any,
            config: RunnableConfig,
        ) -> BaseMessage:
            try:
                response = model.invoke(model_input, config=config)
                LLMManager._raise_for_empty_response(response, target)
                return response
            except Exception as error:
                LLMManager._raise_for_failover(error, target)
                raise

        async def ainvoke(
            model_input: Any,
            config: RunnableConfig,
        ) -> BaseMessage:
            try:
                response = await model.ainvoke(
                    model_input,
                    config=config,
                )
                LLMManager._raise_for_empty_response(response, target)
                return response
            except Exception as error:
                LLMManager._raise_for_failover(error, target)
                raise

        return RunnableLambda(
            invoke,
            afunc=ainvoke,
            name=f"{target.provider}:{target.model}",
        )

    @staticmethod
    def _raise_for_empty_response(
        response: BaseMessage,
        target: ModelTarget,
    ) -> None:
        if extract_response_text(response).strip():
            return

        logger.warning(
            "Empty LLM provider response: provider=%s model=%s",
            target.provider,
            target.model,
        )
        raise RetryableProviderError(
            f"Provider '{target.provider}' returned an empty response while "
            f"using model '{target.model}'."
        )

    @staticmethod
    def _raise_for_failover(
        error: Exception,
        target: ModelTarget,
    ) -> None:
        if not is_retryable_provider_error(error):
            return

        logger.warning(
            "Transient LLM provider failure: provider=%s model=%s error_type=%s",
            target.provider,
            target.model,
            error.__class__.__name__,
        )
        raise RetryableProviderError(
            f"Provider '{target.provider}' temporarily failed while using "
            f"model '{target.model}' ({error.__class__.__name__})."
        ) from error

    @classmethod
    def _create_model_chain(
        cls,
        config: Config,
        targets: tuple[ModelTarget, ...],
        temperature: float,
    ) -> ChatRunnable:
        if not targets:
            raise ValueError("At least one LLM model target is required.")

        available_candidates: list[tuple[BaseChatModel, ModelTarget]] = [
            (
                cls._create_model(
                    config=config,
                    target=targets[0],
                    temperature=temperature,
                ),
                targets[0],
            )
        ]
        for target in targets[1:]:
            try:
                model = cls._create_model(
                    config=config,
                    target=target,
                    temperature=temperature,
                )
            except Exception as error:
                logger.warning(
                    "Skipping unavailable LLM fallback: "
                    "provider=%s model=%s error_type=%s",
                    target.provider,
                    target.model,
                    error.__class__.__name__,
                )
                continue
            available_candidates.append((model, target))

        candidates = [
            cls._wrap_fallback_candidate(model, target)
            for model, target in available_candidates
        ]
        if len(candidates) == 1:
            return candidates[0]

        return candidates[0].with_fallbacks(
            candidates[1:],
            exceptions_to_handle=(RetryableProviderError,),
        )

    @classmethod
    def _get_role_model(
        cls,
        role: str,
        temperature: float,
    ) -> ChatRunnable:
        config = get_config()
        targets = config.get_model_chain(role)
        chain_signature = tuple(
            (target.provider, target.model) for target in targets
        )
        config_fingerprint = cls._config_fingerprint(config)
        if cls._active_config_fingerprint != config_fingerprint:
            cls._instances.clear()
            cls._active_config_fingerprint = config_fingerprint

        cache_key: ModelCacheKey = (
            config_fingerprint,
            role,
            temperature,
            chain_signature,
        )

        if cache_key not in cls._instances:
            cls._instances[cache_key] = cls._create_model_chain(
                config=config,
                targets=targets,
                temperature=temperature,
            )
        return cls._instances[cache_key]

    @classmethod
    def get_router_model(cls) -> ChatRunnable:
        return cls._get_role_model("router", temperature=0.0)

    @classmethod
    def get_sql_generator_model(cls) -> ChatRunnable:
        config = get_config()
        return cls._get_role_model(
            "sql_generator",
            temperature=config.temperature,
        )

    @classmethod
    def get_reflector_model(cls) -> ChatRunnable:
        return cls._get_role_model("reflector", temperature=0.1)

    @classmethod
    def get_visualizer_model(cls) -> ChatRunnable:
        return cls._get_role_model("visualizer", temperature=0.0)

    @classmethod
    def clear_cache(cls) -> None:
        cls._instances.clear()
        cls._active_config_fingerprint = None


def get_router_llm() -> ChatRunnable:
    return LLMManager.get_router_model()


def get_sql_generator_llm() -> ChatRunnable:
    return LLMManager.get_sql_generator_model()


def get_reflector_llm() -> ChatRunnable:
    return LLMManager.get_reflector_model()


def get_visualizer_llm() -> ChatRunnable:
    return LLMManager.get_visualizer_model()
