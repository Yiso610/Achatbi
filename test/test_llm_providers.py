"""Offline tests for provider configuration, failover, and response handling."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from typing import Any
import unittest
from unittest.mock import Mock, patch


# infrastructure.config validates its default provider at import time. Force a
# deterministic offline default before importing it, regardless of local env.
os.environ["LLM_PROVIDER"] = "qwen"
os.environ["DASHSCOPE_API_KEY"] = "offline-test-qwen-key"
for _chain_variable in (
    "ROUTER_MODEL_CHAIN",
    "SQL_GENERATOR_MODEL_CHAIN",
    "REFLECTOR_MODEL_CHAIN",
    "VISUALIZER_MODEL_CHAIN",
):
    os.environ[_chain_variable] = ""


from langchain_core.messages import AIMessage  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from infrastructure.config import (  # noqa: E402
    Config,
    ModelTarget,
    parse_model_chain,
)
from infrastructure.llm import (  # noqa: E402
    LLMManager,
    extract_response_text,
    is_retryable_provider_error,
)


def _multi_provider_config(**overrides: Any) -> Config:
    values: dict[str, Any] = {
        "llm_provider": "qwen",
        "dashscope_api_key": "offline-qwen-key",
        "deepseek_api_key": "offline-deepseek-key",
        "openai_api_key": "offline-openai-key",
        "google_api_key": "offline-google-key",
        "anthropic_api_key": "offline-anthropic-key",
        "dashscope_base_url": "https://qwen.test/v1",
        "deepseek_base_url": "https://deepseek.test/v1",
        "openai_base_url": "https://openai.test/v1",
        "anthropic_base_url": "",
        "router_model_chain": "qwen:qwen-router,deepseek:deepseek-router",
        "sql_generator_model_chain": "qwen:qwen-sql,openai:openai-sql",
        "reflector_model_chain": "deepseek:deepseek-reflector",
        "visualizer_model_chain": "gemini:gemini-viz,claude:claude-viz",
        "llm_max_retries": 0,
    }
    values.update(overrides)
    return Config(**values)


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeChatModel:
    def __init__(
        self,
        *,
        response: str = "",
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def invoke(self, model_input: Any, config: Any = None) -> AIMessage:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.response)

    async def ainvoke(
        self,
        model_input: Any,
        config: Any = None,
    ) -> AIMessage:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.response)


class ProviderConfigTestCase(unittest.TestCase):
    def test_legacy_qwen_configuration_remains_supported(self) -> None:
        config = Config(
            llm_provider="qwen",
            dashscope_api_key="offline-qwen-key",
            router_model_chain="",
            sql_generator_model_chain="",
            reflector_model_chain="",
            visualizer_model_chain="",
        )

        self.assertEqual(
            config.get_model_chain("router"),
            (ModelTarget(provider="qwen", model=config.router_model),),
        )

    def test_only_referenced_provider_requires_a_key(self) -> None:
        config = Config(
            llm_provider="openai",
            openai_api_key="offline-openai-key",
            dashscope_api_key="",
            router_model="test-router",
            sql_generator_model="test-sql",
            reflector_model="test-reflector",
            visualizer_model="test-visualizer",
            router_model_chain="",
            sql_generator_model_chain="",
            reflector_model_chain="",
            visualizer_model_chain="",
        )

        self.assertEqual(config.configured_providers(), {"openai"})

    def test_missing_key_for_referenced_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "Missing API key.*deepseek",
        ) as raised:
            _multi_provider_config(deepseek_api_key="")
        self.assertNotIn("offline-openai-key", str(raised.exception))
        self.assertNotIn("offline-anthropic-key", str(raised.exception))

    def test_model_chain_normalizes_aliases_and_removes_duplicates(self) -> None:
        chain = parse_model_chain(
            "dashscope:qwen-plus,claude:claude-model,"
            "anthropic:claude-model"
        )

        self.assertEqual(
            [(target.provider, target.model) for target in chain],
            [
                ("qwen", "qwen-plus"),
                ("anthropic", "claude-model"),
            ],
        )

    def test_openai_compatible_provider_requires_base_url(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Missing base URL.*openai"):
            Config(
                llm_provider="openai",
                openai_api_key="offline-openai-key",
                openai_base_url="",
                router_model="test-router",
                sql_generator_model="test-sql",
                reflector_model="test-reflector",
                visualizer_model="test-visualizer",
                router_model_chain="",
                sql_generator_model_chain="",
                reflector_model_chain="",
                visualizer_model_chain="",
            )

    def test_remote_provider_base_url_requires_https(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must use HTTPS"):
            _multi_provider_config(
                openai_base_url="http://api.example.test/v1",
            )

        config = _multi_provider_config(
            openai_base_url="http://127.0.0.1:8000/v1",
        )
        self.assertEqual(
            config.openai_base_url,
            "http://127.0.0.1:8000/v1",
        )

    def test_non_qwen_provider_requires_explicit_model_names(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "still use the Qwen default model",
        ):
            Config(
                llm_provider="openai",
                openai_api_key="offline-openai-key",
                router_model_chain="",
                sql_generator_model_chain="",
                reflector_model_chain="",
                visualizer_model_chain="",
            )

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Unsupported LLM provider"):
            parse_model_chain("unknown:model")

    def test_api_keys_are_redacted_from_config_repr(self) -> None:
        config = _multi_provider_config()
        rendered = repr(config)

        self.assertNotIn("offline-openai-key", rendered)
        self.assertNotIn("offline-anthropic-key", rendered)
        self.assertIn("**********", rendered)


class ProviderRuntimeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tracing_environment = patch.dict(
            os.environ,
            {
                "LANGSMITH_API_KEY": "",
                "LANGCHAIN_API_KEY": "",
                "LANGSMITH_TRACING": "false",
                "LANGCHAIN_TRACING_V2": "false",
            },
            clear=False,
        )
        self.tracing_environment.start()

    def tearDown(self) -> None:
        LLMManager.clear_cache()
        self.tracing_environment.stop()

    def test_transient_failure_uses_next_provider(self) -> None:
        config = _multi_provider_config()
        primary = _FakeChatModel(error=_StatusError(503))
        fallback = _FakeChatModel(response="fallback result")
        targets = (
            ModelTarget(provider="qwen", model="qwen-router"),
            ModelTarget(provider="deepseek", model="deepseek-router"),
        )

        with patch.object(
            LLMManager,
            "_create_model",
            side_effect=[primary, fallback],
        ):
            chain = LLMManager._create_model_chain(
                config,
                targets,
                temperature=0.0,
            )
            response = asyncio.run(chain.ainvoke("test prompt"))

        self.assertEqual(response.content, "fallback result")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_successful_primary_does_not_use_fallback(self) -> None:
        config = _multi_provider_config()
        primary = _FakeChatModel(response="primary result")
        fallback = _FakeChatModel(response="must not run")
        targets = (
            ModelTarget(provider="qwen", model="qwen-router"),
            ModelTarget(provider="deepseek", model="deepseek-router"),
        )

        with patch.object(
            LLMManager,
            "_create_model",
            side_effect=[primary, fallback],
        ):
            chain = LLMManager._create_model_chain(
                config,
                targets,
                temperature=0.0,
            )
            response = asyncio.run(chain.ainvoke("test prompt"))

        self.assertEqual(response.content, "primary result")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_empty_primary_response_uses_fallback(self) -> None:
        config = _multi_provider_config()
        primary = _FakeChatModel(response="   ")
        fallback = _FakeChatModel(response="fallback result")
        targets = (
            ModelTarget(provider="qwen", model="qwen-router"),
            ModelTarget(provider="deepseek", model="deepseek-router"),
        )

        with patch.object(
            LLMManager,
            "_create_model",
            side_effect=[primary, fallback],
        ):
            chain = LLMManager._create_model_chain(
                config,
                targets,
                temperature=0.0,
            )
            response = asyncio.run(chain.ainvoke("test prompt"))

        self.assertEqual(response.content, "fallback result")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_unavailable_fallback_does_not_block_primary(self) -> None:
        config = _multi_provider_config()
        primary = _FakeChatModel(response="primary result")
        targets = (
            ModelTarget(provider="qwen", model="qwen-router"),
            ModelTarget(provider="deepseek", model="deepseek-router"),
        )

        with patch.object(
            LLMManager,
            "_create_model",
            side_effect=[primary, RuntimeError("adapter unavailable")],
        ):
            chain = LLMManager._create_model_chain(
                config,
                targets,
                temperature=0.0,
            )
            response = asyncio.run(chain.ainvoke("test prompt"))

        self.assertEqual(response.content, "primary result")
        self.assertEqual(primary.calls, 1)

    def test_single_model_empty_response_is_rejected(self) -> None:
        config = _multi_provider_config()
        primary = _FakeChatModel(response=" ")
        targets = (
            ModelTarget(provider="qwen", model="qwen-router"),
        )

        with patch.object(
            LLMManager,
            "_create_model",
            return_value=primary,
        ):
            chain = LLMManager._create_model_chain(
                config,
                targets,
                temperature=0.0,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "returned an empty response",
            ):
                asyncio.run(chain.ainvoke("test prompt"))

        self.assertEqual(primary.calls, 1)

    def test_invalid_request_does_not_fail_over(self) -> None:
        config = _multi_provider_config()
        primary_error = _StatusError(400)
        primary = _FakeChatModel(error=primary_error)
        fallback = _FakeChatModel(response="must not run")
        targets = (
            ModelTarget(provider="qwen", model="qwen-router"),
            ModelTarget(provider="deepseek", model="deepseek-router"),
        )

        with patch.object(
            LLMManager,
            "_create_model",
            side_effect=[primary, fallback],
        ):
            chain = LLMManager._create_model_chain(
                config,
                targets,
                temperature=0.0,
            )
            with self.assertRaises(_StatusError) as raised:
                asyncio.run(chain.ainvoke("test prompt"))

        self.assertIs(raised.exception, primary_error)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_retryable_status_classification(self) -> None:
        self.assertTrue(is_retryable_provider_error(_StatusError(429)))
        self.assertTrue(is_retryable_provider_error(_StatusError(500)))
        self.assertFalse(is_retryable_provider_error(_StatusError(401)))
        self.assertFalse(is_retryable_provider_error(ValueError("bad input")))

    def test_openai_compatible_provider_factory_uses_own_credentials(self) -> None:
        config = _multi_provider_config()
        expected = {
            "qwen": ("offline-qwen-key", "https://qwen.test/v1"),
            "deepseek": (
                "offline-deepseek-key",
                "https://deepseek.test/v1",
            ),
            "openai": ("offline-openai-key", "https://openai.test/v1"),
        }

        for provider, (api_key, base_url) in expected.items():
            with self.subTest(provider=provider), patch(
                "infrastructure.llm.ChatOpenAI"
            ) as constructor:
                LLMManager._create_model(
                    config,
                    ModelTarget(provider=provider, model=f"{provider}-model"),
                    temperature=0.2,
                )

                arguments = constructor.call_args.kwargs
                self.assertEqual(arguments["model"], f"{provider}-model")
                self.assertEqual(
                    arguments["api_key"].get_secret_value(),
                    api_key,
                )
                self.assertEqual(arguments["base_url"], base_url)
                self.assertEqual(arguments["temperature"], 0.2)

    def test_native_provider_factories_use_provider_specific_adapters(self) -> None:
        config = _multi_provider_config()
        gemini_constructor = Mock(return_value=_FakeChatModel())
        anthropic_constructor = Mock(return_value=_FakeChatModel())
        gemini_module = types.ModuleType("langchain_google_genai")
        anthropic_module = types.ModuleType("langchain_anthropic")
        gemini_module.ChatGoogleGenerativeAI = gemini_constructor
        anthropic_module.ChatAnthropic = anthropic_constructor

        with patch.dict(
            sys.modules,
            {
                "langchain_google_genai": gemini_module,
                "langchain_anthropic": anthropic_module,
            },
        ):
            LLMManager._create_model(
                config,
                ModelTarget(provider="gemini", model="gemini-model"),
                temperature=0.0,
            )
            LLMManager._create_model(
                config,
                ModelTarget(provider="claude", model="claude-model"),
                temperature=0.1,
            )

        gemini_arguments = gemini_constructor.call_args.kwargs
        self.assertEqual(gemini_arguments["model"], "gemini-model")
        self.assertEqual(
            gemini_arguments["google_api_key"].get_secret_value(),
            "offline-google-key",
        )

        anthropic_arguments = anthropic_constructor.call_args.kwargs
        self.assertEqual(anthropic_arguments["model"], "claude-model")
        self.assertEqual(
            anthropic_arguments["api_key"].get_secret_value(),
            "offline-anthropic-key",
        )

    def test_installed_native_adapters_construct_without_network(self) -> None:
        config = _multi_provider_config()

        gemini = LLMManager._create_model(
            config,
            ModelTarget(provider="gemini", model="gemini-test"),
            temperature=0.0,
        )
        anthropic = LLMManager._create_model(
            config,
            ModelTarget(provider="anthropic", model="claude-test"),
            temperature=0.1,
        )

        self.assertEqual(type(gemini).__name__, "ChatGoogleGenerativeAI")
        self.assertEqual(type(anthropic).__name__, "ChatAnthropic")

    def test_cache_reuses_chain_until_cleared(self) -> None:
        config = _multi_provider_config()
        first_model = _FakeChatModel(response="first")
        second_model = _FakeChatModel(response="second")

        with patch("infrastructure.llm.get_config", return_value=config), patch.object(
            LLMManager,
            "_create_model_chain",
            side_effect=[first_model, second_model],
        ) as factory:
            first = LLMManager.get_router_model()
            cached = LLMManager.get_router_model()
            LLMManager.clear_cache()
            rebuilt = LLMManager.get_router_model()

        self.assertIs(first, cached)
        self.assertIsNot(first, rebuilt)
        self.assertEqual(factory.call_count, 2)

    def test_cache_discards_clients_after_key_rotation(self) -> None:
        first_config = _multi_provider_config(
            dashscope_api_key="first-offline-qwen-key",
        )
        rotated_config = _multi_provider_config(
            dashscope_api_key="rotated-offline-qwen-key",
        )
        first_model = _FakeChatModel(response="first")
        rotated_model = _FakeChatModel(response="rotated")

        with patch(
            "infrastructure.llm.get_config",
            side_effect=[first_config, rotated_config],
        ), patch.object(
            LLMManager,
            "_create_model_chain",
            side_effect=[first_model, rotated_model],
        ) as factory:
            first = LLMManager.get_router_model()
            rotated = LLMManager.get_router_model()

        self.assertIsNot(first, rotated)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(len(LLMManager._instances), 1)
        rendered_keys = repr(tuple(LLMManager._instances))
        self.assertNotIn("first-offline-qwen-key", rendered_keys)
        self.assertNotIn("rotated-offline-qwen-key", rendered_keys)

    def test_response_text_normalizes_content_blocks(self) -> None:
        response = type(
            "Response",
            (),
            {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "second"},
                ]
            },
        )()

        self.assertEqual(extract_response_text(response), "first\nsecond")
        self.assertEqual(extract_response_text(AIMessage(content="plain")), "plain")


if __name__ == "__main__":
    unittest.main()
