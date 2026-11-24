"""V1.8 backup and restore (ADR-0018).

Backups are full main-database snapshots with a sidecar manifest. The ledger is
never included: restore replays ledger events newer than the snapshot watermark
so an older backup cannot revive deleted, cleared or opted-out data.

Restore modes are explicit: ``merge`` keeps snapshot-era rows and current newer
values, ``revert`` overwrites the same IDs from the backup, and
``disaster-rebuild`` imports into an empty target under a new lineage.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...app.ledger_service import (
    LedgerService,
    boundaries_from_payload,
    lineage_fields_from_payload,
    time_from_payload,
)
from ...app.write_coordinator import WriteCoordinator
from ...core.errors import (
    Conflict,
    NotFound,
    StorageError,
    ValidationError,
)
from ...core.model import ArchiveReason, LedgerEvent, LedgerEventType
from ...core.ports import (
    RESTORE_MODES,
    Clock,
    ConsentRepository,
    IdGenerator,
    LedgerReplayStore,
    SearchIndex,
)

BACKUP_FORMAT = "weave-memory-backup-v1"
MANIFEST_SCHEMA = 2
MANIFEST_MIN_READER = 2
KNOWN_SECTIONS = frozenset({"core", "archive"})
BACKUP_ROOT_PARTS = ("backups", "v1.8")
JOURNAL_NAME = "restore_journal.json"
# Restore rewrites authoritative tables directly, bypassing the store's dirty
# marker. A non-empty sentinel commits with the transaction so a failed
# index.rebuild() leaves is_healthy() False until a rebuild really succeeds.
_RESTORE_FTS_SENTINEL = '["__restore__"]'


def _ledger_timestamp(value: object, name: str) -> str:
    return (
        time_from_payload(value, name)
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

_TABLES = (
    "memory_scopes", "memories", "memory_revisions", "memory_sources",
    "memory_tags", "memory_relations", "memory_consents", "memory_operations",
    "memory_meta", "memory_lineage_meta", "extraction_jobs", "import_jobs",
    "provider_call_audit",
)

_MEMORY_COLUMNS_V9 = (
    "id", "scope_id", "subject_id", "memory_type", "content",
    "normalized_content", "normalization_version", "status", "source_type",
    "current_revision", "valid_from", "valid_until", "superseded_by",
    "created_at", "updated_at", "last_accessed_at", "metadata_json",
    "candidate_expires_at", "db_lineage_id", "creation_seq",
)
_MEMORY_COLUMNS_V10 = _MEMORY_COLUMNS_V9 + (
    "archived_at", "purge_after", "archive_reason",
)
_MEMORY_COLUMNS = _MEMORY_COLUMNS_V10
_REVISION_COLUMNS = (
    "id", "memory_id", "revision", "content", "status", "changed_at",
    "change_reason", "snapshot_json",
)
_SOURCE_COLUMNS = (
    "id", "memory_id", "source_type", "operator_type", "operator_id",
    "evidence_ref", "captured_at", "metadata_json",
)
_TAG_COLUMNS = ("memory_id", "tag", "created_at")
_RELATION_COLUMNS = (
    "id", "source_memory_id", "target_memory_id", "relation_type",
    "created_at", "metadata_json",
)
_SCOPE_COLUMNS = (
    "id", "platform", "tenant_id", "scope_type", "scope_external_id",
    "subject_type", "subject_id", "created_at", "is_enabled", "disabled_at",
    "admin_ids_json", "admin_source",
)
_CONSENT_COLUMNS = (
    "id", "subject_id", "scope_id", "consent_type", "state", "source",
    "granted_at", "revoked_at", "metadata_json",
)
_OPERATION_COLUMNS = (
    "id", "scope_id", "memory_id", "operation_type", "operator_type",
    "operator_id", "occurred_at", "metadata_json",
)

_SENSITIVE_KEY_PARTS = (
    "token", "api_key", "apikey", "password", "secret", "credential",
)


def _sql_columns(columns: tuple[str, ...], prefix: str = "") -> str:
    return ", ".join(f"{prefix}{column}" for column in columns)


def _insert_select(table: str, columns: tuple[str, ...],
                   *, or_ignore: bool = False) -> str:
    prefix = "INSERT OR IGNORE INTO" if or_ignore else "INSERT INTO"
    return (
        f"{prefix} {table} ({_sql_columns(columns)}) "
        f"SELECT {_sql_columns(columns, 'b.')} FROM backup_db.{table} b"
    )


def _utc_stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("ab") as handle:
        os.fsync(handle.fileno())


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if any(part in name.lower() for part in _SENSITIVE_KEY_PARTS):
                result[name] = "***"
            else:
                result[name] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


@contextmanager
def _readonly(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise StorageError("could not open backup database") from exc
    try:
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


class BackupService:
    def __init__(self, *, database: Any, store: LedgerReplayStore,
                 index: SearchIndex, consents: ConsentRepository,
                 coordinator: WriteCoordinator, ledger: LedgerService,
                 clock: Clock, ids: IdGenerator, data_dir: str | Path,
                 config: Mapping[str, Any] | None = None,
                 enabled: bool = True, keep_last: int = 5,
                 retention_days: int = 30,
                 vector: Any | None = None) -> None:
        self._database = database
        self._store = store
        self._index = index
        self._consents = consents
        self._coordinator = coordinator
        self._ledger = ledger
        self._clock = clock
        self._ids = ids
        self._data_dir = Path(data_dir)
        self._config = dict(config or {})
        self._enabled = enabled
        self._keep_last = max(1, keep_last)
        self._retention_days = max(1, retention_days)
        self._vector = vector
        self._root = self._data_dir.joinpath(*BACKUP_ROOT_PARTS)
        self._journal_path = self._data_dir / JOURNAL_NAME

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise StorageError(
                "backup/restore is disabled while ledger_first_enabled=false",
            )

    def _backup_dir(self, backup_id: str) -> Path:
        if not backup_id or any(char in backup_id for char in ("/", "\\", "..")):
            raise ValidationError("invalid backup id")
        return self._root / backup_id

    # ------------------------------------------------------------------ backup
    def create_backup(self) -> dict[str, object]:
        self._require_enabled()
        return self._coordinator.run_write_locked(self._create_locked)

    def _create_locked(self) -> dict[str, object]:
        self._ledger.strict_drain(self._store, self._consents)
        lineage = self._store.current_lineage_id()
        high_watermarks = self._store.lineage_high_watermarks()
        ledger_store = self._ledger.ledger
        now = self._clock.now()
        backup_id = f"{_utc_stamp(now)}-{lineage}"
        self._root.mkdir(parents=True, exist_ok=True)
        final_dir = self._backup_dir(backup_id)
        counter = 1
        while final_dir.exists():
            final_dir = self._backup_dir(f"{backup_id}_{counter}")
            counter += 1
        backup_id = final_dir.name
        temp_dir = self._root / f".tmp-{backup_id}-{uuid4().hex}"
        temp_dir.mkdir()
        try:
            main_path = temp_dir / "main.sqlite3"
            self._database.backup_to(main_path)
            self._verify_file(main_path)
            row_counts = self._table_row_counts(main_path)
            core_section = {
                "schema_version": self._schema_version(main_path),
                "db_lineage_id": lineage,
                "ledger_id": ledger_store.ledger_id(),
                "ledger_seq_at_backup": ledger_store.last_seq(),
                "creation_seq_at_backup": high_watermarks.get(lineage, 0),
                "lineage_high_watermarks": high_watermarks,
                "sha256": _sha256_file(main_path),
                "size": main_path.stat().st_size,
                "table_row_counts": row_counts,
                "config_redacted": True,
            }
            payload = {
                "format": BACKUP_FORMAT,
                "manifest_schema": MANIFEST_SCHEMA,
                "min_reader_version": MANIFEST_MIN_READER,
                "backup_id": backup_id,
                "created_at": now.isoformat(),
                **core_section,
                "sections": {
                    "core": core_section,
                    "archive": self._archive_section(main_path),
                },
            }
            (temp_dir / "sidecar.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            (temp_dir / "config.json").write_text(
                json.dumps(_redact(self._config), ensure_ascii=False, indent=2,
                           sort_keys=True),
                encoding="utf-8",
            )
            (temp_dir / "README.txt").write_text(
                "织忆 V1.8 备份\n"
                f"备份 ID：{backup_id}\n"
                "main.sqlite3 是主库快照；sidecar.json 记录水位与哈希；"
                "config.json 为脱敏配置。\n"
                "memory_ledger.sqlite3 不在备份内，恢复时必须使用同一账本。\n"
                "恢复命令：/织忆 恢复 <备份ID> [merge|revert|disaster-rebuild] 确认\n",
                encoding="utf-8",
            )
            for name in ("main.sqlite3", "sidecar.json", "config.json", "README.txt"):
                _fsync_file(temp_dir / name)
            os.replace(temp_dir, final_dir)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        self._enforce_retention()
        return dict(payload)

    # ------------------------------------------------------------------- list
    def list_backups(self) -> list[dict[str, object]]:
        self._require_enabled()
        if not self._root.exists():
            return []
        items: list[dict[str, object]] = []
        for directory in self._root.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            manifest = directory / "sidecar.json"
            if not manifest.exists():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                items.append(payload)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return items

    def _load_manifest(self, backup_id: str) -> tuple[dict[str, Any], Path]:
        directory = self._backup_dir(backup_id)
        sidecar = directory / "sidecar.json"
        if not sidecar.exists():
            raise NotFound("backup not found")
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError("backup sidecar is invalid") from exc
        if not isinstance(payload, dict) or payload.get("format") != BACKUP_FORMAT:
            raise ValidationError("backup format is not supported")
        try:
            manifest_schema = int(payload.get("manifest_schema", 1))
            min_reader = int(payload.get("min_reader_version", 1))
        except (TypeError, ValueError) as exc:
            raise ValidationError("backup manifest version is invalid") from exc
        if manifest_schema not in (1, 2):
            raise ValidationError("backup manifest schema is not supported")
        if min_reader > MANIFEST_SCHEMA:
            raise ValidationError("backup requires a newer reader")
        sections = payload.get("sections")
        if sections is not None:
            if not isinstance(sections, dict) or "core" not in sections:
                raise ValidationError("backup manifest sections are invalid")
            unknown = set(sections) - KNOWN_SECTIONS
            if unknown:
                raise ValidationError("backup manifest contains an unknown section")
        return payload, directory

    # ----------------------------------------------------------------- verify
    @staticmethod
    def _schema_version(path: Path) -> int:
        with _readonly(path) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
            ).fetchone()
            return int(row[0]) if row is not None else 0

    @staticmethod
    def _verify_file(path: Path) -> None:
        with _readonly(path) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise StorageError("backup database integrity check failed")

    def _verify_backup(self, manifest: Mapping[str, Any], directory: Path,
                       mode: str) -> None:
        main_path = directory / "main.sqlite3"
        if not main_path.exists():
            raise StorageError("backup snapshot is missing")
        if _sha256_file(main_path) != manifest.get("sha256"):
            raise StorageError("backup snapshot hash does not match sidecar")
        if main_path.stat().st_size != int(manifest.get("size", -1)):
            raise StorageError("backup snapshot size does not match sidecar")
        self._verify_file(main_path)
        if self._schema_version(main_path) not in (9, 10):
            raise StorageError("backup schema version is not supported")
        ledger_store = self._ledger.ledger
        coverage = ledger_store.coverage_report()
        if str(manifest.get("ledger_id")) != ledger_store.ledger_id():
            raise StorageError("backup ledger_id does not match the current ledger")
        if not coverage["contiguous"]:
            raise StorageError("ledger segments are not contiguous")
        ledger_seq = int(manifest.get("ledger_seq_at_backup", -1))
        if ledger_seq > ledger_store.last_seq():
            raise StorageError("backup ledger watermark is ahead of the current ledger")
        if ledger_seq < int(coverage["earliest_retained_seq"]) - 1:
            raise StorageError("ledger no longer covers the backup watermark")
        high_watermarks = manifest.get("lineage_high_watermarks")
        if not isinstance(high_watermarks, dict):
            raise StorageError("backup lineage high watermarks are missing")
        parsing_watermarks = {
            str(key): int(value) for key, value in high_watermarks.items()
        }
        with _readonly(main_path) as connection:
            lineage_row = connection.execute(
                "SELECT value FROM memory_meta WHERE key='db_lineage_id'",
            ).fetchone()
            if lineage_row is None or not str(lineage_row[0]).strip():
                raise StorageError("backup is missing its current db_lineage_id")
            backup_lineage = str(lineage_row[0])
            if str(manifest.get("db_lineage_id")) != backup_lineage:
                raise StorageError("backup sidecar lineage does not match the snapshot")
            rows = connection.execute(
                "SELECT id, db_lineage_id, creation_seq FROM memories",
            ).fetchall()
            seen: set[tuple[str, int]] = set()
            for row in rows:
                memory_lineage = row["db_lineage_id"]
                seq = row["creation_seq"]
                if not isinstance(memory_lineage, str) or not memory_lineage:
                    raise StorageError("backup memory has a NULL db_lineage_id")
                if type(seq) is not int or seq < 1:
                    raise StorageError("backup memory has an invalid creation_seq")
                if memory_lineage not in parsing_watermarks:
                    raise StorageError("backup memory lineage has no high watermark")
                if seq > parsing_watermarks[memory_lineage]:
                    raise StorageError("backup memory exceeds its lineage high watermark")
                key = (memory_lineage, seq)
                if key in seen:
                    raise StorageError("backup memory creation_seq is duplicated")
                seen.add(key)
            creation_seq_at_backup = int(manifest.get("creation_seq_at_backup", -1))
            if creation_seq_at_backup > parsing_watermarks.get(backup_lineage, -1):
                raise StorageError("backup creation_seq_at_backup exceeds the lineage watermark")
            if mode != "disaster-rebuild":
                current = self._store.current_lineage_id()
                if backup_lineage != current:
                    raise Conflict(
                        "backup lineage does not match the current database; "
                        "use disaster-rebuild to adopt it",
                    )

    def _verify_clear_boundaries(self, manifest: Mapping[str, Any],
                                 directory: Path,
                                 events: tuple[LedgerEvent, ...], *,
                                 mode: str) -> None:
        """Reject clear boundaries that exceed every persisted high watermark.

        ``merge``/``revert`` run against the live database, whose per-lineage
        counter only grows, so a boundary above it cannot come from a real
        clear. ``disaster-rebuild`` targets an empty database by design: the
        ledger is then the only authority, and post-backup clears may legitimately
        exceed the backup watermark.
        """
        if mode == "disaster-rebuild":
            return
        known: dict[str, int] = {
            str(key): int(value)
            for key, value in dict(
                manifest.get("lineage_high_watermarks") or {},
            ).items()
        }
        with _readonly(directory / "main.sqlite3") as connection:
            rows = connection.execute(
                "SELECT db_lineage_id, high_watermark FROM memory_lineage_meta",
            ).fetchall()
        for row in rows:
            lineage = str(row["db_lineage_id"])
            known[lineage] = max(known.get(lineage, 0), int(row["high_watermark"]))
        for lineage, high in self._store.lineage_high_watermarks().items():
            known[lineage] = max(known.get(lineage, 0), int(high))
        for event in events:
            if event.event_type is not LedgerEventType.CLEAR_SCOPE:
                continue
            for lineage, boundary in boundaries_from_payload(event.payload).items():
                high = known.get(lineage)
                if high is None or boundary > high:
                    raise StorageError(
                        "clear boundary exceeds the persisted high watermark",
                    )

    # ---------------------------------------------------------------- restore
    def restore(self, backup_id: str, *, mode: str = "merge",
                confirm: bool = False, dry_run: bool = False,
                operator_id: str = "admin") -> dict[str, object]:
        self._require_enabled()
        if mode not in RESTORE_MODES:
            raise ValidationError(
                "restore mode must be merge, revert or disaster-rebuild",
            )
        if not dry_run and not confirm:
            raise ValidationError("restore requires explicit confirmation")
        return self._coordinator.run_maintenance_locked(
            lambda: self._restore_locked(
                backup_id, mode=mode, dry_run=dry_run,
                operator_id=operator_id, resume=None,
            ),
        )

    def recover_if_needed(self) -> dict[str, object] | None:
        """Resume an interrupted restore before any read or write is served."""
        if not self._journal_path.exists():
            return None
        try:
            journal = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError("restore journal is corrupt; refusing to start") from exc
        if not isinstance(journal, dict):
            raise StorageError("restore journal is invalid; refusing to start")
        if journal.get("phase") == "complete":
            return None
        required = (
            "backup_id", "mode", "backup_sha256", "ledger_id",
            "ledger_seq_at_backup", "pre_restore_snapshot",
        )
        if any(not journal.get(key) for key in required):
            raise StorageError("restore journal is incomplete; refusing to start")
        if journal.get("mode") not in RESTORE_MODES:
            raise StorageError("restore journal mode is invalid")
        if not self._enabled:
            raise StorageError(
                "restore journal exists but backup/restore is disabled",
            )
        return self._coordinator.run_write_locked(
            lambda: self._restore_locked(
                str(journal["backup_id"]), mode=str(journal["mode"]),
                dry_run=False, operator_id="restore-journal", resume=journal,
            ),
        )

    # ------------------------------------------------------ restore snapshots
    def _snapshot_root(self) -> Path:
        return self._data_dir / "restore_snapshots"

    def list_restore_snapshots(self) -> list[dict[str, object]]:
        """List pre-restore snapshots so an operator can roll back by hand."""
        root = self._snapshot_root()
        if not root.exists():
            return []
        entries: list[dict[str, object]] = []
        for directory in sorted(
            (path for path in root.iterdir()
             if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ):
            snapshot = directory / "main.sqlite3"
            if not snapshot.exists():
                continue
            metadata: dict[str, Any] = {}
            meta_path = directory / "metadata.json"
            if meta_path.exists():
                try:
                    loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    loaded = {}
                if isinstance(loaded, dict):
                    metadata = loaded
            entries.append({
                "snapshot_id": directory.name,
                "created_at": metadata.get("created_at"),
                "db_lineage_id": metadata.get("db_lineage_id"),
                "size": snapshot.stat().st_size,
            })
        return entries

    def rollback_to_snapshot(self, snapshot_id: str, *, confirm: bool = False,
                             operator_id: str = "admin") -> dict[str, object]:
        """Restore one pre-restore snapshot into the live database."""
        self._require_enabled()
        if not confirm:
            raise ValidationError("snapshot rollback requires explicit confirmation")
        return self._coordinator.run_maintenance_locked(
            lambda: self._rollback_snapshot_locked(snapshot_id, operator_id),
        )

    def _rollback_snapshot_locked(self, snapshot_id: str,
                                  operator_id: str) -> dict[str, object]:
        if not snapshot_id or any(char in snapshot_id for char in ("/", "\\", "..")):
            raise ValidationError("invalid snapshot id")
        directory = self._snapshot_root() / snapshot_id
        snapshot = directory / "main.sqlite3"
        if not snapshot.exists():
            raise NotFound("restore snapshot not found")
        self._verify_file(snapshot)
        metadata: dict[str, Any] = {}
        meta_path = directory / "metadata.json"
        if meta_path.exists():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StorageError(
                    "restore snapshot metadata is corrupt",
                ) from exc
            if not isinstance(loaded, dict):
                raise StorageError("restore snapshot metadata is invalid")
            metadata = loaded
        lineage = str(metadata.get("db_lineage_id") or "")
        if not lineage or lineage != self._store.current_lineage_id():
            raise Conflict(
                "restore snapshot belongs to a different database lineage",
            )
        if self._journal_path.exists():
            try:
                journal = json.loads(self._journal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise StorageError(
                    "restore journal is corrupt; refusing to roll back",
                ) from exc
            if not isinstance(journal, dict) or journal.get("phase") != "failed":
                raise StorageError(
                    "an unfinished restore journal exists; restart the plugin first",
                )
        safety = self._pre_restore_snapshot()
        with _readonly(snapshot) as source:
            source.backup(self._database.connection)
        if self._database.path != ":memory:":
            self._verify_file(Path(self._database.path))
        self._database.connection.execute(
            "INSERT INTO memory_meta(key,value) VALUES('fts_dirty_ids',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_RESTORE_FTS_SENTINEL,),
        )
        self._index.rebuild()
        if self._vector is not None:
            self._vector.mark_stale(at=self._clock.now())
        try:
            self._journal_path.unlink()
        except OSError:
            pass
        return {
            "snapshot_id": snapshot_id,
            "snapshot_path": str(snapshot),
            "db_lineage_id": lineage,
            "pre_rollback_snapshot": safety,
            "operator_id": operator_id,
            "rolled_back": True,
        }

    def _replay_events(self, manifest: Mapping[str, Any]) -> tuple[LedgerEvent, ...]:
        watermark = int(manifest.get("ledger_seq_at_backup", 0))
        events = self._ledger.ledger.events_after(watermark)
        unique: dict[str, LedgerEvent] = {}
        for event in events:
            existing = unique.get(event.event_id)
            if existing is None or event.seq < existing.seq:
                unique[event.event_id] = event
        ordered = tuple(sorted(unique.values(), key=lambda item: item.seq))
        try:
            for event in ordered:
                if event.event_type is LedgerEventType.UNKNOWN:
                    if str(event.payload.get("category", "")).lower() == "informational":
                        continue
                    raise StorageError(
                        f"unknown ledger event {event.raw_event_type!r}; refusing restore",
                    )
                if event.event_type is LedgerEventType.DELETE_MEMORY:
                    lineage_fields_from_payload(event.payload)
                elif event.event_type is LedgerEventType.CLEAR_SCOPE:
                    boundaries_from_payload(event.payload)
                elif event.event_type is LedgerEventType.REMOVE_ATTACHMENT:
                    lineage_fields_from_payload(event.payload)
                    if (not event.payload.get("attachment_type")
                            or not event.payload.get("attachment_key")):
                        raise StorageError(
                            "remove_attachment event is missing attachment identity",
                        )
                elif event.event_type is LedgerEventType.DESTROY_SCOPE:
                    scope_id = event.scope_id or event.payload.get("scope_id")
                    if not scope_id:
                        raise StorageError("destroy_scope event is missing scope_id")
                    mode = event.payload.get("backup_mode")
                    if mode not in {"REQUIRED", "SKIPPED"}:
                        raise StorageError("destroy_scope event has an invalid backup_mode")
                    if mode == "REQUIRED" and (
                        not event.payload.get("backup_path")
                        or not event.payload.get("backup_sha256")
                    ):
                        raise StorageError(
                            "destroy_scope REQUIRED event is missing backup identity",
                        )
                    if not event.payload.get("operation_id"):
                        raise StorageError(
                            "destroy_scope event is missing operation_id",
                        )
                elif event.event_type is LedgerEventType.CONSENT_CHANGE:
                    if not all(event.payload.get(key) for key in (
                        "consent_type", "new_state", "source",
                    )):
                        raise StorageError("consent_change event is missing fields")
                elif event.event_type is LedgerEventType.ARCHIVE_MEMORY:
                    lineage_fields_from_payload(event.payload)
                    try:
                        ArchiveReason(str(event.payload.get("archive_reason")))
                    except ValueError as exc:
                        raise StorageError("archive_memory event has an invalid reason") from exc
                    time_from_payload(event.payload.get("archived_at"), "archived_at")
                    time_from_payload(event.payload.get("purge_after"), "purge_after")
                elif event.event_type is LedgerEventType.RESTORE_MEMORY:
                    lineage_fields_from_payload(event.payload)
                    time_from_payload(event.payload.get("occurred_at"), "occurred_at")
                elif event.event_type is LedgerEventType.PURGE_MEMORY:
                    lineage_fields_from_payload(event.payload)
        except ValidationError as exc:
            raise StorageError(f"ledger event cannot be replayed: {exc}") from exc
        return ordered

    def _pre_exclusion_sets(self, directory: Path,
                            events: tuple[LedgerEvent, ...],
                            ) -> tuple[set[str], set[str]]:
        """Rows already excluded by the ledger must not be inserted at all.

        Inserting them first would collide with current ACTIVE rows on the
        deduplication index before the final exclusion pass could remove them.
        """
        delete_ids: set[str] = set()
        destroy_scopes: set[str] = set()
        clear_conditions: list[tuple[str, str, int]] = []
        for event in events:
            payload = event.payload
            if event.event_type is LedgerEventType.DELETE_MEMORY:
                memory_id = event.memory_id or payload.get("memory_id")
                if isinstance(memory_id, str):
                    delete_ids.add(memory_id)
            elif event.event_type is LedgerEventType.PURGE_MEMORY:
                memory_id = event.memory_id or payload.get("memory_id")
                if isinstance(memory_id, str):
                    delete_ids.add(memory_id)
            elif event.event_type is LedgerEventType.DESTROY_SCOPE:
                scope_id = event.scope_id or payload.get("scope_id")
                if isinstance(scope_id, str):
                    destroy_scopes.add(scope_id)
            elif event.event_type is LedgerEventType.CLEAR_SCOPE:
                scope_id = event.scope_id
                if not scope_id:
                    raise StorageError("clear_scope event is missing scope_id")
                for lineage, boundary in boundaries_from_payload(payload).items():
                    clear_conditions.append((scope_id, lineage, boundary))
        excluded_ids = set(delete_ids)
        main_path = directory / "main.sqlite3"
        with _readonly(main_path) as connection:
            rows = connection.execute(
                "SELECT id, scope_id, db_lineage_id, creation_seq FROM memories",
            ).fetchall()
        backup_ids = {row["id"] for row in rows}
        excluded_ids &= backup_ids
        for row in rows:
            if row["scope_id"] in destroy_scopes:
                excluded_ids.add(row["id"])
                continue
            if any(
                row["scope_id"] == scope and row["db_lineage_id"] == lineage
                and int(row["creation_seq"]) <= boundary
                for scope, lineage, boundary in clear_conditions
            ):
                excluded_ids.add(row["id"])
        return excluded_ids, destroy_scopes

    def _restore_locked(self, backup_id: str, *, mode: str, dry_run: bool,
                        operator_id: str,
                        resume: Mapping[str, Any] | None) -> dict[str, object]:
        manifest, directory = self._load_manifest(backup_id)
        if not dry_run:
            # Settle any crash-left intent before reading the replay window.
            self._ledger.strict_drain(self._store, self._consents)
        self._verify_backup(manifest, directory, mode)
        events = self._replay_events(manifest)
        self._verify_clear_boundaries(manifest, directory, events, mode=mode)
        if dry_run:
            return self._dry_run_report(manifest, directory, events, mode)
        excluded_ids, excluded_scopes = self._pre_exclusion_sets(directory, events)
        main_path = directory / "main.sqlite3"
        current_lineage = self._store.current_lineage_id()
        if mode == "disaster-rebuild":
            if resume is not None:
                new_lineage = str(resume.get("new_db_lineage_id") or "")
                if not new_lineage:
                    raise StorageError("disaster journal is missing its new lineage")
            else:
                new_lineage = self._ids.new_id("lineage")
                if self._target_has_data():
                    raise Conflict(
                        "disaster-rebuild requires an empty target database",
                    )
        else:
            new_lineage = current_lineage
        pre_snapshot = (
            str(resume.get("pre_restore_snapshot")) if resume is not None
            else self._pre_restore_snapshot()
        )
        if resume is None:
            journal = {
                "phase": "prepared",
                "backup_id": backup_id,
                "backup_dir": str(directory),
                "mode": mode,
                "backup_sha256": manifest.get("sha256"),
                "db_lineage_id": manifest.get("db_lineage_id"),
                "ledger_id": manifest.get("ledger_id"),
                "ledger_seq_at_backup": manifest.get("ledger_seq_at_backup"),
                "creation_seq_at_backup": manifest.get("creation_seq_at_backup"),
                "lineage_high_watermarks": manifest.get("lineage_high_watermarks"),
                "pre_restore_snapshot": pre_snapshot,
                "new_db_lineage_id": new_lineage if mode == "disaster-rebuild" else None,
                "started_at": self._clock.now().isoformat(),
                "operator_id": operator_id,
            }
        else:
            journal = dict(resume)
            journal["phase"] = "resuming"
        _atomic_json(self._journal_path, journal)
        try:
            snapshot_schema = self._schema_version(main_path)
            with self._attached(main_path):
                with self._database.transaction() as tx:
                    if mode == "disaster-rebuild":
                        self._clear_target(tx)
                        self._import_all(
                            tx, excluded_ids=excluded_ids,
                            excluded_scopes=excluded_scopes,
                            snapshot_schema=snapshot_schema,
                        )
                        tx.execute(
                            "INSERT INTO memory_meta(key,value) VALUES('db_lineage_id',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (new_lineage,),
                        )
                    else:
                        self._merge_tables(
                            tx, mode=mode, excluded_ids=excluded_ids,
                            excluded_scopes=excluded_scopes,
                            snapshot_schema=snapshot_schema,
                        )
                    self._normalize_archive_columns(tx)
                    applied_excluded = self._apply_exclusions(tx, events)
                    self._advance_watermarks(
                        tx, manifest, events, mode=mode,
                        current_lineage=new_lineage,
                    )
                    self._cleanup_import_jobs(tx)
                    tx.execute(
                        "INSERT INTO memory_meta(key,value) VALUES('fts_dirty_ids',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (_RESTORE_FTS_SENTINEL,),
                    )
            self._index.rebuild()
            if self._vector is not None:
                # Derived vectors are not part of the snapshot; the index
                # worker rebuilds them from authoritative text when STALE.
                self._vector.mark_stale(at=self._clock.now())
            journal["phase"] = "complete"
            journal["completed_at"] = self._clock.now().isoformat()
            _atomic_json(self._journal_path, journal)
            try:
                self._journal_path.unlink()
            except OSError:
                pass
        except BaseException:
            journal["phase"] = "failed"
            _atomic_json(self._journal_path, journal)
            raise
        return {
            "backup_id": backup_id,
            "mode": mode,
            "dry_run": False,
            "db_lineage_id": new_lineage,
            "ledger_seq_at_backup": manifest.get("ledger_seq_at_backup"),
            "creation_seq_at_backup": manifest.get("creation_seq_at_backup"),
            "excluded_count": len(excluded_ids),
            "excluded_rows_deleted": applied_excluded,
            "pre_restore_snapshot": pre_snapshot,
            "restored": True,
        }

    @contextmanager
    def _attached(self, main_path: Path) -> Iterator[sqlite3.Connection]:
        connection = self._database.connection
        try:
            connection.execute(
                "ATTACH DATABASE ? AS backup_db", (str(main_path),),
            )
        except sqlite3.Error as exc:
            raise StorageError("could not attach backup database") from exc
        try:
            yield connection
        finally:
            try:
                connection.execute("DETACH DATABASE backup_db")
            except sqlite3.Error:
                pass

    def _prepare_exclusion_tables(self, tx: sqlite3.Connection,
                                  excluded_ids: set[str],
                                  excluded_scopes: set[str]) -> None:
        tx.execute(
            "CREATE TEMP TABLE IF NOT EXISTS restore_excluded_memories "
            "(id TEXT PRIMARY KEY)",
        )
        tx.execute(
            "CREATE TEMP TABLE IF NOT EXISTS restore_excluded_scopes "
            "(id TEXT PRIMARY KEY)",
        )
        tx.execute("DELETE FROM restore_excluded_memories")
        tx.execute("DELETE FROM restore_excluded_scopes")
        tx.executemany(
            "INSERT OR IGNORE INTO restore_excluded_memories(id) VALUES (?)",
            [(memory_id,) for memory_id in excluded_ids],
        )
        tx.executemany(
            "INSERT OR IGNORE INTO restore_excluded_scopes(id) VALUES (?)",
            [(scope_id,) for scope_id in excluded_scopes],
        )

    def _merge_tables(self, tx: sqlite3.Connection, *, mode: str,
                      excluded_ids: set[str],
                      excluded_scopes: set[str],
                      snapshot_schema: int) -> None:
        self._prepare_exclusion_tables(tx, excluded_ids, excluded_scopes)
        memory_columns = (
            _MEMORY_COLUMNS_V10 if snapshot_schema >= 10 else _MEMORY_COLUMNS_V9
        )
        scope_filter = (
            " AND b.id NOT IN (SELECT id FROM restore_excluded_scopes)"
        )
        memory_filter = (
            " AND b.id NOT IN (SELECT id FROM restore_excluded_memories)"
            " AND b.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)"
        )
        child_filter = (
            " AND b.memory_id NOT IN (SELECT id FROM restore_excluded_memories)"
        )
        relation_filter = (
            " AND b.source_memory_id NOT IN (SELECT id FROM restore_excluded_memories)"
            " AND b.target_memory_id NOT IN (SELECT id FROM restore_excluded_memories)"
        )
        tx.execute(
            _insert_select("memory_scopes", _SCOPE_COLUMNS)
            + " WHERE NOT EXISTS (SELECT 1 FROM memory_scopes m WHERE m.id=b.id)"
            + scope_filter,
        )
        tx.execute(
            _insert_select("memories", memory_columns)
            + " WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id=b.id)"
            + memory_filter,
        )
        assignment = ", ".join(f"{column}=b.{column}" for column in memory_columns[1:])
        if mode == "revert":
            tx.execute(
                f"UPDATE memories AS m SET {assignment} "
                "FROM backup_db.memories AS b WHERE m.id=b.id"
                " AND m.id NOT IN (SELECT id FROM restore_excluded_memories)"
                " AND m.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)",
            )
            tx.execute(
                "DELETE FROM memory_revisions WHERE memory_id IN "
                "(SELECT id FROM backup_db.memories)",
            )
            tx.execute(
                "DELETE FROM memory_sources WHERE memory_id IN "
                "(SELECT id FROM backup_db.memories)",
            )
            tx.execute(
                "DELETE FROM memory_tags WHERE memory_id IN "
                "(SELECT id FROM backup_db.memories)",
            )
            # Revert resets source-owned child rows of restored memories to the
            # backup. Relations whose source is not in the backup belong to a
            # non-restored memory and are intentionally left untouched.
            tx.execute(
                "DELETE FROM memory_relations WHERE source_memory_id IN "
                "(SELECT id FROM backup_db.memories)",
            )
        else:
            tx.execute(
                f"UPDATE memories AS m SET {assignment} "
                "FROM backup_db.memories AS b WHERE m.id=b.id "
                "AND m.id NOT IN (SELECT id FROM restore_excluded_memories) "
                "AND m.scope_id NOT IN (SELECT id FROM restore_excluded_scopes) "
                "AND (b.updated_at > m.updated_at "
                "OR (b.updated_at = m.updated_at AND b.current_revision > m.current_revision))",
            )
        tx.execute(
            _insert_select("memory_revisions", _REVISION_COLUMNS,
                           or_ignore=mode != "revert") + " WHERE true" + child_filter,
        )
        tx.execute(
            _insert_select("memory_sources", _SOURCE_COLUMNS,
                           or_ignore=mode != "revert") + " WHERE true" + child_filter,
        )
        tx.execute(
            _insert_select("memory_tags", _TAG_COLUMNS,
                           or_ignore=mode != "revert") + " WHERE true" + child_filter,
        )
        tx.execute(
            _insert_select("memory_relations", _RELATION_COLUMNS,
                           or_ignore=mode != "revert") + " WHERE true" + relation_filter,
        )
        tx.execute(
            _insert_select("memory_consents", _CONSENT_COLUMNS)
            + """ WHERE NOT EXISTS (
                SELECT 1 FROM memory_consents c
                WHERE c.scope_id=b.scope_id AND c.subject_id=b.subject_id
                  AND c.consent_type=b.consent_type)
                AND b.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)""",
        )
        tx.execute(
            _insert_select("memory_operations", _OPERATION_COLUMNS,
                           or_ignore=True)
            + " WHERE b.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)",
        )
        self._drop_exclusion_tables(tx)

    def _clear_target(self, tx: sqlite3.Connection) -> None:
        for table in (
            "memory_embeddings", "index_jobs", "provider_call_audit",
            "memory_relations", "memory_tags", "memory_sources",
            "memory_revisions", "memories", "memory_consents",
            "memory_operations", "import_jobs", "extraction_jobs",
            "memory_scopes",
        ):
            tx.execute(f"DELETE FROM {table}")
        tx.execute(
            "INSERT INTO index_state(target,status,updated_at) VALUES('VECTOR','STALE',?) "
            "ON CONFLICT(target) DO UPDATE SET status='STALE', updated_at=excluded.updated_at",
            (self._clock.now().isoformat(),),
        )

    def _normalize_archive_columns(self, tx: sqlite3.Connection) -> None:
        """Keep archive columns consistent after restoring a v9 snapshot."""
        tx.execute(
            """UPDATE memories
               SET archived_at=NULL, purge_after=NULL, archive_reason=NULL
               WHERE status<>'ARCHIVED'
                 AND (archived_at IS NOT NULL OR purge_after IS NOT NULL OR archive_reason IS NOT NULL)""",
        )
        tx.execute(
            """UPDATE memories
               SET archived_at=COALESCE(archived_at, updated_at),
                   archive_reason=COALESCE(archive_reason, 'CANDIDATE_EXPIRED'),
                   purge_after=COALESCE(
                       purge_after,
                       strftime('%Y-%m-%dT%H:%M:%f', COALESCE(archived_at, updated_at), '+30 days') || '000Z'
                   )
               WHERE status='ARCHIVED'
                 AND (archived_at IS NULL OR purge_after IS NULL OR archive_reason IS NULL)""",
        )

    def _import_all(self, tx: sqlite3.Connection, *, excluded_ids: set[str],
                    excluded_scopes: set[str], snapshot_schema: int) -> None:
        self._prepare_exclusion_tables(tx, excluded_ids, excluded_scopes)
        memory_columns = (
            _MEMORY_COLUMNS_V10 if snapshot_schema >= 10 else _MEMORY_COLUMNS_V9
        )
        tx.execute(
            _insert_select("memory_scopes", _SCOPE_COLUMNS)
            + " WHERE b.id NOT IN (SELECT id FROM restore_excluded_scopes)",
        )
        tx.execute(
            _insert_select("memories", memory_columns)
            + " WHERE b.id NOT IN (SELECT id FROM restore_excluded_memories)"
            + " AND b.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)",
        )
        child_filter = (
            " WHERE b.memory_id NOT IN (SELECT id FROM restore_excluded_memories)"
        )
        tx.execute(
            _insert_select("memory_revisions", _REVISION_COLUMNS) + child_filter,
        )
        tx.execute(
            _insert_select("memory_sources", _SOURCE_COLUMNS) + child_filter,
        )
        tx.execute(
            _insert_select("memory_tags", _TAG_COLUMNS) + child_filter,
        )
        tx.execute(
            _insert_select("memory_relations", _RELATION_COLUMNS)
            + " WHERE b.source_memory_id NOT IN (SELECT id FROM restore_excluded_memories)"
            + " AND b.target_memory_id NOT IN (SELECT id FROM restore_excluded_memories)",
        )
        tx.execute(
            _insert_select("memory_consents", _CONSENT_COLUMNS)
            + " WHERE b.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)",
        )
        tx.execute(
            _insert_select("memory_operations", _OPERATION_COLUMNS)
            + " WHERE b.scope_id NOT IN (SELECT id FROM restore_excluded_scopes)",
        )
        self._drop_exclusion_tables(tx)

    @staticmethod
    def _drop_exclusion_tables(tx: sqlite3.Connection) -> None:
        tx.execute("DROP TABLE IF EXISTS restore_excluded_memories")
        tx.execute("DROP TABLE IF EXISTS restore_excluded_scopes")

    # -------------------------------------------------------------- exclusions
    def _apply_exclusions(self, tx: sqlite3.Connection,
                          events: tuple[LedgerEvent, ...]) -> int:
        excluded = 0
        consents: dict[tuple[str, str, str], LedgerEvent] = {}
        archive_states: dict[str, LedgerEvent] = {}
        for event in events:
            payload = event.payload
            if event.event_type is LedgerEventType.DELETE_MEMORY:
                lineage, seq = lineage_fields_from_payload(payload)
                memory_id = event.memory_id or payload.get("memory_id")
                if not isinstance(memory_id, str):
                    raise StorageError("delete_memory event is missing memory_id")
                row = tx.execute(
                    "SELECT 1 FROM memories WHERE id=? AND db_lineage_id=? AND creation_seq=?",
                    (memory_id, lineage, seq),
                ).fetchone()
                if row is not None:
                    self._delete_memories(tx, (memory_id,))
                    excluded += 1
            elif event.event_type is LedgerEventType.PURGE_MEMORY:
                lineage, seq = lineage_fields_from_payload(payload)
                memory_id = event.memory_id or payload.get("memory_id")
                if not isinstance(memory_id, str):
                    raise StorageError("purge_memory event is missing memory_id")
                row = tx.execute(
                    "SELECT 1 FROM memories WHERE id=? AND db_lineage_id=? AND creation_seq=?",
                    (memory_id, lineage, seq),
                ).fetchone()
                if row is not None:
                    self._delete_memories(tx, (memory_id,))
                    excluded += 1
            elif event.event_type in (
                LedgerEventType.ARCHIVE_MEMORY, LedgerEventType.RESTORE_MEMORY,
            ):
                memory_id = event.memory_id or payload.get("memory_id")
                if not isinstance(memory_id, str):
                    raise StorageError("archive/restore event is missing memory_id")
                current = archive_states.get(memory_id)
                if current is None or event.seq > current.seq:
                    archive_states[memory_id] = event
            elif event.event_type is LedgerEventType.CLEAR_SCOPE:
                scope_id = event.scope_id
                if not scope_id:
                    raise StorageError("clear_scope event is missing scope_id")
                boundaries = boundaries_from_payload(payload)
                for lineage, boundary in boundaries.items():
                    rows = tx.execute(
                        """SELECT id FROM memories WHERE scope_id=? AND db_lineage_id=?
                        AND creation_seq<=?""",
                        (scope_id, lineage, boundary),
                    ).fetchall()
                    ids = tuple(row["id"] for row in rows)
                    if ids:
                        self._delete_memories(tx, ids)
                        excluded += len(ids)
            elif event.event_type is LedgerEventType.REMOVE_ATTACHMENT:
                memory_id = event.memory_id or payload.get("memory_id")
                attachment_type = str(payload.get("attachment_type", ""))
                attachment_key = str(payload.get("attachment_key", ""))
                if not isinstance(memory_id, str) or not attachment_type or not attachment_key:
                    raise StorageError("remove_attachment event is incomplete")
                if attachment_type == "source":
                    tx.execute(
                        "DELETE FROM memory_sources WHERE memory_id=? AND id=?",
                        (memory_id, attachment_key),
                    )
                elif attachment_type == "tag":
                    tx.execute(
                        "DELETE FROM memory_tags WHERE memory_id=? AND tag=?",
                        (memory_id, attachment_key),
                    )
                elif attachment_type == "relation":
                    tx.execute(
                        """DELETE FROM memory_relations WHERE id=?
                        AND (source_memory_id=? OR target_memory_id=?)""",
                        (attachment_key, memory_id, memory_id),
                    )
                else:
                    raise StorageError("unknown attachment_type in ledger")
            elif event.event_type is LedgerEventType.DESTROY_SCOPE:
                scope_id = event.scope_id or payload.get("scope_id")
                if not isinstance(scope_id, str) or not scope_id:
                    raise StorageError("destroy_scope event is missing scope_id")
                excluded += self._destroy_scope_rows(tx, scope_id)
            elif event.event_type is LedgerEventType.CONSENT_CHANGE:
                key = (
                    str(event.scope_id or payload.get("scope_id") or ""),
                    str(event.subject_id or payload.get("subject_id") or ""),
                    str(payload.get("consent_type") or ""),
                )
                if not all(key):
                    raise StorageError("consent_change event is missing its key")
                current = consents.get(key)
                if current is None or event.seq > current.seq:
                    consents[key] = event
        for (scope_id, subject_id, consent_type), event in consents.items():
            self._enforce_consent_state(
                tx, scope_id, subject_id, consent_type, event,
            )
        for memory_id, event in archive_states.items():
            payload = event.payload
            lineage, seq = lineage_fields_from_payload(payload)
            row = tx.execute(
                "SELECT status FROM memories WHERE id=? AND db_lineage_id=? AND creation_seq=?",
                (memory_id, lineage, seq),
            ).fetchone()
            if row is None:
                continue
            if event.event_type is LedgerEventType.ARCHIVE_MEMORY:
                reason_raw = str(payload.get("archive_reason") or "")
                try:
                    reason = ArchiveReason(reason_raw)
                except ValueError as exc:
                    raise StorageError("archive_memory event has an invalid reason") from exc
                tx.execute(
                    """UPDATE memories SET status='ARCHIVED', archived_at=?,
                       purge_after=?, archive_reason=?, candidate_expires_at=NULL
                       WHERE id=?""",
                    (
                        _ledger_timestamp(payload.get("archived_at"), "archived_at"),
                        _ledger_timestamp(payload.get("purge_after"), "purge_after"),
                        reason.value, memory_id,
                    ),
                )
            else:
                tx.execute(
                    """UPDATE memories SET status='ACTIVE', archived_at=NULL,
                       purge_after=NULL, archive_reason=NULL
                       WHERE id=? AND status='ARCHIVED'""",
                    (memory_id,),
                )
        return excluded

    @staticmethod
    def _delete_memories(tx: sqlite3.Connection, memory_ids: tuple[str, ...]) -> None:
        placeholders = ",".join("?" for _ in memory_ids)
        tx.execute(
            f"DELETE FROM memory_relations WHERE source_memory_id IN ({placeholders}) "
            f"OR target_memory_id IN ({placeholders})",
            (*memory_ids, *memory_ids),
        )
        tx.execute(
            f"DELETE FROM memory_tags WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        tx.execute(
            f"DELETE FROM memory_sources WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        tx.execute(
            f"DELETE FROM memory_revisions WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        tx.execute(
            f"DELETE FROM memory_embeddings WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        tx.execute(
            f"DELETE FROM index_jobs WHERE memory_id IN ({placeholders})",
            memory_ids,
        )
        tx.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})",
            memory_ids,
        )

    def _destroy_scope_rows(self, tx: sqlite3.Connection, scope_id: str) -> int:
        rows = tx.execute(
            "SELECT id FROM memories WHERE scope_id=?", (scope_id,),
        ).fetchall()
        ids = tuple(row["id"] for row in rows)
        if ids:
            self._delete_memories(tx, ids)
        tx.execute("DELETE FROM memory_consents WHERE scope_id=?", (scope_id,))
        tx.execute("DELETE FROM memory_operations WHERE scope_id=?", (scope_id,))
        tx.execute("DELETE FROM import_jobs WHERE scope_id=?", (scope_id,))
        tx.execute("DELETE FROM extraction_jobs WHERE scope_id=?", (scope_id,))
        tx.execute("DELETE FROM memory_scopes WHERE id=?", (scope_id,))
        return len(ids)

    def _enforce_consent_state(self, tx: sqlite3.Connection, scope_id: str,
                               subject_id: str, consent_type: str,
                               event: LedgerEvent) -> None:
        if tx.execute(
            "SELECT 1 FROM memory_scopes WHERE id=?", (scope_id,),
        ).fetchone() is None:
            return
        row = tx.execute(
            """SELECT state FROM memory_consents
            WHERE scope_id=? AND subject_id=? AND consent_type=?
            ORDER BY rowid DESC LIMIT 1""",
            (scope_id, subject_id, consent_type),
        ).fetchone()
        desired = str(event.payload.get("new_state", ""))
        if desired not in {"GRANTED", "REVOKED"}:
            raise StorageError("consent_change new_state is invalid")
        if row is not None and row["state"] == desired:
            return
        now = self._clock.now()
        consent_id = self._ids.new_id("consent")
        granted = now.isoformat() if desired == "GRANTED" else None
        revoked = now.isoformat() if desired == "REVOKED" else None
        tx.execute(
            """INSERT INTO memory_consents
            (id, subject_id, scope_id, consent_type, state, source,
             granted_at, revoked_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'restore_replay', ?, ?, '{}')""",
            (consent_id, subject_id, scope_id, consent_type, desired, granted, revoked),
        )

    # --------------------------------------------------------------- watermarks
    def _advance_watermarks(self, tx: sqlite3.Connection,
                            manifest: Mapping[str, Any],
                            events: tuple[LedgerEvent, ...], *,
                            mode: str, current_lineage: str) -> None:
        final_max = {
            row["db_lineage_id"]: int(row["maximum"])
            for row in tx.execute(
                "SELECT db_lineage_id, MAX(creation_seq) AS maximum FROM memories "
                "GROUP BY db_lineage_id",
            ).fetchall()
        }
        clear_max: dict[str, int] = {}
        for event in events:
            if event.event_type is not LedgerEventType.CLEAR_SCOPE:
                continue
            for lineage, boundary in boundaries_from_payload(event.payload).items():
                clear_max[lineage] = max(clear_max.get(lineage, 0), boundary)
        backup_hw_raw = manifest.get("lineage_high_watermarks", {})
        backup_hw = {str(key): int(value) for key, value in dict(backup_hw_raw).items()}  # type: ignore[arg-type]
        existing = {
            row["db_lineage_id"]: int(row["high_watermark"])
            for row in tx.execute(
                "SELECT db_lineage_id, high_watermark FROM memory_lineage_meta",
            ).fetchall()
        }
        lineages = set(final_max) | set(clear_max) | set(backup_hw) | set(existing)
        lineages.add(current_lineage)
        for lineage in lineages:
            if mode == "disaster-rebuild" and lineage == current_lineage:
                value = 0
            else:
                value = max(
                    existing.get(lineage, 0),
                    final_max.get(lineage, 0),
                    backup_hw.get(lineage, 0),
                    clear_max.get(lineage, 0),
                )
                if mode != "disaster-rebuild" and lineage == current_lineage:
                    value = max(
                        value, int(manifest.get("creation_seq_at_backup", 0)),
                    )
            tx.execute(
                "INSERT INTO memory_lineage_meta(db_lineage_id, high_watermark) "
                "VALUES (?, ?) ON CONFLICT(db_lineage_id) DO UPDATE "
                "SET high_watermark=excluded.high_watermark",
                (lineage, value),
            )

    @staticmethod
    def _cleanup_import_jobs(tx: sqlite3.Connection) -> None:
        rows = tx.execute(
            "SELECT id, created_memory_ids_json FROM import_jobs",
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(row["created_memory_ids_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(raw, list):
                continue
            kept = [
                memory_id for memory_id in raw
                if isinstance(memory_id, str) and tx.execute(
                    "SELECT 1 FROM memories WHERE id=?", (memory_id,),
                ).fetchone() is not None
            ]
            if kept != raw:
                tx.execute(
                    "UPDATE import_jobs SET created_memory_ids_json=? WHERE id=?",
                    (json.dumps(kept, ensure_ascii=False, separators=(",", ":")),
                     row["id"]),
                )

    # ------------------------------------------------------------------ dry run
    def _dry_run_report(self, manifest: Mapping[str, Any], directory: Path,
                        events: tuple[LedgerEvent, ...], mode: str) -> dict[str, object]:
        main_path = directory / "main.sqlite3"
        excluded: set[str] = set()
        clear_boundaries: list[tuple[str, str, int]] = []
        destroy_scopes: set[str] = set()
        delete_ids: set[str] = set()
        for event in events:
            payload = event.payload
            if event.event_type is LedgerEventType.DELETE_MEMORY:
                memory_id = event.memory_id or payload.get("memory_id")
                if isinstance(memory_id, str):
                    delete_ids.add(memory_id)
            elif event.event_type is LedgerEventType.PURGE_MEMORY:
                memory_id = event.memory_id or payload.get("memory_id")
                if isinstance(memory_id, str):
                    delete_ids.add(memory_id)
            elif event.event_type is LedgerEventType.CLEAR_SCOPE:
                scope_id = event.scope_id
                if scope_id:
                    for lineage, boundary in boundaries_from_payload(payload).items():
                        clear_boundaries.append((scope_id, lineage, boundary))
            elif event.event_type is LedgerEventType.DESTROY_SCOPE:
                scope_id = event.scope_id or payload.get("scope_id")
                if isinstance(scope_id, str):
                    destroy_scopes.add(scope_id)
        with _readonly(main_path) as connection:
            rows = connection.execute(
                "SELECT id, scope_id, db_lineage_id, creation_seq FROM memories",
            ).fetchall()
            backup_ids = {row["id"] for row in rows}
        for row in rows:
            memory_id = row["id"]
            if (memory_id in delete_ids or row["scope_id"] in destroy_scopes
                    or any(
                        row["scope_id"] == scope and row["db_lineage_id"] == lineage
                        and int(row["creation_seq"]) <= boundary
                        for scope, lineage, boundary in clear_boundaries
                    )):
                excluded.add(memory_id)
        current_ids = {
            row[0] for row in self._database.connection.execute(
                "SELECT id FROM memories",
            )
        }
        will_revive = [row["id"] for row in rows if row["id"] not in excluded]
        return {
            "backup_id": manifest.get("backup_id"),
            "mode": mode,
            "dry_run": True,
            "total": len(rows),
            "will_revive": len(will_revive),
            "will_not_revive": len(excluded),
            "will_revive_ids": will_revive[:200],
            "will_not_revive_ids": sorted(excluded)[:200],
            "current_rows_not_in_backup": len(current_ids - backup_ids),
            "ledger_seq_at_backup": manifest.get("ledger_seq_at_backup"),
            "creation_seq_at_backup": manifest.get("creation_seq_at_backup"),
            "note": "dry-run 只读；不会修改主库。",
        }

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _archive_section(path: Path) -> dict[str, object]:
        with _readonly(path) as connection:
            row = connection.execute(
                """SELECT count(*) AS count, min(purge_after) AS min_after,
                          max(purge_after) AS max_after
                   FROM memories WHERE status='ARCHIVED'""",
            ).fetchone()
            reasons = {
                str(item["archive_reason"]): int(item["count"])
                for item in connection.execute(
                    """SELECT archive_reason, count(*) AS count FROM memories
                       WHERE status='ARCHIVED' AND archive_reason IS NOT NULL
                       GROUP BY archive_reason""",
                ).fetchall()
            }
        return {
            "archive_schema": 1,
            "archived_count": int(row["count"]) if row is not None else 0,
            "purge_after_min": row["min_after"] if row is not None else None,
            "purge_after_max": row["max_after"] if row is not None else None,
            "archive_reason_counts": reasons,
        }

    def _table_row_counts(self, path: Path) -> dict[str, int]:
        counts: dict[str, int] = {}
        with _readonly(path) as connection:
            for table in _TABLES:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if exists is None:
                    continue
                row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
                counts[table] = int(row[0]) if row is not None else 0
        return counts

    def _target_has_data(self) -> bool:
        try:
            memory_count = int(self._database.connection.execute(
                "SELECT count(*) FROM memories",
            ).fetchone()[0])
            scope_count = int(self._database.connection.execute(
                "SELECT count(*) FROM memory_scopes",
            ).fetchone()[0])
        except sqlite3.Error as exc:
            raise StorageError("could not inspect target database") from exc
        return memory_count > 0 or scope_count > 0

    def _pre_restore_snapshot(self) -> str:
        base = self._data_dir / "restore_snapshots" / _utc_stamp(self._clock.now())
        directory = base
        counter = 1
        while directory.exists():
            directory = base.with_name(f"{base.name}_{counter}")
            counter += 1
        directory.mkdir(parents=True, exist_ok=False)
        target = directory / "main.sqlite3"
        self._database.backup_to(target)
        metadata = {
            "db_lineage_id": self._store.current_lineage_id(),
            "lineage_high_watermarks": self._store.lineage_high_watermarks(),
            "created_at": self._clock.now().isoformat(),
        }
        _atomic_json(directory / "metadata.json", metadata)
        _fsync_file(target)
        return str(target)

    def _enforce_retention(self) -> None:
        if not self._root.exists():
            return
        entries = sorted(
            (path for path in self._root.iterdir()
             if path.is_dir() and not path.name.startswith(".")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        cutoff = self._clock.now().timestamp() - self._retention_days * 86400
        for index, path in enumerate(entries):
            if index < self._keep_last:
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
