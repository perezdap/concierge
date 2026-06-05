"""Runtime ConfigStore with versioned apply pipeline (P2-ADMIN-1)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError

from ..config import GatewayConfig, expand_env
from .models import (
    ConfigVersion,
    ConfigVersionStatus,
    ConfigVersionSummary,
    ValidationIssue,
    ValidationResult,
)

_STATE_ACTIVE = "active_version_id"
_STATE_DRAFT = "draft_version_id"
_STATE_LAST_KNOWN_GOOD = "last_known_good_version_id"

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_versions (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT,
    config_json      TEXT NOT NULL,
    validation_json  TEXT,
    parent_id        TEXT,
    promoted_at      TEXT
);
CREATE TABLE IF NOT EXISTS config_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def redact_config_dict(config: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy safe for API reads."""
    from .redaction import redact_config_for_api

    return redact_config_for_api(config)  # type: ignore[return-value]


def validate_gateway_config(config: dict[str, Any]) -> ValidationResult:
    try:
        GatewayConfig.model_validate(config)
        return ValidationResult(ok=True)
    except ValidationError as exc:
        issues = [
            ValidationIssue(
                path=".".join(str(p) for p in err["loc"]),
                message=err["msg"],
                code=err.get("type"),
            )
            for err in exc.errors()
        ]
        return ValidationResult(ok=False, issues=issues)


def config_to_redacted_yaml(config: dict[str, Any]) -> str:
    from .redaction import redact_config_for_yaml_export

    return yaml.safe_dump(
        redact_config_for_yaml_export(config),
        sort_keys=False,
        default_flow_style=False,
    )


class ConfigStore(ABC):
    """Versioned runtime configuration store."""

    @abstractmethod
    async def get_active_redacted(self) -> dict[str, Any] | None:
        """Return the active config dict with secrets redacted, or None if unset."""

    @abstractmethod
    async def get_active_version(self) -> ConfigVersion | None:
        ...

    @abstractmethod
    async def get_draft(self) -> ConfigVersion | None:
        ...

    @abstractmethod
    async def create_or_update_draft(
        self,
        config: dict[str, Any],
        *,
        created_by: str | None = None,
    ) -> ConfigVersion:
        ...

    @abstractmethod
    async def validate_draft(self) -> ValidationResult:
        ...

    @abstractmethod
    async def promote_draft_to_active(
        self,
        *,
        created_by: str | None = None,
    ) -> ConfigVersion:
        """Atomically validate and promote the current draft to active."""

    @abstractmethod
    async def list_versions(
        self,
        *,
        limit: int = 50,
    ) -> list[ConfigVersionSummary]:
        ...

    @abstractmethod
    async def rollback(self) -> ConfigVersion:
        """Activate the last-known-good version; supersede current active."""

    @abstractmethod
    async def import_yaml(
        self,
        yaml_text: str,
        *,
        created_by: str | None = None,
        expand_environment: bool = True,
    ) -> ConfigVersion:
        ...

    @abstractmethod
    async def export_active_yaml_redacted(self) -> str | None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...


class SqliteConfigStore(ConfigStore):
    """ConfigStore backed by SQLite (dev / single-node)."""

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SQLITE_SCHEMA)
        self._conn.commit()

    async def get_active_redacted(self) -> dict[str, Any] | None:
        version = await self.get_active_version()
        if version is None:
            return None
        return version.redacted_config()

    async def get_active_version(self) -> ConfigVersion | None:
        active_id = await self._get_state(_STATE_ACTIVE)
        if not active_id:
            return None
        return await self._get_version(active_id)

    async def get_draft(self) -> ConfigVersion | None:
        draft_id = await self._get_state(_STATE_DRAFT)
        if not draft_id:
            return None
        return await self._get_version(draft_id)

    async def create_or_update_draft(
        self,
        config: dict[str, Any],
        *,
        created_by: str | None = None,
    ) -> ConfigVersion:
        existing = await self.get_draft()
        version = ConfigVersion(
            id=existing.id if existing else str(uuid4()),
            status=ConfigVersionStatus.DRAFT,
            created_by=created_by,
            config=config,
        )
        await self._upsert_version(version)
        await self._set_state(_STATE_DRAFT, version.id)
        return version

    async def validate_draft(self) -> ValidationResult:
        draft = await self.get_draft()
        if draft is None:
            return ValidationResult(
                ok=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message="No draft config exists",
                        code="no_draft",
                    )
                ],
            )
        result = validate_gateway_config(draft.config)
        draft.validation = result
        await self._upsert_version(draft)
        return result

    async def promote_draft_to_active(
        self,
        *,
        created_by: str | None = None,
    ) -> ConfigVersion:
        draft = await self.get_draft()
        if draft is None:
            raise ValueError("No draft config to promote")

        validation = validate_gateway_config(draft.config)
        draft.validation = validation
        await self._upsert_version(draft)
        if not validation.ok:
            raise ValueError("Draft validation failed; cannot promote")

        def _promote() -> str:
            now = _iso_now()
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                active_id = self._get_state_sync(_STATE_ACTIVE)
                draft_row = cur.execute(
                    "SELECT id, config_json, created_by FROM config_versions WHERE id = ?",
                    (draft.id,),
                ).fetchone()
                if draft_row is None:
                    raise ValueError("Draft version disappeared during promote")

                if active_id:
                    cur.execute(
                        "UPDATE config_versions SET status = ? WHERE id = ?",
                        (ConfigVersionStatus.SUPERSEDED.value, active_id),
                    )
                    self._set_state_sync(_STATE_LAST_KNOWN_GOOD, active_id, cur)
                    parent_id = active_id
                else:
                    parent_id = None

                cur.execute(
                    """
                    UPDATE config_versions
                    SET status = ?, promoted_at = ?, created_by = COALESCE(?, created_by),
                        validation_json = ?, parent_id = COALESCE(parent_id, ?)
                    WHERE id = ?
                    """,
                    (
                        ConfigVersionStatus.ACTIVE.value,
                        now,
                        created_by,
                        validation.model_dump_json(),
                        parent_id,
                        draft.id,
                    ),
                )
                self._set_state_sync(_STATE_ACTIVE, draft.id, cur)
                self._delete_state_sync(_STATE_DRAFT, cur)
                cur.execute("COMMIT")
                return draft.id
            except Exception:
                cur.execute("ROLLBACK")
                raise

        promoted_id = await asyncio.to_thread(_promote)
        version = await self._get_version(promoted_id)
        assert version is not None
        return version

    async def list_versions(self, *, limit: int = 50) -> list[ConfigVersionSummary]:
        def _run() -> list[ConfigVersionSummary]:
            rows = self._conn.execute(
                """
                SELECT id, status, created_at, created_by, validation_json, parent_id, promoted_at
                FROM config_versions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            out: list[ConfigVersionSummary] = []
            for row in rows:
                validation_ok = None
                if row[4]:
                    parsed = json.loads(row[4])
                    validation_ok = parsed.get("ok")
                out.append(
                    ConfigVersionSummary(
                        id=row[0],
                        status=ConfigVersionStatus(row[1]),
                        created_at=_parse_iso(row[2]),
                        created_by=row[3],
                        validation_ok=validation_ok,
                        parent_id=row[5],
                        promoted_at=_parse_iso(row[6]) if row[6] else None,
                    )
                )
            return out

        return await asyncio.to_thread(_run)

    async def rollback(self) -> ConfigVersion:
        lkg_id = await self._get_state(_STATE_LAST_KNOWN_GOOD)
        if not lkg_id:
            raise ValueError("No last-known-good version to roll back to")

        def _rollback() -> str:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                active_id = self._get_state_sync(_STATE_ACTIVE)
                now = _iso_now()
                if active_id and active_id != lkg_id:
                    cur.execute(
                        "UPDATE config_versions SET status = ? WHERE id = ?",
                        (ConfigVersionStatus.ROLLED_BACK.value, active_id),
                    )
                cur.execute(
                    "UPDATE config_versions SET status = ?, promoted_at = ? WHERE id = ?",
                    (ConfigVersionStatus.ACTIVE.value, now, lkg_id),
                )
                self._set_state_sync(_STATE_ACTIVE, lkg_id, cur)
                if active_id and active_id != lkg_id:
                    self._set_state_sync(_STATE_LAST_KNOWN_GOOD, active_id, cur)
                cur.execute("COMMIT")
                return lkg_id
            except Exception:
                cur.execute("ROLLBACK")
                raise

        rolled_id = await asyncio.to_thread(_rollback)
        version = await self._get_version(rolled_id)
        assert version is not None
        return version

    async def import_yaml(
        self,
        yaml_text: str,
        *,
        created_by: str | None = None,
        expand_environment: bool = True,
    ) -> ConfigVersion:
        text = expand_env(yaml_text) if expand_environment else yaml_text
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("YAML must deserialize to a mapping")
        return await self.create_or_update_draft(data, created_by=created_by)

    async def export_active_yaml_redacted(self) -> str | None:
        active = await self.get_active_version()
        if active is None:
            return None
        return config_to_redacted_yaml(active.config)

    def close(self) -> None:
        self._conn.close()

    async def _get_state(self, key: str) -> str | None:
        return await asyncio.to_thread(self._get_state_sync, key)

    def _get_state_sync(self, key: str, cur: sqlite3.Cursor | None = None) -> str | None:
        if cur is None:
            row = self._conn.execute(
                "SELECT value FROM config_state WHERE key = ?", (key,)
            ).fetchone()
        else:
            row = cur.execute("SELECT value FROM config_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    async def _set_state(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set_state_sync, key, value)

    def _set_state_sync(
        self, key: str, value: str, cur: sqlite3.Cursor | None = None
    ) -> None:
        if cur is None:
            self._conn.execute(
                "INSERT OR REPLACE INTO config_state (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._conn.commit()
        else:
            cur.execute(
                "INSERT OR REPLACE INTO config_state (key, value) VALUES (?, ?)",
                (key, value),
            )

    def _delete_state_sync(self, key: str, cur: sqlite3.Cursor) -> None:
        cur.execute("DELETE FROM config_state WHERE key = ?", (key,))

    async def _get_version(self, version_id: str) -> ConfigVersion | None:
        def _run() -> ConfigVersion | None:
            row = self._conn.execute(
                """
                SELECT id, status, created_at, created_by, config_json, validation_json,
                       parent_id, promoted_at
                FROM config_versions WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                return None
            validation = None
            if row[5]:
                validation = ValidationResult.model_validate_json(row[5])
            return ConfigVersion(
                id=row[0],
                status=ConfigVersionStatus(row[1]),
                created_at=_parse_iso(row[2]),
                created_by=row[3],
                config=json.loads(row[4]),
                validation=validation,
                parent_id=row[6],
                promoted_at=_parse_iso(row[7]) if row[7] else None,
            )

        return await asyncio.to_thread(_run)

    async def _upsert_version(self, version: ConfigVersion) -> None:
        def _run() -> None:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO config_versions
                    (id, status, created_at, created_by, config_json, validation_json,
                     parent_id, promoted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.id,
                    version.status.value,
                    _iso_dt(version.created_at),
                    version.created_by,
                    json.dumps(version.config),
                    version.validation.model_dump_json() if version.validation else None,
                    version.parent_id,
                    _iso_dt(version.promoted_at) if version.promoted_at else None,
                ),
            )
            self._conn.commit()

        await asyncio.to_thread(_run)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _iso_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)