"""Operação sem o dono por perto: teto de gasto, funil, relatório e alertas.

- **Teto diário de IA** (`TETO_DIARIO_USD`, padrão 10): somados os créditos
  gastos por todo mundo hoje, passou do teto, a IA para de responder até o
  dia virar e o dono recebe um e-mail. Um bug ou um abuso não vira fatura.
- **Funil** (`eventos`): contadores sem cookie nem serviço externo. Cada
  passo importante (cadastro, e-mail confirmado, primeira mensagem, máquina
  conectada, pagamento) vira uma linha; o Admin e o relatório mostram quantas
  pessoas chegaram a cada passo.
- **Relatório diário** por e-mail a `ADMIN_EMAILS`, disparado pelo cron.
- **Alerta imediato**: falhas repetidas da IA ou do e-mail em 10 minutos
  mandam um aviso (no máximo um por hora).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from . import app as nucleo

log = logging.getLogger("minijarvis")

TETO_DIARIO_USD = float(os.environ.get("TETO_DIARIO_USD", "10"))
PASSOS_FUNIL = ["cadastro", "email_confirmado", "primeira_mensagem", "maquina_conectada", "pagamento"]


def _desde(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(timespec="seconds")


def _hoje() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")


# ------------------------------------------------------------------ eventos ---

def registrar(nome: str, uid: int | None = None) -> None:
    """Grava um passo do funil (ou um incidente). Nunca derruba a requisição."""
    try:
        with nucleo.transacao() as con:
            con.execute("INSERT INTO eventos (nome, usuario_id, criado_em) VALUES (?, ?, ?)",
                        (nome, uid, nucleo.agora()))
    except Exception as exc:  # noqa: BLE001
        log.warning("não registrei o evento %s: %s", nome, type(exc).__name__)


def primeira_vez(nome: str, uid: int) -> None:
    """Registra só se este usuário ainda não passou por este passo."""
    r = nucleo.db().execute("SELECT 1 FROM eventos WHERE nome=? AND usuario_id=?", (nome, uid)).fetchone()
    if r is None:
        registrar(nome, uid)


def funil(horas: float | None = None) -> dict[str, int]:
    sql = "SELECT nome, COUNT(DISTINCT usuario_id) AS n FROM eventos WHERE nome IN ({})".format(
        ",".join("?" * len(PASSOS_FUNIL)))
    args: list[Any] = list(PASSOS_FUNIL)
    if horas is not None:
        sql += " AND criado_em > ?"
        args.append(_desde(horas))
    rows = nucleo.db().execute(sql + " GROUP BY nome", args).fetchall()
    cont = {r["nome"]: r["n"] for r in rows}
    return {p: cont.get(p, 0) for p in PASSOS_FUNIL}


def faxina() -> None:
    """Apaga incidentes e alertas com mais de 30 dias (o funil fica)."""
    with nucleo.transacao() as con:
        con.execute("DELETE FROM eventos WHERE (nome LIKE ? OR nome LIKE ? OR nome = 'teto_atingido') "
                    "AND criado_em < ?", ("falha:%", "alerta:%", _desde(24 * 30)))


# --------------------------------------------------------------------- teto ---

def gasto_hoje_usd() -> float:
    r = nucleo.db().execute("SELECT COALESCE(SUM(-delta), 0) AS g FROM extrato WHERE delta < 0 AND criado_em >= ?",
                            (_hoje(),)).fetchone()
    return float(r["g"]) / nucleo.CREDITOS_POR_DOLAR


def conferir_teto() -> None:
    """Estoura com 503 se a conta de IA de hoje passou do teto."""
    if TETO_DIARIO_USD <= 0:
        return
    gasto = gasto_hoje_usd()
    if gasto >= TETO_DIARIO_USD:
        if nucleo.db().execute("SELECT 1 FROM eventos WHERE nome='teto_atingido' AND criado_em >= ?",
                               (_hoje(),)).fetchone() is None:
            registrar("teto_atingido")
            alertar("Teto diário de IA atingido",
                    f"O Jarvis gastou US$ {gasto:.2f} em IA hoje (teto: US$ {TETO_DIARIO_USD:.2f}). "
                    "Ele parou de responder até o dia virar (UTC). Se for uso legítimo, aumente "
                    "TETO_DIARIO_USD na Vercel.")
        from fastapi import HTTPException

        raise HTTPException(503, "O Jarvis atingiu o limite de uso de hoje. Volte amanhã.")


# ------------------------------------------------------------------ alertas ---

def admins() -> list[str]:
    return [e.strip() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]


def alertar(assunto: str, texto: str) -> bool:
    from . import correio

    if not admins() or not correio.configurado():
        log.warning("ALERTA (sem e-mail configurado): %s: %s", assunto, texto)
        return False
    try:
        for a in admins():
            correio.enviar(a, f"[Jarvis] {assunto}", texto, f"<pre style='font:14px/1.5 monospace'>{texto}</pre>")
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("não consegui mandar o alerta: %s", type(exc).__name__)
        return False


def incidente(tipo: str, limite: int = 5, janela_min: int = 10) -> None:
    """Registra uma falha (ia, email, webhook...). `limite` falhas em
    `janela_min` minutos disparam um alerta, no máximo um por hora por tipo."""
    registrar(f"falha:{tipo}")
    n = nucleo.db().execute("SELECT COUNT(*) AS n FROM eventos WHERE nome=? AND criado_em > ?",
                            (f"falha:{tipo}", _desde(janela_min / 60))).fetchone()["n"]
    if n < limite:
        return
    if nucleo.db().execute("SELECT 1 FROM eventos WHERE nome=? AND criado_em > ?",
                           (f"alerta:{tipo}", _desde(1))).fetchone():
        return
    registrar(f"alerta:{tipo}")
    alertar(f"{n} falhas de {tipo} em {janela_min} minutos",
            f"O Jarvis registrou {n} falhas de '{tipo}' nos últimos {janela_min} minutos. "
            "Veja os logs na Vercel (Deployments > Logs).")


# ---------------------------------------------------------------- relatório ---

def relatorio(horas: float = 24) -> dict[str, Any]:
    db = nucleo.db()
    desde = _desde(horas)

    def n(sql: str, *args: Any) -> int:
        return int(db.execute(sql, args).fetchone()["n"])

    creditos = n("SELECT COALESCE(SUM(-delta),0) AS n FROM extrato WHERE delta < 0 AND criado_em > ?", desde)
    receita = db.execute("SELECT COALESCE(SUM(valor_brl),0) AS v, COUNT(*) AS n FROM pagamentos "
                         "WHERE status='aprovado' AND atualizado_em > ?", (desde,)).fetchone()
    return {
        "periodo_h": horas,
        "contas_total": n("SELECT COUNT(*) AS n FROM usuarios"),
        "contas_novas": n("SELECT COUNT(*) AS n FROM usuarios WHERE criado_em > ?", desde),
        "pagantes": n("SELECT COUNT(*) AS n FROM usuarios WHERE plano <> 'gratis'"),
        "mensagens": n("SELECT COUNT(*) AS n FROM mensagens WHERE papel='user' AND oculta=0 AND criado_em > ?", desde),
        "usuarios_ativos": n("SELECT COUNT(DISTINCT c.usuario_id) AS n FROM mensagens m JOIN conversas c "
                             "ON c.id=m.conversa_id WHERE m.papel='user' AND m.criado_em > ?", desde),
        "creditos_gastos": creditos,
        "custo_ia_usd": round(creditos / nucleo.CREDITOS_POR_DOLAR, 2),
        "custo_hoje_usd": round(gasto_hoje_usd(), 2),
        "teto_usd": TETO_DIARIO_USD,
        "maquinas_total": n("SELECT COUNT(*) AS n FROM agentes"),
        "maquinas_novas": n("SELECT COUNT(*) AS n FROM agentes WHERE criado_em > ?", desde),
        "tarefas_concluidas": n("SELECT COUNT(*) AS n FROM jobs WHERE status='concluido' AND criado_em > ?", desde),
        "tarefas_falhas": n("SELECT COUNT(*) AS n FROM jobs WHERE status='falhou' AND criado_em > ?", desde),
        "pagamentos": int(receita["n"]), "receita_brl": round(float(receita["v"]), 2),
        "lista_espera": n("SELECT COUNT(*) AS n FROM interesses WHERE criado_em > ?", desde),
        "falhas_ia": n("SELECT COUNT(*) AS n FROM eventos WHERE nome='falha:ia' AND criado_em > ?", desde),
        "falhas_email": n("SELECT COUNT(*) AS n FROM eventos WHERE nome='falha:email' AND criado_em > ?", desde),
        "funil_periodo": funil(horas), "funil_total": funil(),
    }


def texto_relatorio(r: dict[str, Any]) -> str:
    f, ft = r["funil_periodo"], r["funil_total"]
    linhas = [
        f"Jarvis · últimas {int(r['periodo_h'])} h",
        "",
        f"Contas: {r['contas_total']} (+{r['contas_novas']})  ·  pagantes: {r['pagantes']}",
        f"Pessoas que conversaram: {r['usuarios_ativos']}  ·  mensagens: {r['mensagens']}",
        f"IA: {r['creditos_gastos']} créditos = US$ {r['custo_ia_usd']:.2f}  "
        f"(hoje US$ {r['custo_hoje_usd']:.2f} de US$ {r['teto_usd']:.2f})",
        f"Máquinas: {r['maquinas_total']} (+{r['maquinas_novas']})  ·  tarefas: {r['tarefas_concluidas']} ok, "
        f"{r['tarefas_falhas']} falhas",
        f"Pagamentos: {r['pagamentos']} = R$ {r['receita_brl']:.2f}  ·  lista de espera: {r['lista_espera']}",
        f"Falhas: IA {r['falhas_ia']}, e-mail {r['falhas_email']}",
        "",
        "Funil (período / total):",
    ]
    for p in PASSOS_FUNIL:
        linhas.append(f"  {p:<20} {f[p]:>5} / {ft[p]}")
    return "\n".join(linhas)


def mandar_relatorio() -> bool:
    r = relatorio()
    return alertar("Relatório diário", texto_relatorio(r))
