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
    assert len(guias) == 4 and all(g["titulo"] and g["descricao"] for g in guias)
    r = cliente.get(f"/guia/{guias[0]['slug']}")
    assert r.status_code == 200 and "Jarvis" in r.text
    assert 'hreflang="pt-BR"' in r.text and 'hreflang="en"' in r.text and 'hreflang="es"' in r.text
    assert "<!--HREFLANG-->" not in r.text
    assert cliente.get("/guia/../app").status_code in (404, 422)
    assert cliente.get("/guia/nao-existe").status_code == 404
    assert "Disallow: /api/" in cliente.get("/robots.txt").text
    sm = cliente.get("/sitemap.xml").text
    assert "/guia/" + guias[0]["slug"] in sm and "/termos" in sm and "/privacidade" in sm
    for lang in ("pt-BR", "en", "es"):
        assert f"?lang={lang}" in sm
        assert f'hreflang="{lang}"' in sm


def test_paginas_legais_e_guias_em_tres_idiomas(cliente):
    # /termos e /privacidade em pt-BR, en e es (com aviso no topo de que o português prevalece)
    for rota in ("/termos", "/privacidade"):
        r_pt = cliente.get(f"{rota}?lang=pt-BR")
        assert r_pt.status_code == 200
        assert '<html lang="pt-BR">' in r_pt.text
        assert 'hreflang="en"' in r_pt.text and 'hreflang="es"' in r_pt.text and "<!--HREFLANG-->" not in r_pt.text

        r_en = cliente.get(rota, headers={"Accept-Language": "en-US,en;q=0.9"})
        assert r_en.status_code == 200
        assert '<html lang="en">' in r_en.text
        assert "Portuguese version" in r_en.text and "prevails" in r_en.text
        assert 'hreflang="pt-BR"' in r_en.text and 'hreflang="es"' in r_en.text

        r_es = cliente.get(f"/es{rota}")
        assert r_es.status_code == 200
        assert '<html lang="es">' in r_es.text
        assert "versión en portugués" in r_es.text and "prevalece" in r_es.text
        assert 'hreflang="pt-BR"' in r_es.text and 'hreflang="en"' in r_es.text

    # Os 4 guias em pt-BR, en e es com hreflang e listagem localizada em /api/guias
    guias_pt = {g["slug"]: g for g in cliente.get("/api/guias?lang=pt-BR").json()}
    guias_en = {g["slug"]: g for g in cliente.get("/api/guias?lang=en").json()}
    guias_es = {g["slug"]: g for g in cliente.get("/api/guias?lang=es").json()}
    assert len(guias_pt) == 4 and set(guias_pt) == set(guias_en) == set(guias_es)
    for slug in guias_pt:
        assert guias_pt[slug]["titulo"] != guias_en[slug]["titulo"]
        assert guias_pt[slug]["titulo"] != guias_es[slug]["titulo"]
        for lang in ("pt-BR", "en", "es"):
            rg = cliente.get(f"/guia/{slug}?lang={lang}")
            assert rg.status_code == 200
            assert f'<html lang="{lang}">' in rg.text
            assert 'rel="alternate" hreflang="pt-BR"' in rg.text
            assert 'rel="alternate" hreflang="en"' in rg.text
            assert 'rel="alternate" hreflang="es"' in rg.text
            assert 'rel="alternate" hreflang="x-default"' in rg.text
            assert "<!--HREFLANG-->" not in rg.text


def test_pagina_inicial_tem_tags_de_compartilhamento(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert '<link rel="canonical" href="http://testserver/">' in r.text
    assert 'hreflang="en"' in r.text and 'hreflang="es"' in r.text
    assert 'property="og:image" content="http://testserver/icones/icone-512.png"' in r.text
    assert "<!--SEO-->" not in r.text
    assert cliente.get("/icones/icone-512.png").status_code == 200  # a imagem existe
