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
    assert eu["plano"] == "basico" and eu["creditos"] == modulo.CREDITOS_INICIAIS + 800
    ate = datetime.fromisoformat(eu["plano_ate"])
    assert timedelta(days=30) < ate - datetime.now(timezone.utc) < timedelta(days=32)
    # a notificação chegando depois não soma de novo
    assert cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": mp_id}}).status_code == 200
    assert cliente.get("/api/eu").json()["creditos"] == modulo.CREDITOS_INICIAIS + 800
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


def test_webhook_e_painel_juntos_creditam_uma_vez_so(cliente, mp):
    import threading

    novo(cliente)
    cliente.post("/api/pagamentos/pix/basico")
    mp_id = next(iter(mp.pagamentos))
    mp.aprovar(mp_id)
    largada, erros = threading.Barrier(8), []

    def corrida():
        try:
            largada.wait()
            pagamento.processar_pagamento(mp_id)
        except Exception as exc:  # noqa: BLE001
            erros.append(exc)

    fios = [threading.Thread(target=corrida) for _ in range(8)]
    [f.start() for f in fios]
    [f.join() for f in fios]
    assert not erros
    assert cliente.get("/api/eu").json()["creditos"] == modulo.CREDITOS_INICIAIS + 800


def test_estorno_tira_o_plano_e_os_creditos(cliente, mp):
    novo(cliente)
    r = cliente.post("/api/pagamentos/pix/pro").json()
    mp_id = next(iter(mp.pagamentos))
    mp.aprovar(mp_id)
    cliente.get(f"/api/pagamentos/{r['id']}")
    assert cliente.get("/api/eu").json()["plano"] == "pro"
    mp.pagamentos[mp_id]["status"] = "refunded"
    cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": mp_id}})
    eu = cliente.get("/api/eu").json()
    assert eu["plano"] == "gratis" and eu["creditos"] == modulo.CREDITOS_INICIAIS
    cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": mp_id}})
    assert cliente.get("/api/eu").json()["creditos"] == modulo.CREDITOS_INICIAIS  # só uma vez


def test_apagar_conta_cancela_a_assinatura(cliente, mp):
    novo(cliente)
    cliente.post("/api/pagamentos/assinatura/pro")
    pre_id = next(iter(mp.preapprovals))
    cliente.post("/api/pagamentos/webhook", json={"type": "payment", "data": {"id": mp.cobrar_assinatura(pre_id)}})
    assert cliente.get("/api/eu").json()["assinatura"] is True
    assert cliente.post("/api/eu/apagar", json={"senha": "senha-forte-1"}).status_code == 200
    assert mp.preapprovals[pre_id]["status"] == "cancelled"
    assert cliente.get("/api/eu").status_code == 401


def test_rotina_diaria_para_quando_o_plano_vence(cliente, mp, monkeypatch):
    u = novo(cliente)
    modulo.db().execute("UPDATE usuarios SET plano='pro' WHERE id=?", (u["id"],))
    cod = cliente.post("/api/parque/parear").json()["codigo"]
    tok = cliente.post("/api/agente/parear", json={"codigo": cod, "hostname": "pc"}).json()["token"]
    cliente.post("/api/agentes/heartbeat", json={"hostname": "pc", "info": {"so": "Linux"}},
                 headers={"Authorization": f"Bearer {tok}"})
    assert cliente.post("/api/rotinas", json={"nome": "Disco", "maquina": "pc", "tipo": "acao",
                                              "texto": "espaco_disco", "frequencia": "diaria"}).status_code == 200
    monkeypatch.setenv("CRON_SECRET", "s3")
    cab = {"Authorization": "Bearer s3"}
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["disparadas"] == 1
    modulo.db().execute("UPDATE rotinas SET ultima_execucao='2020-01-01T00:00:00+00:00'")
    modulo.db().execute("UPDATE usuarios SET plano='gratis' WHERE id=?", (u["id"],))
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["disparadas"] == 0


def _pro_com_maquinas(cliente, n=3):
    from .test_parque import parear

    u = novo(cliente)
    modulo.db().execute("UPDATE usuarios SET plano='pro' WHERE id=?", (u["id"],))
    ags = [parear(cliente, f"pc-{i}", livre=False) for i in range(1, n + 1)]
    return u, ags


def test_quem_desce_de_plano_fica_so_com_as_maquinas_do_plano(cliente, mp):
    u, ags = _pro_com_maquinas(cliente, 3)
    assert not any(m["bloqueada"] for m in cliente.get("/api/parque").json()["maquinas"])
    # uma tarefa já na fila para a 3ª máquina, ainda no Pro
    jid = cliente.post("/api/jobs", json={"maquina": "pc-3", "tipo": "acao", "texto": "espaco_disco"}).json()["id"]
    modulo.db().execute("UPDATE usuarios SET plano='basico' WHERE id=?", (u["id"],))  # Básico: 2 máquinas
    ms = cliente.get("/api/parque").json()["maquinas"]
    assert [m["hostname"] for m in ms if m["bloqueada"]] == ["pc-3"]  # as mais antigas ficam
    # tarefa nova na bloqueada: recusada com aviso e convite ao upgrade (402)
    r = cliente.post("/api/jobs", json={"maquina": "pc-3", "tipo": "acao", "texto": "espaco_disco"})
    assert r.status_code == 402 and "bloqueada" in r.json()["detail"]
    assert cliente.post("/api/jobs", json={"maquina": "pc-2", "tipo": "acao", "texto": "espaco_disco"}).status_code == 200
    # a máquina bloqueada segue ligada, mas não recebe trabalho (nem o que já estava na fila)
    assert cliente.get("/api/trabalho?espera=0", headers=ags[2]).status_code == 204
    assert cliente.get("/api/trabalho?espera=0", headers=ags[1]).json()["id"] != jid
    # o Jarvis é avisado e não propõe tarefas nela
    assert "BLOQUEADA" in parque_resumo(u["id"])
    # desconectar uma libera a próxima da fila
    mid = next(m["id"] for m in ms if m["hostname"] == "pc-1")
    assert cliente.delete(f"/api/maquinas/{mid}").status_code == 200
    assert not any(m["bloqueada"] for m in cliente.get("/api/parque").json()["maquinas"])
    assert cliente.get("/api/trabalho?espera=0", headers=ags[2]).json()["id"] == jid
    # voltar ao Pro libera tudo
    modulo.db().execute("UPDATE usuarios SET plano='gratis' WHERE id=?", (u["id"],))
    assert [m["hostname"] for m in cliente.get("/api/parque").json()["maquinas"] if m["bloqueada"]] == ["pc-3"]


def parque_resumo(uid):
    from nuvem import parque

    return parque.resumo_do_parque(uid)


def test_sugestoes_do_pro_nao_gastam_creditos(cliente, mp):
    u = novo(cliente)
    r = cliente.post("/api/sugestoes/gerar").json()  # grátis: cobra
    assert r["creditos"] < modulo.CREDITOS_INICIAIS
    modulo.db().execute("UPDATE usuarios SET plano='pro', creditos=0 WHERE id=?", (u["id"],))
    modulo.db().execute("DELETE FROM sugestoes")
    r = cliente.post("/api/sugestoes/gerar")  # Pro sem nenhum crédito: funciona e não cobra
    assert r.status_code == 200 and r.json()["creditos"] == 0 and r.json()["sugestoes"]
    assert cliente.get("/api/eu").json()["creditos"] == 0


def test_cron_renova_as_sugestoes_do_pro_sem_cobrar(cliente, mp, monkeypatch):
    u, _ = _pro_com_maquinas(cliente, 1)
    modulo.db().execute("UPDATE usuarios SET creditos=50 WHERE id=?", (u["id"],))
    monkeypatch.setenv("CRON_SECRET", "s3")
    cab = {"Authorization": "Bearer s3"}
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["sugestoes"] == 1
    assert cliente.get("/api/sugestoes").json()["sugestoes"]
    assert cliente.get("/api/eu").json()["creditos"] == 50
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["sugestoes"] == 0  # já estão do dia
    # quem não é Pro não ganha sugestões automáticas
    modulo.db().execute("UPDATE usuarios SET plano='basico' WHERE id=?", (u["id"],))
    modulo.db().execute("DELETE FROM sugestoes")
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["sugestoes"] == 0
