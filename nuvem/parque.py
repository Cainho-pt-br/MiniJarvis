"""Parque de máquinas de cada conta: agentes, tarefas (jobs), cérebro e rotinas.

Fala o mesmo protocolo do broker original (`broker/app.py`), para o
`agente/agente.py` funcionar igual, com uma diferença: cada agente se
identifica com um token próprio (`Authorization: Bearer ...`), obtido pelo
pareamento por código. Assim cada conta só enxerga e comanda as próprias
máquinas.

Tudo que o broker original guardava em memória (progresso, log, quem está
online) aqui fica no banco, porque na Vercel cada requisição pode cair numa
instância diferente.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import acoes
from . import app as nucleo

router = APIRouter()

ONLINE_S = 150            # sem sinal por mais que isso, a máquina aparece desligada
ORFAO_S = 180             # job executando sem notícia por mais que isso volta para a fila
HEARTBEAT_S = int(os.environ.get("PARQUE_HEARTBEAT_S", "60"))
ESPERA_MAX_S = float(os.environ.get("PARQUE_ESPERA_S", "50"))
CONSULTA_S = 2.5          # de quanto em quanto tempo a espera longa olha a fila
LOG_MAX = 64 * 1024
TIPOS = {"shell", "claude", "gemini", "codex"}
MAQUINAS_GRATIS = 1
_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O e 1/I, para ditar sem erro


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _dt(carimbo: str | None) -> datetime | None:
    if not carimbo:
        return None
    try:
        return datetime.fromisoformat(carimbo)
    except ValueError:
        return None


def _idade(carimbo: str | None) -> float:
    d = _dt(carimbo)
    return (datetime.now(timezone.utc) - d).total_seconds() if d else 1e9


def _tags(csv: str | None) -> set[str]:
    return {t.strip() for t in (csv or "").split(",") if t.strip()}


def _json(texto: str | None, padrao: Any = None) -> Any:
    try:
        return json.loads(texto) if texto else padrao
    except ValueError:
        return padrao


# ------------------------------------------------------------ autenticação ---

def agente_atual(request: Request) -> Any:
    cab = request.headers.get("authorization", "")
    if not cab.lower().startswith("bearer "):
        raise HTTPException(401, "Agente sem token. Pareie esta máquina pelo painel.")
    row = nucleo.db().execute("SELECT * FROM agentes WHERE token_hash = ?",
                              (_hash(cab[7:].strip()),)).fetchone()
    if row is None:
        raise HTTPException(401, "Token de agente inválido. Pareie esta máquina de novo.")
    return row


def _ver_job(jid: int, uid: int) -> Any:
    j = nucleo.db().execute("SELECT * FROM jobs WHERE id = ? AND usuario_id = ?", (jid, uid)).fetchone()
    if j is None:
        raise HTTPException(404, "Tarefa não encontrada.")
    return j


# ----------------------------------------------------------------- zelador ---

def zelar(uid: int) -> None:
    """Devolve à fila o job cuja máquina sumiu no meio da execução."""
    rows = nucleo.db().execute(
        "SELECT id, tentativas, max_tentativas, visto_em, iniciado_em, cancelar FROM jobs "
        "WHERE usuario_id = ? AND status = 'executando'", (uid,)).fetchall()
    for r in rows:
        if _idade(r["visto_em"] or r["iniciado_em"]) < ORFAO_S:
            continue
        with nucleo.transacao() as con:
            if r["cancelar"]:
                con.execute("UPDATE jobs SET status='cancelado', terminado_em=? WHERE id=? AND status='executando'",
                            (nucleo.agora(), r["id"]))
            elif r["tentativas"] < r["max_tentativas"]:
                con.execute("UPDATE jobs SET status='pendente', agente=NULL, "
                            "mensagem='a máquina parou de responder; voltou para a fila' "
                            "WHERE id=? AND status='executando'", (r["id"],))
            else:
                con.execute("UPDATE jobs SET status='falhou', terminado_em=?, "
                            "mensagem='a máquina parou de responder' WHERE id=? AND status='executando'",
                            (nucleo.agora(), r["id"]))


# --------------------------------------------------------------- pareamento ---

def limite_maquinas(plano: str) -> int | None:
    return next((p["maquinas"] for p in nucleo.PLANOS if p["id"] == plano), MAQUINAS_GRATIS)


@router.post("/api/parque/parear")
def gerar_codigo(request: Request, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    limite = limite_maquinas(u["plano"])
    if limite is not None:
        n = nucleo.db().execute("SELECT COUNT(*) AS n FROM agentes WHERE usuario_id = ?", (u["id"],)).fetchone()["n"]
        if n >= limite:
            raise HTTPException(402, f"Seu plano conecta até {limite} máquina{'s' if limite > 1 else ''}. "
                                     "Assine o Pro para montar seu parque inteiro.")
    codigo = "".join(secrets.choice(_ALFABETO) for _ in range(6))
    expira = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(timespec="seconds")
    with nucleo.transacao() as con:
        con.execute("DELETE FROM pareamentos WHERE usuario_id = ? OR expira_em < ?", (u["id"], nucleo.agora()))
        con.execute("INSERT INTO pareamentos VALUES (?, ?, ?)", (codigo, u["id"], expira))
    from .instaladores import _base, comandos

    return {"codigo": codigo, "expira_em": expira, "comandos": comandos(_base(request), codigo)}


class Parear(BaseModel):
    codigo: str = Field(max_length=12)
    hostname: str = Field(min_length=1, max_length=80)


@router.post("/api/agente/parear")
def parear(p: Parear, request: Request) -> dict[str, Any]:
    nucleo.limitar(f"parear:{nucleo.ip_cliente(request)}", 20, 300)
    codigo = p.codigo.strip().upper().replace("-", "")
    hostname = re.sub(r"[^\w.\-]", "-", p.hostname.strip())[:80]
    token = "mjag_" + secrets.token_urlsafe(32)
    with nucleo.transacao() as con:
        par = con.execute("SELECT * FROM pareamentos WHERE codigo = ? AND expira_em > ?",
                          (codigo, nucleo.agora())).fetchone()
        if par is None:
            raise HTTPException(400, "Código inválido ou expirado. Gere outro no painel.")
        con.execute("DELETE FROM pareamentos WHERE codigo = ?", (codigo,))
        existe = con.execute("SELECT id FROM agentes WHERE usuario_id = ? AND hostname = ?",
                             (par["usuario_id"], hostname)).fetchone()
        if existe:  # mesma máquina pareando de novo: troca o token, mantém o histórico
            con.execute("UPDATE agentes SET token_hash = ?, visto_em = ? WHERE id = ?",
                        (_hash(token), nucleo.agora(), existe["id"]))
        else:
            con.execute("INSERT INTO agentes (usuario_id, hostname, tags, token_hash, visto_em, criado_em) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (par["usuario_id"], hostname, hostname, _hash(token), nucleo.agora(), nucleo.agora()))
    return {"token": token, "hostname": hostname}


# ---------------------------------------------------- protocolo do agente ---

class Heartbeat(BaseModel):
    hostname: str = ""
    tags: str = ""
    cpu_pct: float | None = None
    ram_pct: float | None = None
    gpu_pct: float | None = None
    vram_livre_mb: int | None = None
    disco_livre_gb: float | None = None
    discos: list[dict[str, Any]] | None = None
    job_atual: int | None = None
    versao_agente: str = "?"
    info: dict[str, Any] | None = None
    cerebro: dict[str, Any] | None = None


@router.post("/api/agentes/heartbeat")
def heartbeat(h: Heartbeat, a: Any = Depends(agente_atual)) -> dict[str, Any]:
    metricas = {k: getattr(h, k) for k in ("cpu_pct", "ram_pct", "gpu_pct", "vram_livre_mb",
                                           "disco_livre_gb", "discos", "cerebro")}
    tags = ",".join(sorted(_tags(h.tags) | {a["hostname"]}))
    with nucleo.transacao() as con:
        con.execute("UPDATE agentes SET tags=?, metricas=?, versao=?, job_atual=?, visto_em=?, "
                    "info=COALESCE(?, info) WHERE id=?",
                    (tags, json.dumps(metricas), h.versao_agente[:40], h.job_atual, nucleo.agora(),
                     json.dumps(h.info) if h.info else None, a["id"]))
    cancelar = False
    if h.job_atual:
        j = nucleo.db().execute("SELECT cancelar FROM jobs WHERE id=? AND usuario_id=?",
                                (h.job_atual, a["usuario_id"])).fetchone()
        cancelar = bool(j and j["cancelar"])
    return {"ok": True, "cancelar": cancelar, "heartbeat_s": HEARTBEAT_S, "espera_s": ESPERA_MAX_S}


def _reivindicar(a: Any, oferecidas: set[str]) -> dict[str, Any] | None:
    candidatos = nucleo.db().execute(
        "SELECT id, tags FROM jobs WHERE usuario_id = ? AND status = 'pendente' ORDER BY id LIMIT 50",
        (a["usuario_id"],)).fetchall()
    for c in candidatos:
        if not _tags(c["tags"]) <= oferecidas:
            continue
        with nucleo.transacao() as con:
            row = con.execute(
                "UPDATE jobs SET status='executando', agente=?, iniciado_em=?, visto_em=?, "
                "tentativas=tentativas+1, progresso=0, mensagem='iniciando' "
                "WHERE id=? AND status='pendente' RETURNING id, tipo, payload, tentativas, max_tentativas",
                (a["hostname"], nucleo.agora(), nucleo.agora(), c["id"])).fetchone()
            if row is None:
                continue  # outra máquina pegou primeiro
            con.execute("UPDATE agentes SET job_atual=? WHERE id=?", (row["id"], a["id"]))
        return {"id": row["id"], "tipo": row["tipo"], "payload": _json(row["payload"], {}),
                "tentativa": row["tentativas"], "max_tentativas": row["max_tentativas"]}
    return None


@router.get("/api/trabalho")
def pegar_trabalho(agente: str = "", tags: str = "", espera: float = 0,
                   a: Any = Depends(agente_atual)) -> Any:
    """Espera longa: segura o pedido até aparecer trabalho (ou o tempo acabar)."""
    with nucleo.transacao() as con:
        con.execute("UPDATE agentes SET visto_em=? WHERE id=?", (nucleo.agora(), a["id"]))
    oferecidas = _tags(tags) | _tags(a["tags"]) | {a["hostname"]}
    zelar(a["usuario_id"])
    fim = time.monotonic() + max(0.0, min(espera, ESPERA_MAX_S))
    while True:
        job = _reivindicar(a, oferecidas)
        if job is not None:
            return job
        resta = fim - time.monotonic()
        if resta <= 0:
            return Response(status_code=204)
        time.sleep(min(CONSULTA_S, resta))


def _job_do_agente(jid: int, a: Any) -> Any:
    j = nucleo.db().execute("SELECT * FROM jobs WHERE id=? AND usuario_id=?", (jid, a["usuario_id"])).fetchone()
    if j is None:
        raise HTTPException(404, "job não encontrado")
    return j


class Progresso(BaseModel):
    agente: str = ""
    progresso: int | None = None
    mensagem: str | None = None
    log: str = ""


@router.post("/api/trabalho/{jid}/progresso")
def progresso(jid: int, p: Progresso, a: Any = Depends(agente_atual)) -> dict[str, Any]:
    j = _job_do_agente(jid, a)
    novo_log = (j["log"] + p.log)[-LOG_MAX:] if p.log else j["log"]
    with nucleo.transacao() as con:
        con.execute("UPDATE jobs SET log=?, progresso=COALESCE(?, progresso), mensagem=COALESCE(?, mensagem), "
                    "visto_em=? WHERE id=?",
                    (novo_log, None if p.progresso is None else max(0, min(100, p.progresso)),
                     p.mensagem, nucleo.agora(), jid))
        con.execute("UPDATE agentes SET visto_em=? WHERE id=?", (nucleo.agora(), a["id"]))
    return {"ok": True, "cancelar": bool(j["cancelar"])}


class Conclusao(BaseModel):
    agente: str = ""
    resultado: dict[str, Any] = Field(default_factory=dict)
    log: str = ""


@router.post("/api/trabalho/{jid}/concluir")
def concluir(jid: int, c: Conclusao, a: Any = Depends(agente_atual)) -> dict[str, bool]:
    j = _job_do_agente(jid, a)
    resultado = dict(c.resultado)
    resultado.pop("saida_completa", None)
    texto = json.dumps(resultado, ensure_ascii=False)[:32_000]
    with nucleo.transacao() as con:
        con.execute("UPDATE jobs SET status='concluido', progresso=100, resultado=?, log=?, "
                    "mensagem='concluído', terminado_em=?, visto_em=? WHERE id=?",
                    (texto, (j["log"] + c.log)[-LOG_MAX:], nucleo.agora(), nucleo.agora(), jid))
        con.execute("UPDATE agentes SET job_atual=NULL, visto_em=? WHERE id=?", (nucleo.agora(), a["id"]))
    return {"ok": True}


class Falha(BaseModel):
    agente: str = ""
    erro: str = ""
    log: str = ""
    motivo: str = ""


@router.post("/api/trabalho/{jid}/falhar")
def falhar(jid: int, f: Falha, a: Any = Depends(agente_atual)) -> dict[str, bool]:
    j = _job_do_agente(jid, a)
    log = (j["log"] + f.log)[-LOG_MAX:]
    with nucleo.transacao() as con:
        if j["cancelar"] or "cancelado" in f.erro:
            con.execute("UPDATE jobs SET status='cancelado', log=?, mensagem='cancelado', terminado_em=? "
                        "WHERE id=?", (log, nucleo.agora(), jid))
        elif j["tentativas"] < j["max_tentativas"]:
            con.execute("UPDATE jobs SET status='pendente', agente=NULL, log=?, mensagem=? WHERE id=?",
                        (log, ("falhou, tentando de novo: " + f.erro)[:300], jid))
        else:
            con.execute("UPDATE jobs SET status='falhou', log=?, mensagem=?, terminado_em=? WHERE id=?",
                        (log, f.erro[:500], nucleo.agora(), jid))
        con.execute("UPDATE agentes SET job_atual=NULL, visto_em=? WHERE id=?", (nucleo.agora(), a["id"]))
    return {"ok": True}


class NotasAgente(BaseModel):
    notas: list[dict[str, str]] = Field(default_factory=list, max_length=300)


@router.post("/api/agente/cerebro")
def receber_cerebro(n: NotasAgente, a: Any = Depends(agente_atual)) -> dict[str, int]:
    """O agente manda as notas .md do cérebro local; elas aparecem no painel."""
    gravadas = 0
    for nota in n.notas:
        nome = (nota.get("nome") or "").strip()[:120]
        if not nome:
            continue
        salvar_nota(a["usuario_id"], nome, (nota.get("conteudo") or "")[:100_000], a["hostname"])
        gravadas += 1
    return {"gravadas": gravadas}


# ------------------------------------------------------------ painel: parque ---

def maquinas(uid: int) -> list[dict[str, Any]]:
    rows = nucleo.db().execute("SELECT * FROM agentes WHERE usuario_id = ? ORDER BY criado_em", (uid,)).fetchall()
    saida = []
    for r in rows:
        online = _idade(r["visto_em"]) < ONLINE_S
        saida.append({
            "id": r["id"], "hostname": r["hostname"], "apelido": r["apelido"] or r["hostname"], "modo": r["modo"],
            "online": online, "visto_em": r["visto_em"], "versao": r["versao"],
            "tags": sorted(_tags(r["tags"])), "info": _json(r["info"], {}),
            "metricas": _json(r["metricas"], {}) if online else {},
            "job_atual": r["job_atual"] if online else None,
        })
    return saida


@router.get("/api/parque")
def ver_parque(u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    zelar(u["id"])
    ms = maquinas(u["id"])
    desde = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    cont = {r["status"]: r["n"] for r in nucleo.db().execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE usuario_id = ? AND criado_em > ? GROUP BY status",
        (u["id"], desde)).fetchall()}
    return {"maquinas": ms, "resumo": {
        "maquinas": len(ms), "online": sum(m["online"] for m in ms),
        "concluidos_24h": cont.get("concluido", 0), "falhas_24h": cont.get("falhou", 0),
        "executando": cont.get("executando", 0),
        "na_fila": cont.get("pendente", 0) + cont.get("aguardando", 0),
    }}


class EditarMaquina(BaseModel):
    apelido: str = Field(min_length=1, max_length=40)


def _maquina(mid: int, uid: int) -> Any:
    m = nucleo.db().execute("SELECT * FROM agentes WHERE id=? AND usuario_id=?", (mid, uid)).fetchone()
    if m is None:
        raise HTTPException(404, "Máquina não encontrada.")
    return m


@router.patch("/api/maquinas/{mid}")
def renomear_maquina(mid: int, e: EditarMaquina, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    _maquina(mid, u["id"])
    with nucleo.transacao() as con:
        con.execute("UPDATE agentes SET apelido=? WHERE id=?", (e.apelido.strip(), mid))
    return {"ok": True}


class Modo(BaseModel):
    modo: str
    senha: str = Field(default="", max_length=200)


@router.post("/api/maquinas/{mid}/modo")
def mudar_modo(mid: int, m: Modo, request: Request, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    """Modo livre deixa a máquina rodar qualquer comando: exige a senha da conta."""
    _maquina(mid, u["id"])
    if m.modo not in ("seguro", "livre"):
        raise HTTPException(400, "Modo inválido.")
    if m.modo == "livre":
        nucleo.limitar(f"modo:{u['id']}", 5, 900)
        if not nucleo.conferir_senha(m.senha, u["senha_hash"]):
            raise HTTPException(403, "Senha incorreta.")
    with nucleo.transacao() as con:
        con.execute("UPDATE agentes SET modo=? WHERE id=?", (m.modo, mid))
    return {"ok": True}


@router.get("/api/acoes")
def listar_acoes() -> list[dict[str, str]]:
    return acoes.catalogo()


@router.delete("/api/maquinas/{mid}")
def remover_maquina(mid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    """Desconecta a máquina: o token dela deixa de valer na hora."""
    _maquina(mid, u["id"])
    with nucleo.transacao() as con:
        con.execute("DELETE FROM agentes WHERE id=?", (mid,))
    return {"ok": True}


# ------------------------------------------------------------- painel: jobs ---

MODO_SEGURO_MSG = ("A máquina '{}' está no modo seguro: ela só roda ações prontas. "
                   "Para rodar comandos livres, mude a máquina para o modo livre no painel.")


def criar_job(uid: int, maquina: str, tipo: str, texto: str, titulo: str = "",
              origem: str = "painel", status: str = "pendente") -> int:
    """Cria uma tarefa. Com tipo 'acao', `texto` é o id de uma ação pronta, e o
    comando vem do catálogo (nuvem/acoes.py), para o sistema daquela máquina."""
    if tipo not in TIPOS and tipo != "acao":
        raise HTTPException(400, "Tipo de tarefa inválido.")
    texto = (texto or "").strip()
    if not texto:
        raise HTTPException(400, "Diga o que a máquina deve fazer.")
    m = nucleo.db().execute("SELECT hostname, apelido, info, modo FROM agentes WHERE usuario_id=? "
                            "AND (hostname=? OR apelido=?)", (uid, maquina, maquina)).fetchone()
    if m is None:
        raise HTTPException(404, f"Máquina '{maquina}' não está no seu parque.")
    if tipo == "acao":
        if texto not in acoes.ACOES:
            raise HTTPException(400, "Ação desconhecida.")
        try:
            payload = {"comando": acoes.comando(texto, _json(m["info"], {})), "acao": texto}
        except ValueError as exc:
            raise HTTPException(409, f"Ainda não dá: {exc}.")
        tipo, titulo = "shell", titulo or acoes.ACOES[texto]["titulo"]
    else:
        if m["modo"] != "livre":
            raise HTTPException(403, MODO_SEGURO_MSG.format(m["apelido"] or m["hostname"]))
        payload = {"comando": texto} if tipo == "shell" else {"prompt": texto}
    with nucleo.transacao() as con:
        return con.inserir(
            "INSERT INTO jobs (usuario_id, tipo, titulo, payload, tags, origem, status, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uid, tipo, (titulo or texto.splitlines()[0])[:120], json.dumps(payload, ensure_ascii=False),
             m["hostname"], origem, status, nucleo.agora()))


class NovoJob(BaseModel):
    maquina: str = Field(min_length=1, max_length=80)
    tipo: str = "shell"
    texto: str = Field(min_length=1, max_length=20_000)
    titulo: str = Field(default="", max_length=120)


@router.post("/api/jobs")
def novo_job(j: NovoJob, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, int]:
    return {"id": criar_job(u["id"], j.maquina, j.tipo, j.texto, j.titulo)}


def _resumo_job(r: Any) -> dict[str, Any]:
    return {k: r[k] for k in ("id", "tipo", "titulo", "status", "agente", "progresso", "mensagem",
                              "origem", "criado_em", "iniciado_em", "terminado_em", "tags")}


@router.get("/api/jobs")
def listar_jobs(status: str | None = None, limite: int = 50,
                u: Any = Depends(nucleo.usuario_verificado)) -> list[dict[str, Any]]:
    zelar(u["id"])
    sql = ("SELECT id, tipo, titulo, status, agente, progresso, mensagem, origem, criado_em, "
           "iniciado_em, terminado_em, tags FROM jobs WHERE usuario_id = ?")
    args: list[Any] = [u["id"]]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limite, 200)))
    return [_resumo_job(r) for r in nucleo.db().execute(sql, args).fetchall()]


@router.get("/api/jobs/{jid}")
def detalhe_job(jid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    j = _ver_job(jid, u["id"])
    d = _resumo_job(j)
    d.update(payload=_json(j["payload"], {}), resultado=_json(j["resultado"]), log=j["log"][-LOG_MAX:],
             tentativas=j["tentativas"])
    return d


@router.post("/api/jobs/{jid}/aprovar")
def aprovar_job(jid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    _ver_job(jid, u["id"])
    with nucleo.transacao() as con:
        con.execute("UPDATE jobs SET status='pendente', mensagem='aprovada; esperando a máquina' "
                    "WHERE id=? AND status='aguardando'", (jid,))
    return {"ok": True}


@router.post("/api/jobs/{jid}/cancelar")
def cancelar_job(jid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    j = _ver_job(jid, u["id"])
    with nucleo.transacao() as con:
        if j["status"] in ("pendente", "aguardando"):
            con.execute("UPDATE jobs SET status='cancelado', mensagem='cancelada', terminado_em=? WHERE id=?",
                        (nucleo.agora(), jid))
        elif j["status"] == "executando":
            con.execute("UPDATE jobs SET cancelar=1, mensagem='cancelando…' WHERE id=?", (jid,))
    return {"ok": True}


@router.post("/api/jobs/{jid}/repetir")
def repetir_job(jid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, int]:
    j = _ver_job(jid, u["id"])
    p = _json(j["payload"], {})
    if p.get("acao"):
        return {"id": criar_job(u["id"], j["tags"].split(",")[0], "acao", p["acao"], j["titulo"], "repetida")}
    return {"id": criar_job(u["id"], j["tags"].split(",")[0], j["tipo"], p.get("comando") or p.get("prompt", ""),
                            j["titulo"], "repetida")}


# ----------------------------------------------------------------- cérebro ---

_RE_LINK = re.compile(r"\[\[([^\]|#]+)")

NOTAS_INICIAIS = {
    "Jarvis": "# Jarvis\n\nEu sou o seu assistente. Aqui fica o que eu sei sobre você e sobre o seu "
              "[[Parque]].\n\nTudo que você me pedir para lembrar vira uma nota neste cérebro, ligada às "
              "outras como num mapa.\n\n- Como trabalho: [[Rotinas]]\n- O que eu sei de você: [[Sobre mim]]\n",
    "Parque": "# Parque\n\nSão os computadores conectados a mim. Cada um roda um agente leve que "
              "recebe tarefas e me conta como está (CPU, memória, disco).\n\nVolte ao [[Jarvis]].\n",
    "Rotinas": "# Rotinas\n\nTarefas que se repetem: limpar downloads toda manhã, fazer backup, "
               "gerar um relatório. Eu rodo sozinho e aviso se algo falhar.\n\nVeja o [[Parque]].\n",
    "Sobre mim": "# Sobre mim\n\nConte ao Jarvis no chat: \"lembre que eu…\" e ele anota aqui.\n\n"
                 "Ligado a [[Jarvis]].\n",
}


def salvar_nota(uid: int, nome: str, conteudo: str, origem: str = "painel") -> None:
    with nucleo.transacao() as con:
        if con.execute("SELECT id FROM notas WHERE usuario_id=? AND nome=?", (uid, nome)).fetchone():
            con.execute("UPDATE notas SET conteudo=?, origem=?, atualizado_em=? WHERE usuario_id=? AND nome=?",
                        (conteudo, origem, nucleo.agora(), uid, nome))
        else:
            con.execute("INSERT INTO notas (usuario_id, nome, conteudo, origem, atualizado_em) VALUES (?, ?, ?, ?, ?)",
                        (uid, nome, conteudo, origem, nucleo.agora()))


def _semear_cerebro(uid: int) -> None:
    if nucleo.db().execute("SELECT COUNT(*) AS n FROM notas WHERE usuario_id=?", (uid,)).fetchone()["n"] == 0:
        for nome, texto in NOTAS_INICIAIS.items():
            salvar_nota(uid, nome, texto, "jarvis")


@router.get("/api/cerebro")
def listar_cerebro(u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    _semear_cerebro(u["id"])
    rows = nucleo.db().execute("SELECT nome, conteudo, origem, atualizado_em FROM notas WHERE usuario_id=? "
                               "ORDER BY nome", (u["id"],)).fetchall()
    nomes = {r["nome"] for r in rows}
    notas, arestas = [], []
    for r in rows:
        links = sorted({m.strip() for m in _RE_LINK.findall(r["conteudo"])})
        notas.append({"nome": r["nome"], "origem": r["origem"], "atualizado_em": r["atualizado_em"],
                      "tamanho": len(r["conteudo"]), "links": links})
        arestas += [{"de": r["nome"], "para": alvo, "existe": alvo in nomes} for alvo in links]
    return {"notas": notas, "arestas": arestas}


@router.get("/api/cerebro/{nome}")
def ler_nota(nome: str, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    r = nucleo.db().execute("SELECT * FROM notas WHERE usuario_id=? AND nome=?", (u["id"], nome)).fetchone()
    if r is None:
        raise HTTPException(404, "Nota não encontrada.")
    return {"nome": r["nome"], "conteudo": r["conteudo"], "origem": r["origem"], "atualizado_em": r["atualizado_em"]}


class Nota(BaseModel):
    conteudo: str = Field(max_length=100_000)


@router.put("/api/cerebro/{nome}")
def gravar_nota(nome: str, n: Nota, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    nome = nome.strip()[:120]
    if not nome:
        raise HTTPException(400, "Dê um nome para a nota.")
    salvar_nota(u["id"], nome, n.conteudo)
    return {"ok": True}


@router.delete("/api/cerebro/{nome}")
def apagar_nota(nome: str, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    with nucleo.transacao() as con:
        con.execute("DELETE FROM notas WHERE usuario_id=? AND nome=?", (u["id"], nome))
    return {"ok": True}


# ----------------------------------------------------------------- rotinas ---

class NovaRotina(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    descricao: str = Field(default="", max_length=300)
    maquina: str = Field(min_length=1, max_length=80)
    tipo: str = "shell"
    texto: str = Field(min_length=1, max_length=20_000)
    frequencia: str = "manual"


@router.get("/api/rotinas")
def listar_rotinas(u: Any = Depends(nucleo.usuario_verificado)) -> list[dict[str, Any]]:
    saida = []
    for r in nucleo.db().execute("SELECT * FROM rotinas WHERE usuario_id=? ORDER BY id", (u["id"],)).fetchall():
        ultimo = nucleo.db().execute("SELECT id, status, criado_em, terminado_em FROM jobs WHERE usuario_id=? "
                                     "AND origem=? ORDER BY id DESC LIMIT 1",
                                     (u["id"], f"rotina:{r['id']}")).fetchone()
        p = _json(r["payload"], {})
        texto = acoes.ACOES[p["acao"]]["titulo"] if p.get("acao") in acoes.ACOES else \
            (p.get("comando") or p.get("prompt", ""))
        saida.append({"id": r["id"], "nome": r["nome"], "descricao": r["descricao"], "maquina": r["maquina"],
                      "tipo": r["tipo"], "texto": texto,
                      "frequencia": r["frequencia"], "ultimo": dict(ultimo) if ultimo else None})
    return saida


@router.post("/api/rotinas")
def criar_rotina(r: NovaRotina, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, int]:
    if (r.tipo not in TIPOS and r.tipo != "acao") or r.frequencia not in ("manual", "diaria"):
        raise HTTPException(400, "Rotina inválida.")
    if r.tipo == "acao" and r.texto not in acoes.ACOES:
        raise HTTPException(400, "Ação desconhecida.")
    if r.frequencia == "diaria" and u["plano"] != "pro":
        raise HTTPException(402, "Rotinas diárias automáticas são do plano Pro.")
    m = nucleo.db().execute("SELECT modo, apelido, hostname FROM agentes WHERE usuario_id=? AND hostname=?",
                            (u["id"], r.maquina)).fetchone()
    if m is None:
        raise HTTPException(404, "Máquina não encontrada.")
    if r.tipo != "acao" and m["modo"] != "livre":
        raise HTTPException(403, MODO_SEGURO_MSG.format(m["apelido"] or m["hostname"]))
    payload = {"acao": r.texto} if r.tipo == "acao" else \
        {"comando": r.texto} if r.tipo == "shell" else {"prompt": r.texto}
    with nucleo.transacao() as con:
        rid = con.inserir("INSERT INTO rotinas (usuario_id, nome, descricao, tipo, payload, maquina, frequencia, "
                          "criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (u["id"], r.nome.strip(), r.descricao.strip(), r.tipo, json.dumps(payload, ensure_ascii=False),
                           r.maquina, r.frequencia, nucleo.agora()))
    return {"id": rid}


def _rodar_rotina(r: Any) -> int:
    p = _json(r["payload"], {})
    jid = criar_job(r["usuario_id"], r["maquina"], r["tipo"], p.get("acao") or p.get("comando") or p.get("prompt", ""),
                    r["nome"], f"rotina:{r['id']}")
    with nucleo.transacao() as con:
        con.execute("UPDATE rotinas SET ultima_execucao=? WHERE id=?", (nucleo.agora(), r["id"]))
    return jid


@router.post("/api/rotinas/{rid}/rodar")
def rodar_rotina(rid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, int]:
    r = nucleo.db().execute("SELECT * FROM rotinas WHERE id=? AND usuario_id=?", (rid, u["id"])).fetchone()
    if r is None:
        raise HTTPException(404, "Rotina não encontrada.")
    return {"id": _rodar_rotina(r)}


@router.delete("/api/rotinas/{rid}")
def apagar_rotina(rid: int, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, bool]:
    with nucleo.transacao() as con:
        con.execute("DELETE FROM rotinas WHERE id=? AND usuario_id=?", (rid, u["id"]))
    return {"ok": True}


@router.get("/api/cron/rotinas")
def cron_rotinas(request: Request) -> dict[str, int]:
    """Chamado pelo Cron da Vercel uma vez por dia: dispara as rotinas diárias."""
    segredo = os.environ.get("CRON_SECRET")
    if not segredo or request.headers.get("authorization") != f"Bearer {segredo}":
        raise HTTPException(401, "não autorizado")
    hoje = nucleo.agora()[:10]
    disparadas = 0
    for r in nucleo.db().execute("SELECT * FROM rotinas WHERE frequencia='diaria'").fetchall():
        if (r["ultima_execucao"] or "")[:10] == hoje:
            continue
        try:
            _rodar_rotina(r)
            disparadas += 1
        except HTTPException:
            continue  # a máquina da rotina foi removida
    return {"disparadas": disparadas}


# --------------------------------------------------------- resumo para a IA ---

def resumo_do_parque(uid: int) -> str:
    ms = maquinas(uid)
    linhas = ["MÁQUINAS DO PARQUE:"] if ms else ["O usuário ainda não conectou nenhuma máquina."]
    for m in ms:
        i, mt = m["info"], m["metricas"]
        if not m["online"]:
            linhas.append(f"- {m['apelido']} ({m['hostname']}) [modo {m['modo']}]: DESLIGADA (visto em {m['visto_em']})")
            continue
        ias = [n for n, k in (("Claude Code", "tem_claude"), ("Gemini", "tem_gemini"), ("Codex", "tem_codex"))
               if i.get(k)]
        ram = f"{round(i['ram_total_mb'] / 1024)} GB RAM" if i.get("ram_total_mb") else ""
        uso = f"CPU {mt.get('cpu_pct')}%, RAM {mt.get('ram_pct')}%" if mt.get("cpu_pct") is not None else ""
        discos = ", ".join(f"{d.get('unidade')} {d.get('livre_gb')} GB livres" for d in (mt.get("discos") or []))
        estado = f"rodando a tarefa #{m['job_atual']}" if m["job_atual"] else "livre"
        linhas.append(f"- {m['apelido']} ({m['hostname']}) [modo {m['modo']}]: LIGADA, {i.get('so', '?')}, {i.get('cpu', '?')}, "
                      f"{ram}{', GPU ' + i['gpu'] if i.get('gpu') else ''}; IAs: {'/'.join(ias) or 'nenhuma'}; "
                      f"{uso}; {discos}; {estado}")
    if ms:
        linhas.append("\nAÇÕES PRONTAS (as únicas permitidas em máquinas no modo seguro; use tipo 'acao'):")
        linhas += [f"- {k}: {v['titulo']} — {v['descricao']}" for k, v in acoes.ACOES.items()]
    jobs = nucleo.db().execute("SELECT id, titulo, status, agente, mensagem, criado_em FROM jobs WHERE usuario_id=? "
                               "ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    if jobs:
        linhas.append("\nTAREFAS RECENTES:")
        linhas += [f"- #{j['id']} {j['titulo']} [{j['status']}] em {j['agente'] or '-'}: {j['mensagem'] or ''}"
                   for j in jobs]
    rotinas = nucleo.db().execute("SELECT nome, maquina, frequencia FROM rotinas WHERE usuario_id=?",
                                  (uid,)).fetchall()
    if rotinas:
        linhas.append("\nROTINAS:")
        linhas += [f"- {r['nome']} em {r['maquina']} ({r['frequencia']})" for r in rotinas]
    return "\n".join(linhas)
