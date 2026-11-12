"""Test configuration: point every test at an isolated SQLite file."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="ocr-archive-tests-"))
os.environ.setdefault("ARCHIVE_DB_PATH", str(_TEST_ROOT / "bootstrap.db"))
os.environ.setdefault("REBUILD_BATCH_DELAY_MS", "120")
os.environ.setdefault("REBUILD_BATCH_SIZE", "2")
os.environ.setdefault("REBUILD_LEASE_SECONDS", "3")

# Must be imported after the environment above is set.
from app.config import SETTINGS  # noqa: E402


def configure_database(path: Path) -> None:
    object.__setattr__(SETTINGS, "db_path", path)
    object.__setattr__(SETTINGS, "rebuild_batch_delay_ms", 120)
    object.__setattr__(SETTINGS, "rebuild_batch_size", 2)
    object.__setattr__(SETTINGS, "rebuild_lease_seconds", 3)


from app import publication


def reset_public_state() -> None:
    """Withdraw every publication from the previous test's database file."""

    conn = publication.connect()
    try:
        publication.begin_immediate(conn)
        conn.execute("DELETE FROM public_fts")
        conn.execute("DELETE FROM publications")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(tmp_path: Path):
    configure_database(tmp_path / "archive.db")
    from app.main import app
    from app.rebuild import cancel_local_rebuilds

    with TestClient(app) as test_client:
        reset_public_state()
        yield test_client
    cancel_local_rebuilds()


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001
    import shutil

    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
