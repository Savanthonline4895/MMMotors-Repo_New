"""The real lifespan must hand the database to every route module (they each hold their own
`db` since server.py was split). Runs startup end to end against in-memory Mongo."""
import pytest
from fastapi.testclient import TestClient

import server
import database


def test_lifespan_wires_db_into_every_route_module(monkeypatch):
    mm = pytest.importorskip("mongomock_motor")
    client = mm.AsyncMongoMockClient()
    monkeypatch.setattr(server, "AsyncIOMotorClient", lambda *a, **kw: client)
    monkeypatch.setattr(server, "AsyncIOMotorGridFSBucket", lambda db: object())
    monkeypatch.setattr(server, "_ensure_indexes", lambda: _noop())   # mongomock lacks some index options
    with TestClient(server.app) as c:
        assert all(m.db is database.db for m in server._DB_MODULES)
        assert c.get("/ready").status_code == 200
        # routes in different modules all reach the database (401 = ran auth against it)
        assert c.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401
        assert c.get("/api/booking/availability").status_code in (200, 422)
        assert c.get("/api/feedback/not-a-token").status_code == 404


async def _noop():
    return None
