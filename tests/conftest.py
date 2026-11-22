from datetime import UTC, datetime

import pytest

from weavemem.core.model import MemoryEntity, MemoryStatus, MemoryType, SourceType
from weavemem.core.normalization import normalize_content

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

@pytest.fixture
def memory_factory():
    def make(*, id="m-1", scope_id="scope-a", subject_id="user-a", content="喜欢黑咖啡。",
             status=MemoryStatus.ACTIVE, revision=1, superseded_by=None):
        return MemoryEntity(
            id=id, scope_id=scope_id, subject_id=subject_id,
            memory_type=MemoryType.PREFERENCE, content=content,
            normalized_content=normalize_content(content), status=status,
            source_type=SourceType.EXPLICIT, current_revision=revision,
            created_at=NOW, updated_at=NOW, superseded_by=superseded_by,
        )
    return make
