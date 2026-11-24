import sqlite3

import pytest
from data.plugins.astrbot_plugin_weave_memory.adapters.sqlite.ledger import SQLiteLedger
from data.plugins.astrbot_plugin_weave_memory.core.errors import StorageError


def test_ledger_requiring_newer_reader_is_refused(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    ledger = SQLiteLedger(path)
    ledger.close()

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE ledger_meta SET value='99' WHERE key='min_reader_version'",
    )
    conn.commit()
    conn.close()

    with pytest.raises(StorageError):
        SQLiteLedger(path)
