import os
from dotenv import load_dotenv

load_dotenv()


def setup_langsmith(
    project_name: str = "智能数据查询平台",
    enabled: bool = True
) -> bool:
    langsmith_api_key = os.getenv("LANGSMITH_API_KEY", "")
    
    if not langsmith_api_key or not enabled:
        print("[INFO] LangSmith tracing disabled (no API key or disabled in config)")
        return False
    
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = project_name
    os.environ["LANGCHAIN_API_KEY"] = langsmith_api_key
    os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
    
    print(f"[INFO] LangSmith tracing enabled for project: {project_name}")
    print(f"[INFO] View traces at: https://smith.langchain.com/o/1b85083a-ff6d-4497-b419-919a3c51d21b/projects/p/22a7d120-b837-460c-befd-7998cceea8fa")
    
    return True


def get_langsmith_config() -> dict:
    return {
        "enabled": os.getenv("LANGCHAIN_TRACING_V2") == "true",
        "project": os.getenv("LANGCHAIN_PROJECT", ""),
        "endpoint": os.getenv("LANGCHAIN_ENDPOINT", ""),
        "has_api_key": bool(os.getenv("LANGSMITH_API_KEY"))
    }
