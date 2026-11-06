import os

import pytest
from fastapi.testclient import TestClient

os.environ["IA_FAKE"] = "1"
os.environ["COOKIE_SEGURO"] = "0"

from nuvem import app as modulo  # noqa: E402


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    """SQLite por padrão; com TEST_DATABASE_URL roda os mesmos testes em Postgres."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.setattr(modulo, "DB_PATH", tmp_path / "teste.db")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        import psycopg

        with psycopg.connect(url, autocommit=True) as con:
            con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        monkeypatch.setenv("DATABASE_URL", url)
    modulo._bancos.clear()
    monkeypatch.setattr(modulo, "COOKIE_SEGURO", False)
    return TestClient(modulo.app)


