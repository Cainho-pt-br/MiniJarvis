"""Pagamento pelo Mercado Pago: assinatura no cartão e Pix de um mês.

Dois jeitos de pagar, sem nada passar pela mão do dono:

- **Assinatura (cartão):** cria um *preapproval* e manda a pessoa ao checkout
  do Mercado Pago. Todo mês o MP cobra e avisa pelo webhook; cada cobrança
  aprovada renova o plano por 31 dias e soma os créditos.
- **Pix (um mês):** cria um pagamento Pix e mostra o QR code no painel. Quando
  o MP avisa que foi pago, o plano é ativado por 31 dias.

O webhook nunca confia no que vem na notificação: sempre busca o pagamento na
API do MP antes de dar créditos, e `pagamentos.referencia` (UNIQUE) impede
somar duas vezes.

Variáveis: MP_ACCESS_TOKEN (obrigatória), MP_WEBHOOK_SECRET (assinatura das
notificações, recomendada), SITE_URL (para onde o checkout volta).
MP_FAKE=1 usa um MP de mentira (testes).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import app as nucleo
from . import cobranca, operacao

log = logging.getLogger("minijarvis")
router = APIRouter()
MP_API = "https://api.mercadopago.com"


# ------------------------------------------------------------- cliente MP ---

class ClienteMP:
    def __init__(self, token: str) -> None:
        import httpx

        self._http = httpx.Client(base_url=MP_API, timeout=20,
                                  headers={"Authorization": f"Bearer {token}"})

    def get(self, caminho: str) -> dict[str, Any]:
        r = self._http.get(caminho)
        r.raise_for_status()
        return r.json()

    def post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        r = self._http.post(caminho, json=corpo, headers={"X-Idempotency-Key": str(uuid.uuid4())})
        if r.status_code >= 400:
            log.error("Mercado Pago %s respondeu %s: %s", caminho, r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json()

    def put(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        r = self._http.put(caminho, json=corpo)
        r.raise_for_status()
        return r.json()


class FakeMP:
    """Mercado Pago de mentira: guarda o que foi criado e deixa o teste
    "aprovar" um pagamento."""

    def __init__(self) -> None:
        self.pagamentos: dict[str, dict[str, Any]] = {}
        self.preapprovals: dict[str, dict[str, Any]] = {}
        self.autorizados: dict[str, dict[str, Any]] = {}

    def post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        pid = str(secrets.randbelow(10**9))
        if caminho == "/v1/payments":
            self.pagamentos[pid] = {**corpo, "id": pid, "status": "pending", "point_of_interaction": {
                "transaction_data": {"qr_code": "00020126pix-falso", "qr_code_base64": "QUJD",
                                     "ticket_url": "https://mp.exemplo/pix"}}}
            return self.pagamentos[pid]
        if caminho == "/preapproval":
            self.preapprovals[pid] = {**corpo, "id": pid, "status": "pending",
                                      "init_point": f"https://mp.exemplo/checkout/{pid}"}
            return self.preapprovals[pid]
        raise ValueError(caminho)

    def get(self, caminho: str) -> dict[str, Any]:
        for prefixo, tabela in (("/v1/payments/", self.pagamentos), ("/preapproval/", self.preapprovals),
                                ("/authorized_payments/", self.autorizados)):
            if caminho.startswith(prefixo):
                return tabela[caminho[len(prefixo):]]
        raise ValueError(caminho)

    def put(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        self.preapprovals[caminho.split("/")[-1]].update(corpo)
        return self.preapprovals[caminho.split("/")[-1]]

    # usados pelos testes
    def aprovar(self, pid: str) -> None:
        self.pagamentos[pid]["status"] = "approved"

    def cobrar_assinatura(self, preapproval_id: str, aprovado: bool = True) -> str:
        """Simula a cobrança mensal: um pagamento ligado ao preapproval."""
        pid = str(secrets.randbelow(10**9))
        self.pagamentos[pid] = {"id": pid, "status": "approved" if aprovado else "rejected",
                                "metadata": {"preapproval_id": preapproval_id},
                                "transaction_amount": self.preapprovals[preapproval_id]["auto_recurring"]["transaction_amount"]}
        self.autorizados[pid] = {"id": pid, "preapproval_id": preapproval_id, "payment": {"id": pid}}
        self.preapprovals[preapproval_id]["status"] = "authorized"
        return pid


_mp: Any = None


def mp() -> Any:
    from .i18n import t

    global _mp
    if _mp is None:
        if os.environ.get("MP_FAKE") == "1":
            _mp = FakeMP()
        elif os.environ.get("MP_ACCESS_TOKEN"):
            _mp = ClienteMP(os.environ["MP_ACCESS_TOKEN"])
    if _mp is None:
        raise HTTPException(503, t("pagamento_indisponivel"))
    return _mp


def site_url(request: Request) -> str:
    from .instaladores import _base

    return os.environ.get("SITE_URL") or _base(request)


def _ref(uid: int, plano: str) -> str:
    return f"mj:{uid}:{plano}"


def _ler_ref(ref: str | None) -> tuple[int, str] | None:
    try:
        marca, uid, plano = (ref or "").split(":")
        if marca == "mj":
            cobranca.plano_info(plano)
            return int(uid), plano
    except ValueError:
        pass
    return None


def _registrar(uid: int, referencia: str, tipo: str, plano: str, valor: float, status: str) -> int | None:
    try:
        with nucleo.transacao() as con:
            return con.inserir("INSERT INTO pagamentos (usuario_id, provedor, referencia, tipo, plano, valor_brl, "
                               "status, criado_em, atualizado_em) VALUES (?, 'mp', ?, ?, ?, ?, ?, ?, ?)",
                               (uid, referencia, tipo, plano, valor, status, nucleo.agora(), nucleo.agora()))
    except nucleo.ErroDuplicado:
        return None


# ------------------------------------------------------------- criar ---

@router.post("/api/pagamentos/assinatura/{plano}")
def assinar(plano: str, request: Request, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, str]:
    """Assinatura mensal no cartão: devolve a URL do checkout do Mercado Pago."""
    from .i18n import t

    try:
        p = cobranca.plano_info(plano)
    except ValueError:
        raise HTTPException(404, t("plano_nao_encontrado"))
    nucleo.limitar(f"pag:{u['id']}", 10, 3600)
    cliente_mp = mp()
    try:
        r = cliente_mp.post("/preapproval", {
            "reason": f"Jarvis {p['nome']}",
            "external_reference": _ref(u["id"], plano),
            "payer_email": u["email"],
            "back_url": f"{site_url(request)}/#planos",
            "auto_recurring": {"frequency": 1, "frequency_type": "months",
                               "transaction_amount": float(p["preco_brl"]), "currency_id": "BRL"},
            "status": "pending",
        })
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("não consegui criar assinatura (%s): %s", plano, type(exc).__name__)
        raise HTTPException(502, t("erro_criar_pagamento"))
    return {"url": r["init_point"]}


@router.post("/api/pagamentos/pix/{plano}")
def pagar_pix(plano: str, request: Request, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    """Um mês do plano pago com Pix: devolve o QR code e o copia-e-cola."""
    from .i18n import t

    try:
        p = cobranca.plano_info(plano)
    except ValueError:
        raise HTTPException(404, t("plano_nao_encontrado"))
    nucleo.limitar(f"pag:{u['id']}", 10, 3600)
    cliente_mp = mp()
    try:
        r = cliente_mp.post("/v1/payments", {
            "transaction_amount": float(p["preco_brl"]),
            "description": f"Jarvis {p['nome']} · 1 mês",
            "payment_method_id": "pix",
            "external_reference": _ref(u["id"], plano),
            "payer": {"email": u["email"], "first_name": u["nome"][:60]},
            "notification_url": f"{site_url(request)}/api/pagamentos/webhook",
        })
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("não consegui criar pix (%s): %s", plano, type(exc).__name__)
        raise HTTPException(502, t("erro_criar_pagamento"))
    pid = _registrar(u["id"], f"mp:{r['id']}", "pix", plano, float(p["preco_brl"]), "pendente")
    dados = (r.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {"id": pid, "valor": p["preco_brl"], "copia_cola": dados.get("qr_code", ""),
            "qr_base64": dados.get("qr_code_base64", ""), "link": dados.get("ticket_url", "")}


@router.get("/api/pagamentos/{pid}")
def ver_pagamento(pid: int, u: Any = Depends(nucleo.usuario_atual)) -> dict[str, Any]:
    from .i18n import t

    r = nucleo.db().execute("SELECT * FROM pagamentos WHERE id=? AND usuario_id=?", (pid, u["id"])).fetchone()
    if r is None:
        raise HTTPException(404, t("pagamento_nao_encontrado"))
    if r["status"] == "pendente" and r["referencia"].startswith("mp:"):
        try:  # o painel está esperando: confere direto no MP, sem depender do webhook
            processar_pagamento(r["referencia"][3:])
        except Exception as exc:  # noqa: BLE001
            log.warning("não consegui consultar o pagamento %s: %s", pid, type(exc).__name__)
        r = nucleo.db().execute("SELECT * FROM pagamentos WHERE id=?", (pid,)).fetchone()
    return {"id": r["id"], "status": r["status"], "plano": r["plano"], "valor": r["valor_brl"]}


def parar_cobranca(u: Any) -> None:
    """Cancela a assinatura no Mercado Pago. Estoura se o MP não confirmar, para
    ninguém achar que cancelou e continuar sendo cobrado."""
    from .i18n import t

    if not u["assinatura_id"]:
        return
    try:
        mp().put(f"/preapproval/{u['assinatura_id']}", {"status": "cancelled"})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("não consegui cancelar a assinatura %s: %s", u["assinatura_id"], type(exc).__name__)
        raise HTTPException(502, t("erro_cancelar_assinatura"))
    with nucleo.transacao() as con:
        con.execute("UPDATE usuarios SET assinatura_id=NULL WHERE id=?", (u["id"],))


@router.post("/api/pagamentos/cancelar")
def cancelar_assinatura(u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    """Para de cobrar; o plano vale até a data já paga."""
    from .i18n import t

    if not u["assinatura_id"]:
        raise HTTPException(400, t("sem_assinatura_ativa"))
    parar_cobranca(u)
    return {"ok": True}


# ------------------------------------------------------------- webhook ---

_STATUS = {"approved": "aprovado", "pending": "pendente", "in_process": "pendente", "authorized": "pendente",
           "rejected": "recusado", "cancelled": "cancelado", "refunded": "estornado", "charged_back": "estornado"}


def processar_pagamento(mp_id: str) -> str | None:
    """Busca o pagamento no MP e, se aprovado e ainda não creditado, ativa o plano."""
    dados = mp().get(f"/v1/payments/{mp_id}")
    status = _STATUS.get(str(dados.get("status")), "pendente")
    ref = _ler_ref(dados.get("external_reference"))
    preapproval_id = (dados.get("metadata") or {}).get("preapproval_id")
    if ref is None and preapproval_id:
        pre = mp().get(f"/preapproval/{preapproval_id}")
        ref = _ler_ref(pre.get("external_reference"))
    if ref is None:
        log.warning("pagamento %s sem referência conhecida", mp_id)
        return None
    uid, plano = ref
    referencia = f"mp:{mp_id}"
    valor = float(dados.get("transaction_amount") or 0)
    atual = nucleo.db().execute("SELECT * FROM pagamentos WHERE referencia=?", (referencia,)).fetchone()
    if atual is None:
        # Nasce pendente: só o passo abaixo marca "aprovado", junto com os créditos.
        _registrar(uid, referencia, "assinatura" if preapproval_id else "pix", plano, valor, "pendente")
        atual = nucleo.db().execute("SELECT * FROM pagamentos WHERE referencia=?", (referencia,)).fetchone()
    if atual["status"] == "aprovado":
        if status == "estornado":  # devolvido ou contestado depois de creditado
            with nucleo.transacao() as con:
                mudou = con.execute("UPDATE pagamentos SET status='estornado', atualizado_em=? WHERE id=? "
                                    "AND status='aprovado' RETURNING id", (nucleo.agora(), atual["id"])).fetchone()
            if mudou:
                cobranca.estornar_plano(uid, plano)
                operacao.alertar("Pagamento estornado", f"O pagamento {mp_id} (conta {uid}, plano {plano}) foi "
                                 "devolvido ou contestado. O plano voltou para o grátis e os créditos foram retirados.")
            return "estornado"
        return "aprovado"  # já creditado
    # A troca para "aprovado" é atômica: se o webhook e o painel chegarem juntos,
    # só quem conseguir mudar a linha dá os créditos.
    with nucleo.transacao() as con:
        mudou = con.execute("UPDATE pagamentos SET status=?, atualizado_em=? WHERE id=? AND status<>'aprovado' "
                            "RETURNING id", (status, nucleo.agora(), atual["id"])).fetchone()
    if status == "aprovado" and mudou:
        cobranca.ativar_plano(uid, plano, f"pagamento {plano} ({atual['tipo']})")
        operacao.primeira_vez("pagamento", uid)
        if preapproval_id:
            with nucleo.transacao() as con:
                con.execute("UPDATE usuarios SET assinatura_id=? WHERE id=?", (preapproval_id, uid))
    return status


def processar_assinatura(preapproval_id: str) -> None:
    pre = mp().get(f"/preapproval/{preapproval_id}")
    ref = _ler_ref(pre.get("external_reference"))
    if ref is None:
        return
    uid, _ = ref
    with nucleo.transacao() as con:
        if pre.get("status") == "authorized":
            con.execute("UPDATE usuarios SET assinatura_id=? WHERE id=?", (preapproval_id, uid))
        elif pre.get("status") in ("cancelled", "paused"):
            con.execute("UPDATE usuarios SET assinatura_id=NULL WHERE id=? AND assinatura_id=?",
                        (uid, preapproval_id))


def _assinatura_valida(request: Request, data_id: str) -> bool:
    """Confere o x-signature do MP (HMAC-SHA256). Sem segredo configurado, aceita:
    a notificação só diz "olhe o pagamento X"; o valor vem sempre da API."""
    segredo = os.environ.get("MP_WEBHOOK_SECRET")
    if not segredo:
        return True
    partes = dict(p.strip().split("=", 1) for p in request.headers.get("x-signature", "").split(",") if "=" in p)
    ts, v1 = partes.get("ts"), partes.get("v1")
    if not ts or not v1:
        return False
    did = data_id.lower() if data_id.isalnum() else data_id
    manifesto = f"id:{did};request-id:{request.headers.get('x-request-id', '')};ts:{ts};"
    esperado = hmac.new(segredo.encode(), manifesto.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


@router.post("/api/pagamentos/webhook")
async def webhook(request: Request) -> JSONResponse:
    from .i18n import t

    try:
        corpo = await request.json()
    except ValueError:
        corpo = {}
    q = request.query_params
    tipo = corpo.get("type") or corpo.get("topic") or q.get("type") or q.get("topic") or ""
    data_id = str((corpo.get("data") or {}).get("id") or q.get("data.id") or q.get("id") or "")
    if not data_id:
        return JSONResponse({"ok": True})
    if not _assinatura_valida(request, data_id):
        raise HTTPException(401, t("assinatura_invalida"))
    # Cada notificação vira uma consulta à API do MP: limita rajadas por origem.
    nucleo.limitar(f"webhook:{nucleo.ip_cliente(request)}", 120, 60)
    try:
        if tipo == "payment":
            processar_pagamento(data_id)
        elif tipo == "subscription_authorized_payment":
            aut = mp().get(f"/authorized_payments/{data_id}")
            pid = (aut.get("payment") or {}).get("id")
            if pid:
                processar_pagamento(str(pid))
        elif tipo == "subscription_preapproval":
            processar_assinatura(data_id)
    except Exception as exc:  # noqa: BLE001  (o MP reenvia se não for 200)
        log.exception("webhook do MP falhou (%s %s): %s", tipo, data_id, type(exc).__name__)
        raise HTTPException(500, t("erro_processar"))
    return JSONResponse({"ok": True})

