import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from helpdesk.db.models import Base
from helpdesk.db.session import make_engine, make_session_factory

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://helpdesk:helpdesk@localhost:5433/helpdesk_test"
)


@pytest.fixture(scope="session")
def _migrated():
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args], env=env, check=True)


@pytest.fixture(scope="session")
async def engine(_migrated):
    eng = make_engine(TEST_DB_URL)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sf(engine):
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)


@pytest.fixture
async def s(sf):
    async with sf() as session:
        yield session


class FrozenClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock_(monkeypatch):
    fc = FrozenClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
    monkeypatch.setattr("helpdesk.clock.utcnow", fc)
    return fc
