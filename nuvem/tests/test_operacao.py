"""Teto de gasto, funil, alertas e relatório diário."""
import os

import pytest

os.environ["IA_FAKE"] = "1"
os.environ["COOKIE_SEGURO"] = "0"

from nuvem import app as modulo  # noqa: E402
from nuvem import correio, operacao  # noqa: E402


@pytest.fixture()
def caixa(monkeypatch):
    msgs = []
    monkeypatch.setattr(correio, "CAIXA_DE_TESTE", msgs)
    monkeypatch.setenv("ADMIN_EMAILS", "dono@exemplo.com")
    return msgs


def novo(c, email="ana@exemplo.com"):
    """Cria a conta e confirma o e-mail com o código que chegou na caixa de teste."""
    import re

    c.post("/api/cadastro", json={"nome": "Ana", "email": email, "senha": "senha-forte-1", "aceito": True})
    caixa = correio.CAIXA_DE_TESTE
    cod = re.search(r"\b(\d{6})\b", caixa[-1]["Subject"]).group(1)
    caixa.clear()
    return c.post("/api/verificar", json={"codigo": cod}).json()


def test_teto_diario_para_a_ia_e_avisa_o_dono(cliente, caixa, monkeypatch):
    novo(cliente)
    assert cliente.post("/api/mensagens", json={"texto": "oi"}).status_code == 200
    monkeypatch.setattr(operacao, "TETO_DIARIO_USD", 0.0005)  # já gastamos 1 crédito = US$ 0,001
    r = cliente.post("/api/mensagens", json={"texto": "oi de novo"})
    assert r.status_code == 503 and "limite de uso de hoje" in r.json()["detail"]
    assert cliente.post("/api/sugestoes/gerar").status_code == 503
    assert len(caixa) == 1 and "Teto" in caixa[0]["Subject"] and caixa[0]["To"] == "dono@exemplo.com"
    cliente.post("/api/mensagens", json={"texto": "e agora?"})
    assert len(caixa) == 1  # um aviso por dia
    monkeypatch.setattr(operacao, "TETO_DIARIO_USD", 0)  # 0 desliga o teto
    assert cliente.post("/api/mensagens", json={"texto": "voltei"}).status_code == 200


def test_falhas_repetidas_da_ia_mandam_alerta(cliente, caixa, monkeypatch):
    class IAQuebrada:
        def responder(self, *a, **k):
            raise RuntimeError("api fora")
            yield  # noqa: pragma

    monkeypatch.setattr(modulo, "_ia", IAQuebrada())
    novo(cliente)
    for _ in range(5):
        cliente.post("/api/mensagens", json={"texto": "oi"})
    assert len(caixa) == 1 and "falhas de ia" in caixa[0]["Subject"]
    cliente.post("/api/mensagens", json={"texto": "oi"})
    assert len(caixa) == 1  # no máximo um alerta por hora


def test_funil_e_relatorio(cliente, caixa, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "dono@exemplo.com,ana@exemplo.com")
    novo(cliente)
    cliente.post("/api/mensagens", json={"texto": "oi"})
    cliente.post("/api/mensagens", json={"texto": "oi"})
    cod = cliente.post("/api/parque/parear").json()["codigo"]
    cliente.post("/api/agente/parear", json={"codigo": cod, "hostname": "pc"})
    f = operacao.funil()
    assert f == {"cadastro": 1, "email_confirmado": 1, "primeira_mensagem": 1, "maquina_conectada": 1, "pagamento": 0}
    r = cliente.get("/api/admin/resumo").json()["relatorio"]
    assert r["contas_novas"] == 1 and r["mensagens"] == 2 and r["usuarios_ativos"] == 1
    assert r["maquinas_novas"] == 1 and r["creditos_gastos"] >= 2 and r["funil_total"] == f
    assert cliente.post("/api/admin/relatorio").json()["enviado"] is True
    assert len(caixa) == 2 and "Relatório diário" in caixa[0]["Subject"]
    assert "primeira_mensagem" in caixa[0].get_body(("plain",)).get_content()
    # o cron manda o relatório também
    monkeypatch.setenv("CRON_SECRET", "s3")
    assert cliente.get("/api/cron/rotinas", headers={"Authorization": "Bearer s3"}).status_code == 200
    assert len(caixa) == 4


def test_heartbeat_informa_versao_minima(cliente, caixa):
    from nuvem import parque

    novo(cliente)
    cod = cliente.post("/api/parque/parear").json()["codigo"]
    tok = cliente.post("/api/agente/parear", json={"codigo": cod, "hostname": "pc"}).json()["token"]
    r = cliente.post("/api/agentes/heartbeat", json={"hostname": "pc"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.json()["versao_minima"] == parque.versao_do_agente() and r.json()["versao_minima"].count(".") == 2
