import sqlite3
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from contextlib import contextmanager

from infrastructure.config import get_config


DATABASE_DIRECTORY = (
    Path(__file__).resolve().parent.parent / "data" / "uploads"
).resolve()
ALLOWED_DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier safely."""
    return '"' + identifier.replace('"', '""') + '"'


# 管理数据库连接并提供
class DatabaseManager:

    # 初始化db manager
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self._validate_database()

    # 校验数据库存在有效
    def _validate_database(self) -> None:
        if (
            self.db_path.parent != DATABASE_DIRECTORY
            or self.db_path.suffix.lower() not in ALLOWED_DATABASE_EXTENSIONS
        ):
            raise PermissionError(
                "Database path is outside the managed ChatBI data directory."
            )
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"Database file not found: {self.db_path}. "
                "Please select an available database source."
            )
    
    @contextmanager
    def get_connection(self, read_only: bool = True):

        # 以只读方式打开
        uri = (
            f"{self.db_path.as_uri()}?mode=ro"
            if read_only
            else str(self.db_path)
        )
        conn = sqlite3.connect(uri, uri=read_only)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        try:
            yield conn
        finally:
            conn.close()
    
    def get_annotated_schema(self) -> str:
        schema_parts = []
        
        with self.get_connection() as conn:
            cursor = conn.cursor()

            imported_descriptions: Dict[Tuple[str, str], str] = {}
            cursor.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='_chatbi_column_metadata'
                """
            )
            if cursor.fetchone():
                cursor.execute(
                    """
                    SELECT table_name, column_name, description
                    FROM _chatbi_column_metadata
                    """
                )
                imported_descriptions = {
                    (row[0], row[1]): row[2] for row in cursor.fetchall()
                }
            
            # Get all tables
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                  AND name NOT LIKE '_chatbi_%'
                ORDER BY name
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            for table in tables:
                has_imported_descriptions = any(
                    metadata_table == table
                    for metadata_table, _ in imported_descriptions
                )
                table_desc = (
                    "Imported business data with source column descriptions"
                    if has_imported_descriptions
                    else "Business data available for analysis"
                )
                
                schema_parts.append(f"\n**{table}**: {table_desc}")
                
                # Get columns for this table
                cursor.execute(f"PRAGMA table_info({quote_identifier(table)})")
                columns = cursor.fetchall()
                
                schema_parts.append("  Columns:")
                for col in columns:
                    col_name = col[1]
                    col_type = col[2]
                    is_pk = col[5]
                    
                    col_desc = imported_descriptions.get(
                        (table, col_name),
                        "No description available",
                    )
                    
                    pk_marker = " [PRIMARY KEY]" if is_pk else ""
                    schema_parts.append(
                        f"    - {col_name} ({col_type}){pk_marker}: {col_desc}"
                    )
        
        return "\n".join(schema_parts)
    
    
    def execute_query(
        self,
        sql_query: str,
        enforce_limit: bool = True
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:

        safety_error = self._read_only_query_error(sql_query)
        if safety_error:
            return [], safety_error

        try:
            # Always enforce the limit at the final execution boundary. The
            # legacy flag cannot be used to bypass the platform guardrail.
            del enforce_limit
            sql_query = self._enforce_limit(sql_query)
            
            with self.get_connection() as conn:
                config = get_config()
                if hasattr(conn, "setlimit"):
                    conn.setlimit(
                        sqlite3.SQLITE_LIMIT_LENGTH,
                        config.max_result_bytes,
                    )
                    conn.setlimit(
                        sqlite3.SQLITE_LIMIT_SQL_LENGTH,
                        config.max_sql_query_bytes,
                    )
                    conn.setlimit(
                        sqlite3.SQLITE_LIMIT_COLUMN,
                        config.max_result_columns,
                    )
                deadline = (
                    time.monotonic()
                    + config.sql_query_timeout_seconds
                )
                conn.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline),
                    10_000,
                )
                cursor = conn.cursor()
                try:
                    cursor.execute(sql_query)
                    if cursor.description is None:
                        return [], "The query did not return a result set."
                    columns = [
                        description[0]
                        for description in cursor.description
                    ]
                    results: List[Dict[str, Any]] = []
                    approximate_bytes = 0
                    for _ in range(config.max_result_rows):
                        row = cursor.fetchone()
                        if row is None:
                            break
                        result = dict(zip(columns, row))
                        approximate_bytes += sum(
                            len(value)
                            if isinstance(value, bytes)
                            else len(str(value).encode("utf-8"))
                            for value in result.values()
                            if value is not None
                        )
                        if approximate_bytes > config.max_result_bytes:
                            return [], (
                                "Query result exceeds the configured "
                                "memory-size limit."
                            )
                        results.append(result)
                finally:
                    conn.set_progress_handler(None, 0)
                
                return results, None
                
        except sqlite3.Error as e:
            return [], str(e)
        except Exception as e:
            return [], f"Unexpected error: {str(e)}"

    @staticmethod
    def _read_only_query_error(sql_query: str) -> Optional[str]:
        """Reject non-query statements even if this class is called directly."""
        normalized = str(sql_query or "").strip()
        if not re.match(r"^(?:SELECT|WITH)\b", normalized, re.IGNORECASE):
            return "Only SELECT or WITH queries are allowed."
        forbidden_keywords = (
            "ATTACH",
            "DETACH",
            "PRAGMA",
            "VACUUM",
            "DROP",
            "DELETE",
            "UPDATE",
            "INSERT",
            "ALTER",
            "CREATE",
            "REPLACE",
            "TRUNCATE",
        )
        if any(
            re.search(rf"\b{keyword}\b", normalized, re.IGNORECASE)
            for keyword in forbidden_keywords
        ):
            return "The query contains a forbidden SQLite operation."
        return None
    
    def _enforce_limit(self, sql_query: str) -> str:

        config = get_config()
        max_rows = config.max_result_rows
        
        # Always enforce the ceiling at the execution boundary. Wrapping also
        # caps explicit oversized LIMIT values and CTE/cross-join queries.
        inner_query = sql_query.strip().rstrip(";")
        return (
            "SELECT * FROM ("
            f"{inner_query}"
            f") AS _chatbi_limited_query LIMIT {max_rows};"
        )
    
    def validate_query_syntax(self, sql_query: str) -> Tuple[bool, Optional[str]]:

        safety_error = self._read_only_query_error(sql_query)
        if safety_error:
            return False, safety_error
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"EXPLAIN QUERY PLAN {sql_query}")
                return True, None
        except sqlite3.Error as e:
            return False, str(e)
    
    def get_table_names(self) -> List[str]:

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                  AND name NOT LIKE '_chatbi_%'
                ORDER BY name
            """)
            return [row[0] for row in cursor.fetchall()]
