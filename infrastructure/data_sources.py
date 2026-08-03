from __future__ import annotations

from dataclasses import dataclass
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
import socket
import sqlite3
from typing import Any
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


class DataSourceError(ValueError):
    """当上传的数据库无法通过安全校验、不能被系统接受时"""


@dataclass(frozen=True)
class StagedDatabaseSnapshot:
    staged_path: Path
    target_path: Path
    display_name: str


@dataclass(frozen=True)
class _ExcelSheetSpec:
    sheet_name: str
    table_name: str
    header_row_number: int
    columns: tuple[dict[str, str], ...]
    row_count: int


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
            specs.append(
                _ExcelSheetSpec(
                    sheet_name=worksheet.title,
                    table_name=table_name,
                    header_row_number=header_row_number,
                    columns=tuple(columns),
                    row_count=row_count,
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
    local.executemany(
        "INSERT INTO _chatbi_source_metadata (key, value) VALUES (?, ?)",
        (
            ("display_name", database),
            ("database_type", database_type),
            ("database_name", database),
            ("snapshot_created_at", datetime.now().isoformat(timespec="seconds")),
        ),
    )


def _create_snapshot_table(
    local: sqlite3.Connection,
    local_table_name: str,
    columns: list[dict[str, str]],
) -> str:
    """Create one SQLite table and return its parameterized insert SQL."""
    column_definitions = ", ".join(
        f"{quote_identifier(column['name'])} "
        f"{_sqlite_type(column['source_type'])}"
        for column in columns
    )
    local.execute(
        f"CREATE TABLE {quote_identifier(local_table_name)} "
        f"({column_definitions})"
    )

    for column in columns:
        description = (
            column.get("comment", "").strip()
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
                    COALESCE(column_comment, '')
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
                }
                for row in cursor.fetchall()
            ]
            if columns:
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
                SELECT
                    column_name,
                    data_type,
                    udt_name
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, table_name),
            )
            columns = [
                {
                    "name": str(row[0]),
                    "source_type": str(row[2] or row[1]),
                    "comment": "",
                }
                for row in cursor.fetchall()
            ]
            if not columns:
                continue

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


def stage_remote_database(
    database_type: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> StagedDatabaseSnapshot:
    """Build and validate a private snapshot without publishing it."""
    provider, host, port, user = _validate_remote_connection(
        database_type,
        host,
        port,
        user,
    )
    database = database.strip()
    if not database:
        raise DataSourceError("请选择需要添加的数据库。")

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

        if not table_specs:
            raise DataSourceError("所选数据库中没有可导入的数据表。")

        with sqlite3.connect(temporary_path) as local:
            local.execute("PRAGMA foreign_keys = OFF")
            _initialize_snapshot_metadata(local, provider, database)

            for table in table_specs:
                insert_sql = _create_snapshot_table(
                    local,
                    table["local_name"],
                    table["columns"],
                )

                if provider == "MySQL":
                    remote_table = (
                        "`"
                        + table["name"].replace("`", "``")
                        + "`"
                    )
                else:
                    remote_schema = quote_identifier(table["schema"])
                    remote_name = quote_identifier(table["name"])
                    remote_table = f"{remote_schema}.{remote_name}"

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
                        local.commit()
                        _check_snapshot_size(temporary_path)

            local.commit()

        inspect_sqlite_database(temporary_path)
        _harden_private_file(temporary_path)
        completed = True
        return StagedDatabaseSnapshot(
            staged_path=temporary_path.resolve(),
            target_path=target_path,
            display_name=database,
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
