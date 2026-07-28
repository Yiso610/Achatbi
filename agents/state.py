from typing import List, Dict, Any, Optional, Annotated
from pydantic import BaseModel, Field, field_validator, ConfigDict


class AgentState(BaseModel):
    # Pydantic数据模型配置
    model_config = ConfigDict(
        # 允许传入模型未声明的额外字段
        extra='allow',
        # 对象创建后重新赋值时也会进行类型校验
        validate_assignment=True,
        # 使用枚举值而非枚举对象
        use_enum_values=True,
        # 允许使用字段名填充数据
        populate_by_name=True,
    )
    
    # ==================== Input ====================
    question: Annotated[
        str,
        Field(
            description="用户的自然语言问题输入",
            min_length=1,
        )
    ]

    database_path: Annotated[
        str,
        Field(
            min_length=1,
            description="相关的数据库path",
        )
    ]
    
    # ==================== Routing ====================
    is_relevant: Annotated[
        bool,
        Field(
            default=False,
            description="提问是否与当前使用active的数据库内容相关"
                        "由intent router node设置"
        )
    ]
    
    # ==================== SQL Generation ====================
    sql_query: Annotated[
        str,
        Field(
            default="",
            description="LLM生成的SQL query"
                       "由SQL generator node设置"
        )
    ]
    
    reasoning: Annotated[
        str,
        Field(
            default="",
            description="LLM解释SQL语句生成的Chain-of-thought reasoning"
                       "用于调试并提高过程透明度"
        )
    ]
    
    # ==================== Validation ====================
    validation_passed: Annotated[
        bool,
        Field(
            default=False,
            description="SQL query 是否通过所有validation检测(safety, syntax, relevancy)"
                       "由SQL validator node设置"
        )
    ]
    
    validation_error: Annotated[
        str,
        Field(
            default="",
            description="validation的失败error信息"
                       "验证通过为空字符串"
        )
    ]
    
    # ==================== Execution ====================
    query_result: Annotated[
        List[Dict[str, Any]],
        Field(
            default_factory=list,
            description="SQL query结果列表"
                       "每个结果都为一个dict"
        )
    ]
    
    error: Annotated[
        str,
        Field(
            default="",
            description="SQL query error"
                       "成功则为空字符串"
        )
    ]
    
    # ==================== Reflection/Retry ====================
    retry_count: Annotated[
        int,
        Field(
            default=0,
            ge=0,   # >= 0
            description="重试次数"
                       "根据reflector node尝试次数而增加"
        )
    ]
    
    # ==================== Visualization ====================
    visualization_spec: Annotated[
        Optional[Dict[str, Any]],
        Field(
            default=None,
            description="数据可视化配置（图表类型、数据列等）"
                       "由可视化节点设置；如果不建议进行可视化，则为 None"
        )
    ]

    analysis_summary: Annotated[
        str,
        Field(
            default="",
            description="基于生成的 SQL 和实际查询结果得出的简洁中文结论。"
        )
    ]
    
    # ==================== Final Output ====================
    final_response: Annotated[
        str,
        Field(
            default="",
            description="给用户的最终格式化的回答"
                       "由format response node或error handling nodes设置"
        )
    ]
    
    # ==================== Validators ====================
    @field_validator('retry_count')
    @classmethod
    def validate_retry_count(cls, v: int) -> int:
        if v < 0:
            raise ValueError("retry_count must be non-negative")
        return v
    
    @field_validator('query_result')
    @classmethod
    def validate_query_result(cls, v: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(v, list):
            raise ValueError("query_result must be a list")
        return v
    
    # ==================== Helper Methods ====================
    def has_error(self) -> bool:
        """检查当前状态是否包含错误"""
        return bool(self.error or self.validation_error)
    
    def is_complete(self) -> bool:
        """检查工作流是否已完成（是否已经生成最终回答）"""
        return bool(self.final_response)
    
    def get_error_message(self) -> str:
        """获取当前错误信息（验证错误或执行错误）"""
        return self.validation_error or self.error
    
    def reset_errors(self) -> None:
        """清空所有错误字段（通常在重试前调用）"""
        self.error = ""
        self.validation_error = ""
