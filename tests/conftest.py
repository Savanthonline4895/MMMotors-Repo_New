"""In-memory MongoDB for handler tests, with real transaction rollback.

mongomock has no transactions, so FakeClient.start_session() snapshots every collection
before the callback and restores it if the callback raises — the same all-or-nothing
outcome Atlas gives. That is the behaviour the stock tests need to pin.
Needs: pip install pytest mongomock-motor
"""
import asyncio
import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

import pytest

try:
    import mongomock
    from mongomock_motor import AsyncMongoMockClient
    mongomock.ignore_feature("session")   # accept session=…; _Session below does the rollback
except ImportError:          # GST / audit tests still run without it
    AsyncMongoMockClient = None


class _Session:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def with_transaction(self, callback):
        names = await self._db.list_collection_names()
        snapshot = {n: await self._db[n].find({}).to_list(None) for n in names}
        try:
            return await callback(self)
        except BaseException:
            for n in set(await self._db.list_collection_names()) | set(snapshot):
                await self._db[n].delete_many({})
                if snapshot.get(n):
                    await self._db[n].insert_many(snapshot[n])
            raise


class FakeClient:
    def __init__(self, db):
        self._db = db

    async def start_session(self):
        return _Session(self._db)


OWNER = {"id": "000000000000000000000001", "username": "owner", "role": "owner", "name": "Owner"}


@pytest.fixture
def mongo(monkeypatch):
    """Fresh in-memory DB wired into database.py and server.py; returns (db, run)."""
    if AsyncMongoMockClient is None:
        pytest.skip("mongomock-motor not installed")
    import database
    import server
    db = AsyncMongoMockClient()["mmmotors_test"]
    monkeypatch.setattr(database, "db", db)
    monkeypatch.setattr(database, "client", FakeClient(db))
    monkeypatch.setattr(server, "db", db)
    loop = asyncio.new_event_loop()
    yield db, loop.run_until_complete
    loop.close()
