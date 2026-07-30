import re
from typing import Tuple, List, Dict, Any

class InputValidator:

    MAX_QUESTION_LENGTH = 500
    MIN_QUESTION_LENGTH = 3
    
    # 可能表示提示词注入的可疑模式
    SUSPICIOUS_PATTERNS = [
        r"ignore\s+(previous|above|all)",
        r"system\s*:",
        r"assistant\s*:",
        r"<\|.*?\|>",  # Special tokens
        r"###\s*instruction",
        r"forget\s+everything",
        r"disregard",
    ]
    
    def validate_question(self, question: str) -> Tuple[bool, str, str]:
        if not question or not question.strip():
            return False, "", "问题不能为空。"
        
        sanitized = question.strip()
        
        if len(sanitized) < self.MIN_QUESTION_LENGTH:
            return (
                False,
                "",
                f"问题过短，至少需要 {self.MIN_QUESTION_LENGTH} 个字符。",
            )
        
        if len(sanitized) > self.MAX_QUESTION_LENGTH:
            return (
                False,
                "",
                f"问题过长，最多允许 {self.MAX_QUESTION_LENGTH} 个字符。",
            )
    
        for pattern in self.SUSPICIOUS_PATTERNS:
            if re.search(pattern, sanitized, re.IGNORECASE):
                return False, "", "问题中包含无效内容，请重新描述。"
        
        sanitized = re.sub(r'\s+', ' ', sanitized)
        
        return True, sanitized, ""


class QueryResultValidator:

    MAX_ROWS = 1000
    MAX_COLUMNS = 50
    
    def validate_results(self, results: List[Dict[str, Any]]) -> Tuple[bool, str]:
        if not results:
            return True, ""
        
        # Check row count
        if len(results) > self.MAX_ROWS:
            return (
                False,
                f"查询结果过多：共 {len(results)} 行，最多允许 "
                f"{self.MAX_ROWS} 行。",
            )
        
        # Check column count
        first_row = results[0]
        if len(first_row) > self.MAX_COLUMNS:
            return (
                False,
                f"查询字段过多：共 {len(first_row)} 列，最多允许 "
                f"{self.MAX_COLUMNS} 列。",
            )
        
        # Validate data types (sample first 10 rows)
        for row in results[:10]:
            for key, value in row.items():
                if not isinstance(value, (str, int, float, bool, type(None))):
                    return (
                        False,
                        f"字段“{key}”包含不支持的数据类型："
                        f"{type(value).__name__}",
                    )
        
        return True, ""
