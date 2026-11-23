"""pytest 夹具：每个测试使用独立临时 SQLite 的 FastAPI TestClient。"""

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test_rain.db"
    monkeypatch.setenv("RAIN_DB_PATH", str(db))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    main.store.close()
