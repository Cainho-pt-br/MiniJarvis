"""Planos pagos: ativação, renovação e vencimento.

Quem paga (cartão recorrente ou Pix de um mês) recebe o plano por 31 dias e
os créditos do plano. Cada pagamento aprovado estende o prazo e soma créditos.
O cron diário rebaixa para o grátis quem venceu; os créditos que sobraram
continuam valendo.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import app as nucleo

DIAS_POR_MES = 31


def plano_info(plano: str) -> dict[str, Any]:
    p = next((p for p in nucleo.PLANOS if p["id"] == plano), None)
    if p is None or plano == "gratis":
        raise ValueError(f"plano inválido: {plano}")
    return p


def ativar_plano(uid: int, plano: str, motivo: str, meses: int = 1) -> str:
    """Dá `meses` do plano ao usuário (estendendo se já tem) e soma os créditos.
    Devolve a nova data de vencimento."""
    p = plano_info(plano)
    u = nucleo.db().execute("SELECT plano, plano_ate FROM usuarios WHERE id=?", (uid,)).fetchone()
    if u is None:
        raise ValueError("usuário não existe")
    agora = datetime.now(timezone.utc)
    base = agora
    if u["plano"] == plano and u["plano_ate"]:
        try:
            base = max(agora, datetime.fromisoformat(u["plano_ate"]))
        except ValueError:
            pass
    ate = (base + timedelta(days=DIAS_POR_MES * meses)).isoformat(timespec="seconds")
    creditos = p["creditos"] * meses
    with nucleo.transacao() as con:
        con.execute("UPDATE usuarios SET plano=?, plano_ate=?, creditos=creditos+? WHERE id=?",
                    (plano, ate, creditos, uid))
        con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                    (uid, creditos, motivo, nucleo.agora()))
    return ate


def rebaixar_vencidos() -> int:
    """Volta para o grátis quem está com o plano pago vencido. Chamado pelo cron."""
    agora = nucleo.agora()
    rows = nucleo.db().execute(
        "SELECT id FROM usuarios WHERE plano <> 'gratis' AND plano_ate IS NOT NULL AND plano_ate < ?",
        (agora,)).fetchall()
    for r in rows:
        with nucleo.transacao() as con:
            con.execute("UPDATE usuarios SET plano='gratis', plano_ate=NULL WHERE id=? AND plano_ate < ?",
                        (r["id"], agora))
            con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, 0, ?, ?)",
                        (r["id"], "plano venceu: voltou para o grátis", agora))
    return len(rows)
