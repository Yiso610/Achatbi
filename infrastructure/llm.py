from typing import Dict

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from infrastructure.config import Config, get_config


class LLMManager:
    # 为每个agent调用创并缓存模型

    _instances: Dict[str, BaseChatModel] = {}

    @staticmethod
    def _create_model(
        config: Config,
        model: str,
        temperature: float,
    ) -> ChatOpenAI:
        # 创建模型客户端
        return ChatOpenAI(
            model=model,
            api_key=config.dashscope_api_key,
            base_url=config.dashscope_base_url,
            temperature=temperature,
            timeout=config.llm_timeout,
            max_retries=config.llm_max_retries,
        )

    @classmethod
    def get_router_model(cls) -> BaseChatModel:
        if "router" not in cls._instances:
            config = get_config()
            cls._instances["router"] = cls._create_model(
                config=config,
                model=config.router_model,
                temperature=0.0,
            )
        return cls._instances["router"]

    @classmethod
    def get_sql_generator_model(cls) -> BaseChatModel:
        if "sql_generator" not in cls._instances:
            config = get_config()
            cls._instances["sql_generator"] = cls._create_model(
                config=config,
                model=config.sql_generator_model,
                temperature=config.temperature,
            )
        return cls._instances["sql_generator"]

    @classmethod
    def get_reflector_model(cls) -> BaseChatModel:
        if "reflector" not in cls._instances:
            config = get_config()
            cls._instances["reflector"] = cls._create_model(
                config=config,
                model=config.reflector_model,
                temperature=0.1,
            )
        return cls._instances["reflector"]

    @classmethod
    def get_visualizer_model(cls) -> BaseChatModel:
        if "visualizer" not in cls._instances:
            config = get_config()
            cls._instances["visualizer"] = cls._create_model(
                config=config,
                model=config.visualizer_model,
                temperature=0.0,
            )
        return cls._instances["visualizer"]

    @classmethod
    def clear_cache(cls) -> None:
        cls._instances.clear()


def get_router_llm() -> BaseChatModel:
    return LLMManager.get_router_model()


def get_sql_generator_llm() -> BaseChatModel:
    return LLMManager.get_sql_generator_model()


def get_reflector_llm() -> BaseChatModel:
    return LLMManager.get_reflector_model()


def get_visualizer_llm() -> BaseChatModel:
    return LLMManager.get_visualizer_model()
