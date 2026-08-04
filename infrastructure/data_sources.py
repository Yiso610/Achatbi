from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import unicodedata
import threading
import time as time_module
from typing import Any, Mapping
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import pymysql
from openpyxl import load_workbook

try:
    import psycopg2
except ImportError: 
    psycopg2 = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOAD_DIRECTORY = PROJECT_ROOT / "data" / "uploads"
ALLOWED_DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
ALLOWED_EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
SQLITE_HEADER = b"SQLite format 3\x00"
MAX_DATABASE_SIZE_BYTES = 100 * 1024 * 1024
MAX_EXCEL_SIZE_BYTES = 20 * 1024 * 1024
MAX_EXCEL_EXPANDED_SIZE_BYTES = 200 * 1024 * 1024
MAX_EXCEL_SHEETS = 50
MAX_EXCEL_ROWS = 250_000
MAX_EXCEL_COLUMNS = 500
REMOTE_DATABASE_TYPES = ("MySQL", "PostgreSQL")
MYSQL_SYSTEM_DATABASES = {
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
}
REMOTE_SYNC_CURSOR_NAMES = (
    "updated_at",
    "update_time",
    "updated_time",
    "modified_at",
    "modified_time",
    "last_modified",
    "last_updated",
    "更新时间",
    "修改时间",
)
_REMOTE_SYNC_LOCKS: dict[str, threading.Lock] = {}
_REMOTE_SYNC_LOCKS_GUARD = threading.Lock()


class DataSourceError(ValueError):
    """当上传的数据库无法通过安全校验、不能被系统接受时"""


@dataclass(frozen=True)
class RemoteDatabaseCredentials:
    """Session-scoped credentials used to refresh one remote snapshot."""

    database_type: str
    host: str
    port: int
    user: str
    password: str = field(repr=False, compare=False)
    database: str


@dataclass(frozen=True)
class StagedDatabaseSnapshot:
    staged_path: Path
    target_path: Path
    display_name: str
    remote_credentials: RemoteDatabaseCredentials | None = None


@dataclass(frozen=True)
class RemoteSyncResult:
    """Outcome of one due-check or remote snapshot synchronization."""

    attempted: bool
    changed: bool
    rows_synced: int
    data_version: int
    synced_at: str
    message: str


@dataclass(frozen=True)
class FieldReadinessIssue:
    """One field problem that needs user review before import."""

    table_name: str
    column_name: str | None
    code: str
    message: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "table_name": self.table_name,
            "column_name": self.column_name,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class FieldReadinessReport:
    """Result of a conservative field-name readability check."""

    status: str
    table_count: int
    column_count: int
    issues: tuple[FieldReadinessIssue, ...]

    @property
    def is_ready(self) -> bool:
        return self.status == "passed"

    @property
    def needs_review(self) -> bool:
        return self.status == "needs_review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "table_count": self.table_count,
            "column_count": self.column_count,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class RemoteDatabasePreflight:
    """A metadata-only inspection performed before a full snapshot."""

    provider: str
    source_key: str
    database: str
    report: FieldReadinessReport


@dataclass(frozen=True)
class _ExcelSheetSpec:
    sheet_name: str
    table_name: str
    header_row_number: int
    columns: tuple[dict[str, str], ...]
    row_count: int
    readiness_issues: tuple[FieldReadinessIssue, ...] = ()


# 保护数据库快照
def ensure_private_upload_directory() -> Path:
    UPLOAD_DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        UPLOAD_DIRECTORY.chmod(0o700)
    except OSError as error:
        raise DataSourceError(f"无法保护数据库存储目录：{error}") from error
    return UPLOAD_DIRECTORY.resolve()


# 设置安全权限
def _harden_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError as error:
        raise DataSourceError(f"无法设置数据库文件权限：{error}") from error


def _create_private_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)
    _harden_private_file(path)


def _write_private_bytes(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    _harden_private_file(path)


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def database_display_name(database_path: Path) -> str:
    path = database_path.resolve()
    if not path.is_file():
        return path.stem

    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                """
                SELECT value
                FROM _chatbi_source_metadata
                WHERE key = 'display_name'
                """
            ).fetchone()
            if row and row[0]:
                return str(row[0])
    except sqlite3.Error:
        pass

    return path.stem


def inspect_sqlite_database(database_path: Path) -> dict[str, Any]:
    """Validate a SQLite file and return its table and column catalog."""
    path = database_path.resolve()
    if not path.is_file():
        raise DataSourceError("数据库文件不存在。")

    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if not quick_check or quick_check[0] != "ok":
                raise DataSourceError("数据库完整性检查未通过。")

            table_rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                  AND name NOT LIKE '_chatbi_%'
                ORDER BY name
                """
            ).fetchall()

            tables = []
            for table_row in table_rows:
                table_name = table_row["name"]
                columns = connection.execute(
                    f"PRAGMA table_info({quote_identifier(table_name)})"
                ).fetchall()
                tables.append(
                    {
                        "name": table_name,
                        "columns": [
                            {
                                "name": column["name"],
                                "type": column["type"] or "未指定",
                                "primary_key": bool(column["pk"]),
                            }
                            for column in columns
                        ],
                    }
                )
        finally:
            connection.close()
    except DataSourceError:
        raise
    except sqlite3.Error as error:
        raise DataSourceError(f"无法读取 SQLite 数据库：{error}") from error

    if not tables:
        raise DataSourceError("数据库中没有可查询的数据表。")

    return {
        "path": str(path),
        "name": path.name,
        "tables": tables,
    }


def _safe_uploaded_name(original_name: str, digest: str) -> str:
    """Build a stable filename without allowing path traversal."""
    source_name = Path(original_name).name
    suffix = Path(source_name).suffix.lower()
    if suffix not in ALLOWED_DATABASE_EXTENSIONS:
        raise DataSourceError("只支持 .db、.sqlite 和 .sqlite3 数据库文件。")

    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", Path(source_name).stem)
    stem = stem.strip("._-")[:60] or "database"
    return f"{stem}-{digest[:10]}{suffix}"


def save_uploaded_database(original_name: str, content: bytes) -> Path:
    """Validate and persist one uploaded SQLite database."""
    if not content:
        raise DataSourceError("上传的数据库文件为空。")
    if len(content) > MAX_DATABASE_SIZE_BYTES:
        raise DataSourceError("数据库文件不能超过 100 MB。")
    if not content.startswith(SQLITE_HEADER):
        raise DataSourceError("文件不是有效的 SQLite 数据库。")

    digest = sha256(content).hexdigest()
    target_name = _safe_uploaded_name(original_name, digest)
    ensure_private_upload_directory()

    target_path = (UPLOAD_DIRECTORY / target_name).resolve()
    if target_path.parent != UPLOAD_DIRECTORY.resolve():
        raise DataSourceError("数据库文件名无效。")

    if target_path.exists():
        _harden_private_file(target_path)
        inspect_sqlite_database(target_path)
        return target_path

    temporary_path = UPLOAD_DIRECTORY / f".{digest}.uploading"
    try:
        _write_private_bytes(temporary_path, content)
        inspect_sqlite_database(temporary_path)
        os.replace(temporary_path, target_path)
        _harden_private_file(target_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return target_path


def _excel_value_is_empty(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and not value.strip()
    )


def _excel_row_width(values: list[Any]) -> int:
    for index in range(len(values) - 1, -1, -1):
        if not _excel_value_is_empty(values[index]):
            return index + 1
    return 0


def _safe_excel_identifier(
    raw_name: Any,
    fallback: str,
    used_names: set[str],
    *,
    max_length: int = 120,
) -> str:
    """Normalize an Excel label into a readable, unique SQLite name."""
    name = str(raw_name or "").replace("\x00", " ").strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", name)
    name = name.strip("._-")[:max_length] or fallback
    if name.lower().startswith(("sqlite_", "_chatbi_")):
        name = f"excel_{name}"

    candidate = name
    suffix = 2
    while candidate.casefold() in used_names:
        suffix_text = f"_{suffix}"
        candidate = f"{name[:max_length - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_names.add(candidate.casefold())
    return candidate


def _excel_value_type(value: Any) -> str | None:
    if _excel_value_is_empty(value):
        return None
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, (float, Decimal)):
        return "REAL"
    if isinstance(value, bytes):
        return "BLOB"
    return "TEXT"


def _merge_excel_types(current: str | None, incoming: str | None) -> str | None:
    if incoming is None:
        return current
    if current is None or current == incoming:
        return incoming
    if {current, incoming} <= {"INTEGER", "REAL"}:
        return "REAL"
    return "TEXT"


def _excel_sqlite_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        return None
    return _sqlite_value(value)


def _validate_excel_upload(original_name: str, content: bytes) -> None:
    suffix = Path(Path(original_name).name).suffix.lower()
    if suffix not in ALLOWED_EXCEL_EXTENSIONS:
        raise DataSourceError("只支持 .xlsx 和 .xlsm Excel 文件。")
    if not content:
        raise DataSourceError("上传的 Excel 文件为空。")
    if len(content) > MAX_EXCEL_SIZE_BYTES:
        raise DataSourceError("Excel 文件不能超过 20 MB。")

    try:
        with ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml"} <= names:
                raise DataSourceError("文件不是有效的 Excel 工作簿。")
            expanded_size = sum(item.file_size for item in archive.infolist())
            if expanded_size > MAX_EXCEL_EXPANDED_SIZE_BYTES:
                raise DataSourceError("Excel 解压后的内容不能超过 200 MB。")
    except BadZipFile as error:
        raise DataSourceError("文件不是有效的 Excel 工作簿。") from error


def _excel_header_readiness_issues(
    table_name: str,
    header_values: list[Any],
    sample_rows: list[list[Any]],
) -> tuple[FieldReadinessIssue, ...]:
    """Detect a first row that is data rather than a semantic header."""
    issues: list[FieldReadinessIssue] = []
    row_looks_like_data = _excel_header_row_looks_like_data(
        header_values,
        sample_rows,
    )
    for column_index, raw_header in enumerate(header_values, start=1):
        column_name = str(raw_header or "").strip()
        if not column_name:
            issues.append(
                FieldReadinessIssue(
                    table_name=table_name,
                    column_name=None,
                    code="missing_field_name",
                    message=(
                        f"工作表“{table_name}”第 {column_index} 列没有字段名。"
                    ),
                )
            )
            continue

        header_type = _excel_value_type(raw_header)
        data_types = [
            _excel_value_type(row[column_index - 1])
            for row in sample_rows
            if len(row) >= column_index
            and _excel_value_type(row[column_index - 1]) is not None
        ]
        same_as_data = bool(
            header_type
            and header_type != "TEXT"
            and data_types
            and all(value_type == header_type for value_type in data_types)
        )
        numeric_text_header = bool(re.fullmatch(r"[+-]?\d+(?:\.\d+)?", column_name))
        normalized_column_name = _safe_excel_identifier(
            raw_header,
            f"column_{column_index}",
            set(),
        )
        if row_looks_like_data:
            issues.append(
                FieldReadinessIssue(
                    table_name=table_name,
                    column_name=normalized_column_name,
                    code="header_row_looks_like_data",
                    message=(
                        f"工作表“{table_name}”第 {column_index} 列的首行值“"
                        f"{column_name}”属于疑似数据行，"
                        "不能作为字段名。"
                    ),
                )
            )
        elif not isinstance(raw_header, str) or numeric_text_header or same_as_data:
            issues.append(
                FieldReadinessIssue(
                    table_name=table_name,
                    column_name=normalized_column_name,
                    code="header_looks_like_data",
                    message=(
                        f"工作表“{table_name}”第 {column_index} 列的首行值“"
                        f"{column_name}”与后续数据类型一致，"
                        "看起来是数据而不是字段名。"
                    ),
                )
            )
        elif field_name_is_ambiguous(normalized_column_name):
            issues.append(
                FieldReadinessIssue(
                    table_name=table_name,
                    column_name=normalized_column_name,
                    code="ambiguous_field_name",
                    message=(
                        f"工作表“{table_name}”第 {column_index} 列的字段名“"
                        f"{normalized_column_name}”过于模糊。"
                    ),
                )
            )
    return tuple(issues)


def _excel_header_row_looks_like_data(
    header_values: list[Any],
    sample_rows: list[list[Any]],
) -> bool:
    """Detect an all-text first row that follows the same pattern as data."""
    if len(header_values) < 2 or len(sample_rows) < 2:
        return False
    comparable_columns = 0
    matching_columns = 0
    ambiguous_columns = 0
    for column_index, raw_header in enumerate(header_values):
        if not isinstance(raw_header, str) or not raw_header.strip():
            return False
        column_name = _safe_excel_identifier(
            raw_header,
            f"column_{column_index + 1}",
            set(),
        )
        if field_name_is_ambiguous(column_name):
            ambiguous_columns += 1
        data_types = [
            _excel_value_type(row[column_index])
            for row in sample_rows
            if len(row) > column_index
            and _excel_value_type(row[column_index]) is not None
        ]
        if not data_types:
            continue
        comparable_columns += 1
        if all(value_type == "TEXT" for value_type in data_types):
            matching_columns += 1
    return (
        comparable_columns >= 2
        and matching_columns >= max(1, (len(header_values) + 1) // 2)
        and ambiguous_columns == len(header_values)
    )


def _scan_excel_workbook(content: bytes) -> list[_ExcelSheetSpec]:
    """Inspect sheets, infer a SQLite schema, and enforce import limits."""
    try:
        workbook = load_workbook(
            BytesIO(content),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    except Exception as error:
        raise DataSourceError(f"无法读取 Excel 工作簿：{error}") from error

    try:
        if len(workbook.worksheets) > MAX_EXCEL_SHEETS:
            raise DataSourceError(
                f"Excel 工作簿最多支持 {MAX_EXCEL_SHEETS} 个工作表。"
            )

        specs: list[_ExcelSheetSpec] = []
        used_table_names: set[str] = set()
        total_rows = 0
        for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
            if (worksheet.max_column or 0) > MAX_EXCEL_COLUMNS:
                raise DataSourceError(
                    f"工作表“{worksheet.title}”超过 "
                    f"{MAX_EXCEL_COLUMNS} 列限制。"
                )
            if (worksheet.max_row or 0) > MAX_EXCEL_ROWS + 1:
                raise DataSourceError(
                    f"工作表“{worksheet.title}”超过 "
                    f"{MAX_EXCEL_ROWS:,} 行限制。"
                )
            header_values: list[Any] | None = None
            header_row_number = 0
            inferred_types: list[str | None] = []
            row_count = 0
            sample_rows: list[list[Any]] = []

            for row_number, raw_row in enumerate(
                worksheet.iter_rows(values_only=True),
                start=1,
            ):
                values = list(raw_row)
                width = _excel_row_width(values)
                if width == 0:
                    continue
                if width > MAX_EXCEL_COLUMNS:
                    raise DataSourceError(
                        f"工作表“{worksheet.title}”超过 "
                        f"{MAX_EXCEL_COLUMNS} 列限制。"
                    )

                if header_values is None:
                    header_values = values[:width]
                    inferred_types = [None] * width
                    header_row_number = row_number
                    continue

                if width > len(header_values):
                    extension = width - len(header_values)
                    header_values.extend([None] * extension)
                    inferred_types.extend([None] * extension)

                row_count += 1
                total_rows += 1
                if len(sample_rows) < 10:
                    sample_rows.append(values[:])
                if total_rows > MAX_EXCEL_ROWS:
                    raise DataSourceError(
                        f"Excel 数据总行数不能超过 {MAX_EXCEL_ROWS:,} 行。"
                    )
                for column_index, value in enumerate(values[:width]):
                    inferred_types[column_index] = _merge_excel_types(
                        inferred_types[column_index],
                        _excel_value_type(value),
                    )

            if header_values is None:
                continue

            used_column_names: set[str] = set()
            columns: list[dict[str, str]] = []
            for column_index, raw_header in enumerate(header_values, start=1):
                column_name = _safe_excel_identifier(
                    raw_header,
                    f"column_{column_index}",
                    used_column_names,
                )
                original_header = str(raw_header or "").strip()[:200]
                description = (
                    f"Excel 原始列：{original_header}"
                    if original_header
                    else f"Excel 第 {column_index} 列"
                )
                columns.append(
                    {
                        "name": column_name,
                        "source_type": (
                            inferred_types[column_index - 1] or "TEXT"
                        ),
                        "comment": description,
                    }
                )

            table_name = _safe_excel_identifier(
                worksheet.title,
                f"sheet_{sheet_index}",
                used_table_names,
                max_length=60,
            )
            readiness_issues = _excel_header_readiness_issues(
                table_name,
                header_values,
                sample_rows,
            )
            specs.append(
                _ExcelSheetSpec(
                    sheet_name=worksheet.title,
                    table_name=table_name,
                    header_row_number=header_row_number,
                    columns=tuple(columns),
                    row_count=row_count,
                    readiness_issues=readiness_issues,
                )
            )
    finally:
        workbook.close()

    if not specs:
        raise DataSourceError("Excel 工作簿中没有可导入的数据表。")
    return specs


def _safe_excel_snapshot_path(original_name: str, content: bytes) -> Path:
    source_name = Path(original_name).name
    safe_stem = re.sub(
        r"[^\w\u4e00-\u9fff-]+",
        "_",
        Path(source_name).stem,
    ).strip("._-")[:60] or "excel"
    digest = sha256(content).hexdigest()[:10]
    unique_suffix = uuid4().hex[:8]
    return (
        UPLOAD_DIRECTORY
        / f"excel_{safe_stem}-{digest}-{unique_suffix}.db"
    ).resolve()


def stage_excel_workbook(
    original_name: str,
    content: bytes,
) -> StagedDatabaseSnapshot:
    """Convert an Excel workbook into a private, query-ready SQLite snapshot."""
    _validate_excel_upload(original_name, content)
    specs = _scan_excel_workbook(content)
    readiness_issues = tuple(
        issue
        for spec in specs
        for issue in spec.readiness_issues
    )
    if readiness_issues:
        messages = "；".join(issue.message for issue in readiness_issues[:5])
        suffix = "" if len(readiness_issues) <= 5 else "；其余问题已省略"
        raise DataSourceError(
            "Excel 表头检查未通过："
            f"{messages}{suffix}。请将第一行改成真实字段名后重新上传。"
        )
    ensure_private_upload_directory()

    target_path = _safe_excel_snapshot_path(original_name, content)
    if target_path.parent != UPLOAD_DIRECTORY.resolve():
        raise DataSourceError("Excel 文件名无效。")
    staged_path = (
        UPLOAD_DIRECTORY
        / f".{target_path.name}.{uuid4().hex}.staged"
    )
    display_name = Path(Path(original_name).name).stem.strip()[:200] or "Excel 数据"
    completed = False

    try:
        _create_private_file(staged_path)
        with sqlite3.connect(staged_path) as local:
            local.execute("PRAGMA foreign_keys = OFF")
            _initialize_snapshot_metadata(local, "Excel", display_name)
            local.executemany(
                "INSERT INTO _chatbi_source_metadata (key, value) VALUES (?, ?)",
                (
                    ("source_file_name", Path(original_name).name),
                    ("sheet_count", str(len(specs))),
                    ("row_count", str(sum(spec.row_count for spec in specs))),
                    (
                        "sheet_mapping",
                        json.dumps(
                            {
                                spec.sheet_name: spec.table_name
                                for spec in specs
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ),
            )

            try:
                workbook = load_workbook(
                    BytesIO(content),
                    read_only=True,
                    data_only=True,
                    keep_links=False,
                )
            except Exception as error:
                raise DataSourceError(
                    f"无法再次读取 Excel 工作簿：{error}"
                ) from error

            try:
                worksheet_by_name = {
                    worksheet.title: worksheet
                    for worksheet in workbook.worksheets
                }
                for spec in specs:
                    insert_sql = _create_snapshot_table(
                        local,
                        spec.table_name,
                        list(spec.columns),
                    )
                    worksheet = worksheet_by_name[spec.sheet_name]
                    batch: list[tuple[Any, ...]] = []
                    for row_number, raw_row in enumerate(
                        worksheet.iter_rows(values_only=True),
                        start=1,
                    ):
                        if row_number <= spec.header_row_number:
                            continue
                        values = list(raw_row[:len(spec.columns)])
                        if not any(
                            not _excel_value_is_empty(value)
                            for value in values
                        ):
                            continue
                        values.extend([None] * (len(spec.columns) - len(values)))
                        batch.append(
                            tuple(_excel_sqlite_value(value) for value in values)
                        )
                        if len(batch) >= 1000:
                            local.executemany(insert_sql, batch)
                            local.commit()
                            batch.clear()
                            _check_snapshot_size(staged_path)
                    if batch:
                        local.executemany(insert_sql, batch)
                    local.commit()
                    _check_snapshot_size(staged_path)
            finally:
                workbook.close()

        inspect_sqlite_database(staged_path)
        _harden_private_file(staged_path)
        completed = True
        return StagedDatabaseSnapshot(
            staged_path=staged_path.resolve(),
            target_path=target_path,
            display_name=display_name,
        )
    except DataSourceError:
        raise
    except Exception as error:
        raise DataSourceError(f"Excel 数据导入失败：{error}") from error
    finally:
        if not completed:
            staged_path.unlink(missing_ok=True)


def _validate_remote_connection(
    database_type: str,
    host: str,
    port: int,
    user: str,
) -> tuple[str, str, int, str]:
    """Validate and normalize remote connection fields."""
    normalized_type = database_type.strip()
    normalized_host = host.strip()
    normalized_user = user.strip()

    if normalized_type not in REMOTE_DATABASE_TYPES:
        raise DataSourceError("暂时只支持 MySQL 和 PostgreSQL。")
    if not normalized_host:
        raise DataSourceError("请输入服务器地址。")
    if not normalized_user:
        raise DataSourceError("请输入用户名。")
    if not 1 <= int(port) <= 65535:
        raise DataSourceError("端口必须在 1 到 65535 之间。")
    if not _remote_host_is_allowed(normalized_host, int(port)):
        raise DataSourceError(
            "服务器地址或端口不在 DATASOURCE_ALLOWED_HOSTS 允许列表中。"
        )
    normalized_host = _resolve_remote_host(
        normalized_host,
        int(port),
    )

    return normalized_type, normalized_host, int(port), normalized_user


def create_remote_database_credentials(
    database_type: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> RemoteDatabaseCredentials:
    """Validate one remote target and return a non-persistent credential."""
    provider, normalized_host, normalized_port, normalized_user = (
        _validate_remote_connection(
            database_type,
            host,
            port,
            user,
        )
    )
    normalized_database = str(database or "").strip()
    if not normalized_database:
        raise DataSourceError("请选择需要添加的数据库。")
    return RemoteDatabaseCredentials(
        database_type=provider,
        host=normalized_host,
        port=normalized_port,
        user=normalized_user,
        password=str(password or ""),
        database=normalized_database,
    )


def _remote_host_is_allowed(host: str, port: int) -> bool:
    """Apply the deployment-controlled outbound host-and-port allowlist."""
    raw_allowlist = os.getenv(
        "DATASOURCE_ALLOWED_HOSTS",
        (
            "localhost:3306,localhost:5432,"
            "127.0.0.1:3306,127.0.0.1:5432,"
            "[::1]:3306,[::1]:5432"
        ),
    )
    normalized_host = host.strip().lower().strip("[]").rstrip(".")
    for raw_endpoint in raw_allowlist.split(","):
        endpoint = raw_endpoint.strip().lower()
        if endpoint == "*":
            return True
        if endpoint.startswith("["):
            bracket = endpoint.find("]")
            if bracket < 0 or endpoint[bracket + 1:bracket + 2] != ":":
                continue
            allowed_host = endpoint[1:bracket]
            allowed_port = endpoint[bracket + 2:]
        elif ":" in endpoint:
            allowed_host, allowed_port = endpoint.rsplit(":", 1)
            allowed_host = allowed_host.rstrip(".")
        else:
            continue
        if allowed_port not in {"*", str(int(port))}:
            continue
        if allowed_host == "*":
            return True
        if allowed_host == normalized_host:
            return True
        if (
            allowed_host.startswith("*.")
            and normalized_host.endswith(allowed_host[1:])
            and normalized_host != allowed_host[2:]
        ):
            return True
    return False


def _resolve_remote_host(host: str, port: int) -> str:
    """Resolve once and reject special-purpose network destinations."""
    try:
        addresses = socket.getaddrinfo(
            host,
            int(port),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise DataSourceError("无法解析数据库服务器地址。") from error

    for address in addresses:
        resolved = str(address[4][0])
        try:
            ip = ipaddress.ip_address(resolved)
        except ValueError:
            continue
        if (
            ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            continue
        return resolved
    raise DataSourceError("数据库服务器解析到了不安全的网络地址。")


def _safe_driver_error(error: Exception, password: str) -> str:
    """Return a concise driver error without echoing the supplied password."""
    message = str(error).strip() or error.__class__.__name__
    if password:
        message = message.replace(password, "******")
    return message[:500]


def _connect_mysql(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str | None = None,
):
    """Open a MySQL connection used only by controlled read queries."""
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=60,
        write_timeout=30,
        autocommit=True,
    )


def _connect_postgresql(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
):
    """Open a read-only PostgreSQL connection."""
    if psycopg2 is None:
        raise DataSourceError("PostgreSQL 驱动尚未安装。")

    connection = psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=database,
        connect_timeout=10,
        application_name="chatbi_snapshot",
    )
    connection.set_session(readonly=True, autocommit=True)
    return connection


def list_remote_databases(
    database_type: str,
    host: str,
    port: int,
    user: str,
    password: str,
) -> list[str]:
    """List databases visible to a MySQL or PostgreSQL account."""
    provider, host, port, user = _validate_remote_connection(
        database_type,
        host,
        port,
        user,
    )

    if provider == "MySQL":
        try:
            with _connect_mysql(host, port, user, password) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SHOW DATABASES")
                    databases = [
                        str(row[0])
                        for row in cursor.fetchall()
                        if str(row[0]) not in MYSQL_SYSTEM_DATABASES
                    ]
        except pymysql.MySQLError as error:
            raise DataSourceError(
                f"MySQL 连接失败：{_safe_driver_error(error, password)}"
            ) from error
    else:
        connection = None
        last_error: Exception | None = None
        for maintenance_database in dict.fromkeys(("postgres", user)):
            try:
                connection = _connect_postgresql(
                    host,
                    port,
                    user,
                    password,
                    maintenance_database,
                )
                break
            except DataSourceError:
                raise
            except Exception as error:
                last_error = error

        if connection is None:
            assert last_error is not None
            raise DataSourceError(
                "PostgreSQL 连接失败："
                f"{_safe_driver_error(last_error, password)}"
            ) from last_error

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT datname
                    FROM pg_database
                    WHERE datallowconn = TRUE
                      AND datistemplate = FALSE
                    ORDER BY datname
                    """
                )
                databases = [str(row[0]) for row in cursor.fetchall()]
        except Exception as error:
            raise DataSourceError(
                "PostgreSQL 数据库列表读取失败："
                f"{_safe_driver_error(error, password)}"
            ) from error
        finally:
            connection.close()

    if not databases:
        raise DataSourceError("该账号没有可访问的业务数据库。")
    return databases


_AMBIGUOUS_FIELD_NAMES = {
    "abc",
    "attr",
    "attribute",
    "asdf",
    "bar",
    "baz",
    "column",
    "data",
    "field",
    "foo",
    "haha",
    "hello",
    "hi",
    "lalala",
    "misc",
    "other",
    "qwerty",
    "temp",
    "test",
    "testing",
    "tmp",
    "undefined",
    "unknown",
    "unnamed",
    "value",
    "var",
    "variable",
    "xxx",
    "xyz",
    "哈哈",
    "呵呵",
    "你好",
    "我",
    "他",
    "值",
    "字段",
    "数据",
    "未知",
    "未命名",
    "列",
}
_COMMON_SHORT_FIELD_NAMES = {
    "年龄",
    "地址",
    "编号",
    "部门",
    "城市",
    "代码",
    "电话",
    "地区",
    "日期",
    "金额",
    "公司",
    "国家",
    "类别",
    "类型",
    "名称",
    "票价",
    "票号",
    "数量",
    "备注",
    "邮箱",
    "姓名",
    "性别",
    "状态",
    "时间",
    "手机",
    "销量",
    "价格",
    "资产",
    "收入",
    "工资",
    "产品",
    "客户",
    "订单",
    "员工",
    "活跃",
    "启用",
    "有效",
    "是否",
    "删除",
    "版本",
    "主键",
    "唯一",
    "天数",
    "月份",
    "年份",
}
_NUMBERED_PLACEHOLDER_PATTERN = re.compile(
    r"(?:attr(?:ibute)?|c|col(?:umn)?|data|f(?:ield|ld)?|"
    r"unnamed|unknown|v(?:alue|ar(?:iable)?)?|x|y|z)[_-]*\d+",
    re.IGNORECASE,
)
_CHINESE_PLACEHOLDER_PATTERN = re.compile(
    r"(?:字段|数据|未知|未命名|列|值)[_-]*\d+"
)


def field_name_is_ambiguous(field_name: str) -> bool:
    """Return True only for high-confidence placeholder or opaque names.

    Ordinary business names do not require a database comment.  In
    particular, names such as ``age``, ``PassengerId``, ``SibSp`` and
    ``订单金额`` are accepted.  The check intentionally favors avoiding false
    positives over trying to understand every possible business abbreviation.
    """
    normalized = unicodedata.normalize(
        "NFKC",
        str(field_name or ""),
    ).strip()
    if not normalized:
        return False
    if "\ufffd" in normalized:
        return True

    canonical = re.sub(
        r"[^\w\u4e00-\u9fff]+",
        "_",
        normalized,
    ).strip("_").casefold()
    if not canonical:
        return True
    if canonical in _AMBIGUOUS_FIELD_NAMES:
        return True
    if (
        re.fullmatch(r"[\u4e00-\u9fff]{2,4}", canonical)
        and canonical not in _COMMON_SHORT_FIELD_NAMES
    ):
        return True
    if re.fullmatch(r"(.{1,3})\1{2,}", canonical):
        return True
    if _NUMBERED_PLACEHOLDER_PATTERN.fullmatch(canonical):
        return True
    if _CHINESE_PLACEHOLDER_PATTERN.fullmatch(canonical):
        return True
    if re.fullmatch(r"(?:expr|expression|no_column_name)[_-]*\d*", canonical):
        return True
    if re.fullmatch(r"[a-z]", canonical):
        return True
    if re.fullmatch(r"[a-z]\d+[a-z]?", canonical):
        return True
    if not any(character.isalpha() for character in canonical):
        return True
    if (
        len(canonical) >= 8
        and re.fullmatch(r"[a-f0-9]+", canonical)
        and re.search(r"[a-f]", canonical)
        and re.search(r"\d", canonical)
    ):
        return True
    return False


def validate_table_specs_readiness(
    table_specs: list[dict[str, Any]],
    field_descriptions: Mapping[tuple[str, str], str] | None = None,
) -> FieldReadinessReport:
    """Check field presence and readability without requiring comments."""
    descriptions = field_descriptions or {}
    issues: list[FieldReadinessIssue] = []
    column_count = 0

    if not table_specs:
        return FieldReadinessReport(
            status="needs_review",
            table_count=0,
            column_count=0,
            issues=(
                FieldReadinessIssue(
                    table_name="",
                    column_name=None,
                    code="no_queryable_tables",
                    message="数据库中不存在可读取的数据表或字段。",
                ),
            ),
        )

    for table in table_specs:
        table_name = str(
            table.get("local_name") or table.get("name") or ""
        ).strip()
        columns = table.get("columns") or []
        display_table = table_name or "未命名表"
        if not columns:
            issues.append(
                FieldReadinessIssue(
                    table_name=table_name,
                    column_name=None,
                    code="no_fields",
                    message=f"数据表“{display_table}”不存在任何字段。",
                )
            )
            continue

        for column in columns:
            column_count += 1
            column_name = str(column.get("name") or "").strip()
            if not column_name:
                issues.append(
                    FieldReadinessIssue(
                        table_name=table_name,
                        column_name=None,
                        code="missing_field_name",
                        message=f"数据表“{display_table}”存在字段名称缺失。",
                    )
                )
                continue

            supplied_description = str(
                descriptions.get((table_name, column_name), "")
            ).strip()
            source_comment = str(column.get("comment") or "").strip()
            if supplied_description or source_comment:
                continue
            if field_name_is_ambiguous(column_name):
                issues.append(
                    FieldReadinessIssue(
                        table_name=table_name,
                        column_name=column_name,
                        code="ambiguous_field_name",
                        message=(
                            f"数据表“{display_table}”的字段“{column_name}”"
                            "名称过于模糊，请补充业务含义。"
                        ),
                    )
                )

    return FieldReadinessReport(
        status="needs_review" if issues else "passed",
        table_count=len(table_specs),
        column_count=column_count,
        issues=tuple(issues),
    )


def _sqlite_type(source_type: str) -> str:
    """Map common MySQL/PostgreSQL types to SQLite affinity."""
    normalized = source_type.lower()
    if any(
        token in normalized
        for token in (
            "tinyint",
            "smallint",
            "mediumint",
            "integer",
            "bigint",
            "int(",
            "int2",
            "int4",
            "int8",
            "serial",
            "boolean",
            "bit",
            "year",
        )
    ):
        return "INTEGER"
    if any(
        token in normalized
        for token in (
            "decimal",
            "numeric",
            "float",
            "double",
            "real",
            "money",
        )
    ):
        return "REAL"
    if any(
        token in normalized
        for token in ("binary", "blob", "bytea")
    ):
        return "BLOB"
    return "TEXT"


def _sqlite_value(value: Any) -> Any:
    """Convert common database-driver values to SQLite-safe values."""
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _safe_remote_snapshot_path(
    database_type: str,
    host: str,
    port: int,
    user: str,
    database: str,
) -> Path:
    """Build a stable local snapshot path without including credentials."""
    safe_database = re.sub(
        r"[^\w\u4e00-\u9fff-]+",
        "_",
        database,
    ).strip("._-")[:60] or "database"
    fingerprint = sha256(
        f"{database_type}|{host}|{port}|{user}|{database}".encode("utf-8")
    ).hexdigest()[:10]
    provider = database_type.lower()
    return (UPLOAD_DIRECTORY / f"{provider}_{safe_database}-{fingerprint}.db").resolve()


def _initialize_snapshot_metadata(
    local: sqlite3.Connection,
    database_type: str,
    database: str,
    remote_credentials: RemoteDatabaseCredentials | None = None,
) -> None:
    """Create internal metadata tables excluded from ChatBI queries."""
    local.execute(
        """
        CREATE TABLE _chatbi_column_metadata (
            table_name TEXT NOT NULL,
            column_name TEXT NOT NULL,
            description TEXT NOT NULL,
            PRIMARY KEY (table_name, column_name)
        )
        """
    )
    local.execute(
        """
        CREATE TABLE _chatbi_source_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    now = datetime.now().isoformat(timespec="seconds")
    metadata = [
        ("display_name", database),
        ("database_type", database_type),
        ("database_name", database),
        ("snapshot_created_at", now),
    ]
    if remote_credentials is not None:
        metadata.extend(
            (
                ("source_host", remote_credentials.host),
                ("source_port", str(remote_credentials.port)),
                ("source_user", remote_credentials.user),
                ("sync_enabled", "1"),
                ("sync_interval_seconds", "300"),
                ("last_sync_epoch", str(time_module.time())),
                ("snapshot_updated_at", now),
                ("data_version", "1"),
            )
        )
    local.executemany(
        "INSERT INTO _chatbi_source_metadata (key, value) VALUES (?, ?)",
        metadata,
    )


def _initialize_remote_sync_schema(local: sqlite3.Connection) -> None:
    local.execute(
        """
        CREATE TABLE _chatbi_sync_state (
            local_table_name TEXT PRIMARY KEY,
            source_schema TEXT NOT NULL,
            source_table_name TEXT NOT NULL,
            sync_mode TEXT NOT NULL
                CHECK (sync_mode IN ('updated', 'append', 'full')),
            primary_key_column TEXT NOT NULL DEFAULT '',
            cursor_column TEXT NOT NULL DEFAULT '',
            last_cursor_json TEXT NOT NULL DEFAULT '',
            last_primary_key_json TEXT NOT NULL DEFAULT '',
            last_synced_at TEXT NOT NULL,
            rows_synced INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _create_snapshot_table(
    local: sqlite3.Connection,
    local_table_name: str,
    columns: list[dict[str, Any]],
    *,
    field_descriptions: Mapping[tuple[str, str], str] | None = None,
) -> str:
    """Create one SQLite table and return its parameterized insert SQL."""
    definitions = [
        f"{quote_identifier(column['name'])} "
        f"{_sqlite_type(column['source_type'])}"
        for column in columns
    ]
    primary_keys = [
        str(column["name"])
        for column in columns
        if bool(column.get("primary_key"))
    ]
    if primary_keys:
        definitions.append(
            "PRIMARY KEY ("
            + ", ".join(quote_identifier(name) for name in primary_keys)
            + ")"
        )
    column_definitions = ", ".join(definitions)
    local.execute(
        f"CREATE TABLE {quote_identifier(local_table_name)} "
        f"({column_definitions})"
    )

    descriptions = field_descriptions or {}
    for column in columns:
        description = (
            str(
                descriptions.get(
                    (local_table_name, column["name"]),
                    "",
                )
            ).strip()
            or column.get("comment", "").strip()
            or f"{column['source_type']} column from remote database"
        )
        local.execute(
            """
            INSERT INTO _chatbi_column_metadata
                (table_name, column_name, description)
            VALUES (?, ?, ?)
            """,
            (local_table_name, column["name"], description),
        )

    placeholders = ", ".join("?" for _ in columns)
    insert_columns = ", ".join(
        quote_identifier(column["name"]) for column in columns
    )
    return (
        f"INSERT INTO {quote_identifier(local_table_name)} "
        f"({insert_columns}) VALUES ({placeholders})"
    )


def _check_snapshot_size(temporary_path: Path) -> None:
    """Stop a remote copy before it grows beyond the demo safety limit."""
    if (
        temporary_path.exists()
        and temporary_path.stat().st_size > MAX_DATABASE_SIZE_BYTES
    ):
        raise DataSourceError("数据库分析副本不能超过 100 MB。")


def _mysql_table_specs(connection, database: str) -> list[dict[str, Any]]:
    """Read MySQL table and column definitions."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (database,),
        )
        table_names = [str(row[0]) for row in cursor.fetchall()]

        specs = []
        for table_name in table_names:
            cursor.execute(
                """
                SELECT
                    column_name,
                    data_type,
                    column_type,
                    COALESCE(column_comment, ''),
                    column_key,
                    extra
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                (database, table_name),
            )
            columns = [
                {
                    "name": str(row[0]),
                    "source_type": str(row[2] or row[1]),
                    "comment": str(row[3] or ""),
                    "primary_key": str(row[4] or "").upper() == "PRI",
                    "auto_increment": "auto_increment" in str(
                        row[5] or ""
                    ).lower(),
                }
                for row in cursor.fetchall()
            ]
            specs.append(
                {
                    "schema": database,
                    "name": table_name,
                    "local_name": table_name,
                    "columns": columns,
                }
            )
    return specs


def _postgresql_table_specs(connection) -> list[dict[str, Any]]:
    """Read PostgreSQL user table and column definitions."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema NOT IN ('information_schema', 'pg_catalog')
              AND table_schema NOT LIKE 'pg_toast%'
            ORDER BY table_schema, table_name
            """
        )
        tables = [(str(row[0]), str(row[1])) for row in cursor.fetchall()]
        duplicate_names: dict[str, int] = {}
        for _, table_name in tables:
            duplicate_names[table_name] = duplicate_names.get(table_name, 0) + 1

        specs = []
        for schema_name, table_name in tables:
            cursor.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s
                  AND tc.table_name = %s
                ORDER BY kcu.ordinal_position
                """,
                (schema_name, table_name),
            )
            primary_keys = {str(row[0]) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT
                    columns.column_name,
                    columns.data_type,
                    columns.udt_name,
                    COALESCE(
                        pg_catalog.col_description(
                            relation.oid,
                            columns.ordinal_position
                        ),
                        ''
                    ),
                    columns.is_identity,
                    columns.column_default
                FROM information_schema.columns AS columns
                LEFT JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.nspname = columns.table_schema
                LEFT JOIN pg_catalog.pg_class AS relation
                  ON relation.relnamespace = namespace.oid
                 AND relation.relname = columns.table_name
                WHERE columns.table_schema = %s
                  AND columns.table_name = %s
                ORDER BY columns.ordinal_position
                """,
                (schema_name, table_name),
            )
            columns = [
                {
                    "name": str(row[0]),
                    "source_type": str(row[2] or row[1]),
                    "comment": str(row[3] or ""),
                    "primary_key": str(row[0]) in primary_keys,
                    "auto_increment": (
                        str(row[4] or "").upper() == "YES"
                        or "nextval(" in str(row[5] or "").lower()
                    ),
                }
                for row in cursor.fetchall()
            ]

            use_plain_name = (
                schema_name == "public"
                and duplicate_names.get(table_name, 0) == 1
            )
            local_name = (
                table_name
                if use_plain_name
                else f"{schema_name}__{table_name}"
            )
            specs.append(
                {
                    "schema": schema_name,
                    "name": table_name,
                    "local_name": local_name,
                    "columns": columns,
                }
            )
    return specs


def preflight_remote_database(
    database_type: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    *,
    field_descriptions: Mapping[tuple[str, str], str] | None = None,
) -> RemoteDatabasePreflight:
    """Read remote catalog metadata without copying business rows."""
    provider, host, port, user = _validate_remote_connection(
        database_type,
        host,
        port,
        user,
    )
    normalized_database = database.strip()
    if not normalized_database:
        raise DataSourceError("请选择需要添加的数据库。")

    target_path = _safe_remote_snapshot_path(
        provider,
        host,
        port,
        user,
        normalized_database,
    )
    connection = None
    try:
        if provider == "MySQL":
            connection = _connect_mysql(
                host,
                port,
                user,
                password,
                normalized_database,
            )
            table_specs = _mysql_table_specs(connection, normalized_database)
        else:
            connection = _connect_postgresql(
                host,
                port,
                user,
                password,
                normalized_database,
            )
            table_specs = _postgresql_table_specs(connection)
    except DataSourceError:
        raise
    except pymysql.MySQLError as error:
        raise DataSourceError(
            f"MySQL 元数据读取失败：{_safe_driver_error(error, password)}"
        ) from error
    except Exception as error:
        raise DataSourceError(
            f"{provider} 元数据读取失败："
            f"{_safe_driver_error(error, password)}"
        ) from error
    finally:
        if connection is not None:
            connection.close()

    return RemoteDatabasePreflight(
        provider=provider,
        source_key=target_path.name,
        database=normalized_database,
        report=validate_table_specs_readiness(
            table_specs,
            field_descriptions,
        ),
    )


def _remote_table_identifier(
    provider: str,
    schema_name: str,
    table_name: str,
) -> str:
    if provider == "MySQL":
        quoted_schema = "`" + schema_name.replace("`", "``") + "`"
        quoted_table = "`" + table_name.replace("`", "``") + "`"
        return f"{quoted_schema}.{quoted_table}"
    return (
        f"{quote_identifier(schema_name)}."
        f"{quote_identifier(table_name)}"
    )


def _remote_sync_strategy(
    columns: list[dict[str, Any]],
) -> tuple[str, str, str]:
    primary_keys = [
        column
        for column in columns
        if bool(column.get("primary_key"))
    ]
    if len(primary_keys) != 1:
        return "full", "", ""

    primary_key = str(primary_keys[0]["name"])
    by_name = {
        str(column["name"]).casefold(): column
        for column in columns
    }
    for candidate in REMOTE_SYNC_CURSOR_NAMES:
        column = by_name.get(candidate.casefold())
        if column is None:
            continue
        source_type = str(column.get("source_type", "")).lower()
        if any(token in source_type for token in ("date", "time")):
            return "updated", primary_key, str(column["name"])

    if bool(primary_keys[0].get("auto_increment")):
        return "append", primary_key, primary_key
    return "full", primary_key, ""


def _cursor_sort_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, datetime):
        return (1, value.isoformat(sep=" "))
    if isinstance(value, (date, time)):
        return (1, value.isoformat())
    if isinstance(value, Decimal):
        return (1, float(value))
    if isinstance(value, (int, float, str)):
        return (1, value)
    return (1, str(value))


def _cursor_json(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(
        _sqlite_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _cursor_from_json(value: str) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise DataSourceError("数据源增量同步游标已损坏。") from error


def _snapshot_metadata(database_path: Path) -> dict[str, str]:
    try:
        with sqlite3.connect(
            f"{database_path.resolve().as_uri()}?mode=ro",
            uri=True,
        ) as connection:
            return {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT key, value FROM _chatbi_source_metadata"
                ).fetchall()
            }
    except sqlite3.Error as error:
        raise DataSourceError("无法读取数据源同步状态。") from error


def snapshot_sync_status(database_path: Path | str) -> dict[str, Any]:
    """Return non-secret synchronization metadata for dashboard display."""
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        return {
            "remote": False,
            "enabled": False,
            "data_version": 0,
            "last_synced_at": "",
        }
    try:
        metadata = _snapshot_metadata(path)
    except DataSourceError:
        return {
            "remote": False,
            "enabled": False,
            "data_version": 0,
            "last_synced_at": "",
        }
    provider = metadata.get("database_type", "")
    return {
        "remote": provider in REMOTE_DATABASE_TYPES,
        "enabled": metadata.get("sync_enabled") == "1",
        "data_version": int(metadata.get("data_version", "0") or 0),
        "last_synced_at": metadata.get(
            "snapshot_updated_at",
            metadata.get("snapshot_created_at", ""),
        ),
    }


def stage_remote_database(
    database_type: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    *,
    field_descriptions: Mapping[tuple[str, str], str] | None = None,
) -> StagedDatabaseSnapshot:
    """Build and validate a private snapshot without publishing it."""
    credentials = create_remote_database_credentials(
        database_type,
        host,
        port,
        user,
        password,
        database,
    )
    provider = credentials.database_type
    host = credentials.host
    port = credentials.port
    user = credentials.user
    password = credentials.password
    database = credentials.database

    ensure_private_upload_directory()
    target_path = _safe_remote_snapshot_path(
        provider,
        host,
        port,
        user,
        database,
    )
    if target_path.parent != UPLOAD_DIRECTORY.resolve():
        raise DataSourceError("数据库名称无效。")

    temporary_path = (
        UPLOAD_DIRECTORY
        / f".{target_path.name}.{uuid4().hex}.staged"
    )
    remote = None
    completed = False

    try:
        _create_private_file(temporary_path)
        if provider == "MySQL":
            try:
                remote = _connect_mysql(
                    host,
                    port,
                    user,
                    password,
                    database,
                )
                table_specs = _mysql_table_specs(remote, database)
            except pymysql.MySQLError as error:
                raise DataSourceError(
                    f"MySQL 数据读取失败：{_safe_driver_error(error, password)}"
                ) from error
        else:
            try:
                remote = _connect_postgresql(
                    host,
                    port,
                    user,
                    password,
                    database,
                )
                table_specs = _postgresql_table_specs(remote)
            except DataSourceError:
                raise
            except Exception as error:
                raise DataSourceError(
                    "PostgreSQL 数据读取失败："
                    f"{_safe_driver_error(error, password)}"
                ) from error

        readiness = validate_table_specs_readiness(
            table_specs,
            field_descriptions,
        )
        if readiness.needs_review:
            raise DataSourceError(
                "数据库字段在同步前未通过检查，请重新检查并处理字段问题。"
            )

        with sqlite3.connect(temporary_path) as local:
            local.execute("PRAGMA foreign_keys = OFF")
            _initialize_snapshot_metadata(
                local,
                provider,
                database,
                credentials,
            )
            _initialize_remote_sync_schema(local)

            for table in table_specs:
                insert_sql = _create_snapshot_table(
                    local,
                    table["local_name"],
                    table["columns"],
                    field_descriptions=field_descriptions,
                )
                sync_mode, primary_key, cursor_column = (
                    _remote_sync_strategy(table["columns"])
                )
                remote_table = _remote_table_identifier(
                    provider,
                    table["schema"],
                    table["name"],
                )
                column_names = [
                    str(column["name"])
                    for column in table["columns"]
                ]
                primary_key_index = (
                    column_names.index(primary_key)
                    if primary_key
                    else -1
                )
                cursor_index = (
                    column_names.index(cursor_column)
                    if cursor_column
                    else -1
                )
                last_cursor = None
                last_primary_key = None
                last_sort_key = None
                copied_rows = 0

                with remote.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM {remote_table}")
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        local.executemany(
                            insert_sql,
                            [
                                tuple(_sqlite_value(value) for value in row)
                                for row in rows
                            ],
                        )
                        copied_rows += len(rows)
                        if sync_mode in {"updated", "append"}:
                            for row in rows:
                                cursor_value = row[cursor_index]
                                primary_key_value = row[primary_key_index]
                                if (
                                    cursor_value is None
                                    or primary_key_value is None
                                ):
                                    continue
                                sort_key = (
                                    _cursor_sort_value(cursor_value),
                                    _cursor_sort_value(primary_key_value),
                                )
                                if (
                                    last_sort_key is None
                                    or sort_key > last_sort_key
                                ):
                                    last_sort_key = sort_key
                                    last_cursor = cursor_value
                                    last_primary_key = primary_key_value
                        local.commit()
                        _check_snapshot_size(temporary_path)

                local.execute(
                    """
                    INSERT INTO _chatbi_sync_state (
                        local_table_name,
                        source_schema,
                        source_table_name,
                        sync_mode,
                        primary_key_column,
                        cursor_column,
                        last_cursor_json,
                        last_primary_key_json,
                        last_synced_at,
                        rows_synced
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        table["local_name"],
                        table["schema"],
                        table["name"],
                        sync_mode,
                        primary_key,
                        cursor_column,
                        _cursor_json(last_cursor),
                        _cursor_json(last_primary_key),
                        datetime.now().isoformat(timespec="seconds"),
                        copied_rows,
                    ),
                )

            local.commit()

        inspect_sqlite_database(temporary_path)
        _harden_private_file(temporary_path)
        completed = True
        return StagedDatabaseSnapshot(
            staged_path=temporary_path.resolve(),
            target_path=target_path,
            display_name=database,
            remote_credentials=credentials,
        )
    except DataSourceError:
        raise
    except Exception as error:
        raise DataSourceError(
            f"{provider} 数据同步失败："
            f"{_safe_driver_error(error, password)}"
        ) from error
    finally:
        if remote is not None:
            remote.close()
        if not completed:
            temporary_path.unlink(missing_ok=True)


def _remote_column_identifier(provider: str, column_name: str) -> str:
    if provider == "MySQL":
        return "`" + column_name.replace("`", "``") + "`"
    return quote_identifier(column_name)


def _snapshot_upsert_sql(
    table_name: str,
    column_names: list[str],
    primary_key: str,
) -> str:
    columns_sql = ", ".join(
        quote_identifier(name) for name in column_names
    )
    placeholders = ", ".join("?" for _ in column_names)
    assignments = [
        f"{quote_identifier(name)} = excluded.{quote_identifier(name)}"
        for name in column_names
        if name != primary_key
    ]
    conflict_action = (
        "DO UPDATE SET " + ", ".join(assignments)
        if assignments
        else "DO NOTHING"
    )
    return (
        f"INSERT INTO {quote_identifier(table_name)} ({columns_sql}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT({quote_identifier(primary_key)}) {conflict_action}"
    )


def _remote_sync_lock(database_path: Path) -> threading.Lock:
    key = str(database_path.resolve())
    with _REMOTE_SYNC_LOCKS_GUARD:
        return _REMOTE_SYNC_LOCKS.setdefault(key, threading.Lock())


def _connect_remote_snapshot(
    credentials: RemoteDatabaseCredentials,
):
    if credentials.database_type == "MySQL":
        return _connect_mysql(
            credentials.host,
            credentials.port,
            credentials.user,
            credentials.password,
            credentials.database,
        )
    return _connect_postgresql(
        credentials.host,
        credentials.port,
        credentials.user,
        credentials.password,
        credentials.database,
    )


def _verify_sync_credentials(
    metadata: dict[str, str],
    credentials: RemoteDatabaseCredentials,
) -> None:
    expected = (
        metadata.get("database_type", ""),
        metadata.get("source_host", ""),
        metadata.get("source_port", ""),
        metadata.get("source_user", ""),
        metadata.get("database_name", ""),
    )
    supplied = (
        credentials.database_type,
        credentials.host,
        str(credentials.port),
        credentials.user,
        credentials.database,
    )
    if expected != supplied:
        raise DataSourceError("当前会话的远程数据库凭据与快照不匹配。")


def sync_remote_database_snapshot(
    database_path: Path | str,
    credentials: RemoteDatabaseCredentials,
    *,
    minimum_interval_seconds: int = 300,
) -> RemoteSyncResult:
    """Apply due remote changes to a private snapshot using an atomic copy."""
    path = Path(database_path).expanduser().resolve()
    upload_directory = ensure_private_upload_directory()
    if path.parent != upload_directory or not path.is_file():
        raise DataSourceError("待同步的数据库快照无效。")

    interval = max(1, int(minimum_interval_seconds))
    lock = _remote_sync_lock(path)
    with lock:
        metadata = _snapshot_metadata(path)
        if metadata.get("sync_enabled") != "1":
            raise DataSourceError(
                "该快照没有增量同步信息，请重新添加远程数据库。"
            )
        _verify_sync_credentials(metadata, credentials)

        now_epoch = time_module.time()
        try:
            last_sync_epoch = float(metadata.get("last_sync_epoch", "0"))
        except ValueError:
            last_sync_epoch = 0.0
        current_version = int(metadata.get("data_version", "0") or 0)
        last_synced_at = metadata.get(
            "snapshot_updated_at",
            metadata.get("snapshot_created_at", ""),
        )
        if now_epoch - last_sync_epoch < interval:
            return RemoteSyncResult(
                attempted=False,
                changed=False,
                rows_synced=0,
                data_version=current_version,
                synced_at=last_synced_at,
                message="未到下一次五分钟同步时间。",
            )

        temporary_path = (
            path.parent / f".{path.name}.{uuid4().hex}.syncing"
        )
        remote = None
        published = False
        try:
            remote = _connect_remote_snapshot(credentials)
            _create_private_file(temporary_path)
            shutil.copyfile(path, temporary_path)
            _harden_private_file(temporary_path)

            rows_synced = 0
            changed = False
            synced_at = datetime.now().isoformat(timespec="seconds")
            with sqlite3.connect(temporary_path) as local:
                local.row_factory = sqlite3.Row
                state_rows = local.execute(
                    """
                    SELECT *
                    FROM _chatbi_sync_state
                    ORDER BY local_table_name
                    """
                ).fetchall()
                if not state_rows:
                    raise DataSourceError(
                        "数据源没有可用的增量同步表。"
                    )

                for state in state_rows:
                    local_table = str(state["local_table_name"])
                    source_schema = str(state["source_schema"])
                    source_table = str(state["source_table_name"])
                    sync_mode = str(state["sync_mode"])
                    primary_key = str(state["primary_key_column"])
                    cursor_column = str(state["cursor_column"])
                    columns = [
                        str(row["name"])
                        for row in local.execute(
                            f"PRAGMA table_info({quote_identifier(local_table)})"
                        ).fetchall()
                    ]
                    if not columns:
                        raise DataSourceError(
                            f"本地快照表“{local_table}”不存在。"
                        )

                    remote_table = _remote_table_identifier(
                        credentials.database_type,
                        source_schema,
                        source_table,
                    )
                    remote_columns = ", ".join(
                        _remote_column_identifier(
                            credentials.database_type,
                            name,
                        )
                        for name in columns
                    )
                    parameters: tuple[Any, ...] = ()
                    order_by = ""
                    where_clause = ""
                    last_cursor = _cursor_from_json(
                        str(state["last_cursor_json"])
                    )
                    last_primary_key = _cursor_from_json(
                        str(state["last_primary_key_json"])
                    )

                    if sync_mode == "updated" and last_cursor is not None:
                        cursor_sql = _remote_column_identifier(
                            credentials.database_type,
                            cursor_column,
                        )
                        primary_sql = _remote_column_identifier(
                            credentials.database_type,
                            primary_key,
                        )
                        where_clause = (
                            f" WHERE ({cursor_sql} > %s) "
                            f"OR ({cursor_sql} = %s AND {primary_sql} > %s)"
                        )
                        parameters = (
                            last_cursor,
                            last_cursor,
                            last_primary_key,
                        )
                        order_by = f" ORDER BY {cursor_sql}, {primary_sql}"
                    elif sync_mode == "append" and last_cursor is not None:
                        primary_sql = _remote_column_identifier(
                            credentials.database_type,
                            primary_key,
                        )
                        where_clause = f" WHERE {primary_sql} > %s"
                        parameters = (last_cursor,)
                        order_by = f" ORDER BY {primary_sql}"

                    query = (
                        f"SELECT {remote_columns} FROM {remote_table}"
                        f"{where_clause}{order_by}"
                    )
                    if sync_mode == "full":
                        previous_rows = int(
                            local.execute(
                                f"SELECT COUNT(*) FROM "
                                f"{quote_identifier(local_table)}"
                            ).fetchone()[0]
                        )
                        local.execute(
                            f"DELETE FROM {quote_identifier(local_table)}"
                        )
                        insert_sql = (
                            f"INSERT INTO {quote_identifier(local_table)} ("
                            + ", ".join(
                                quote_identifier(name) for name in columns
                            )
                            + ") VALUES ("
                            + ", ".join("?" for _ in columns)
                            + ")"
                        )
                    else:
                        previous_rows = 0
                        insert_sql = _snapshot_upsert_sql(
                            local_table,
                            columns,
                            primary_key,
                        )

                    primary_index = (
                        columns.index(primary_key)
                        if primary_key
                        else -1
                    )
                    cursor_index = (
                        columns.index(cursor_column)
                        if cursor_column
                        else -1
                    )
                    table_rows = 0
                    max_cursor = last_cursor
                    max_primary_key = last_primary_key
                    max_sort_key = (
                        (
                            _cursor_sort_value(last_cursor),
                            _cursor_sort_value(last_primary_key),
                        )
                        if last_cursor is not None
                        else None
                    )

                    with remote.cursor() as cursor:
                        cursor.execute(query, parameters)
                        while True:
                            rows = cursor.fetchmany(1000)
                            if not rows:
                                break
                            local.executemany(
                                insert_sql,
                                [
                                    tuple(_sqlite_value(value) for value in row)
                                    for row in rows
                                ],
                            )
                            table_rows += len(rows)
                            rows_synced += len(rows)
                            if sync_mode in {"updated", "append"}:
                                for row in rows:
                                    cursor_value = row[cursor_index]
                                    primary_value = row[primary_index]
                                    if (
                                        cursor_value is None
                                        or primary_value is None
                                    ):
                                        continue
                                    sort_key = (
                                        _cursor_sort_value(cursor_value),
                                        _cursor_sort_value(primary_value),
                                    )
                                    if (
                                        max_sort_key is None
                                        or sort_key > max_sort_key
                                    ):
                                        max_sort_key = sort_key
                                        max_cursor = cursor_value
                                        max_primary_key = primary_value
                            _check_snapshot_size(temporary_path)

                    if table_rows or (sync_mode == "full" and previous_rows):
                        changed = True
                    local.execute(
                        """
                        UPDATE _chatbi_sync_state
                        SET last_cursor_json = ?,
                            last_primary_key_json = ?,
                            last_synced_at = ?,
                            rows_synced = ?
                        WHERE local_table_name = ?
                        """,
                        (
                            _cursor_json(max_cursor),
                            _cursor_json(max_primary_key),
                            synced_at,
                            table_rows,
                            local_table,
                        ),
                    )

                new_version = current_version + 1
                local.executemany(
                    """
                    INSERT INTO _chatbi_source_metadata (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        ("last_sync_epoch", str(now_epoch)),
                        ("snapshot_updated_at", synced_at),
                        ("data_version", str(new_version)),
                    ),
                )
                local.commit()

            inspect_sqlite_database(temporary_path)
            os.replace(temporary_path, path)
            _harden_private_file(path)
            published = True
            return RemoteSyncResult(
                attempted=True,
                changed=changed,
                rows_synced=rows_synced,
                data_version=new_version,
                synced_at=synced_at,
                message=(
                    f"已同步 {rows_synced} 条变更记录。"
                    if rows_synced
                    else "同步完成，源数据没有新变化。"
                ),
            )
        except DataSourceError:
            raise
        except Exception as error:
            raise DataSourceError(
                f"远程数据库自动同步失败："
                f"{_safe_driver_error(error, credentials.password)}"
            ) from error
        finally:
            if remote is not None:
                remote.close()
            if not published:
                temporary_path.unlink(missing_ok=True)


def list_database_sources() -> list[Path]:
    """List persistent database sources added through the platform."""
    sources: list[Path] = []
    if not UPLOAD_DIRECTORY.exists():
        return sources
    ensure_private_upload_directory()

    for candidate in sorted(UPLOAD_DIRECTORY.iterdir()):
        resolved = candidate.resolve()
        if (
            resolved.parent == UPLOAD_DIRECTORY.resolve()
            and candidate.is_file()
            and not candidate.name.startswith(".")
            and candidate.suffix.lower() in ALLOWED_DATABASE_EXTENSIONS
        ):
            _harden_private_file(resolved)
            sources.append(resolved)

    return sources
