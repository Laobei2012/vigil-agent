import os

import psycopg
import pytest

from vigil_agent.state.store import Store

DSN = os.environ.get("VIGIL_TEST_DATABASE_URL", "postgresql://vigil:vigil@localhost:55432/vigil")
TABLES = (
    "inbox",
    "processed_events",
    "incidents",
    "transcripts",
    "investigations",
    "spend",
    "feedback",
    "pending_flaps",
    "slack_outbox",
)


def _db_available() -> bool:
    try:
        psycopg.connect(DSN, connect_timeout=2).close()
        return True
    except psycopg.OperationalError:
        return False


@pytest.fixture
async def store():
    if not _db_available():
        pytest.skip("PostgreSQL is not available; set VIGIL_TEST_DATABASE_URL")
    s = Store(DSN, max_size=30)
    await s.open()
    await s.migrate()
    await s.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    yield s
    await s.close()
