"""Authorized entrypoint for the Text-to-SQL workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from auth.constants import QUERY_DATA
from auth.models import AuthError, DataSourceRecord, ValidationError
from auth.service import AuthService


QueryRunner = Callable[[str, Path], Any]


class AuthorizedQueryService:
    """Keep UI visibility and actual query authorization separate."""

    def __init__(
        self,
        auth_service: AuthService,
        runner: QueryRunner | None = None,
    ) -> None:
        self.auth_service = auth_service
        if runner is None:
            from agents.graph import run_agent

            runner = run_agent
        self.runner = runner

    def execute(
        self,
        *,
        user_id: int,
        session_version: int,
        datasource_id: int,
        question: str,
    ) -> tuple[Any, DataSourceRecord]:
        try:
            self.auth_service.validate_session(
                user_id,
                session_version,
            )
            datasource = self.auth_service.resolve_authorized_datasource(
                user_id,
                datasource_id,
                QUERY_DATA,
            )
            source_version = datasource.version
        except AuthError:
            self.auth_service.record_audit(
                actor_user_id=user_id,
                action="query_data",
                outcome="denied",
                target_type="datasource",
                target_id=datasource_id,
                datasource_id=datasource_id,
            )
            raise

        try:
            result = self.runner(question, datasource.path)
            # Re-check before results are returned to the UI. This also catches
            # a role, account, or data-source grant changed during a long query.
            self.auth_service.validate_session(
                user_id,
                session_version,
            )
            datasource = self.auth_service.resolve_authorized_datasource(
                user_id,
                datasource_id,
                QUERY_DATA,
            )
            if datasource.version != source_version:
                raise ValidationError(
                    "查询期间数据源已更新，请重新发起查询。"
                )
        except AuthError:
            self.auth_service.record_audit(
                actor_user_id=user_id,
                action="query_data",
                outcome="denied",
                target_type="datasource",
                target_id=datasource_id,
                datasource_id=datasource_id,
            )
            raise
        except Exception as error:
            self.auth_service.record_audit(
                actor_user_id=user_id,
                action="query_data",
                outcome="failure",
                target_type="datasource",
                target_id=datasource_id,
                datasource_id=datasource_id,
                details={
                    "error_type": error.__class__.__name__,
                    "question_length": len(question),
                },
            )
            raise

        has_error = bool(
            getattr(result, "error", "")
            or getattr(result, "validation_error", "")
        )
        rows = getattr(result, "query_result", []) or []
        self.auth_service.record_audit(
            actor_user_id=user_id,
            action="query_data",
            outcome="failure" if has_error else "success",
            target_type="datasource",
            target_id=datasource_id,
            datasource_id=datasource_id,
            details={
                "question_length": len(question),
                "returned_rows": len(rows),
            },
        )
        return result, datasource
