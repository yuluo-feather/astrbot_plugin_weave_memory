"""V0 startup consistency checks; no background task queue exists in V0."""

from dataclasses import dataclass

from ..core.errors import IndexUnavailable, StorageError
from ..core.ports import SchemaVersionProvider, SearchIndex


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    schema_version: int
    fts_rebuilt: bool
    indexed_active_count: int


class StartupRecoveryService:
    def __init__(self, database: SchemaVersionProvider, index: SearchIndex) -> None:
        self._database = database
        self._index = index

    def run(self) -> RecoveryReport:
        version = self._database.schema_version()
        if version != self._database.CURRENT_SCHEMA_VERSION:
            raise StorageError("database schema migration is incomplete")
        try:
            self._index.integrity_check()
            healthy = self._index.is_healthy()
        except IndexUnavailable:
            count = self._index.recreate_and_rebuild()
            return RecoveryReport(version, True, count)
        if not healthy:
            count = self._index.rebuild()
            return RecoveryReport(version, True, count)
        return RecoveryReport(version, False, self._index.document_count())
