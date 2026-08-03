"""Server-local account administration commands."""

from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path
import sys

# Allow direct execution from the scripts directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.models import AuthError
from auth.service import get_auth_service


def _prompt_required(label: str, supplied: str | None = None) -> str:
    value = str(supplied or "").strip()
    while not value:
        value = input(f"{label}: ").strip()
    return value


def initialize_admin(args: argparse.Namespace) -> int:
    service = get_auth_service()
    if service.has_users():
        print("权限数据库已经存在账号，初始化已取消。")
        return 1

    username = _prompt_required("管理员用户名", args.username)
    display_name = _prompt_required("管理员显示名称", args.display_name)
    email = str(args.email or "").strip()
    password = getpass("管理员密码: ")
    confirmation = getpass("再次输入管理员密码: ")
    if password != confirmation:
        print("两次输入的密码不一致。")
        return 1

    try:
        user = service.bootstrap_admin(
            username=username,
            display_name=display_name,
            email=email,
            password=password,
        )
    except AuthError as error:
        print(f"初始化失败：{error}")
        return 1

    print(f"系统管理员“{user.username}”已创建。")
    print("现在可以启动 Streamlit 并登录。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ChatBI 本地账号管理命令",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser(
        "init-admin",
        help="安全创建首个系统管理员",
    )
    init_parser.add_argument("--username")
    init_parser.add_argument("--display-name")
    init_parser.add_argument("--email", default="")
    init_parser.set_defaults(handler=initialize_admin)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
