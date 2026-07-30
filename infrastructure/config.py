import os
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv


# 将env变量加载到环境变量中
load_dotenv()


class Config(BaseModel):
    dashscope_api_key: str = Field(
        default_factory=lambda: os.getenv("DASHSCOPE_API_KEY", ""),
        description="Alibaba Cloud Model Studio API key",
    )

    dashscope_base_url: str = Field(
        default_factory=lambda: os.getenv(
            "DASHSCOPE_BASE_URL",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        description="OpenAI-compatible endpoint for Qwen",
    )

    # 模型配置
    router_model: str = Field(
        default_factory=lambda: os.getenv("ROUTER_MODEL", "qwen-plus"),
        description="Qwen model for intent routing and fast operations",
    )

    sql_generator_model: str = Field(
        default_factory=lambda: os.getenv("SQL_GENERATOR_MODEL", "qwen-plus"),
        description="Qwen model for SQL generation",
    )

    reflector_model: str = Field(
        default_factory=lambda: os.getenv("REFLECTOR_MODEL", "qwen-plus"),
        description="Qwen model for error correction and reflection",
    )

    visualizer_model: str = Field(
        default_factory=lambda: os.getenv("VISUALIZER_MODEL", "qwen-plus"),
        description="Qwen model for chart recommendations",
    )
    
    # 应用设置
    max_retry_count: int = Field(
        default_factory=lambda: int(os.getenv("MAX_RETRY_COUNT", "3")),
        description="Maximum number of retry attempts for SQL generation",
        ge=1,
        le=5
    )
    
    max_result_rows: int = Field(
        default_factory=lambda: int(os.getenv("MAX_RESULT_ROWS", "50")),
        description="Maximum number of rows to return from queries",
        ge=1,
        le=1000
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
        description="Timeout for each Qwen request in seconds",
        ge=1,
        le=300,
    )

    llm_max_retries: int = Field(
        default_factory=lambda: int(os.getenv("LLM_MAX_RETRIES", "2")),
        description="Maximum retries for transient Qwen API failures",
        ge=0,
        le=5,
    )

    @field_validator("dashscope_api_key")
    @classmethod
    def validate_api_key(cls, v: str) -> str:
        placeholders = {
            "",
            "your_api_key_here",
            "your_dashscope_api_key_here",
            "sk-your-dashscope-api-key",
        }
        if v.strip().strip('"').strip("'") in placeholders:
            raise ValueError(
                "DASHSCOPE_API_KEY is required."
            )
        return v

    @field_validator("dashscope_base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        value = v.strip().rstrip("/")
        if not value.startswith(("https://", "http://")):
            raise ValueError("DASHSCOPE_BASE_URL must be an HTTP(S) URL.")
        return value

    class Config:
        frozen = True  # 配置对象创建后不能修改
        validate_assignment = True

try:
    config = Config()
except Exception as e:
    print(f"[ERROR] Configuration Error: {e}")
    print("\n[INFO] Quick Fix:")
    print("1. Copy .env.example to .env")
    print("2. Add your DASHSCOPE_API_KEY to the .env file")
    raise


def get_config() -> Config:
    return config
