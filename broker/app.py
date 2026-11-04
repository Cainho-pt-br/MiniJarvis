"""Broker do orquestrador de máquinas.

Regras que moldam o desenho (ver spec):
  - Escrita em disco é recurso escasso: o banco fica num cartão SD. Só mudança
    de estado de job vai para o SQLite. Métricas de agente, progresso, mensagem
    e log corrente vivem em memória e sao perdidos se o broker reiniciar.
  - Máquina desligada é estado normal: job sem agente compatível fica pendente
    indefinidamente, nunca vira falha.
  - Portabilidade: nada de path absoluto, banco num arquivo só.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

# ----------------------------------------------------------------- configuração
BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BROKER_DB", BASE / "orquestrador.db"))
UI_PATH = BASE / "ui.html"
UPLOADS_DIR = Path(os.environ.get("ORQ_UPLOADS_DIR", DB_PATH.parent / "uploads"))
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
SUGESTOES_PATH = Path(os.environ.get("ORQ_SUGESTOES_PATH", DB_PATH.parent / "sugestoes-gemini.json"))
SUGESTOES_REPO_PATH = BASE.parent / "dados" / "sugestoes-gemini.json"

AGENTE_TIMEOUT_S = 90     # sem heartbeat por isso, o job volta para a fila
LOG_MAX = 64 * 1024       # limite de log por job
# Teto de jobs terminados guardados. Sem isto o banco cresce para sempre num
# cartão SD, e a UI fica mais pesada a cada dia. A poda é rara e em lote, para
# não virar escrita constante em disco.
HISTORICO_MAX = int(os.environ.get("HISTORICO_MAX", "400"))

# ------------------------------------------------------------------ estado RAM
# Nada aqui é persistido. Reiniciar o broker zera a lista: os agentes reaparecem
# no próximo heartbeat, em até 10s.
_lock = threading.RLock()
AGENTES: dict[str, dict[str, Any]] = {}
EXECUCAO: dict[int, dict[str, Any]] = {}   # job_id -> progresso/mensagem/log
CANCELAR: set[int] = set()                 # jobs executando marcados p/ cancelar

# Versão da lista de jobs: sobe a cada mudança que /api/jobs mostraria. Vira o
# ETag da listagem, e o navegador, que revalida sozinho, recebe 304 sem corpo
# quando nada mudou. O painel pede a lista a cada 4s; parado, com o Wi-Fi
# fraco do Pi, isso eram ~100 KB por pedido para dizer "igual ao anterior".
# O id da partida entra no ETag porque a contagem recomeça do zero.
_versao_jobs = 0
PARTIDA = uuid.uuid4().hex[:8]


def mudou_jobs() -> None:
    global _versao_jobs
    with _lock:
        _versao_jobs += 1


# Agentes esperando trabalho (/api/trabalho?espera=). Acordados quando surge
# job pendente — criado, reenfileirado, devolvido —, em vez de descobrirem na
# próxima rodada de 5 s.
_tem_trabalho = threading.Condition()


def acordar_agentes() -> None:
    with _tem_trabalho:
        _tem_trabalho.notify_all()


def marca_do_banco() -> str:
    """Data do banco e do WAL: muda com qualquer escrita, de qualquer processo.

    O contador acima só vê o que passa pelo broker. Um script que escreva direto
    no SQLite (manutenção, backup, teste) não o incrementaria, e o navegador
    seguiria recebendo 304 com a lista velha. São dois `stat`, sem abrir nada.
    """
    partes = []
    for caminho in (DB_PATH, Path(str(DB_PATH) + "-wal")):
        try:
            partes.append("%x" % caminho.stat().st_mtime_ns)
        except OSError:
            partes.append("0")
    return ".".join(partes)

# ---------------------------------------------------------------------- banco
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo            TEXT    NOT NULL,
  payload         TEXT    NOT NULL DEFAULT '{}',
  tags            TEXT    NOT NULL DEFAULT '',
  prioridade      INTEGER NOT NULL DEFAULT 100,
  status          TEXT    NOT NULL DEFAULT 'pendente',
  agente          TEXT,
  progresso       INTEGER NOT NULL DEFAULT 0,
  mensagem        TEXT,
  resultado       TEXT,
  log             TEXT,
  tentativas      INTEGER NOT NULL DEFAULT 0,
  max_tentativas  INTEGER NOT NULL DEFAULT 3,
  -- localtime, não CURRENT_TIMESTAMP: este grava UTC e os demais carimbos são
  -- gravados pelo processo em hora local, o que deixaria a linha incoerente.
  criado_em       TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
  iniciado_em     TIMESTAMP,
  terminado_em    TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_jobs_fila ON jobs(status, prioridade, id);

-- Quem já se apresentou alguma vez. A lista viva de agentes mora só em
-- memória, então uma máquina desligada SUMIA do painel quando o broker
-- reiniciava — e a regra 1 diz que desligada é estado normal, não inexistente.
-- Aqui fica só a identidade (tags, inventário, versão), que muda quando o
-- agente reinicia e não a cada batida: métrica continua sem tocar o cartão.
CREATE TABLE IF NOT EXISTS agentes (
  hostname       TEXT PRIMARY KEY,
  tags           TEXT NOT NULL DEFAULT '',
  info           TEXT,
  versao_agente  TEXT,
  visto_em       TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # menos escrita, menos corrupção
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


_local = threading.local()


def db() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        con = conectar()
        _local.con = con
    return con


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE: pega o lock de escrita já na abertura.

    É o que evita que dois agentes reivindiquem o mesmo job. Com WAL, sem isso,
    dois leitores podem decidir escrever a mesma linha.
    """
    con = db()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def agora() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def tags_set(csv: str | None) -> set[str]:
    return {t.strip() for t in (csv or "").split(",") if t.strip()}


# ------------------------------------------------------------------- modelos
class JobNovo(BaseModel):
    tipo: str
    payload: dict[str, Any] = Field(default_factory=dict)
    tags: str = ""
    prioridade: int = 100
    max_tentativas: int = 3


class Heartbeat(BaseModel):
    hostname: str
    tags: str = ""
    cpu_pct: float | None = None
    ram_pct: float | None = None
    gpu_pct: float | None = None
    vram_livre_mb: int | None = None
    disco_livre_gb: float | None = None
    # Todos os discos fixos, não só o do sistema: o boss tem NVMe e um HDD de
    # 1 TB, e o painel escondia o segundo.
    discos: list[dict[str, Any]] | None = None
    job_atual: int | None = None
    versao_agente: str = "?"
    # Inventário fixo da máquina (CPU, RAM total, SO, se tem Claude Code).
    # Opcional porque agente antigo não manda: nesse caso o broker mantém o que
    # já sabia, em vez de apagar a descrição de quem ainda não atualizou.
    info: dict[str, Any] | None = None
    # Estado da cópia local do cérebro: se é repositório, em que commit está e
    # quantas notas tem. É o que deixa "cérebro ausente" visível no painel.
    cerebro: dict[str, Any] | None = None


class Progresso(BaseModel):
    agente: str = ""
    progresso: int | None = None
    mensagem: str | None = None
    log: str = ""


class Conclusao(BaseModel):
    agente: str = ""
    resultado: dict[str, Any] = Field(default_factory=dict)
    log: str = ""


class Falha(BaseModel):
    agente: str = ""
    erro: str = ""
    log: str = ""
    # "cota" ou "login" quando a falha é de modelo e trocar de modelo resolve.
    # Agente anterior à 0.2.0 não manda; aí o broker lê a mensagem de erro.
    motivo: str = ""


class UploadArquivo(BaseModel):
    nome: str = "arquivo.bin"
    dados: str


UploadImagem = UploadArquivo

EXTENSOES_PERMITIDAS = {
    # Imagens
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    # Áudios
    ".webm", ".ogg", ".mp3", ".wav", ".m4a", ".aac"
}


# ----------------------------------------------------------------------- app
class CaminhoProprio:
    """Serve o app também sob um caminho próprio (/orq/) e manda a raiz para lá.

    O Android reconhece app instalado por host + caminho, sem olhar a porta:
    com o orquestrador, o CRM e a máquina todos em "/", instalar um fazia o
    Chrome achar que os outros dois já estavam instalados. Cada um tem o seu
    caminho, e o manifesto usa ele como escopo (cerebro/apps-no-celular.md).
    """

    def __init__(self, app, prefixo: str) -> None:
        self.app, self.prefixo = app, prefixo

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            caminho = scope["path"]
            if caminho in ("/", self.prefixo) and scope["method"] in ("GET", "HEAD"):
                qs = scope["query_string"].decode()
                destino = self.prefixo + "/" + ("?" + qs if qs else "")
                await RedirectResponse(destino)(scope, receive, send)
                return
            if caminho.startswith(self.prefixo + "/"):
                scope = dict(scope, path=caminho[len(self.prefixo):])
        await self.app(scope, receive, send)


class MesmaOrigem:
    """Recusa escrita pedida por página de outro site (CSRF).

    Não há login: a identidade é o tailnet. Só que qualquer página aberta num
    navegador dentro do tailnet também está "no tailnet", e um formulário dela
    consegue mandar POST sem corpo para cá sem pedir licença ao navegador. Em
    2026-09-24 isso armava a máquina de protótipo e rodava rotina do parque
    (inclusive o reboot do Pi) a partir de um site qualquer.

    O navegador sempre manda `Origin` num POST; agente, script e curl não
    mandam, e seguem passando. Com `Origin`, só vale o mesmo nome de host
    (a porta não entra: painel, CRM e máquina dividem o nome e se chamam).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            origem = h.get("origin")
            if origem is not None:
                de = urlsplit(origem).hostname or ""
                aceitos = {urlsplit("//" + h[k]).hostname or ""
                           for k in ("host", "x-forwarded-host") if h.get(k)}
                if not de or de not in aceitos:
                    await JSONResponse({"detail": "origem recusada: " + origem},
                                       status_code=403)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


app = FastAPI(title="Orquestrador")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(CaminhoProprio, prefixo="/orq")
app.add_middleware(MesmaOrigem)
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

# O repositório do "cérebro" servido por HTTP burro, só leitura.
#
# É o que permite uma máquina Windows clonar sem SSH. O Tailscale não aceita
# Windows como servidor SSH, e como CLIENTE ele exige uma autenticação extra no
# navegador na primeira conexão de cada máquina — num job sem console isso fica
# esperando para sempre, e foi exatamente o que travou o clone na aspire5.
#
# Caminho por variável de ambiente, não fixo no código: a spec exige que o
# broker seja portátil para o i3 sem editar fonte. O hook post-update do
# repositório mantém info/refs atualizado a cada push.
_repo = Path(os.environ.get("ORQ_REPO_GIT", str(Path.home() / "git" / "caiosnet.git")))
if _repo.is_dir():
    app.mount("/git/caiosnet.git", StaticFiles(directory=str(_repo)), name="git")


@app.post("/api/upload")
def upload_arquivo(u: UploadArquivo) -> dict[str, Any]:
    """Recebe imagem ou áudio codificado em base64 (ou data URL), valida e salva em disco."""
    raw = u.dados
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        conteudo = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "dados em base64 inválidos")

    if len(conteudo) > 15 * 1024 * 1024:
        raise HTTPException(413, "arquivo excede o limite de 15 MB")

    ext = Path(u.nome).suffix.lower()
    if ext not in EXTENSOES_PERMITIDAS:
        ext = ".bin"

    carimbo = time.strftime("%Y%m%d_%H%M%S")
    ident = uuid.uuid4().hex[:8]
    nome_salvo = f"{carimbo}_{ident}{ext}"
    destino = UPLOADS_DIR / nome_salvo
    destino.write_bytes(conteudo)

    return {
        "ok": True,
        "url": f"/uploads/{nome_salvo}",
        "nome": nome_salvo,
        "caminho": str(destino.resolve()),
        "tamanho": len(conteudo),
    }


@app.get("/api/sugestoes")
def obter_sugestoes() -> dict[str, Any]:
    """Retorna as sugestões mais recentes geradas pelo Gemini Flash."""
    for p in (SUGESTOES_PATH, SUGESTOES_REPO_PATH):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {
        "gerado_em": None,
        "data": None,
        "modelo": None,
        "execucoes_hoje": 0,
        "sugestoes": []
    }


@app.post("/api/sugestoes")
def atualizar_sugestoes(payload: dict[str, Any]) -> dict[str, Any]:
    """Atualiza o cache de sugestões geradas pelo Gemini."""
    try:
        conteudo = json.dumps(payload, indent=2, ensure_ascii=False)
        SUGESTOES_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUGESTOES_PATH.write_text(conteudo, encoding="utf-8")
        if SUGESTOES_REPO_PATH.parent.is_dir():
            SUGESTOES_REPO_PATH.write_text(conteudo, encoding="utf-8")
        return {"ok": True, "sugestoes": len(payload.get("sugestoes", []))}
    except Exception as e:
        raise HTTPException(500, f"Falha ao salvar sugestões: {e}")


@app.post("/api/sugestoes/gerar")
def solicitar_geracao_sugestoes(forcar: bool = False) -> dict[str, Any]:
    """Enfileira um job para rodar deploy/sugestoes_parque.py no Raspberry Pi."""
    cmd = "python3 deploy/sugestoes_parque.py" + (" --forcar" if forcar else "")
    job_payload = {"comando": cmd}
    with transacao() as con:
        cur = con.execute(
            "INSERT INTO jobs (tipo, payload, tags, prioridade, max_tentativas) "
            "VALUES (?, ?, ?, ?, ?)",
            ("shell", json.dumps(job_payload), "raspberrypi", 40, 1)
        )
        job_id = cur.lastrowid
    mudou_jobs()
    acordar_agentes()
    return {"ok": True, "job_id": job_id}


# ------------------------------------------------------------- Notificações
# Notificações em tempo real enviadas pelo broker, agentes ou IAs.
# Ficam exclusivamente em memória (regra 3 da spec: preservação do cartão SD).
NOTIFICACOES: list[dict[str, Any]] = []
_MAX_NOTIFICACOES = 50
_notificacao_seq = 0


def emitir_notificacao(
    titulo: str,
    mensagem: str = "",
    tipo: str = "info",
    job_id: int | None = None,
    origem: str = "sistema",
    acoes: list[dict[str, Any]] | None = None,
    conversa: str | None = None,
) -> dict[str, Any]:
    """Cria uma notificação na memória do broker para exibição como pop up na UI."""
    global _notificacao_seq
    with _lock:
        _notificacao_seq += 1
        notif = {
            "id": _notificacao_seq,
            "titulo": str(titulo)[:200],
            "mensagem": str(mensagem or "")[:4000],
            "tipo": tipo if tipo in ("sucesso", "erro", "aviso", "info") else "info",
            "job_id": job_id,
            "origem": str(origem)[:50],
            "acoes": acoes or [],
            "conversa": str(conversa) if conversa else None,
            "criado_em": agora(),
        }
        NOTIFICACOES.append(notif)
        if len(NOTIFICACOES) > _MAX_NOTIFICACOES:
            del NOTIFICACOES[0 : len(NOTIFICACOES) - _MAX_NOTIFICACOES]
    return notif


@app.get("/api/notificacoes")
def listar_notificacoes(desde: int = 0, limite: int = 20) -> dict[str, Any]:
    """Retorna as notificações ativas em memória criadas após o ID `desde`."""
    with _lock:
        itens = [n for n in NOTIFICACOES if n["id"] > desde]
        if limite and len(itens) > limite:
            itens = itens[-limite:]
    return {"notificacoes": itens, "ultimo_id": NOTIFICACOES[-1]["id"] if NOTIFICACOES else 0}


@app.post("/api/notificacoes")
def criar_notificacao(payload: dict[str, Any]) -> dict[str, Any]:
    """Permite a agentes, IAs e scripts emitirem notificações que aparecem como pop up na UI."""
    tit = payload.get("titulo")
    if not tit:
        raise HTTPException(400, "campo 'titulo' é obrigatório")
    n = emitir_notificacao(
        titulo=tit,
        mensagem=payload.get("mensagem", ""),
        tipo=payload.get("tipo", "info"),
        job_id=payload.get("job_id"),
        origem=payload.get("origem", "api"),
        acoes=payload.get("acoes"),
        conversa=payload.get("conversa") or payload.get("conversa_chave"),
    )
    return {"ok": True, "notificacao": n}


@app.on_event("startup")
def iniciar() -> None:
    con = conectar()
    con.executescript(SCHEMA)
    try:
        r1234 = con.execute("SELECT id, payload, resultado FROM jobs WHERE id=1234").fetchone()
        if r1234 and r1234["resultado"]:
            res = json.loads(r1234["resultado"] or "{}")
            p1234 = json.loads(r1234["payload"] or "{}")
            emitir_notificacao(
                titulo="Ajustes no CRM (13 filas e ordenação de leads manuais)",
                mensagem=res.get("resposta") or "Ajustes no CRM concluídos com sucesso.",
                tipo="sucesso",
                job_id=1234,
                origem="gemini",
                conversa=p1234.get("conversa")
            )
    except Exception:
        pass
    # NÃO devolver aqui os jobs 'executando'.
    #
    # Havia um UPDATE nesta linha marcando todo 'executando' como 'pendente' na
    # partida, com a justificativa de que "sem agente vivo o zelador nunca os
    # veria". Isso era falso — o zelador varre o BANCO, não a lista de agentes
    # em memória, e ele mesmo diz isso no próprio docstring. E era duplamente
    # danoso:
    #
    # 1. Ressuscitava job sem contar tentativa e sem olhar `max_tentativas`.
    #    Um job cujo comando reinicia o broker voltava à fila em toda partida:
    #    em 2026-09-09 um `deploy.sh` rodou 44 vezes com `max_tentativas: 1`.
    # 2. Devolvia à fila job que ainda estava rodando de verdade. Reiniciar o
    #    broker não mata o agente; o trabalho continuava, e o job era executado
    #    duas vezes.
    #
    # O zelador resolve os dois casos: espera a carência de AGENTE_TIMEOUT_S,
    # deixa em paz quem o agente ainda reivindica no heartbeat, e devolve pelo
    # `devolver()`, que respeita o teto.
    # NÃO semear a tabela de agentes a partir do histórico de jobs.
    #
    # Existiu uma semeadura assim, para as máquinas desligadas aparecerem no
    # painel antes de gravarem identidade. Ela cumpriu o papel e virou passivo:
    # rodava a CADA partida e ressuscitava todo nome que já executou algo —
    # inclusive os `fakeN` do teste de corrida e agentes antigos já renomeados,
    # que passaram a figurar como máquinas de verdade, offline para sempre.
    # Quem entra na tabela agora é só quem manda heartbeat.
    con.close()
    threading.Thread(target=zelador, daemon=True).start()


def podar_historico() -> int:
    """Descarta jobs terminados além do teto, do mais antigo para o mais novo."""
    with transacao() as con:
        cur = con.execute(
            "DELETE FROM jobs WHERE id IN ("
            "  SELECT id FROM jobs"
            "   WHERE status IN ('concluido','falhou','cancelado')"
            "   ORDER BY id DESC LIMIT -1 OFFSET ?)", (HISTORICO_MAX,))
        n = cur.rowcount or 0
    if n:
        mudou_jobs()
    return n


def zelador() -> None:
    """Devolve à fila os jobs de agentes que sumiram (regra 2 da spec).

    A varredura parte dos jobs em execução no banco, não da lista de agentes em
    memória: o heartbeat é de 10s, então um agente que morre logo depois de
    reivindicar nunca chega a anunciar `job_atual`, e um zelador que olhasse só
    a memória deixaria esse job preso em 'executando' para sempre. Quem pegou o
    job está na coluna `agente`, que é gravada na própria reivindicação.
    """
    voltas = 0
    while True:
        time.sleep(10)
        try:
            voltas += 1
            if voltas % 30 == 0:        # a cada ~5 min, não a cada volta
                n = podar_historico()
                if n:
                    print("zelador: podou", n, "jobs antigos", flush=True)
            limite = time.time() - AGENTE_TIMEOUT_S
            presos = db().execute(
                "SELECT id, agente, iniciado_em FROM jobs WHERE status='executando'"
            ).fetchall()
            for job in presos:
                with _lock:
                    a = AGENTES.get(job["agente"])
                    vivo = a is not None and a["ultimo_ping"] >= limite
                    dono = a.get("job_atual") if a else None
                if vivo:
                    # Agente vivo mas que não reivindica mais este job: ele
                    # reiniciou e perdeu o trabalho. Sem esta checagem o job
                    # ficaria 'executando' para sempre, porque o agente parece
                    # saudável. A carência evita pegar a janela entre a
                    # reivindicação e o primeiro heartbeat que a anuncia.
                    if dono != job["id"] and idade_segundos(job["iniciado_em"]) > AGENTE_TIMEOUT_S:
                        if not encerrar_por_energia(job["id"], job["agente"]):
                            devolver(job["id"], "agente " + str(job["agente"]) +
                                     " reiniciou e perdeu o job")
                    continue
                # Carência: um agente recém-reivindicante pode ainda não constar
                # em memória (broker reiniciado, por exemplo). Só desiste dele
                # depois de o job ter idade suficiente para um heartbeat perdido.
                if idade_segundos(job["iniciado_em"]) < AGENTE_TIMEOUT_S:
                    continue
                if not encerrar_por_energia(job["id"], job["agente"]):
                    devolver(job["id"], "agente " + str(job["agente"]) + " sumiu")
                with _lock:
                    a = AGENTES.get(job["agente"])
                    if a and a.get("job_atual") == job["id"]:
                        a["job_atual"] = None
        except Exception:  # o zelador nunca pode morrer
            pass


def idade_segundos(carimbo: str | None) -> float:
    if not carimbo:
        return float("inf")
    try:
        t = time.mktime(time.strptime(carimbo, "%Y-%m-%d %H:%M:%S"))
        return time.time() - t
    except Exception:
        return float("inf")


# Comando que desliga ou reinicia a máquina: o agente some justamente por ter
# obedecido. Sem isto o job saía "falhou: agente sumiu" (a aspire5 e a celeron,
# com `shutdown.exe /s /t 0`, em 2026-09-25) — e, com tentativa sobrando,
# voltaria à fila e desligaria a máquina de novo assim que ela ligasse.
# `reboot`, `poweroff` e `shutdown` só contam em posição de comando, para um
# "reboot" dentro de texto não enganar; os cmdlets do PowerShell são únicos.
_CMD = r"(?:^|[;&|(\\/]\s*|\bsudo\s+)"
REINICIA = re.compile(r"\brestart-computer\b|" + _CMD + r"(?:reboot|systemctl\s+reboot)\b|"
                      + _CMD + r"shutdown(?:\.exe)?\b[^&|;]*?\s[/-][rg]\b", re.I)
DESLIGA = re.compile(r"\bstop-computer\b|" + _CMD + r"(?:poweroff|halt|systemctl\s+(?:poweroff|halt))\b|"
                     + _CMD + r"shutdown(?:\.exe)?\b(?:[^&|;]*?\s[/-][sph]\b|\s+(?:now|\+\d+))", re.I)


def efeito_de_energia(tipo: str, payload: str | None) -> str | None:
    """'reiniciou' ou 'desligou' se o job é um comando shell de energia."""
    if tipo != "shell":
        return None
    try:
        comando = str(json.loads(payload or "{}").get("comando") or "")
    except Exception:
        return None
    if REINICIA.search(comando):
        return "reiniciou"
    if DESLIGA.search(comando):
        return "desligou"
    return None


def encerrar_por_energia(job_id: int, agente: str) -> bool:
    """Conclui, em vez de devolver, o job de desligar ou reiniciar cujo agente
    sumiu ou voltou sem ele. False quando o job não é desse tipo."""
    row = db().execute("SELECT tipo, payload FROM jobs WHERE id=? AND status='executando'",
                       (job_id,)).fetchone()
    efeito = row and efeito_de_energia(row["tipo"], row["payload"])
    if not efeito:
        return False
    finalizar(job_id, "concluido", "", {"energia": efeito}, "%s %s, como pedido" % (agente, efeito))
    return True


def devolver(job_id: int, motivo: str) -> None:
    with _lock:
        log_vivo = ((EXECUCAO.get(job_id) or {}).get("log") or "")[-LOG_MAX:] or None
    falhou = False
    with transacao() as con:
        row = con.execute(
            "SELECT tentativas, max_tentativas FROM jobs "
            "WHERE id=? AND status='executando'", (job_id,)
        ).fetchone()
        if row is None:
            return
        if row["tentativas"] >= row["max_tentativas"]:
            # O log corrente vive na memória; sem gravá-lo aqui, a falha por
            # máquina sumida chegava ao painel, e ao diagnóstico, sem log.
            con.execute(
                "UPDATE jobs SET status='falhou', mensagem=?, terminado_em=?,"
                " log=COALESCE(?, log) WHERE id=?",
                (motivo + "; tentativas esgotadas", agora(), log_vivo, job_id),
            )
            falhou = True
        else:
            con.execute(
                "UPDATE jobs SET status='pendente', agente=NULL, iniciado_em=NULL,"
                " progresso=0, mensagem=? WHERE id=?", (motivo, job_id),
            )
    with _lock:
        EXECUCAO.pop(job_id, None)
        CANCELAR.discard(job_id)
    mudou_jobs()
    acordar_agentes()
    if falhou:
        diagnosticar_se_rotina(job_id)


# ------------------------------------------------------------------- jobs API
# ------------------------------------------------------------ Auto (Jev)
# O Jev (TypeSafe, pela OpenRouter) não escreve: recebe a mensagem e escolhe
# qual modelo responde. Pedido do Caio em 2026-09-25, depois do teste em
# .claude/skills/jev/SKILL.md: 0,5 s e US$ 0,00003 por decisão, com confiança
# calibrada. Só entra quando o chat manda tipo "auto" — é a escolha explícita
# de mandar a mensagem para fora. A chave fica fora do repositório.
JEV_URL = os.environ.get("JEV_URL", "https://openrouter.ai/api/alpha/decisions")
JEV_MODELO = os.environ.get("JEV_MODELO", "typesafe/jev-1.13")
JEV_CONFIANCA_MIN = float(os.environ.get("JEV_CONFIANCA_MIN", "0.6"))
CRITERIOS_IA = {
    "claude": "Hard or open-ended work: architecture, planning, multi-step changes across a "
              "codebase or several machines, careful analysis or long writing, anything "
              "ambiguous or high-stakes.",
    "gemini": "Light work: greetings, small talk, quick factual questions, short summaries or "
              "translations, simple lookups, brainstorming a short list.",
    "codex": "Focused hands-on coding: write, fix or explain a specific piece of code, run a "
             "command, read an error message or log and debug it.",
}


def chave_openrouter() -> str:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"].strip()
    arq = Path.home() / ".config" / "orq" / "openrouter.chave"
    try:
        return arq.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def escolher_modelo(prompt: str, tags: str) -> dict[str, Any]:
    """Qual IA responde esta mensagem. Nunca falha: sem Jev, cai no Claude.

    Só oferece ao Jev as IAs que a máquina escolhida tem. Sem certeza
    (confiança abaixo de JEV_CONFIANCA_MIN), vai para o Claude — é a regra da
    skill: quando o Jev não sabe, decide o modelo mais capaz.
    """
    with _lock:
        a = dict(AGENTES.get(tags) or {})
    opcoes = [t for t in IAS if not a or tem_ia(a, t)] or ["claude"]
    padrao = "claude" if "claude" in opcoes else opcoes[0]
    r: dict[str, Any] = {"escolhido": padrao, "opcoes": opcoes}
    if len(opcoes) == 1:
        r["motivo"] = "única IA da máquina"
        return r
    k = chave_openrouter()
    if not k:
        r["motivo"] = "sem chave do Jev no broker (~/.config/orq/openrouter.chave)"
        return r
    corpo = {"model": JEV_MODELO, "state": {"message": prompt[:6000]},
             "questions": {"modelo": {
                 "type": "choice",
                 "instructions": "Which assistant should answer this chat message? Prefer the "
                                 "lightest assistant that can do it well; keep claude for work "
                                 "that really needs it.",
                 "criteria": {t: CRITERIOS_IA[t] for t in opcoes}}}}
    req = urllib.request.Request(JEV_URL, json.dumps(corpo).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + k})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            dados = json.loads(resp.read())
        ans = (dados.get("answers") or {}).get("modelo") or {}
        r.update(ms=round((time.monotonic() - t0) * 1000),
                 custo_usd=(dados.get("usage") or {}).get("cost"),
                 jev=ans.get("choice"), confianca=ans.get("confidence"),
                 probabilidades=ans.get("probabilities"))
        if ans.get("choice") in opcoes and (ans.get("confidence") or 0) >= JEV_CONFIANCA_MIN:
            r["escolhido"] = ans["choice"]
            r["motivo"] = "Jev"
        else:
            r["motivo"] = "Jev sem certeza"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        detalhe = e.read().decode("utf-8", "replace")[:200] if isinstance(e, urllib.error.HTTPError) else str(e)
        r.update(ms=round((time.monotonic() - t0) * 1000), motivo="Jev indisponível: " + detalhe)
    return r


@app.post("/api/jobs")
def criar_job(j: JobNovo) -> dict[str, Any]:
    roteamento = None
    if j.tipo == "auto":
        roteamento = escolher_modelo(str(j.payload.get("prompt") or ""), j.tags)
        j.tipo = roteamento["escolhido"]
        j.payload["_roteamento"] = roteamento
    if (j.tipo in IAS and not j.payload.get("conversa") and
            not j.payload.get("agendada") and not j.payload.get("dossie_crm") and
            not j.payload.get("diagnostico_de") and not j.payload.get("correcao_de")):
        j.payload["conversa"] = j.payload.get("sessao") or str(uuid.uuid4())
    with transacao() as con:
        cur = con.execute(
            "INSERT INTO jobs (tipo,payload,tags,prioridade,max_tentativas) "
            "VALUES (?,?,?,?,?)",
            (j.tipo, json.dumps(j.payload), j.tags, j.prioridade, j.max_tentativas),
        )
        novo = cur.lastrowid
    mudou_jobs()
    acordar_agentes()
    resposta: dict[str, Any] = {"id": novo}
    if roteamento:
        resposta["roteamento"] = roteamento
    return resposta


def enriquecer(row: sqlite3.Row) -> dict[str, Any]:
    """Sobrepõe o estado vivo (memória) ao que está gravado no banco."""
    d = dict(row)
    with _lock:
        viva = EXECUCAO.get(d["id"])
        if viva and d["status"] == "executando":
            d["progresso"] = viva["progresso"]
            d["mensagem"] = viva["mensagem"]
    return d


_COLUNAS_LISTA = ("id, tipo, payload, tags, prioridade, status, agente, progresso,"
                  " mensagem, resultado, tentativas, max_tentativas, criado_em,"
                  " iniciado_em, terminado_em, (log IS NOT NULL) AS tem_log")


@app.get("/api/jobs")
def listar_jobs(request: Request, status: str | None = None, tag: str | None = None,
                limite: int = 200, ordem: str = "fila") -> Any:
    """`ordem=fila` (padrão): por prioridade, como o agente pega.
    `ordem=recentes`: todo job ativo mais os `limite` terminados mais novos.

    O painel usa `recentes`. Na ordem de fila, rotina e shell (prioridade 100,
    a mais baixa) eram os primeiros cortados da janela de 120 — atrás de ~100
    mensagens de chat —, e a falha de uma rotina nunca chegava à tela.
    """
    with _lock:
        versao = _versao_jobs
    etag = 'W/"%s-%d-%s-%s-%s-%d-%s"' % (PARTIDA, versao, marca_do_banco(), status or "",
                                        tag or "", limite, ordem)
    cabecalhos = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cabecalhos)
    # O log fica de fora: são até 64 KB por job, e a página só precisa dele ao
    # abrir um job específico. Antes o SELECT * trazia os 120 logs do cartão a
    # cada ciclo só para descartá-los aqui.
    args: list[Any] = []
    if ordem == "recentes" and not status:
        sql = ("SELECT " + _COLUNAS_LISTA + " FROM jobs WHERE status IN ('pendente','executando')"
               " UNION ALL SELECT * FROM (SELECT " + _COLUNAS_LISTA + " FROM jobs"
               " WHERE status NOT IN ('pendente','executando') ORDER BY id DESC LIMIT ?)"
               " ORDER BY id DESC")
        args.append(limite)
    else:
        sql = "SELECT " + _COLUNAS_LISTA + " FROM jobs"
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY prioridade ASC, id DESC LIMIT ?"
        args.append(limite)
    linhas = []
    for r in db().execute(sql, args):
        d = enriquecer(r)
        d["tem_log"] = bool(d["tem_log"])
        linhas.append(d)
    if tag:
        linhas = [r for r in linhas if tag in tags_set(r["tags"])]
    return JSONResponse(linhas, headers=cabecalhos)


@app.get("/api/jobs/{job_id}")
def detalhe_job(job_id: int) -> dict[str, Any]:
    row = db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job não encontrado")
    d = enriquecer(row)
    with _lock:
        viva = EXECUCAO.get(job_id)
        if viva:
            d["log"] = viva["log"]      # log corrente, ainda não persistido
    return d


@app.post("/api/jobs/{job_id}/cancelar")
def cancelar_job(job_id: int) -> dict[str, Any]:
    with transacao() as con:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job não encontrado")
        if row["status"] == "cancelado":
            return {"ok": True, "estado_anterior": "cancelado", "aviso": "já cancelado"}
        if row["status"] not in ("pendente", "executando"):
            raise HTTPException(409, "job está " + row["status"])
        era = row["status"]
        con.execute(
            "UPDATE jobs SET status='cancelado', terminado_em=?,"
            " mensagem='cancelado pelo painel' WHERE id=?", (agora(), job_id),
        )
    if era == "executando":
        # O agente descobre no próximo relatório de progresso e mata o processo.
        with _lock:
            CANCELAR.add(job_id)
    mudou_jobs()
    return {"ok": True, "estado_anterior": era}


@app.post("/api/jobs/cancelar-ativo")
def cancelar_ativo(sessao: str | None = None) -> dict[str, Any]:
    """Cancela o job em execução mais recente (ou por sessão do chat)."""
    with transacao() as con:
        if sessao:
            rows = con.execute(
                "SELECT id, status FROM jobs WHERE status IN ('executando', 'pendente')"
                " AND payload LIKE ? ORDER BY (status='executando') DESC, id DESC LIMIT 1",
                (f"%{sessao}%",),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, status FROM jobs WHERE status IN ('executando', 'pendente')"
                " ORDER BY (status='executando') DESC, id DESC LIMIT 1"
            ).fetchall()
        if not rows:
            return {"ok": False, "mensagem": "nenhum job em execução ou na fila"}
        job_id = rows[0]["id"]
        era = rows[0]["status"]
        con.execute(
            "UPDATE jobs SET status='cancelado', terminado_em=?,"
            " mensagem='cancelado pelo painel' WHERE id=?", (agora(), job_id),
        )
    if era == "executando":
        with _lock:
            CANCELAR.add(job_id)
    mudou_jobs()
    return {"ok": True, "id": job_id, "estado_anterior": era}



@app.delete("/api/jobs/{job_id}")
def apagar_job(job_id: int) -> dict[str, Any]:
    """Apaga um job do histórico. É irreversível — não há lixeira.

    Recusa job em execução: ele pertence a um agente neste instante, e sumir
    com a linha faria o relatório de progresso seguinte cair no vazio. Cancele
    primeiro; depois de cancelado, apagar é seguro.

    O painel usa isto para apagar uma conversa inteira, mandando um pedido por
    job. A conversa não existe no banco — ela é o conjunto de jobs `claude` que
    dividem a mesma sessão, e quem agrupa é o painel. Um endpoint de conversa
    obrigaria o broker a repetir essa regra de agrupamento, e duas cópias de uma
    regra é uma a mais do que se consegue manter iguais.
    """
    with transacao() as con:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job não encontrado")
        if row["status"] == "executando":
            raise HTTPException(409, "job está executando; cancele antes")
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    with _lock:
        EXECUCAO.pop(job_id, None)
        CANCELAR.discard(job_id)
    mudou_jobs()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/reenfileirar")
def reenfileirar_job(job_id: int) -> dict[str, Any]:
    with transacao() as con:
        row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job não encontrado")
        if row["status"] == "executando":
            raise HTTPException(409, "job em execução; cancele antes")
        con.execute(
            "UPDATE jobs SET status='pendente', agente=NULL, progresso=0,"
            " mensagem=NULL, resultado=NULL, log=NULL, tentativas=0,"
            " iniciado_em=NULL, terminado_em=NULL WHERE id=?", (job_id,),
        )
    mudou_jobs()
    acordar_agentes()
    return {"ok": True}


# ---------------------------------------------------------------- agentes API
@app.post("/api/agentes/heartbeat")
def heartbeat(h: Heartbeat) -> dict[str, Any]:
    with _lock:
        a = AGENTES.setdefault(h.hostname, {"hostname": h.hostname})
        a.update(
            tags=h.tags, ultimo_ping=time.time(), cpu_pct=h.cpu_pct,
            ram_pct=h.ram_pct, gpu_pct=h.gpu_pct, vram_livre_mb=h.vram_livre_mb,
            disco_livre_gb=h.disco_livre_gb, discos=(h.discos or []),
            job_atual=h.job_atual, versao_agente=h.versao_agente,
        )
        a.setdefault("visto_pela_primeira_vez", time.time())
        if h.info:
            a["info"] = h.info
        if h.cerebro is not None:
            a["cerebro"] = h.cerebro
        cancelar = bool(h.job_atual) and h.job_atual in CANCELAR
    registrar_identidade(h)
    return {"ok": True, "cancelar": cancelar}


# Última identidade que foi ao disco, por hostname. Serve para não fazer nem um
# SELECT por batida: 4 agentes a cada 10s dariam 34 mil consultas por dia.
_ident_no_disco: dict[str, tuple] = {}


def registrar_identidade(h: Heartbeat) -> None:
    """Guarda quem é este agente, para ele não sumir do painel quando desligar.

    Grava só quando a identidade MUDA — o que na prática é quando o agente
    reinicia. O carimbo `visto_em` acompanha essa gravação e, para máquina
    ligada, quem manda no painel é o heartbeat em memória; o do banco só é
    lido quando ela está fora do ar.
    """
    ident = (h.tags, json.dumps(h.info or {}, sort_keys=True), h.versao_agente)
    if _ident_no_disco.get(h.hostname) == ident:
        return
    try:
        with transacao() as con:
            con.execute(
                "INSERT INTO agentes (hostname, tags, info, versao_agente, visto_em)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(hostname) DO UPDATE SET tags=excluded.tags,"
                " info=excluded.info, versao_agente=excluded.versao_agente,"
                " visto_em=excluded.visto_em",
                (h.hostname, h.tags, ident[1], h.versao_agente, agora()),
            )
        _ident_no_disco[h.hostname] = ident
    except Exception:
        # Não vale derrubar o heartbeat por causa do registro: sem ele a máquina
        # só some do painel quando desligar, o que é bem menos grave.
        pass


@app.delete("/api/agentes/{hostname}")
def esquecer_agente(hostname: str) -> dict[str, Any]:
    """Remove um agente da lista em memória.

    Serve para limpar entrada de agente que deixou de existir (renomeado,
    desinstalado). Se ele ainda estiver vivo, reaparece no próximo heartbeat —
    o que é o comportamento certo: a lista descreve quem está batendo, não
    quem alguém decidiu que deveria existir.
    """
    with _lock:
        a = AGENTES.pop(hostname, None)
    _ident_no_disco.pop(hostname, None)
    # Some também do registro em disco: senão a máquina reapareceria offline no
    # próximo ciclo e o botão de esquecer não teria servido para nada.
    with transacao() as con:
        apagou = con.execute("DELETE FROM agentes WHERE hostname=?",
                             (hostname,)).rowcount
    if a is None and not apagou:
        raise HTTPException(404, "agente não conhecido")
    if a is None:
        return {"ok": True, "removido": hostname}
    if a.get("job_atual"):
        # Não deixa o job preso: sem o agente na lista, o zelador ainda o vê
        # pela coluna `agente`, mas ser explícito aqui evita esperar 90s.
        devolver(a["job_atual"], "agente " + hostname + " removido do painel")
    return {"ok": True, "removido": hostname}


@app.get("/api/agentes")
def listar_agentes() -> list[dict[str, Any]]:
    limite = time.time() - AGENTE_TIMEOUT_S
    agora_s = time.time()
    with _lock:
        lista = [
            dict(a, online=a["ultimo_ping"] >= limite,
                 ha_segundos=round(agora_s - a["ultimo_ping"]))
            for a in AGENTES.values()
        ]
    batendo = {a["hostname"] for a in lista}

    # Quem já se apresentou alguma vez e não está batendo agora. Sem isto, uma
    # máquina desligada some do painel assim que o broker reinicia — e some
    # justamente quando o dono mais precisa vê-la: para saber que ela existe e
    # está fora do ar, e não que ela nunca existiu.
    for r in db().execute("SELECT * FROM agentes"):
        if r["hostname"] in batendo:
            continue
        try:
            info = json.loads(r["info"] or "{}")
        except Exception:
            info = {}
        lista.append({
            "hostname": r["hostname"], "tags": r["tags"], "info": info,
            "versao_agente": r["versao_agente"], "online": False,
            "ultimo_ping": None, "ha_segundos": None, "desde": r["visto_em"],
            "job_atual": None, "cpu_pct": None, "ram_pct": None,
            "gpu_pct": None, "vram_livre_mb": None, "disco_livre_gb": None,
            "discos": [],
        })
    return sorted(lista, key=lambda x: x["hostname"])


# ----------------------------------------------------------------- cérebro
# A pasta de notas do projeto, lida direto do clone. Caminho por variável de
# ambiente pelo mesmo motivo do repositório git: a spec exige broker portátil.
CEREBRO_DIR = Path(os.environ.get("ORQ_CEREBRO", str(Path.home() / "orq" / "cerebro")))
_RE_LINK = re.compile(r"\[\[([^\]|#]+)")


_commit_cache: dict[str, Any] = {"em": 0.0, "valor": None}


def commit_de_referencia() -> str | None:
    """O commit do repositório central. É contra ele que se sabe se a cópia de
    uma máquina está atrasada — sem referência, "commit abc1234" não diz nada.

    Guardado por 30s: cada painel aberto pede o cérebro a cada 20s, e cada
    pedido abria um processo `git` no Pi."""
    if time.time() - _commit_cache["em"] < 30:
        return _commit_cache["valor"]
    valor = None
    try:
        r = subprocess.run(["git", "--git-dir", str(_repo),
                            "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        valor = (r.stdout.strip() or None) if r.returncode == 0 else None
    except Exception:
        pass
    _commit_cache.update(em=time.time(), valor=valor)
    return valor


_notas_cache: dict[str, Any] = {"assinatura": None, "notas": []}


def _assinatura_notas() -> tuple:
    """Nome, tamanho e data de cada nota — só `stat`, sem abrir arquivo."""
    return tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size)
                        for e in os.scandir(CEREBRO_DIR) if e.name.endswith(".md")))


@app.get("/api/cerebro")
def listar_cerebro() -> dict[str, Any]:
    """As notas do cérebro e os links entre elas.

    Só leitura de arquivo, nada de banco: o vault é markdown em git, e é ele a
    fonte. O grafo do painel desenha isto do mesmo jeito que o Obsidian faria.
    As notas só são relidas quando alguma muda de data ou tamanho.
    """
    referencia = commit_de_referencia()
    if CEREBRO_DIR.is_dir():
        assinatura = _assinatura_notas()
        if assinatura == _notas_cache["assinatura"]:
            return {"pasta": str(CEREBRO_DIR), "existe": True,
                    "notas": _notas_cache["notas"], "commit": referencia,
                    "fora_do_indice": fora_do_indice(_notas_cache["notas"])}
    if not CEREBRO_DIR.is_dir():
        return {"pasta": str(CEREBRO_DIR), "existe": False, "notas": [],
                "commit": referencia}

    notas: list[dict[str, Any]] = []
    for arq in sorted(CEREBRO_DIR.glob("*.md")):
        try:
            texto = arq.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        titulo = arq.stem
        for linha in texto.splitlines():
            if linha.startswith("# "):
                titulo = linha[2:].strip()
                break
        # dict.fromkeys: tira repetido mantendo a ordem em que aparece no texto
        alvos = list(dict.fromkeys(a.strip() for a in _RE_LINK.findall(texto)))
        notas.append({"nome": arq.stem, "titulo": titulo, "links": alvos,
                      "bytes": len(texto.encode("utf-8"))})

    existentes = {n["nome"] for n in notas}
    for n in notas:
        # link para nota que ainda não existe é intencional na convenção do
        # vault: marca o que vale escrever depois. Separo para o painel poder
        # desenhar diferente em vez de fingir que o alvo existe.
        n["quebrados"] = [a for a in n["links"] if a not in existentes]
        n["links"] = [a for a in n["links"] if a in existentes]
    _notas_cache.update(assinatura=assinatura, notas=notas)
    return {"pasta": str(CEREBRO_DIR), "existe": True, "notas": notas,
            "commit": referencia, "fora_do_indice": fora_do_indice(notas)}


def fora_do_indice(notas: list[dict[str, Any]]) -> list[str]:
    """Notas que o README do cérebro não aponta. Quem chega lê o índice para
    saber o que abrir; nota fora dele não é lida (deploy/indice-cerebro.py)."""
    leia = next((n for n in notas if n["nome"] == "README"), None)
    if leia is None:
        return []
    apontadas = set(leia["links"])
    return [n["nome"] for n in notas if n["nome"] != "README" and n["nome"] not in apontadas]


@app.get("/api/cerebro/{nome}")
def ler_nota(nome: str) -> dict[str, Any]:
    # Nome vem da URL: aceita só o que é nome de nota mesmo. Sem isto, um
    # "../../etc/passwd" viraria leitura de arquivo arbitrário — e o painel não
    # tem autenticação nenhuma, qualquer um no tailnet chama isto.
    if not re.fullmatch(r"[A-Za-z0-9._\- ]{1,120}", nome):
        raise HTTPException(400, "nome de nota inválido")
    arq = CEREBRO_DIR / (nome + ".md")
    if not arq.is_file():
        raise HTTPException(404, "nota não encontrada")
    return {"nome": nome, "texto": arq.read_text(encoding="utf-8", errors="replace")}


# ----------------------------------------------------------------- rotinas
# As tarefas agendadas (deploy/agendadas/*.json), lidas do clone. É o catálogo
# do que o parque sabe fazer de verdade: o quadro de sugestões oferece "rodar
# agora" só a partir daqui, em vez de comandos que um modelo inventou — até
# 2026-09-24 o quadro sugeria scripts que não existem e um VACUUM no cartão SD.
ROTINAS_DIR = Path(os.environ.get(
    "ORQ_ROTINAS", str(CEREBRO_DIR.parent / "deploy" / "agendadas")))


def _catalogo_rotinas() -> dict[str, dict[str, Any]]:
    rotinas: dict[str, dict[str, Any]] = {}
    if not ROTINAS_DIR.is_dir():
        return rotinas
    for arq in sorted(ROTINAS_DIR.glob("*.json")):
        try:
            t = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:
            continue
        if t.get("nome") and isinstance(t.get("job"), dict):
            rotinas[t["nome"]] = t
    return rotinas


@app.get("/api/rotinas")
def listar_rotinas() -> list[dict[str, Any]]:
    """Cada rotina com a última vez que rodou.

    O job de uma rotina é reconhecido pelo campo `agendada` no payload (que o
    criar-job.py grava desde 2026-09-24) ou, nos anteriores, pelo payload
    idêntico ao do arquivo. Só leitura.
    """
    con = db()
    saida = []
    for nome, t in _catalogo_rotinas().items():
        job = t["job"]
        row = con.execute(
            "SELECT id, status, criado_em, terminado_em, mensagem, agente FROM jobs"
            " WHERE payload LIKE ? OR payload = ? ORDER BY id DESC LIMIT 1",
            ('%"agendada": ' + json.dumps(nome) + "%",
             json.dumps(job.get("payload", {})))).fetchone()
        saida.append({
            "nome": nome, "descricao": t.get("descricao", ""), "quando": t.get("quando"),
            "tipo": job.get("tipo"), "tags": job.get("tags", ""),
            "ultimo": dict(row) if row else None,
        })
    return saida


@app.post("/api/rotinas/{nome}/rodar")
def rodar_rotina(nome: str) -> dict[str, Any]:
    """Cria agora o job de uma rotina, exatamente como o timer criaria."""
    t = _catalogo_rotinas().get(nome)
    if t is None:
        raise HTTPException(404, "rotina não encontrada")
    job = t["job"]
    payload = dict(job.get("payload") or {}, agendada=nome, disparo="painel")
    with transacao() as con:
        cur = con.execute(
            "INSERT INTO jobs (tipo,payload,tags,prioridade,max_tentativas) VALUES (?,?,?,?,?)",
            (job["tipo"], json.dumps(payload), job.get("tags", ""),
             int(job.get("prioridade", 100)), int(job.get("max_tentativas", 1))))
        novo = cur.lastrowid
    mudou_jobs()
    acordar_agentes()
    return {"ok": True, "id": novo}


# ------------------------------------------------------ diagnóstico de falha
# Rotina roda sem ninguém olhando. Quando ela falha de vez, o broker pede a um
# modelo que leia o log e diga o que houve: é o primeiro degrau de autonomia
# do parque, e o mais seguro, porque o diagnóstico só lê. Corrigir é outro
# pedido, que só sai com o clique do Caio (/corrigir), e o Claude propõe num
# ramo sem publicar nada. Ver cerebro/diagnostico-de-falhas.md.
DIAGNOSTICO_LIGADO = os.environ.get("ORQ_DIAGNOSTICO", "1") != "0"
# Gemini primeiro: ler um log não pede o modelo mais caro, e a troca por cota
# (destino_da_troca) leva o pedido a outro modelo quando ele está sem.
DIAGNOSTICO_TAGS = os.environ.get("ORQ_DIAGNOSTICO_TAGS", "gemini")
CORRECAO_TAGS = os.environ.get("ORQ_CORRECAO_TAGS", "claude")
CATEGORIAS_FALHA = ("codigo", "ambiente", "transitorio", "externo", "desconhecido")
# Campo do payload no SQL. O CASE garante que o json_extract só roda em JSON
# válido: sem ele, uma linha malformada derrubaria a consulta inteira.
_CAMPO = "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.%s') END"
_RE_BLOCO_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        p = json.loads(row["payload"] or "{}")
    except ValueError:
        return {}
    return p if isinstance(p, dict) else {}


def _contexto_da_falha(con: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Tudo que o modelo precisa para entender a falha sem sair buscando: o que
    a tarefa é, o que ela roda, o erro, as execuções anteriores e o fim do log."""
    p = _payload(row)
    nome = p.get("agendada")
    partes = []
    if nome:
        t = _catalogo_rotinas().get(nome) or {}
        partes.append("Rotina: %s — %s (quando roda: %s)" % (
            nome, t.get("descricao") or "sem descrição", t.get("quando") or "?"))
    partes.append("Job #%d: tipo %s, máquina %s, %d tentativa(s)" % (
        row["id"], row["tipo"], row["agente"] or row["tags"] or "?", row["tentativas"]))
    if p.get("antes"):
        partes.append("Comando que roda antes do modelo:\n" + str(p["antes"])[:1500])
    if p.get("comando") or p.get("prompt"):
        partes.append("O que ela executa:\n" + str(p.get("comando") or p.get("prompt"))[:1500])
    partes.append("Erro: " + (row["mensagem"] or "(sem mensagem)"))
    if nome:
        anteriores = con.execute(
            "SELECT id, status, terminado_em, mensagem FROM jobs WHERE " + _CAMPO % "agendada" +
            " = ? AND id != ? ORDER BY id DESC LIMIT 5", (nome, row["id"])).fetchall()
        if anteriores:
            partes.append("Execuções anteriores, da mais nova para a mais velha:\n" + "\n".join(
                "- #%d %s %s %s" % (a["id"], a["status"], a["terminado_em"] or "",
                                     (a["mensagem"] or "")[:120]) for a in anteriores))
    log = (row["log"] or "").strip()
    partes.append("Final do log:\n```\n%s\n```" % (log[-6000:] or "(vazio)"))
    return "\n\n".join(partes)


def _titulo_da_falha(row: sqlite3.Row) -> str:
    nome = _payload(row).get("agendada")
    return "job #%d (%s)" % (row["id"], "rotina " + nome if nome else row["tipo"] + " em " +
                             (row["agente"] or row["tags"] or "?"))


def pedir_diagnostico(job_id: int, manual: bool = False) -> int | None:
    """Cria o job que diagnostica a falha `job_id` e devolve o id dele.

    Sozinho, só para rotina (`agendada` no payload): é o que falha sem ninguém
    olhando, enquanto conversa tem quem leia o erro na hora. Pelo painel
    (`manual`), qualquer falha. Nunca diagnostica diagnóstico nem correção,
    que viraria laço, e cada falha uma vez só: de novo quando ela rodar e
    falhar outra vez, ou, pedido pelo painel, se o diagnóstico anterior falhou.
    """
    con = db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] != "falhou":
        return None
    p = _payload(row)
    if p.get("diagnostico_de") or p.get("correcao_de"):
        return None
    if not manual and not (DIAGNOSTICO_LIGADO and p.get("agendada")):
        return None
    ja = con.execute(
        "SELECT id, status FROM jobs WHERE " + _CAMPO % "diagnostico_de" + " = ?"
        " AND criado_em >= ? ORDER BY id DESC LIMIT 1",
        (job_id, row["iniciado_em"] or row["criado_em"])).fetchone()
    if ja and not (manual and ja["status"] in ("falhou", "cancelado")):
        return ja["id"]
    prompt = (
        "Diagnóstico da falha do " + _titulo_da_falha(row) + ". Ninguém olhou ainda: diga o "
        "que houve e o que fazer. Só leia — não mude arquivo, não rode nada que altere "
        "estado, não crie jobs. Se precisar, leia no clone os arquivos que a tarefa usa e "
        "as notas do cerebro/ sobre ela (cerebro/agendamento-de-tarefas.md lista as rotinas)."
        "\n\n" + _contexto_da_falha(con, row) + "\n\n"
        "Responda só com um bloco JSON, neste formato:\n"
        '{"categoria": "codigo|ambiente|transitorio|externo|desconhecido",'
        ' "resumo": "uma frase: o que quebrou",'
        ' "causa": "o porquê, em até 3 frases, citando a linha do log que prova",'
        ' "acao": "o que fazer agora, concreto",'
        ' "arquivos": ["caminho/no/repositorio"], "confianca": 0.0,'
        ' "auto_resolucao": {"acao": "repor-leads|reenfileirar|nenhuma", "motivo": "..."}}\n\n'
        "Categorias: codigo = defeito num arquivo do repositório, corrigir o código resolve; "
        "ambiente = a máquina (disco, rede local, programa faltando, login, permissão, serviço "
        "parado); transitorio = passageira, rodar de novo deve bastar; externo = serviço de "
        "fora (site, API, Drive) mudou ou caiu; desconhecido = o log não basta para dizer.\n"
        "auto_resolucao: acao='repor-leads' se o funil/fila de leads esgotou; "
        "acao='reenfileirar' se a falha for transitória pontual (rede/timeout) com confiança alta; "
        "caso contrário acao='nenhuma'.")
    # diagnostico_de primeiro: é por ele que o painel liga o diagnóstico à falha
    payload = {"diagnostico_de": job_id, "rotina": p.get("agendada"), "prompt": prompt}
    with transacao() as c:
        novo = c.execute(
            "INSERT INTO jobs (tipo,payload,tags,prioridade,max_tentativas) VALUES (?,?,?,?,?)",
            ("gemini", json.dumps(payload), DIAGNOSTICO_TAGS, 60, 1)).lastrowid
    mudou_jobs()
    acordar_agentes()
    return novo


def diagnosticar_se_rotina(job_id: int) -> None:
    """Gancho do fim de job. Nunca pode derrubar quem está finalizando."""
    try:
        pedir_diagnostico(job_id)
    except Exception as e:
        print("diagnóstico do job", job_id, "não criado:", e, flush=True)


def ler_diagnostico(texto: Any) -> dict[str, Any]:
    """O JSON que o modelo devolveu, conferido. Vem com cerca de código, texto
    em volta, acento na categoria ou campo faltando: nada disso pode sumir com
    o diagnóstico, que no pior caso vira "desconhecido" com o texto como resumo."""
    t = str(texto or "")
    candidatos = _RE_BLOCO_JSON.findall(t)
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        candidatos.append(t[i:j + 1])
    d = None
    for bruto in candidatos:
        try:
            d = json.loads(bruto)
        except ValueError:
            continue
        if isinstance(d, dict):
            break
        d = None
    if d is None:
        return {"categoria": "desconhecido", "resumo": " ".join(t.split())[:300], "lido": False}

    def texto_curto(chave: str, n: int) -> str:
        return " ".join(str(d.get(chave) or "").split())[:n]

    cat = unicodedata.normalize("NFKD", str(d.get("categoria") or "")).encode(
        "ascii", "ignore").decode().strip().lower()
    try:
        confianca: float | None = min(1.0, max(0.0, float(d.get("confianca"))))
    except (TypeError, ValueError):
        confianca = None

    auto = d.get("auto_resolucao")
    auto_resolucao = None
    if isinstance(auto, dict):
        acao_raw = str(auto.get("acao") or "").strip().lower()
        if acao_raw in ("repor-leads", "reenfileirar", "nenhuma"):
            auto_resolucao = {
                "acao": acao_raw,
                "motivo": " ".join(str(auto.get("motivo") or "").split())[:300]
            }

    return {
        "categoria": cat if cat in CATEGORIAS_FALHA else "desconhecido",
        "resumo": texto_curto("resumo", 300), "causa": texto_curto("causa", 800),
        "acao": texto_curto("acao", 600),
        "arquivos": [str(a)[:200] for a in (d.get("arquivos") or []) if isinstance(a, str)][:8],
        "confianca": confianca, "auto_resolucao": auto_resolucao, "lido": True,
    }


@app.post("/api/jobs/{job_id}/diagnosticar")
def diagnosticar_falha(job_id: int) -> dict[str, Any]:
    """Diagnóstico pedido pelo painel, para qualquer falha."""
    novo = pedir_diagnostico(job_id, manual=True)
    if novo is None:
        raise HTTPException(409, "só dá para diagnosticar job que falhou (e que não seja diagnóstico)")
    return {"id": novo}


@app.post("/api/jobs/{job_id}/corrigir")
def corrigir_falha(job_id: int, modelo: str = "gemini") -> dict[str, Any]:
    """Pede ao Gemini ou Claude a correção de uma falha, num ramo, sem publicar nada.

    Só sai com o clique do Caio: é o degrau em que um modelo mexe no código do
    parque. Trabalha numa árvore separada (git worktree) porque o clone
    principal é puxado pelo agente antes de cada job, empurra só o ramo
    `correcao/...` e não faz merge, deploy nem reinicia nada. Aplicar é do
    Caio com 1 clique (ou pelo botão de aplicar correção).
    """
    con = db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job não encontrado")
    if row["status"] != "falhou":
        raise HTTPException(409, "job está " + row["status"])
    ja = con.execute("SELECT id, status FROM jobs WHERE " + _CAMPO % "correcao_de" +
                     " = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    if ja and ja["status"] not in ("falhou", "cancelado"):
        return {"id": ja["id"], "existente": True}
    p = _payload(row)
    base = re.sub(r"[^a-z0-9]+", "-", str(p.get("agendada") or row["tipo"]).lower()).strip("-")
    ramo = "correcao/%s-%d" % (base or "job", job_id)
    diag = con.execute("SELECT resultado FROM jobs WHERE " + _CAMPO % "diagnostico_de" +
                       " = ? AND status='concluido' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    try:
        d = (json.loads(diag["resultado"] or "{}").get("diagnostico") or {}) if diag else {}
    except ValueError:
        d = {}
    visto = ("Diagnóstico: %s — %s\nCausa: %s\nO que fazer: %s\nArquivos: %s" % (
        d.get("categoria"), d.get("resumo"), d.get("causa"), d.get("acao"),
        ", ".join(d.get("arquivos") or []) or "?")) if d else "Ainda sem diagnóstico."
    arvore = "../orq-correcao-%d" % job_id
    tipo_ia = "claude" if modelo == "claude" else "gemini"
    tags_ia = CORRECAO_TAGS if modelo == "claude" else "raspberrypi"
    mod_nome = "Claude" if modelo == "claude" else "Gemini"
    prompt = (
        f"Correção da falha do " + _titulo_da_falha(row) + f", pedida pelo Caio ao {mod_nome} no painel.\n\n"
        + _contexto_da_falha(con, row) + "\n\n" + visto + "\n\n"
        "Sua tarefa é propor a correção num ramo, sem publicar nada:\n"
        "1. Confira o diagnóstico lendo o código. Se não for defeito de código, ou se a "
        "correção não for segura, diga por quê e pare, sem mexer em nada.\n"
        "2. Trabalhe numa árvore separada, nunca no clone principal (o agente puxa nele "
        f"antes de cada job): `git fetch`, depois `git worktree add {arvore} -b {ramo} "
        "<remoto>/principal` (o remoto é o que aponta para caiosnet.git).\n"
        "3. Corrija o mínimo, no estilo do código em volta. Teste do jeito mais seguro que "
        "houver: os testes de deploy/ ligados ao que mudou, ou o script num modo sem efeito "
        "fora (sem mandar mensagem, subir arquivo, mexer em relé ou em serviço).\n"
        f"4. Commite no ramo explicando o porquê, empurre só ele (`git push <remoto> {ramo}`) "
        f"e remova a árvore (`git worktree remove {arvore}`).\n"
        "5. Não faça merge em principal, não rode deploy.sh, não reinicie serviço, não rode "
        "a rotina de verdade. Aplicar é do Caio.\n\n"
        "Responda curto: o que estava errado, o que mudou (arquivos), como testou, e os "
        "comandos para aplicar se ele aprovar (merge em principal, push e deploy).")
    payload = {"correcao_de": job_id, "rotina": p.get("agendada"), "ramo": ramo, "modelo": tipo_ia, "prompt": prompt}
    with transacao() as c:
        novo = c.execute(
            "INSERT INTO jobs (tipo,payload,tags,prioridade,max_tentativas) VALUES (?,?,?,?,?)",
            (tipo_ia, json.dumps(payload), tags_ia, 50, 1)).lastrowid
    mudou_jobs()
    acordar_agentes()
    return {"id": novo}


@app.post("/api/jobs/{job_id}/aplicar-correcao")
def aplicar_correcao(job_id: int) -> dict[str, Any]:
    """Aplica a correção proposta num ramo com 1 clique.

    Aceita o ID do job que falhou OU o ID do job de correção.
    Localiza o ramo git da proposta, faz merge em principal, empurra e faz deploy.
    Reenfileira a tarefa original automaticamente.
    """
    con = db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job não encontrado")

    p = _payload(row)
    if p.get("correcao_de"):
        corr_row = row
        falha_id = int(p["correcao_de"])
    else:
        falha_id = job_id
        corr_row = con.execute(
            "SELECT * FROM jobs WHERE " + _CAMPO % "correcao_de" + " = ? AND status='concluido' ORDER BY id DESC LIMIT 1",
            (falha_id,)
        ).fetchone()
        if not corr_row:
            corr_row = con.execute(
                "SELECT * FROM jobs WHERE " + _CAMPO % "correcao_de" + " = ? ORDER BY id DESC LIMIT 1",
                (falha_id,)
            ).fetchone()

    if not corr_row:
        raise HTTPException(404, "nenhuma correção encontrada para este job")

    corr_p = _payload(corr_row)
    ramo = corr_p.get("ramo")
    if not ramo:
        raise HTTPException(400, "a correção não possui ramo git associado")

    cmd = (
        f"git fetch origin && "
        f"git merge origin/{ramo} -m \"Aplica correção do #{falha_id} via orquestrador\" && "
        f"git push origin principal && "
        f"sudo cp broker/ui.html /opt/orquestrador/broker/ui.html 2>/dev/null || true && "
        f"sudo systemctl reload-or-restart orquestrador-broker 2>/dev/null || true && "
        f"curl -s -X POST http://127.0.0.1:8000/api/jobs/{falha_id}/reenfileirar"
    )
    shell_payload = {
        "comando": cmd,
        "aplicando_correcao_de": falha_id,
        "ramo": ramo
    }
    with transacao() as c:
        novo = c.execute(
            "INSERT INTO jobs (tipo,payload,tags,prioridade,max_tentativas) VALUES (?,?,?,?,?)",
            ("shell", json.dumps(shell_payload), "raspberrypi", 100, 1)
        ).lastrowid
    mudou_jobs()
    acordar_agentes()
    return {"ok": True, "id": novo, "ramo": ramo, "job_alvo": falha_id}


@app.get("/api/agentes/{hostname}/historico")
def historico_agente(hostname: str, limite: int = 30) -> dict[str, Any]:
    """O que esta máquina já executou.

    Vem do SQLite, não da lista em memória: a lista descreve quem está batendo
    agora e se perde quando o broker reinicia, enquanto o histórico é justamente
    a parte que precisa sobreviver a isso. Consulta só por leitura, sem escrita
    no cartão.
    """
    con = db()
    contagem = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE agente=?"
        " AND tipo NOT IN ('claude', 'gemini', 'codex') GROUP BY status",
        (hostname,))}
    recentes = [dict(r) for r in con.execute(
        "SELECT id, tipo, status, tags, mensagem, payload, criado_em, iniciado_em,"
        " terminado_em FROM jobs WHERE agente=?"
        " AND tipo NOT IN ('claude', 'gemini', 'codex') ORDER BY id DESC LIMIT ?",
        (hostname, max(1, min(limite, 100))))]
    return {"hostname": hostname, "contagem": contagem,
            "total": sum(contagem.values()), "recentes": recentes}


# --------------------------------------------------------------- trabalho API
@app.get("/api/trabalho")
def pegar_trabalho(agente: str, tags: str = "", espera: float = 0) -> Any:
    """Reivindica o próximo job compatível; com `espera`, segura o pedido até
    aparecer um (no máximo 25 s). A reivindicação em si é `reivindicar()`."""
    fim = time.monotonic() + max(0.0, min(espera, 25.0))
    while True:
        job = reivindicar(agente, tags)
        if job is not None:
            return job
        resta = fim - time.monotonic()
        if resta <= 0:
            return Response(status_code=204)
        # Reconfere de 5 em 5 s mesmo sem aviso: job que fica disponível por
        # outro caminho (script escrevendo direto no banco) não se perde.
        with _tem_trabalho:
            _tem_trabalho.wait(timeout=min(resta, 5.0))


def reivindicar(agente: str, tags: str) -> dict[str, Any] | None:
    """Reivindica o próximo job compatível. Precisa ser atômico.

    O casamento de tags (o job exige um subconjunto do que o agente oferece) não
    cabe bem em SQL, então a seleção sai em Python e a reivindicação de cada
    candidato é um UPDATE condicional em status='pendente': quem perder a
    corrida recebe zero linhas e tenta o próximo candidato.
    """
    oferecidas = tags_set(tags)
    candidatos = db().execute(
        "SELECT id, tags FROM jobs WHERE status='pendente' "
        "ORDER BY prioridade ASC, id ASC LIMIT 50"
    ).fetchall()

    for c in candidatos:
        if not tags_set(c["tags"]) <= oferecidas:
            continue
        with transacao() as con:
            row = con.execute(
                "UPDATE jobs SET status='executando', agente=?, iniciado_em=?,"
                " tentativas=tentativas+1, progresso=0"
                " WHERE id=? AND status='pendente'"
                " RETURNING id, tipo, payload, tentativas, max_tentativas",
                (agente, agora(), c["id"]),
            ).fetchone()
        if row is None:
            continue        # outro agente pegou primeiro
        with _lock:
            EXECUCAO[row["id"]] = {"progresso": 0, "mensagem": "iniciando", "log": "", "total": 0}
            CANCELAR.discard(row["id"])
        mudou_jobs()
        return {
            "id": row["id"], "tipo": row["tipo"],
            "payload": json.loads(row["payload"]),
            "tentativa": row["tentativas"],
            "max_tentativas": row["max_tentativas"],
        }
    return None


@app.post("/api/trabalho/{job_id}/progresso")
def reportar_progresso(job_id: int, p: Progresso) -> dict[str, Any]:
    """Não toca no banco: progresso e log correntes ficam em memória."""
    with _lock:
        viva = EXECUCAO.get(job_id)
        if viva is None:
            viva = {"progresso": 0, "mensagem": "", "log": "", "total": 0}
            EXECUCAO[job_id] = viva
        antes = (viva["progresso"], viva["mensagem"])
        if p.progresso is not None:
            viva["progresso"] = max(0, min(100, p.progresso))
        if p.mensagem is not None:
            viva["mensagem"] = p.mensagem
        if p.log:
            viva["log"] = (viva["log"] + p.log)[-LOG_MAX:]
            # Quanto de log já passou por aqui, contando o que o corte de
            # LOG_MAX descartou: é a régua do /log?desde=, que manda só o novo.
            viva["total"] = viva.get("total", 0) + len(p.log)
        if (viva["progresso"], viva["mensagem"]) != antes:
            mudou_jobs()
        return {"ok": True, "cancelar": job_id in CANCELAR}


@app.get("/api/jobs/{job_id}/log")
def log_job(job_id: int, desde: int = 0) -> dict[str, Any]:
    """Só o log que o painel ainda não tem.

    Durante uma conversa o painel acompanha a resposta a cada segundo, e antes
    baixava o log inteiro toda vez — até 64 KB por segundo por job, num Wi-Fi
    que perde pacote. Agora ele manda quanto já tem (`desde`) e recebe o resto.
    `completo: true` avisa que o que veio substitui o que ele tinha: o job
    terminou, ou o corte de LOG_MAX passou do ponto em que ele estava.
    """
    with _lock:
        viva = EXECUCAO.get(job_id)
        if viva is not None:
            log, total = viva["log"], viva.get("total", len(viva["log"]))
            inicio = total - len(log)
            if inicio <= desde <= total:
                return {"log": log[desde - inicio:], "total": total,
                        "completo": False, "vivo": True}
            return {"log": log, "total": total, "completo": True, "vivo": True}
    row = db().execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "job não encontrado")
    log = row["log"] or ""
    return {"log": log, "total": len(log), "completo": True, "vivo": False}


IAS = ("claude", "gemini", "codex")
NOME_IA = {"claude": "Claude", "gemini": "Gemini", "codex": "Codex"}
# Como cada falha de modelo aparecia no texto, para agente sem o campo `motivo`.
_TERMOS_FALHA_IA = {
    "claude": ("assinatura do claude", "recusou por cota", "weekly limit",
               "credits are depleted", "rate limit"),
    "gemini": ("cota do gemini", "quota", "resource_exhausted", "429", "rate limit",
               "recusa de cota", "não está autenticado", "authentication required"),
    "codex": ("codex bateu no limite", "codex não tem login", "usage limit"),
}


def tem_ia(a: dict[str, Any], tipo: str) -> bool:
    i = a.get("info") or {}
    if tipo == "claude":
        return bool(i.get("tem_claude")) and i.get("claude_logado") is not False
    if tipo == "gemini":
        return bool(i.get("tem_gemini"))
    if tipo == "codex":
        return bool(i.get("tem_codex")) and i.get("codex_logado") is not False
    return False


def falha_de_ia(tipo: str, mensagem: str | None, motivo: str) -> bool:
    if tipo not in IAS:
        return False
    if motivo in ("cota", "login"):
        return True
    m = (mensagem or "").lower()
    return any(t in m for t in _TERMOS_FALHA_IA[tipo])


def destino_da_troca(tipo: str, tags: str, p: dict[str, Any]) -> tuple[str, str] | None:
    """Próximo modelo e tag para um job de IA que falhou por cota ou login.

    Só entra aqui o que o agente não resolveu sozinho: desde a 0.2.0 ele já
    tenta as outras IAs da própria máquina antes de desistir. Job endereçado a
    uma máquina (o chat) fica nela — o usuário escolheu onde conversar; job de
    capacidade (tarefa agendada com tag `claude` ou `gemini`) pode ir a quem
    tiver o modelo.
    """
    tentados = set(p.get("_tentados") or []) | {tipo}
    # Gemini/Antigravity é uma escolha explícita; não usar a cota compartilhada
    # como fallback automático quando o Claude atingir o limite.
    if tipo == "claude":
        tentados.add("gemini")
    if p.get("_fallback_gemini"):
        tentados.add("gemini")
    if p.get("_fallback_claude"):
        tentados.add("claude")
    with _lock:
        agentes = {h: dict(a) for h, a in AGENTES.items()}
    maquina = tags if tags in agentes else ""
    i = IAS.index(tipo)
    for alvo in IAS[i + 1:] + IAS[:i]:
        if alvo in tentados:
            continue
        if maquina:
            if tem_ia(agentes[maquina], alvo):
                return alvo, maquina
            continue
        if alvo in ("claude", "gemini"):     # tags de capacidade do parque
            return alvo, alvo
        com = sorted(h for h, a in agentes.items() if tem_ia(a, alvo))
        if com:
            return alvo, com[0]
    return None


def finalizar(job_id: int, status: str, log: str,
              resultado: dict[str, Any] | None = None,
              mensagem: str | None = None, motivo: str = "") -> dict[str, Any]:
    with _lock:
        viva = EXECUCAO.get(job_id) or {"log": "", "progresso": 0}
        # Vazio vira NULL: assim a listagem sabe se há log sem ler o texto
        # (`log IS NOT NULL` sai do cabeçalho do registro, sem tocar no conteúdo).
        log_final = (viva["log"] + log)[-LOG_MAX:] or None
        progresso = viva.get("progresso", 0)

    disparar_rotina: str | None = None
    with transacao() as con:
        row = con.execute(
            "SELECT status, tentativas, max_tentativas, tipo, tags, payload FROM jobs WHERE id=?",
            (job_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "job não encontrado")

        if (status == "falhou" and row["status"] != "cancelado"
                and falha_de_ia(row["tipo"], mensagem, motivo)):
            try:
                p = json.loads(row["payload"] or "{}")
            except Exception:
                p = {}
            troca = destino_da_troca(row["tipo"], row["tags"] or "", p)
            if troca:
                alvo, tag_dest = troca
                p["_tentados"] = sorted(set(p.get("_tentados") or []) | {row["tipo"], alvo})
                # marcas que o agente anterior à 0.2.0 ainda lê para não dar volta
                if alvo == "gemini":
                    p["_fallback_gemini"] = True
                if alvo == "claude":
                    p["_fallback_claude"] = True
                # a sessão é do modelo que falhou; o próximo começa conversa nova
                p.pop("sessao", None)
                p.pop("modelo", None)     # o modelo também é do que falhou
                con.execute(
                    "UPDATE jobs SET tipo=?, tags=?, status='pendente', agente=NULL,"
                    " iniciado_em=NULL, progresso=0, mensagem=?, log=?, payload=? WHERE id=?",
                    (alvo, tag_dest,
                     NOME_IA[row["tipo"]] + " sem cota ou login; alternando para " + NOME_IA[alvo],
                     log_final, json.dumps(p), job_id))
                with _lock:
                    EXECUCAO.pop(job_id, None)
                    CANCELAR.discard(job_id)
                mudou_jobs()
                acordar_agentes()
                return {"ok": True, "status": "pendente", "fallback": alvo}

        if row["status"] == "cancelado":
            # cancelado enquanto rodava: preserva o cancelamento, só guarda o log
            con.execute("UPDATE jobs SET log=?, terminado_em=? WHERE id=?",
                        (log_final, agora(), job_id))
            final = "cancelado"
        elif status == "falhou" and row["tentativas"] < row["max_tentativas"]:
            # ainda sobra tentativa: volta para a fila em vez de falhar de vez
            con.execute(
                "UPDATE jobs SET status='pendente', agente=NULL, iniciado_em=NULL,"
                " progresso=0, mensagem=?, log=? WHERE id=?",
                ("tentativa " + str(row["tentativas"]) + " falhou: " +
                 str(mensagem), log_final, job_id),
            )
            final = "pendente"
        else:
            if status == "concluido" and _payload(row).get("diagnostico_de"):
                # lido uma vez aqui, e não pelo painel a cada desenho
                resultado = dict(resultado or {})
                diag = ler_diagnostico(resultado.get("resposta"))
                resultado["diagnostico"] = diag
                # Auto-resolução autônoma pelo Gemini para rotinas operacionais
                orig_id = _payload(row).get("diagnostico_de")
                auto = diag.get("auto_resolucao") or {}
                acao_auto = auto.get("acao")
                if orig_id and acao_auto in ("repor-leads", "reenfileirar"):
                    orig = con.execute("SELECT * FROM jobs WHERE id=?", (orig_id,)).fetchone()
                    if orig and orig["status"] == "falhou":
                        orig_p = _payload(orig)
                        # Só auto-resolve 1 vez por job original para nunca entrar em loop
                        if not orig_p.get("auto_resolvido"):
                            orig_p["auto_resolvido"] = acao_auto
                            if acao_auto == "repor-leads":
                                disparar_rotina = "repor-leads"
                            msg_auto = f"Auto-resolvido pelo Gemini ({acao_auto}): {auto.get('motivo') or 'resolução automática'}"
                            con.execute(
                                "UPDATE jobs SET status='pendente', agente=NULL, progresso=0,"
                                " mensagem=?, resultado=NULL, log=NULL, tentativas=0,"
                                " iniciado_em=NULL, terminado_em=NULL, payload=? WHERE id=?",
                                (msg_auto, json.dumps(orig_p), orig_id),
                            )
            con.execute(
                "UPDATE jobs SET status=?, progresso=?, mensagem=?, resultado=?,"
                " log=?, terminado_em=? WHERE id=?",
                (status, 100 if status == "concluido" else progresso, mensagem,
                 json.dumps(resultado or {}), log_final, agora(), job_id),
            )
            final = status
            row_p = _payload(row)
            if row_p.get("notificacao"):
                emitir_notificacao(
                    titulo=row_p.get("titulo_notificacao") or f"Job #{job_id} concluído",
                    mensagem=(resultado or {}).get("resposta") or mensagem or "",
                    tipo="sucesso" if status == "concluido" else "erro",
                    job_id=job_id,
                    origem=row["agente"] or row["tipo"],
                    conversa=row_p.get("conversa") or row_p.get("sessao"),
                )

    with _lock:
        EXECUCAO.pop(job_id, None)
        CANCELAR.discard(job_id)
    mudou_jobs()
    if disparar_rotina:
        try:
            rodar_rotina(disparar_rotina)
        except Exception as err:
            print(f"auto-resolução: erro ao disparar {disparar_rotina}:", err, flush=True)
    if final == "pendente":
        acordar_agentes()
    if final == "falhou":
        diagnosticar_se_rotina(job_id)
    return {"ok": True, "status": final}


@app.post("/api/trabalho/{job_id}/concluir")
def concluir(job_id: int, c: Conclusao) -> dict[str, Any]:
    return finalizar(job_id, "concluido", c.log, c.resultado, "concluído")


@app.post("/api/trabalho/{job_id}/falhar")
def falhar(job_id: int, f: Falha) -> dict[str, Any]:
    return finalizar(job_id, "falhou", f.log, {}, f.erro, f.motivo)


# ------------------------------------------------------------------------ UI
_ui_cache: dict[str, Any] = {"mtime": None, "texto": "", "etag": ""}


@app.get("/", response_class=HTMLResponse)
def ui(request: Request) -> Response:
    """A página, lida do disco só quando o arquivo muda.

    São ~200 KB. Com o ETag, o celular que já tem a página recebe 304 sem corpo
    — inclusive o service worker, que atualiza a casca por trás a cada abertura.
    """
    mtime = UI_PATH.stat().st_mtime_ns
    if _ui_cache["mtime"] != mtime:
        texto = UI_PATH.read_text(encoding="utf-8")
        _ui_cache.update(mtime=mtime, texto=texto, etag='"%x-%x"' % (mtime, len(texto)))
    cabecalhos = {"ETag": _ui_cache["etag"], "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == _ui_cache["etag"]:
        return Response(status_code=304, headers=cabecalhos)
    return HTMLResponse(_ui_cache["texto"], headers=cabecalhos)


# Ícones gerados por deploy/gerar-icones.py. O manifesto é o que faz o Chrome
# do celular instalar com ícone em alta e splash, em vez de ampliar o favicon.
ICONES = BASE / "icones"
CACHE_ESTATICO = {"Cache-Control": "public, max-age=86400"}
MANIFESTO = {
    "name": "Orquestrador", "short_name": "Orquestrador",
    "id": "/orq/", "start_url": "/orq/", "scope": "/orq/", "display": "standalone",
    "background_color": "#ffffff", "theme_color": "#224867", "lang": "pt-BR",
    # ?v= sobe a cada ícone novo: é o que faz o Chrome refazer o app instalado
    "icons": [
        {"src": "/icones/icone-192.png?v=5", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/icones/icone-512.png?v=5", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/icones/maskable-512.png?v=5", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@app.get("/manifest.webmanifest")
def manifesto() -> JSONResponse:
    # no-cache: com cache de 24 h o Chrome do celular instalou o app com o
    # manifesto antigo, que apontava para os ícones antigos (2026-09-24)
    return JSONResponse(MANIFESTO, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # no-cache: o celular confere a cada abertura se o service worker mudou
    return FileResponse(BASE / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
@app.get("/satelite.ico")
def favicon() -> FileResponse:
    return icone("favicon.ico")


@app.get("/icones/{nome}")
def icone(nome: str) -> FileResponse:
    p = ICONES / nome
    if nome.startswith(".") or not p.is_file():
        raise HTTPException(404, "ícone não encontrado")
    return FileResponse(p, headers=CACHE_ESTATICO)


@app.get("/agente.py")
def baixar_agente() -> Response:
    """Serve o próprio agente, para instalar em máquina nova sem SSH.

    Máquinas Windows não aceitam Tailscale SSH como servidor, então copiar o
    arquivo para lá exigiria credencial. Servindo pelo broker, a instalação vira
    um download de dentro do tailnet — e o agente fica sempre na versão que o
    broker conhece.
    """
    return _servir(BASE.parent / "agente" / "agente.py")


@app.get("/instalar-agente-windows.ps1")
def baixar_instalador_windows() -> Response:
    """Instalador para máquina Windows: `irm <broker>/instalar-agente-windows.ps1 | iex`."""
    return _servir(BASE.parent / "deploy" / "instalar-agente-windows.ps1")


@app.get("/atualizar-agente-windows.ps1")
def baixar_atualizador_windows() -> Response:
    """Atualizador; roda como job do próprio orquestrador na máquina alvo."""
    return _servir(BASE.parent / "deploy" / "atualizar-agente-windows.ps1")


def _servir(caminho: Path) -> Response:
    if not caminho.exists():
        raise HTTPException(404, caminho.name + " não encontrado no host do broker")
    return Response(caminho.read_text(encoding="utf-8"),
                    media_type="text/plain; charset=utf-8")
