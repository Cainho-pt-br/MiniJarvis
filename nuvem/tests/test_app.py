import json
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
            con.execute("DROP TABLE IF EXISTS extrato, mensagens, conversas, sessoes, usuarios CASCADE")
        monkeypatch.setenv("DATABASE_URL", url)
    modulo._bancos.clear()
    monkeypatch.setattr(modulo, "COOKIE_SEGURO", False)
    modulo._tentativas.clear()
    return TestClient(modulo.app)


def cadastrar(c, email="ana@exemplo.com"):
    r = c.post("/api/cadastro", json={"nome": "Ana", "email": email, "senha": "senha-forte-1"})
    assert r.status_code == 200, r.text
    return r.json()


def eventos(texto):
    saida = []
    for bloco in texto.strip().split("\n\n"):
        linhas = dict(l.split(": ", 1) for l in bloco.splitlines())
        saida.append((linhas["event"], json.loads(linhas["data"])))
    return saida


def test_cadastro_entrada_e_saida(cliente):
    u = cadastrar(cliente)
    assert u["creditos"] == modulo.CREDITOS_INICIAIS
    assert cliente.get("/api/eu").json()["email"] == "ana@exemplo.com"
    cliente.post("/api/sair")
    assert cliente.get("/api/eu").status_code == 401
    assert cliente.post("/api/entrar", json={"email": "ana@exemplo.com", "senha": "errada"}).status_code == 401
    assert cliente.post("/api/entrar", json={"email": "ANA@exemplo.com", "senha": "senha-forte-1"}).status_code == 200


def test_email_duplicado_e_senha_curta(cliente):
    cadastrar(cliente)
    r = cliente.post("/api/cadastro", json={"nome": "B", "email": "ana@exemplo.com", "senha": "senha-forte-1"})
    assert r.status_code == 409
    r = cliente.post("/api/cadastro", json={"nome": "B", "email": "b@exemplo.com", "senha": "curta"})
    assert r.status_code == 422


def test_conversa_cobra_creditos_e_guarda_historico(cliente):
    cadastrar(cliente)
    r = cliente.post("/api/mensagens", json={"texto": "Oi, Jarvis"})
    ev = eventos(r.text)
    assert ev[0][0] == "conversa"
    assert "".join(d for e, d in ev if e == "texto") == "Entendi: Oi, Jarvis"
    fim = ev[-1][1]
    assert fim["custo"] >= 1
    assert fim["creditos"] == modulo.CREDITOS_INICIAIS - fim["custo"]

    cid = ev[0][1]["id"]
    r = cliente.post("/api/mensagens", json={"texto": "Continua", "conversa_id": cid})
    assert eventos(r.text)[-1][0] == "fim"
    conversa = cliente.get(f"/api/conversas/{cid}").json()
    assert [m["papel"] for m in conversa["mensagens"]] == ["user", "assistant", "user", "assistant"]


def test_sem_creditos_bloqueia(cliente):
    u = cadastrar(cliente)
    modulo.db().execute("UPDATE usuarios SET creditos = 0 WHERE id = ?", (u["id"],))
    assert cliente.post("/api/mensagens", json={"texto": "Oi"}).status_code == 402


def test_usuario_nao_ve_conversa_de_outro(cliente):
    cadastrar(cliente)
    cid = eventos(cliente.post("/api/mensagens", json={"texto": "segredo"}).text)[0][1]["id"]
    cliente.post("/api/sair")
    cliente.cookies.clear()
    cadastrar(cliente, "beto@exemplo.com")
    assert cliente.get(f"/api/conversas/{cid}").status_code == 404
    assert cliente.post("/api/mensagens", json={"texto": "x", "conversa_id": cid}).status_code == 404
    assert cliente.get("/api/conversas").json() == []


def test_apagar_conta(cliente):
    cadastrar(cliente)
    cliente.post("/api/mensagens", json={"texto": "Oi"})
    assert cliente.delete("/api/eu").status_code == 200
    assert modulo.db().execute("SELECT COUNT(*) AS n FROM conversas").fetchone()["n"] == 0


def test_limite_de_tentativas(cliente):
    for _ in range(10):
        cliente.post("/api/entrar", json={"email": "x@exemplo.com", "senha": "errada"})
    assert cliente.post("/api/entrar", json={"email": "x@exemplo.com", "senha": "errada"}).status_code == 429


def test_falha_da_ia_vai_para_o_log_sem_cobrar(cliente, monkeypatch, caplog):
    class IAQuebrada:
        def responder(self, historico, final, modelo):
            final.modelo = modelo
            raise RuntimeError("chave inválida")
            yield  # pragma: no cover

    monkeypatch.setattr(modulo, "_ia", IAQuebrada())
    cadastrar(cliente)
    with caplog.at_level("ERROR", logger="minijarvis"):
        ev = eventos(cliente.post("/api/mensagens", json={"texto": "Oi"}).text)
    assert ev[-1][0] == "erro"
    assert "RuntimeError" in caplog.text and "chave inválida" in caplog.text
    assert cliente.get("/api/eu").json()["creditos"] == modulo.CREDITOS_INICIAIS
