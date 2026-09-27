import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="proxyhub-test-")
os.environ["PROXYHUB_DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["PROXYHUB_DATA_DIR"] = TMP
os.environ["PROXYHUB_SECRET_KEY"] = "test-secret-key-1234567890abcdef"
os.environ["PROXYHUB_SUBSCRIPTION_URL"] = "https://sub.example.com/FAKE-SECRET-99/xyz"

sys.path.insert(0, "/opt/projects/proxy-hub/backend")

import pytest

from app.config import refresh_settings
from app.db import init_db, reset_engine


@pytest.fixture()
def db():
    reset_engine()
    refresh_settings()
    init_db()
    from app.db import Base, get_session_factory

    session = get_session_factory()()
    try:
        # Isolate: wipe all rows before each test (same temp DB file).
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        yield session
    finally:
        session.close()
