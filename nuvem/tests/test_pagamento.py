"""Pagamento (Mercado Pago de mentira), renovação e vencimento do plano."""
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ["IA_FAKE"] = "1"
os.environ["COOKIE_SEGURO"] = "0"

from nuvem import app as modulo  # noqa: E402
from nuvem import pagamento  # noqa: E402


@pytest.fixture()
def mp(monkeypatch):
    monkeypatch.setenv("MP_FAKE", "1")
    monkeypatch.setattr(pagamento, "_mp", None)
    return pagamento.mp()


def novo(c, email="ana@exemplo.com"):
    return c.post("/api/cadastro", json={"nome": "Ana", "email": email, "senha": "senha-forte-1", "aceito": True}).json()


def test_sem_mercado_pago_configurado(cliente, monkeypatch):
    monkeypatch.delenv("MP_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(pagamento, "_mp", None)
    novo(cliente)
    assert cliente.post("/api/pagamentos/pix/pro").status_code == 503


def test_pix_ativa_o_plano_por_31_dias(cliente, mp):
    u = novo(cliente)
    assert cliente.post("/api/pagamentos/pix/gratis").status_code == 404
    r = cliente.post("/api/pagamentos/pix/basico").json()
    assert r["valor"] == 29.90 and r["copia_cola"] and r["qr_base64"]
    assert cliente.get(f"/api/pagamentos/{r['id']}").json()["status"] == "pendente"
    mp_id = next(iter(mp.pagamentos))
    mp.aprovar(mp_id)
    # o painel consulta e já vê aprovado, mesmo sem webhook
    assert cliente.get(f"/api/pagamentos/{r['id']}").json()["status"] == "aprovado"
    eu = cliente.get("/api/eu").json()
    assert eu["plano"] == "basico" and eu["creditos"] == modulo.CREDITOS_INICIAIS + 2000
    ate = datetime.fromisoformat(eu["plano_ate"])
    assert timedelta(days=30) < ate - datetime.now(timezone.utc) < timedelta(days=32)
    # a notificação chegando depois não soma de novo
    assert cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": mp_id}}).status_code == 200
    assert cliente.get("/api/eu").json()["creditos"] == modulo.CREDITOS_INICIAIS + 2000
    assert u["creditos"] == modulo.CREDITOS_INICIAIS


def test_assinatura_no_cartao_renova_todo_mes(cliente, mp):
    novo(cliente)
    r = cliente.post("/api/pagamentos/assinatura/pro").json()
    assert r["url"].startswith("https://mp.exemplo/checkout/")
    pre_id = next(iter(mp.preapprovals))
    assert mp.preapprovals[pre_id]["auto_recurring"]["transaction_amount"] == 59.90
    # primeira cobrança
    pid = mp.cobrar_assinatura(pre_id)
    cab = {"x-signature": "ts=1,v1=x", "x-request-id": "r1"}
    assert cliente.post("/api/pagamentos/webhook", json={"type": "subscription_authorized_payment",
                                                         "data": {"id": pid}}, headers=cab).status_code == 200
    eu = cliente.get("/api/eu").json()
    assert eu["plano"] == "pro" and eu["assinatura"] is True and eu["creditos"] == modulo.CREDITOS_INICIAIS + 4500
    ate1 = eu["plano_ate"]
    # segunda cobrança (mês seguinte): estende o prazo e soma de novo
    pid2 = mp.cobrar_assinatura(pre_id)
    cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": pid2}})
    eu = cliente.get("/api/eu").json()
    assert eu["creditos"] == modulo.CREDITOS_INICIAIS + 9000 and eu["plano_ate"] > ate1
    # cobrança recusada não muda nada
    pid3 = mp.cobrar_assinatura(pre_id, aprovado=False)
    cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": pid3}})
    assert cliente.get("/api/eu").json()["creditos"] == modulo.CREDITOS_INICIAIS + 9000
    # cancelar: para de cobrar, mas o plano segue até o vencimento
    assert cliente.post("/api/pagamentos/cancelar").status_code == 200
    assert mp.preapprovals[pre_id]["status"] == "cancelled"
    eu = cliente.get("/api/eu").json()
    assert eu["assinatura"] is False and eu["plano"] == "pro"
    assert cliente.post("/api/pagamentos/cancelar").status_code == 400


def test_webhook_com_assinatura_invalida(cliente, mp, monkeypatch):
    monkeypatch.setenv("MP_WEBHOOK_SECRET", "segredo")
    r = cliente.post("/api/pagamentos/webhook?data.id=123", json={"type": "payment", "data": {"id": "123"}},
                     headers={"x-signature": "ts=1,v1=errada", "x-request-id": "r"})
    assert r.status_code == 401
    # sem id, só responde ok (o MP manda pings assim)
    assert cliente.post("/api/pagamentos/webhook", json={}).status_code == 200


def test_cron_rebaixa_plano_vencido(cliente, mp, monkeypatch):
    u = novo(cliente)
    r = cliente.post("/api/pagamentos/pix/pro").json()
    mp.aprovar(next(iter(mp.pagamentos)))
    cliente.get(f"/api/pagamentos/{r['id']}")
    monkeypatch.setenv("CRON_SECRET", "s3")
    cab = {"Authorization": "Bearer s3"}
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["rebaixados"] == 0
    modulo.db().execute("UPDATE usuarios SET plano_ate='2020-01-01T00:00:00+00:00' WHERE id=?", (u["id"],))
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["rebaixados"] == 1
    eu = cliente.get("/api/eu").json()
    assert eu["plano"] == "gratis" and eu["plano_ate"] is None
    assert eu["creditos"] == modulo.CREDITOS_INICIAIS + 4500  # o que sobrou continua valendo
