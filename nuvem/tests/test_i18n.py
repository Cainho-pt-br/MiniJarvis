"""Testes de internacionalização (i18n): pt-BR, en e es."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import nuvem.acoes as acoes
import nuvem.app as app_mod
import nuvem.correio as correio
import nuvem.falhas as falhas
import nuvem.i18n as i18n
import nuvem.ia as ia_mod
import nuvem.jarvis as jarvis_mod
import nuvem.pagamento as pagamento
import nuvem.parque as parque
import nuvem.tarefas as tarefas


# Palavras tipicamente portuguesas que não podem vazar nas respostas em en/es.
_VAZAMENTOS_PT_EM_EN = (
    "não", "você", "senha", "incorretos", "créditos", "máquina", "inválido",
    "cadastre", "pagamento", "indisponível", "tente", "código", "expirado",
    "esgotaram", "confirme", "precisa", "conversa", "encontrada",
)

# Palavras exclusivamente portuguesas que não existem em espanhol.
_VAZAMENTOS_PT_EM_ES = (
    "não", "você", "senha", "incorretos", "indisponível",
    "esgotaram", "cadastre", "tentativas", "endereço", "cérebro",
)


def _sem_portugues(texto: str, idioma: str) -> None:
    baixo = texto.lower()
    proibidas = _VAZAMENTOS_PT_EM_EN if idioma == "en" else _VAZAMENTOS_PT_EM_ES
    for p in proibidas:
        assert not re.search(rf"\b{re.escape(p)}\b", baixo), (
            f"Vazamento de português ({p!r}) em {idioma}: {texto!r}"
        )


def test_todas_as_chaves_tem_traducao_nos_tres_idiomas():
    """Percorre todos os dicionários de tradução e falha se faltar qualquer idioma."""
    assert i18n.IDIOMAS == ("pt-BR", "en", "es")
    for chave, traducoes in i18n.TEXTOS.items():
        for idm in i18n.IDIOMAS:
            assert idm in traducoes, f"Chave {chave!r} sem tradução para {idm}"
            val = traducoes[idm]
            assert isinstance(val, str), f"Chave {chave!r} não é string em {idm}"
            if not (chave == "nota_moeda" and idm == "pt-BR"):
                assert val.strip(), f"Chave {chave!r} vazia em {idm}"
            vars_pt = set(re.findall(r"\{(\w+)\}", traducoes["pt-BR"]))
            vars_id = set(re.findall(r"\{(\w+)\}", val))
            assert vars_pt == vars_id, f"Placeholders divergentes em {chave!r} ({idm}): {vars_id} != {vars_pt}"

    for plano_id, traducoes in i18n.PLANOS_TEXTOS.items():
        for idm in i18n.IDIOMAS:
            assert idm in traducoes, f"Plano {plano_id!r} sem tradução para {idm}"
            bloco = traducoes[idm]
            for campo in ("nome", "descricao"):
                assert campo in bloco and bloco[campo].strip(), f"Plano {plano_id!r} ({idm}) sem campo {campo}"
            assert isinstance(bloco.get("itens"), list) and len(bloco["itens"]) >= 3

    for cod_acao in acoes.ACOES:
        assert cod_acao in acoes._TRADUCOES_ACOES, f"Ação {cod_acao!r} sem traduções"
        for idm in i18n.IDIOMAS:
            assert idm in acoes._TRADUCOES_ACOES[cod_acao], f"Ação {cod_acao!r} sem tradução em {idm}"
            titulo, desc = acoes._TRADUCOES_ACOES[cod_acao][idm]
            assert titulo.strip() and desc.strip()

    for idm in i18n.IDIOMAS:
        assert idm in tarefas.TAREFAS_POR_IDIOMA
        assert len(tarefas.TAREFAS_POR_IDIOMA[idm]) == len(tarefas.TAREFAS)
        assert idm in parque.NOTAS_INICIAIS_POR_IDIOMA
        assert len(parque.NOTAS_INICIAIS_POR_IDIOMA[idm]) == len(parque.NOTAS_INICIAIS)
        assert idm in falhas._PADRAO_FALHA
        for padrao, msgs in falhas._REGRAS:
            assert idm in msgs and msgs[idm].strip(), f"Regra de falha {padrao!r} sem {idm}"

    # Verifica também o dicionário I18N do front-end (index.html).
    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"const I18N = (\{.*?\n\});", html, re.S)
    assert m, "Bloco const I18N não encontrado em index.html"
    for idm in ("pt-BR", "en", "es"):
        assert f'"{idm}"' in m.group(1)


def test_ordem_de_prioridade_do_idioma(cliente):
    """Preferência salva (usuarios.idioma + localStorage/X-Idioma) > navigator.languages > Accept-Language > pt-BR."""
    c = cliente

    # 1. Sem cabeçalhos -> pt-BR
    r = c.get("/api/planos")
    assert r.json()[0]["nome"] == "Grátis"

    # 2. Accept-Language -> es
    r = c.get("/api/planos", headers={"Accept-Language": "es-MX,es;q=0.9,en;q=0.8"})
    assert r.json()[0]["nome"] == "Gratis"

    # 3. navigator.languages (X-Nav-Idiomas) ganha de Accept-Language
    r = c.get("/api/planos", headers={
        "Accept-Language": "es-MX,es;q=0.9",
        "X-Nav-Idiomas": "en-US,pt-BR",
    })
    assert r.json()[0]["nome"] == "Free"

    # 4. Preferência salva no localStorage (X-Idioma) ganha de navigator.languages e Accept-Language
    r = c.get("/api/planos", headers={
        "Accept-Language": "en-US",
        "X-Nav-Idiomas": "en-US",
        "X-Idioma": "es",
    })
    assert r.json()[0]["nome"] == "Gratis"

    # 5. Preferência salva em usuarios.idioma ganha de navigator.languages e Accept-Language
    r = c.post("/api/cadastro", json={
        "nome": "Alice", "email": "alice@teste.com", "senha": "senha-forte-123", "aceito": True,
    })
    assert r.status_code == 200
    assert r.json()["idioma"] is None

    # Enquanto usuarios.idioma é None, segue navigator.languages / Accept-Language
    r = c.get("/api/planos", headers={"X-Nav-Idiomas": "en-US", "Accept-Language": "pt-BR"})
    assert r.json()[0]["nome"] == "Free"

    # Salva idioma "es" na conta
    r = c.post("/api/eu/idioma", json={"idioma": "es"})
    assert r.status_code == 200
    assert r.json()["idioma"] == "es"

    # Agora mesmo que o navegador mande X-Nav-Idiomas: en-US e Accept-Language: pt-BR,
    # a preferência salva no banco ("es") vence.
    r = c.get("/api/planos", headers={"X-Nav-Idiomas": "en-US", "Accept-Language": "pt-BR"})
    assert r.json()[0]["nome"] == "Gratis"


@pytest.mark.parametrize("idioma", ["en", "es"])
def test_fluxos_principais_sem_vazar_portugues(cliente, monkeypatch, idioma):
    """Testa cadastro, login, erro de login, chat com erro da IA, pagamento recusado e e-mail de código em en e es."""
    hdrs = {"Accept-Language": idioma, "X-Nav-Idiomas": idioma}
    c = TestClient(app_mod.app, headers=hdrs)

    # 1. Erros de validação no cadastro
    r = c.post("/api/cadastro", json={"nome": "Jo", "email": "invalido", "senha": "12345678", "aceito": True})
    assert r.status_code == 400
    _sem_portugues(r.json()["detail"], idioma)

    r = c.post("/api/cadastro", json={"nome": "Jo", "email": "jo@teste.com", "senha": "12345678", "aceito": False})
    assert r.status_code == 400
    _sem_portugues(r.json()["detail"], idioma)

    # 2. Cadastro com confirmação de e-mail (captura o e-mail enviado)
    emails_enviados: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(correio, "configurado", lambda: True)
    monkeypatch.setattr(
        correio,
        "enviar",
        lambda para, assunto, texto, html=None: emails_enviados.append((para, assunto, texto, html or "")),
    )

    r = c.post("/api/cadastro", json={
        "nome": "John Doe", "email": f"john-{idioma}@example.com", "senha": "password-123", "aceito": True,
    })
    assert r.status_code == 200
    assert r.json()["idioma"] == idioma
    assert len(emails_enviados) == 1
    _, assunto_cod, texto_cod, html_cod = emails_enviados[-1]
    _sem_portugues(assunto_cod, idioma)
    _sem_portugues(texto_cod, idioma)
    _sem_portugues(html_cod, idioma)

    # Confirma o e-mail extraindo o código de 6 dígitos enviado
    m_cod = re.search(r"\b(\d{6})\b", texto_cod)
    assert m_cod
    r = c.post("/api/verificar", json={"codigo": m_cod.group(1)})
    assert r.status_code == 200
    assert r.json()["email_verificado"] is True

    # Notas iniciais do cérebro devem nascer no idioma da pessoa
    notas = c.get("/api/cerebro").json()["notas"]
    assert len(notas) >= 2
    for n in notas:
        _sem_portugues(n["nome"], idioma)
        det = c.get(f"/api/cerebro/{n['nome']}").json()
        _sem_portugues(det["conteudo"], idioma)

    # Ações prontas devem vir traduzidas
    lista_acoes = c.get("/api/acoes").json()
    for ac in lista_acoes:
        _sem_portugues(ac["titulo"], idioma)
        _sem_portugues(ac["descricao"], idioma)

    # 3. Erro de login (credenciais incorretas)
    r = c.post("/api/entrar", json={"email": f"john-{idioma}@example.com", "senha": "wrong-password"})
    assert r.status_code == 401
    _sem_portugues(r.json()["detail"], idioma)

    # Login correto (o cookie mj_disp já foi gravado no verificar_email)
    r = c.post("/api/entrar", json={"email": f"john-{idioma}@example.com", "senha": "password-123"})
    assert r.status_code == 200
    assert r.json()["email"] == f"john-{idioma}@example.com"

    # 4. Chat com erro da IA e conversa não encontrada
    r_404 = c.get("/api/conversas/999999")
    assert r_404.status_code == 404
    _sem_portugues(r_404.json()["detail"], idioma)

    class IAQuebrada:
        def responder(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("API down")

    monkeypatch.setattr(app_mod, "_ia", IAQuebrada())
    r_stream = c.post("/api/mensagens", json={"texto": "Hello Jarvis"})
    assert r_stream.status_code == 200
    assert "event: erro" in r_stream.text
    for ln in r_stream.text.splitlines():
        if ln.startswith("data: "):
            dado = json.loads(ln[6:])
            if isinstance(dado, str):
                _sem_portugues(dado, idioma)

    # 5. Pagamento recusado / indisponível
    monkeypatch.delenv("MP_FAKE", raising=False)
    monkeypatch.delenv("MP_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(pagamento, "_mp", None)
    r = c.post("/api/pagamentos/pix/pro")
    assert r.status_code == 503
    _sem_portugues(r.json()["detail"], idioma)

    class MPQuebrado:
        def post(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("mp refused")

    monkeypatch.setattr(pagamento, "_mp", MPQuebrado())
    r = c.post("/api/pagamentos/pix/pro")
    assert r.status_code == 502
    _sem_portugues(r.json()["detail"], idioma)

    # 6. E-mail de recibo no idioma do usuário
    emails_enviados.clear()
    correio.enviar_recibo(f"john-{idioma}@example.com", "John", "Pro", 59.90, 4500, idioma=idioma)
    assert len(emails_enviados) == 1
    _, assunto_rec, texto_rec, html_rec = emails_enviados[-1]
    _sem_portugues(assunto_rec, idioma)
    _sem_portugues(texto_rec, idioma)
    _sem_portugues(html_rec, idioma)

    # 7. Explicações de falha e instaladores no idioma
    exp = falhas.explicar_falha("falhou", "código 127", "bash: htop: command not found", idioma=idioma)
    assert exp
    _sem_portugues(exp, idioma)
    sh = "\n".join(l for l in c.get("/instalar.sh").text.splitlines() if not l.lstrip().startswith("#"))
    _sem_portugues(sh, idioma)
    ps1 = "\n".join(l for l in c.get("/instalar.ps1").text.splitlines() if not l.lstrip().startswith("#"))
    _sem_portugues(ps1, idioma)


def test_ia_prompt_cache_preservado_e_sugestoes_no_idioma():
    """O prompt base SISTEMA[0] (com cache_control) não muda entre idiomas;
    para en/es um bloco extra é anexado sem quebrar o cache."""
    base_pt = ia_mod.sistema_para_idioma("pt-BR")
    base_en = ia_mod.sistema_para_idioma("en")
    base_es = ia_mod.sistema_para_idioma("es")

    assert len(base_pt) == 1
    assert len(base_en) == 2
    assert len(base_es) == 2
    # O primeiro bloco (com cache_control ephemeral) é idêntico nos 3 idiomas
    assert base_pt[0] == base_en[0] == base_es[0]
    assert base_en[0]["cache_control"] == {"type": "ephemeral"}
    assert "English" in base_en[1]["text"]
    assert "español" in base_es[1]["text"]

    # Prompt de sugestões do parque inclui instrução de idioma
    assert "English" in jarvis_mod.prompt_sugestoes("nenhuma", idioma="en")
    assert "español" in jarvis_mod.prompt_sugestoes("nenhuma", idioma="es")
    assert "português" in jarvis_mod.prompt_sugestoes("nenhuma", idioma="pt-BR")
