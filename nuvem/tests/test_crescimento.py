"""Indicação, link compartilhável, guias e sitemap."""
import os
import re

import pytest

os.environ["IA_FAKE"] = "1"
os.environ["COOKIE_SEGURO"] = "0"

from nuvem import app as modulo  # noqa: E402
from nuvem import correio, crescimento  # noqa: E402


@pytest.fixture()
def caixa(monkeypatch):
    msgs = []
    monkeypatch.setattr(correio, "CAIXA_DE_TESTE", msgs)
    return msgs


def cadastrar(c, email, indicacao=""):
    c.post("/api/cadastro", json={"nome": "Ana", "email": email, "senha": "senha-forte-1", "aceito": True,
                                  "indicacao": indicacao})


def confirmar(c, caixa):
    cod = re.search(r"\b(\d{6})\b", caixa[-1]["Subject"]).group(1)
    return c.post("/api/verificar", json={"codigo": cod}).json()


def test_indicacao_premia_os_dois_so_depois_de_confirmar(cliente, caixa):
    cadastrar(cliente, "ana@exemplo.com")
    ana = confirmar(cliente, caixa)
    ind = cliente.get("/api/indicacao").json()
    assert ind["codigo"] == ana["codigo_indicacao"] and ind["link"].endswith("/?ref=" + ind["codigo"])
    assert ind["indicados"] == 0
    cliente.cookies.clear()
    cadastrar(cliente, "bia@exemplo.com", ind["codigo"])
    assert cliente.get("/api/eu").json()["creditos"] == 0  # nada antes de confirmar
    bia = confirmar(cliente, caixa)
    assert bia["creditos"] == modulo.CREDITOS_INICIAIS + crescimento.BONUS_INDICADO
    cliente.cookies.clear()
    cliente.post("/api/entrar", json={"email": "ana@exemplo.com", "senha": "senha-forte-1"})
    cod = re.search(r"\b(\d{6})\b", caixa[-1]["Subject"]).group(1)
    desafio = cliente.post("/api/entrar", json={"email": "ana@exemplo.com", "senha": "senha-forte-1"}).json()
    cod = re.search(r"\b(\d{6})\b", caixa[-1]["Subject"]).group(1)
    ana = cliente.post("/api/entrar/codigo", json={"desafio": desafio["desafio"], "codigo": cod}).json()
    assert ana["creditos"] == modulo.CREDITOS_INICIAIS + crescimento.BONUS_INDICADOR
    assert cliente.get("/api/indicacao").json()["indicados"] == 1
    # código inventado é ignorado em silêncio
    cliente.cookies.clear()
    cadastrar(cliente, "caio@exemplo.com", "zzzzzzzz")
    assert confirmar(cliente, caixa)["creditos"] == modulo.CREDITOS_INICIAIS


def test_indicador_tem_teto(cliente, caixa, monkeypatch):
    monkeypatch.setattr(crescimento, "TETO_INDICADOR", 150)
    cadastrar(cliente, "ana@exemplo.com")
    confirmar(cliente, caixa)
    codigo = cliente.get("/api/indicacao").json()["codigo"]
    for e in ("b@exemplo.com", "c@exemplo.com", "d@exemplo.com"):
        cliente.cookies.clear()
        cadastrar(cliente, e, codigo)
        confirmar(cliente, caixa)
    ana = modulo.db().execute("SELECT creditos FROM usuarios WHERE email='ana@exemplo.com'").fetchone()
    assert ana["creditos"] == modulo.CREDITOS_INICIAIS + 150  # 100 + 50, depois nada


def test_link_compartilhavel(cliente):
    cadastrar(cliente, "ana@exemplo.com")
    cid = cliente.post("/api/mensagens", json={"texto": "me conta uma piada"}).text
    cid = int(re.search(r'"id": (\d+)', cid).group(1))
    assert cliente.post("/api/conversas/999/compartilhar").status_code == 404
    url = cliente.post(f"/api/conversas/{cid}/compartilhar").json()["url"]
    assert cliente.post(f"/api/conversas/{cid}/compartilhar").json()["url"] == url  # o mesmo link sempre
    caminho = "/c/" + url.rsplit("/", 1)[1]
    cliente.cookies.clear()  # qualquer pessoa vê, sem login
    pagina = cliente.get(caminho)
    assert pagina.status_code == 200 and "me conta uma piada" in pagina.text and "Entendi:" in pagina.text
    assert "?ref=" in pagina.text and 'name="robots" content="noindex"' in pagina.text
    cliente.post("/api/entrar", json={"email": "ana@exemplo.com", "senha": "senha-forte-1"})
    assert cliente.delete(f"/api/conversas/{cid}/compartilhar").json() == {"ok": True}
    assert cliente.get(caminho).status_code == 404


def test_guias_robots_e_sitemap(cliente):
    guias = cliente.get("/api/guias").json()
    assert len(guias) >= 3 and all(g["titulo"] and g["descricao"] for g in guias)
    r = cliente.get(f"/guia/{guias[0]['slug']}")
    assert r.status_code == 200 and "Jarvis" in r.text
    assert cliente.get("/guia/../app").status_code in (404, 422)
    assert cliente.get("/guia/nao-existe").status_code == 404
    assert "Disallow: /api/" in cliente.get("/robots.txt").text
    sm = cliente.get("/sitemap.xml").text
    assert "/guia/" + guias[0]["slug"] in sm and "/termos" in sm
