import re
import json
from pathlib import Path
from typing import Dict, Any


from agents.state import AgentState
from infrastructure.llm import (
    extract_response_text,
    get_router_llm,
    get_sql_generator_llm,
    get_reflector_llm,
    get_visualizer_llm
)
from infrastructure.prompts import (
    get_intent_router_prompt,
    get_sql_generator_prompt,
    get_error_reflection_prompt,
    get_visualization_prompt
)
from infrastructure.db_manager import DatabaseManager
from infrastructure.config import get_config


# 创建一个数据库管理器实例即当前处理的数据库
def get_database_manager(state: AgentState | Dict[str, Any]) -> DatabaseManager:
    if isinstance(state, AgentState):
        database_path = state.database_path
    else:
        database_path = state.get("database_path", "")

    if not database_path:
        raise ValueError("A selected database path is required for analysis.")
    return DatabaseManager(Path(database_path))


async def intent_router_node(state: AgentState) -> Dict[str, Any]:
    question = state.question if isinstance(state, AgentState) else state["question"]
    db_manager = get_database_manager(state)

    # 获取router模型
    llm = get_router_llm()
    
    # 生成提示词
    schema = db_manager.get_annotated_schema()
    prompt = get_intent_router_prompt(question, schema)
    
    try:
        # 异步调用模型，读取并统一模型回答
        response = await llm.ainvoke(prompt)
        response_text = extract_response_text(response).strip().upper()
        
        # 判断是否相关
        is_relevant = "RELEVANT" in response_text and "NOT_RELEVANT" not in response_text
        
        return {
            "is_relevant": is_relevant,
            "final_response": "" if is_relevant else (
                "当前问题超出了所选数据库的数据范围。"
                "请围绕数据库中的字段和业务数据重新提问。"
            )
        }
        
    except Exception as e:
        # 发生错误时，默认认为问题相关，并交给后续节点处理
        print(
            "[WARNING] Intent router error_type="
            f"{e.__class__.__name__}"
        )
        return {
            "is_relevant": True,
            "final_response": ""
        }


async def sql_generator_node(state: AgentState) -> Dict[str, Any]:
    question = state.question if isinstance(state, AgentState) else state["question"]
    db_manager = get_database_manager(state)
    
    # 获取带业务说明的数据库 Schema
    schema = db_manager.get_annotated_schema()
    
    prompt = get_sql_generator_prompt(question, schema)
    llm = get_sql_generator_llm()
    
    try:
        response = await llm.ainvoke(prompt)
        response_text = extract_response_text(response)
        
        # 提取推理过程，即 SQL 代码块之前的所有内容
        reasoning_match = re.search(
            r'(?:Reasoning|推理|查询思路)\s*[:：](.*?)```sql',
            response_text,
            re.DOTALL | re.IGNORECASE,
        )
        if reasoning_match:
            reasoning = reasoning_match.group(1).strip()
        else:
            sql_block_start = response_text.lower().find("```sql")
            reasoning = (
                response_text[:sql_block_start].strip()
                if sql_block_start >= 0
                else ""
            )
            reasoning = re.sub(
                r"^(?:Reasoning|推理|查询思路)\s*[:：]\s*",
                "",
                reasoning,
                flags=re.IGNORECASE,
            ).strip()
            if not reasoning:
                reasoning = "未提供查询思路说明。"
        
        # 从代码块中提取 SQL 查询语句
        sql_match = re.search(r'```sql\s*(.*?)\s*```', response_text, re.DOTALL)
        if sql_match:
            sql_query = sql_match.group(1).strip()
        else:
            # 备用方案：直接尝试将回答内容作为 SQL 语句
            sql_query = response_text.strip()
        
        return {
            "sql_query": sql_query,
            "reasoning": reasoning,
            "validation_passed": False,  # 后续由SQL校验节点设置
            "validation_error": ""
        }
        
    except Exception as e:
        return {
            "sql_query": "",
            "reasoning": "",
            "validation_passed": False,
            "validation_error": (
                f"SQL 生成失败（{e.__class__.__name__}）。"
            )
        }


def sql_validator_node(state: AgentState) -> Dict[str, Any]:
    sql_query = state.sql_query if isinstance(state, AgentState) else state["sql_query"]
    db_manager = get_database_manager(state)
    
    # 安全性检验
    dangerous_keywords = [
        r'\bDROP\b',
        r'\bDELETE\b',
        r'\bUPDATE\b',
        r'\bINSERT\b',
        r'\bALTER\b',
        r'\bTRUNCATE\b',
        r'\bCREATE\b',
        r'\bREPLACE\b'
    ]
    
    for keyword_pattern in dangerous_keywords:
        if re.search(keyword_pattern, sql_query, re.IGNORECASE):
            keyword = keyword_pattern.replace(r'\b', '')
            return {
                "validation_passed": False,
                "validation_error": (
                    f"查询中包含禁止使用的关键字：{keyword}。"
                    "为保证安全，仅允许执行 SELECT 查询。"
                )
            }
    
    # 相关性检查：确保查询只引用有效的表（intent router）
    valid_tables = set(db_manager.get_table_names())
    
    # 从查询中提取表名（使用简单的模式匹配）
    # 用于识别 FROM 和 JOIN 子句中的表名
    table_pattern = r'\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)'
    referenced_tables = set(re.findall(table_pattern, sql_query, re.IGNORECASE))
    cte_pattern = (
        r'(?:\bWITH(?:\s+RECURSIVE)?\b|,)\s*'
        r'([a-zA-Z_][a-zA-Z0-9_]*)\s+AS\s*\('
    )
    cte_names = set(re.findall(cte_pattern, sql_query, re.IGNORECASE))

    invalid_tables = referenced_tables - valid_tables - cte_names
    if invalid_tables:
        return {
            "validation_passed": False,
            "validation_error": (
                f"查询引用了不存在的数据表：{', '.join(invalid_tables)}。"
                f"当前可用数据表：{', '.join(sorted(valid_tables))}"
            )
        }
    
    # 语法检查: 调用数据库语句检查
    is_valid, syntax_error = db_manager.validate_query_syntax(sql_query)
    if not is_valid:
        return {
            "validation_passed": False,
            "validation_error": f"SQL 语法错误：{syntax_error}"
        }
    
    # 强制输出LIMIT：若无自动添加
    config = get_config()
    limit_match = re.search(
        r'\bLIMIT\s+(\d+)',
        sql_query,
        re.IGNORECASE,
    )
    if limit_match:
        requested_limit = int(limit_match.group(1))
        if requested_limit > config.max_result_rows:
            sql_query = (
                sql_query[:limit_match.start(1)]
                + str(config.max_result_rows)
                + sql_query[limit_match.end(1):]
            )
    else:
        sql_query = sql_query.rstrip(';')
        sql_query = f"{sql_query} LIMIT {config.max_result_rows};"
    
    # 通过所有检查
    return {
        "sql_query": sql_query,
        "validation_passed": True,
        "validation_error": ""
    }


def executor_node(state: AgentState) -> Dict[str, Any]:
    sql_query = state.sql_query if isinstance(state, AgentState) else state["sql_query"]
    db_manager = get_database_manager(state)
    
    results, error = db_manager.execute_query(sql_query, enforce_limit=True)
    
    if error:
        return {
            "query_result": [],
            "error": error
        }
    else:
        return {
            "query_result": results,
            "error": ""
        }


async def reflector_node(state: AgentState) -> Dict[str, Any]:
    question = state.question if isinstance(state, AgentState) else state["question"]
    sql_query = state.sql_query if isinstance(state, AgentState) else state["sql_query"]
    db_manager = get_database_manager(state)
    
    # 由校验或运行中获取错误
    if isinstance(state, AgentState):
        error = state.error or state.validation_error
        retry_count = state.retry_count
    else:
        error = state.get("error") or state.get("validation_error", "")
        retry_count = state.get("retry_count", 0)
    
    retry_count += 1
    
    config = get_config()
    if retry_count > config.max_retry_count:
        return {
            "retry_count": retry_count,
            "final_response": (
                f"经过 {config.max_retry_count} 次尝试后仍未能生成有效的 "
                "SQL 查询，请调整问题后重试。"
            )
        }
    
    # 获取数据库结构作为反思上下文
    schema = db_manager.get_annotated_schema()

    llm = get_reflector_llm()
    
    # 生成错误反思提示词
    prompt = get_error_reflection_prompt(question, sql_query, error, schema)
    
    try:
        response = await llm.ainvoke(prompt)
        response_text = extract_response_text(response)
        
        # Extract explanation
        explanation_match = re.search(
            r'(?:Explanation|说明|修正说明)\s*[:：](.*?)```sql',
            response_text,
            re.DOTALL | re.IGNORECASE,
        )
        explanation = explanation_match.group(1).strip() if explanation_match else ""
        
        # Extract corrected SQL
        sql_match = re.search(r'```sql\s*(.*?)\s*```', response_text, re.DOTALL)
        if sql_match:
            corrected_sql = sql_match.group(1).strip()
        else:
            corrected_sql = sql_query  # Keep original if extraction fails
        
        return {
            "sql_query": corrected_sql,
            "reasoning": f"第 {retry_count} 次修正：{explanation}",
            "retry_count": retry_count,
            "validation_passed": False,  # Will be re-validated
            "error": ""  # Clear error for retry
        }
        
    except Exception:
        return {
            "retry_count": retry_count,
            "final_response": "SQL 自动修正失败，请调整问题后重试。"
        }


async def visualizer_node(state: AgentState) -> Dict[str, Any]:
    question = state.question if isinstance(state, AgentState) else state["question"]
    query_result = state.query_result if isinstance(state, AgentState) else state["query_result"]
    sql_query = state.sql_query if isinstance(state, AgentState) else state["sql_query"]
    
    if not query_result:
        return {
            "visualization_spec": None,
            "analysis_summary": "",
            "final_response": "没有找到符合条件的数据。"
        }
    
    if len(query_result) > 0:
        columns = list(query_result[0].keys())
        row_count = len(query_result)
    else:
        columns = []
        row_count = 0
    
    llm = get_visualizer_llm()
    
    # 纳入真实查询结果值，使分析结论有数据依据。
    # 对文本值设置长度上限，避免结果集过宽导致发送给模型的请求过大。
    result_preview = []
    for row in query_result[:50]:
        preview_row = {}
        for key, value in row.items():
            if value is None or isinstance(value, (int, float, bool)):
                preview_row[str(key)] = value
            else:
                preview_row[str(key)] = str(value)[:300]
        result_preview.append(preview_row)

    prompt = get_visualization_prompt(
        question,
        columns,
        row_count,
        sql_query,
        json.dumps(result_preview, ensure_ascii=False),
    )
    
    try:
        response = await llm.ainvoke(prompt)
        response_text = extract_response_text(response).strip()
        
        # 解析 JSON 格式的回答
        # 清理可能存在的 Markdown 代码块标记
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].strip()
        else:
            json_str = response_text
            
        viz_spec = json.loads(json_str)
        if not isinstance(viz_spec, dict):
            raise ValueError("可视化模型未返回 JSON 对象")
        analysis_summary = str(viz_spec.pop("summary", "") or "").strip()

        if str(viz_spec.get("chart_type", "")).lower() == "none":
            viz_spec = {
                "chart_type": "none",
                "x_column": None,
                "y_column": None,
                "z_column": None,
                "title": str(viz_spec.get("title", "查询结果") or "查询结果"),
            }
        
        return {
            "visualization_spec": viz_spec,
            "analysis_summary": analysis_summary,
            "final_response": (
                "查询成功，未生成图表。"
                if viz_spec["chart_type"] == "none"
                else "查询和图表生成成功。"
            )
        }
        
    except Exception as e:
        print(
            "[WARNING] Visualization error_type="
            f"{e.__class__.__name__}"
        )
        return {
            "visualization_spec": {
                "chart_type": "bar",
                "title": "查询结果",
            },
            "analysis_summary": "",
            "final_response": "查询成功，已使用默认图表展示结果。"
        }


async def format_response_node(state: AgentState) -> Dict[str, Any]:
    error = state.error or state.validation_error
    if error:
        if "syntax error" in str(error).lower():
            error_type = "SQL 语法错误"
            suggestion = "请简化问题，或补充更明确的查询条件。"
        elif "no such table" in str(error).lower() or "no such column" in str(error).lower():
            error_type = "数据库结构错误"
            suggestion = "请确认所提到的数据表和字段存在于当前数据库中。"
        elif "forbidden keyword" in str(error).lower():
            error_type = "安全校验未通过"
            suggestion = "平台只允许执行只读查询。"
        else:
            error_type = "查询处理失败"
            suggestion = "请换一种更明确的方式描述问题。"
            
        return {
            "final_response": (
                f"**{error_type}**\n\n"
                f"经过 {state.retry_count} 次尝试后仍未能生成有效结果。\n\n"
                f"**建议：**\n{suggestion}"
            )
        }
    
    if not state.query_result:
        return {
            "final_response": (
                "查询执行成功，但没有找到符合条件的数据。"
                "可以尝试放宽筛选条件。"
            )
        }
    
    row_count = len(state.query_result)
    return {
        "final_response": f"查询成功，共找到 {row_count} 条结果。"
    }
