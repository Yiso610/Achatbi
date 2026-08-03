"""System roles and permission codes."""

QUERY_DATA = "query_data"
IMPORT_DATASOURCE = "import_datasource"
DELETE_DATASOURCE = "delete_datasource"
MANAGE_USERS = "manage_users"
MANAGE_DATASOURCE_ACCESS = "manage_datasource_access"
VIEW_AUDIT_LOGS = "view_audit_logs"

PERMISSIONS = {
    QUERY_DATA: "查询数据",
    IMPORT_DATASOURCE: "导入数据源",
    DELETE_DATASOURCE: "删除数据源",
    MANAGE_USERS: "管理用户",
    MANAGE_DATASOURCE_ACCESS: "分配数据源",
    VIEW_AUDIT_LOGS: "查看审计日志",
}

ROLE_VIEWER = "viewer"
ROLE_DATA_MANAGER = "data_manager"
ROLE_ADMIN = "admin"

ROLE_LABELS = {
    ROLE_VIEWER: "查询用户",
    ROLE_DATA_MANAGER: "数据管理员",
    ROLE_ADMIN: "系统管理员",
}

ROLE_DESCRIPTIONS = {
    ROLE_VIEWER: "仅可查询明确授权的数据源",
    ROLE_DATA_MANAGER: "可查询和导入数据源，但不可删除或管理账号",
    ROLE_ADMIN: "拥有全部功能权限，并可管理账号和数据源授权",
}

ROLE_PERMISSIONS = {
    ROLE_VIEWER: {QUERY_DATA},
    ROLE_DATA_MANAGER: {QUERY_DATA, IMPORT_DATASOURCE},
    ROLE_ADMIN: set(PERMISSIONS),
}
