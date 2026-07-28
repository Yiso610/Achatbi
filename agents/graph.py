import asyncio
from pathlib import Path
from typing import Literal, Optional
from langgraph.graph import StateGraph, END

from agents.state import AgentState
from agents.nodes import (
    intent_router_node,
    sql_generator_node,
    sql_validator_node,
    executor_node,
    reflector_node,
    visualizer_node,
    format_response_node
)
# 为已经编译好的工作流图建立全局缓存，并尽量只创建一份
_COMPILED_GRAPH: Optional[StateGraph] = None
_GRAPH_LOCK = asyncio.Lock()


def should_continue_after_routing(state: AgentState) -> Literal["generate_sql", "end"]:
    is_relevant = state.get("is_relevant") if isinstance(state, dict) else state.is_relevant
    if is_relevant:
        return "generate_sql"
    else:
        return "end"


def should_continue_after_validation(
    state: AgentState
) -> Literal["execute_query", "reflect"]:
    validation_passed = state.get("validation_passed") if isinstance(state, dict) else state.validation_passed
    if validation_passed:
        return "execute_query"
    else:
        return "reflect"


def should_continue_after_execution(
    state: AgentState
) -> Literal["visualize", "reflect"]:
    error = state.get("error") if isinstance(state, dict) else state.error
    if error:
        return "reflect"
    else:
        return "visualize"


def should_continue_after_reflection(
    state: AgentState
) -> Literal["validate_sql", "end"]:
    final_response = state.get("final_response") if isinstance(state, dict) else state.final_response
    if final_response:
        return "end"
    else:
        # 重试
        return "validate_sql"


def build_graph() -> StateGraph:
    # 创建工作流图
    workflow = StateGraph(AgentState)
    
    # 添加节点
    workflow.add_node("route_intent", intent_router_node)
    workflow.add_node("generate_sql", sql_generator_node)
    workflow.add_node("validate_sql", sql_validator_node)
    workflow.add_node("execute_query", executor_node)
    workflow.add_node("reflect", reflector_node)
    workflow.add_node("visualize", visualizer_node)
    workflow.add_node("format_response", format_response_node)
    
    # 设置入口节点
    workflow.set_entry_point("route_intent")
    
    # 添加条件边
    workflow.add_conditional_edges(
        "route_intent",
        should_continue_after_routing,
        {
            "generate_sql": "generate_sql",
            "end": "format_response"
        }
    )
    
    # 添加从 SQL 生成节点到 SQL 校验节点的线性边
    workflow.add_edge("generate_sql", "validate_sql")
    
    # 添加 SQL 校验后的条件边
    workflow.add_conditional_edges(
        "validate_sql",
        should_continue_after_validation,
        {
            "execute_query": "execute_query",
            "reflect": "reflect"
        }
    )
    
    # 添加 SQL 执行后的条件边
    workflow.add_conditional_edges(
        "execute_query",
        should_continue_after_execution,
        {
            "visualize": "visualize",
            "reflect": "reflect"
        }
    )
    
    # 添加反思节点后的条件边
    workflow.add_conditional_edges(
        "reflect",
        should_continue_after_reflection,
        {
            "validate_sql": "validate_sql",
            "end": "format_response"
        }
    )
    
    # 添加从可视化节点到响应格式化节点的线性边
    workflow.add_edge("visualize", "format_response")
    
    # 响应格式化完成后进入工作流结束标记
    workflow.add_edge("format_response", END)
    
    return workflow.compile()


# 获取已经编译好的工作流图
async def get_compiled_graph() -> StateGraph:
    global _COMPILED_GRAPH
    
    if _COMPILED_GRAPH is None:
        async with _GRAPH_LOCK:
            # Double-check after acquiring lock
            if _COMPILED_GRAPH is None:
                _COMPILED_GRAPH = build_graph()
    
    return _COMPILED_GRAPH


def clear_graph_cache():
    global _COMPILED_GRAPH
    _COMPILED_GRAPH = None


def run_agent(
    question: str,
    database_path: str | Path,
) -> AgentState:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    return loop.run_until_complete(arun_agent(question, database_path))


async def arun_agent(
    question: str,
    database_path: str | Path,
) -> AgentState:
    # 获取已经缓存的工作流图
    graph = await get_compiled_graph()
    
    # 初始化状态
    selected_database = Path(database_path).expanduser().resolve()
    initial_state = AgentState(
        question=question,
        database_path=str(selected_database),
    )
    
    # 异步运行
    final_state = await graph.ainvoke(initial_state.model_dump())
    
    return AgentState(**final_state)


# 导出工作流图，用于可视化或调试
def get_graph_visualization() -> str:
    graph = build_graph()
    try:
        return graph.get_graph().draw_mermaid()
    except:
        return "Graph visualization not available. Install mermaid support."
