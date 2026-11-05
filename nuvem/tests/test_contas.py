import re

import pytest

from nuvem import app as modulo
from nuvem import correio


@pytest.fixture()
def caixa(monkeypatch):
    """Liga a confirmação por e-mail e captura as mensagens."""
    msgs = []
    monkeypatch.setattr(correio, "CAIXA_DE_TESTE", msgs)
    return msgs


def codigo_de(msg):
    return re.search(r"\b(\d{6})\b", msg["Subject"]).group(1)


def novo(c, email="ana@gmail.com"):
    return c.post("/api/cadastro", json={"nome": "Ana", "email": email, "senha": "senha-forte-1"})


def test_conta_nova_so_ganha_creditos_depois_de_confirmar(cliente, caixa):
    u = novo(cliente).json()
    assert u["email_verificado"] is False and u["creditos"] == 0
    assert len(caixa) == 1 and caixa[0]["To"] == "ana@gmail.com"
    # sem confirmar não usa a IA nem conecta máquina
    assert cliente.post("/api/mensagens", json={"texto": "oi"}).status_code == 403
    assert cliente.post("/api/parque/parear").status_code == 403
    assert cliente.post("/api/verificar", json={"codigo": "000000"}).status_code == 400
    u = cliente.post("/api/verificar", json={"codigo": codigo_de(caixa[0])}).json()
    assert u["email_verificado"] is True and u["creditos"] == modulo.CREDITOS_INICIAIS
    assert cliente.post("/api/parque/parear").status_code == 200


def test_codigo_trava_depois_de_5_erros(cliente, caixa):
    novo(cliente)
    for _ in range(5):
        cliente.post("/api/verificar", json={"codigo": "999999"})
    r = cliente.post("/api/verificar", json={"codigo": codigo_de(caixa[0])})
    assert r.status_code == 429


def test_reenviar_tem_intervalo(cliente, caixa):
    novo(cliente)
    assert cliente.post("/api/verificar/reenviar").status_code == 200
    assert cliente.post("/api/verificar/reenviar").status_code == 429  # um por minuto
    assert len(caixa) == 2


def test_email_igual_escrito_diferente_nao_cria_outra_conta(cliente):
    assert novo(cliente, "caio.rosa@gmail.com").status_code == 200
    cliente.cookies.clear()
    for variante in ("Caio.Rosa+jarvis@googlemail.com", "c.a.i.o.r.o.s.a@gmail.com"):
        assert novo(cliente, variante).status_code == 409


def test_email_descartavel_recusado(cliente):
    r = novo(cliente, "teste@mailinator.com")
    assert r.status_code == 400 and "temporários" in r.json()["detail"]


def test_limite_de_cadastros_por_ip(cliente):
    for i in range(3):
        assert novo(cliente, f"pessoa{i}@exemplo.com").status_code == 200
        cliente.cookies.clear()
    assert novo(cliente, "pessoa9@exemplo.com").status_code == 429


def test_navegador_novo_pede_codigo_por_email(cliente, caixa):
    novo(cliente)
    cliente.post("/api/verificar", json={"codigo": codigo_de(caixa[0])})
    cliente.post("/api/sair")
    # mesmo navegador (cookie de dispositivo confiável): entra direto
    r = cliente.post("/api/entrar", json={"email": "ana@gmail.com", "senha": "senha-forte-1"}).json()
    assert r["email"] == "ana@gmail.com"
    # navegador novo: pede código
    cliente.post("/api/sair")
    cliente.cookies.clear()
    r = cliente.post("/api/entrar", json={"email": "ana@gmail.com", "senha": "senha-forte-1"}).json()
    assert r["precisa_codigo"] is True and r["email"] == "an•@gmail.com"
    assert cliente.get("/api/eu").status_code == 401
    assert cliente.post("/api/entrar/codigo", json={"desafio": r["desafio"], "codigo": "111111"}).status_code == 400
    u = cliente.post("/api/entrar/codigo", json={"desafio": r["desafio"], "codigo": codigo_de(caixa[-1])}).json()
    assert u["email"] == "ana@gmail.com" and cliente.get("/api/eu").status_code == 200


def test_sair_de_todos(cliente, caixa):
    novo(cliente)
    cliente.post("/api/verificar", json={"codigo": codigo_de(caixa[0])})
    assert cliente.post("/api/sair-de-todos").status_code == 200
    assert cliente.get("/api/eu").status_code == 401


def test_senha_errada_bloqueia_conta_depois_de_8(cliente):
    novo(cliente)
    for _ in range(8):
        cliente.post("/api/entrar", json={"email": "ana@gmail.com", "senha": "errada"})
    r = cliente.post("/api/entrar", json={"email": "ana@gmail.com", "senha": "senha-forte-1"})
    assert r.status_code == 429


def test_admin(cliente, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "chefe@exemplo.com")
    novo(cliente, "ana@exemplo.com")
    assert cliente.get("/api/admin/resumo").status_code == 404  # não-admin nem vê que existe
    cliente.cookies.clear()
    chefe = novo(cliente, "chefe@exemplo.com").json()
    assert chefe["admin"] is True
    r = cliente.get("/api/admin/resumo").json()
    ana = next(u for u in r["usuarios"] if u["email"] == "ana@exemplo.com")
    assert cliente.post(f"/api/admin/usuarios/{ana['id']}/plano", json={"plano": "pro", "creditos": 4500}).status_code == 200
    ana = next(u for u in cliente.get("/api/admin/resumo").json()["usuarios"] if u["email"] == "ana@exemplo.com")
    assert ana["plano"] == "pro" and ana["creditos"] == modulo.CREDITOS_INICIAIS + 4500


def test_cabecalhos_de_seguranca(cliente):
    r = cliente.get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert cliente.get("/docs").status_code == 404
