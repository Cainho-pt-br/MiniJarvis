"""MiniJarvis Nuvem: a versão online, para qualquer pessoa usar pelo navegador.

Contas com e-mail e senha, chat com IA em streaming, histórico de conversas,
tarefas prontas e um saldo de créditos que paga o custo da IA.

Rodar localmente:
    pip install -r nuvem/requirements.txt
    IA_FAKE=1 uvicorn nuvem.app:app --reload        # sem chave, IA de mentira
    ANTHROPIC_API_KEY=... uvicorn nuvem.app:app     # com o Claude de verdade
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .ia import Resposta, criar_ia, custo_usd
from .tarefas import TAREFAS

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NUVEM_DB", BASE / "nuvem.db"))
CREDITOS_INICIAIS = int(os.environ.get("CREDITOS_INICIAIS", "300"))
CREDITOS_POR_DOLAR = int(os.environ.get("CREDITOS_POR_DOLAR", "1000"))  # 1 crédito = US$ 0,001 de IA
SESSAO_DIAS = 30
COOKIE = "mj_sessao"
COOKIE_SEGURO = os.environ.get("COOKIE_SEGURO", "1") != "0"  # desligue só em http local
MAX_MENSAGEM = 20_000
HISTORICO_MAX_MSGS = 40

app = FastAPI(title="MiniJarvis Nuvem")
_ia = None


def ia() -> Any:
    global _ia
    if _ia is None:
        _ia = criar_ia()
    return _ia


# ----------------------------------------------------------------- banco ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    nome TEXT NOT NULL,
    senha_hash TEXT NOT NULL,
    creditos INTEGER NOT NULL DEFAULT 0,
    plano TEXT NOT NULL DEFAULT 'gratis',
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token_hash TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expira_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversas (
    id INTEGER PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    titulo TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY,
    conversa_id INTEGER NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    papel TEXT NOT NULL CHECK (papel IN ('user', 'assistant')),
    texto TEXT NOT NULL,
    conteudo_api TEXT,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extrato (
    id INTEGER PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    delta INTEGER NOT NULL,
    motivo TEXT NOT NULL,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversas_usuario ON conversas(usuario_id, atualizado_em);
CREATE INDEX IF NOT EXISTS idx_mensagens_conversa ON mensagens(conversa_id, id);
"""

_local = threading.local()


def db() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "caminho", None) != DB_PATH:
        con = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(SCHEMA)
        _local.con, _local.caminho = con, DB_PATH
    return con


@contextmanager
def transacao() -> Iterator[sqlite3.Connection]:
    con = db()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------------------------------------- senhas/sessão ---

_ITERACOES = 310_000


def gerar_hash(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, _ITERACOES)
    return f"pbkdf2_sha256${_ITERACOES}${sal.hex()}${h.hex()}"


def conferir_senha(senha: str, guardado: str) -> bool:
    try:
        _, it, sal, h = guardado.split("$")
        novo = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(sal), int(it))
        return hmac.compare_digest(novo.hex(), h)
    except ValueError:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def abrir_sessao(resp: Response, usuario_id: int) -> None:
    token = secrets.token_urlsafe(32)
    expira = datetime.now(timezone.utc) + timedelta(days=SESSAO_DIAS)
    with transacao() as con:
        con.execute("INSERT INTO sessoes VALUES (?, ?, ?)",
                    (_hash_token(token), usuario_id, expira.isoformat(timespec="seconds")))
    resp.set_cookie(COOKIE, token, max_age=SESSAO_DIAS * 86400, httponly=True,
                    secure=COOKIE_SEGURO, samesite="lax")


def usuario_atual(request: Request) -> sqlite3.Row:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Faça login para continuar.")
    row = db().execute(
        "SELECT u.* FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id "
        "WHERE s.token_hash = ? AND s.expira_em > ?", (_hash_token(token), agora())).fetchone()
    if row is None:
        raise HTTPException(401, "Sua sessão expirou. Entre de novo.")
    return row


# Limite simples de tentativas de login por IP (memória do processo).
_tentativas: dict[str, list[float]] = {}


def limitar_tentativas(chave: str, maximo: int = 10, janela: float = 300) -> None:
    t = time.monotonic()
    lista = [x for x in _tentativas.get(chave, []) if t - x < janela]
    if len(lista) >= maximo:
        raise HTTPException(429, "Muitas tentativas. Espere alguns minutos.")
    lista.append(t)
    _tentativas[chave] = lista


# ------------------------------------------------------------------ contas ---

class Cadastro(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=200)
    senha: str = Field(min_length=8, max_length=200)


class Entrada(BaseModel):
    email: str
    senha: str


_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _publico(u: sqlite3.Row) -> dict[str, Any]:
    return {"id": u["id"], "nome": u["nome"], "email": u["email"],
            "creditos": u["creditos"], "plano": u["plano"]}


@app.post("/api/cadastro")
def cadastrar(c: Cadastro, request: Request, resp: Response) -> dict[str, Any]:
    limitar_tentativas("cad:" + (request.client.host if request.client else "?"))
    email = c.email.strip().lower()
    if not _RE_EMAIL.match(email):
        raise HTTPException(400, "E-mail inválido.")
    try:
        with transacao() as con:
            cur = con.execute(
                "INSERT INTO usuarios (email, nome, senha_hash, creditos, criado_em) VALUES (?, ?, ?, ?, ?)",
                (email, c.nome.strip(), gerar_hash(c.senha), CREDITOS_INICIAIS, agora()))
            uid = cur.lastrowid
            con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                        (uid, CREDITOS_INICIAIS, "boas-vindas", agora()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    abrir_sessao(resp, uid)
    return _publico(db().execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone())


@app.post("/api/entrar")
def entrar(e: Entrada, request: Request, resp: Response) -> dict[str, Any]:
    limitar_tentativas("login:" + (request.client.host if request.client else "?"))
    u = db().execute("SELECT * FROM usuarios WHERE email = ?", (e.email.strip().lower(),)).fetchone()
    if u is None or not conferir_senha(e.senha, u["senha_hash"]):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    abrir_sessao(resp, u["id"])
    return _publico(u)


@app.post("/api/sair")
def sair(request: Request, resp: Response) -> dict[str, bool]:
    token = request.cookies.get(COOKIE)
    if token:
        with transacao() as con:
            con.execute("DELETE FROM sessoes WHERE token_hash = ?", (_hash_token(token),))
    resp.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/eu")
def eu(u: sqlite3.Row = Depends(usuario_atual)) -> dict[str, Any]:
    return _publico(u)


@app.delete("/api/eu")
def apagar_conta(resp: Response, u: sqlite3.Row = Depends(usuario_atual)) -> dict[str, bool]:
    """Direito de exclusão (LGPD): apaga a conta e tudo ligado a ela."""
    with transacao() as con:
        con.execute("DELETE FROM usuarios WHERE id = ?", (u["id"],))
    resp.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------- conversas ---

@app.get("/api/tarefas")
def tarefas() -> list[dict[str, str]]:
    return TAREFAS


@app.get("/api/conversas")
def listar_conversas(u: sqlite3.Row = Depends(usuario_atual)) -> list[dict[str, Any]]:
    rows = db().execute("SELECT id, titulo, atualizado_em FROM conversas WHERE usuario_id = ? "
                        "ORDER BY atualizado_em DESC LIMIT 100", (u["id"],)).fetchall()
    return [dict(r) for r in rows]


def _conversa_do_usuario(cid: int, uid: int) -> sqlite3.Row:
    c = db().execute("SELECT * FROM conversas WHERE id = ? AND usuario_id = ?", (cid, uid)).fetchone()
    if c is None:
        raise HTTPException(404, "Conversa não encontrada.")
    return c


@app.get("/api/conversas/{cid}")
def ver_conversa(cid: int, u: sqlite3.Row = Depends(usuario_atual)) -> dict[str, Any]:
    c = _conversa_do_usuario(cid, u["id"])
    msgs = db().execute("SELECT papel, texto, criado_em FROM mensagens WHERE conversa_id = ? ORDER BY id",
                        (cid,)).fetchall()
    return {"id": c["id"], "titulo": c["titulo"], "mensagens": [dict(m) for m in msgs]}


@app.delete("/api/conversas/{cid}")
def apagar_conversa(cid: int, u: sqlite3.Row = Depends(usuario_atual)) -> dict[str, bool]:
    _conversa_do_usuario(cid, u["id"])
    with transacao() as con:
        con.execute("DELETE FROM conversas WHERE id = ?", (cid,))
    return {"ok": True}


class NovaMensagem(BaseModel):
    texto: str = Field(min_length=1, max_length=MAX_MENSAGEM)
    conversa_id: int | None = None


def _historico_api(cid: int) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT papel, texto, conteudo_api FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT ?",
        (cid, HISTORICO_MAX_MSGS)).fetchall()[::-1]
    # A janela tem que começar por uma mensagem do usuário.
    while rows and rows[0]["papel"] != "user":
        rows = rows[1:]
    return [{"role": r["papel"],
             "content": json.loads(r["conteudo_api"]) if r["conteudo_api"] else r["texto"]} for r in rows]


def _sse(evento: str, dados: Any) -> str:
    return f"event: {evento}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"


@app.post("/api/mensagens")
def enviar_mensagem(m: NovaMensagem, u: sqlite3.Row = Depends(usuario_atual)) -> StreamingResponse:
    if u["creditos"] <= 0:
        raise HTTPException(402, "Seus créditos acabaram. Recarregue para continuar conversando.")
    texto = m.texto.strip()
    with transacao() as con:
        if m.conversa_id is None:
            titulo = texto.splitlines()[0][:60] or "Nova conversa"
            cid = con.execute("INSERT INTO conversas (usuario_id, titulo, criado_em, atualizado_em) "
                              "VALUES (?, ?, ?, ?)", (u["id"], titulo, agora(), agora())).lastrowid
        else:
            cid = _conversa_do_usuario(m.conversa_id, u["id"])["id"]
        con.execute("INSERT INTO mensagens (conversa_id, papel, texto, criado_em) VALUES (?, 'user', ?, ?)",
                    (cid, texto, agora()))
    historico = _historico_api(cid)
    motor = ia()

    def gerar() -> Iterator[str]:
        yield _sse("conversa", {"id": cid})
        final = Resposta()
        try:
            for pedaco in motor.responder(historico, final):
                yield _sse("texto", pedaco)
        except Exception as exc:  # noqa: BLE001 - a pessoa precisa ver algo amigável
            app.state.ultimo_erro = repr(exc)
            yield _sse("erro", "A IA não respondeu agora. Tente de novo em instantes.")
            return
        if final.recusada and not final.texto:
            final.texto = "Não posso ajudar com esse pedido. Que tal tentar de outro jeito?"
        custo = max(1, math.ceil(custo_usd(motor.modelo, final.tokens_entrada, final.tokens_saida)
                                 * CREDITOS_POR_DOLAR))
        with transacao() as con:
            con.execute("INSERT INTO mensagens (conversa_id, papel, texto, conteudo_api, criado_em) "
                        "VALUES (?, 'assistant', ?, ?, ?)",
                        (cid, final.texto, json.dumps(final.conteudo, ensure_ascii=False), agora()))
            con.execute("UPDATE conversas SET atualizado_em = ? WHERE id = ?", (agora(), cid))
            con.execute("UPDATE usuarios SET creditos = MAX(0, creditos - ?) WHERE id = ?", (custo, u["id"]))
            con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                        (u["id"], -custo, f"conversa {cid}", agora()))
            saldo = con.execute("SELECT creditos FROM usuarios WHERE id = ?", (u["id"],)).fetchone()[0]
        yield _sse("fim", {"custo": custo, "creditos": saldo})

    return StreamingResponse(gerar(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ planos ---

PLANOS = [
    {"id": "gratis", "nome": "Grátis", "preco_brl": 0, "creditos": CREDITOS_INICIAIS,
     "descricao": "Para experimentar, sem cartão."},
    # Créditos calibrados para a IA custar no máximo ~35% do preço (ver docs/PRODUTO.md).
    {"id": "basico", "nome": "Básico", "preco_brl": 29.90, "creditos": 2000,
     "descricao": "Uso no dia a dia."},
    {"id": "pro", "nome": "Pro", "preco_brl": 59.90, "creditos": 4500,
     "descricao": "Uso intenso e computador conectado."},
]


@app.get("/api/planos")
def planos() -> list[dict[str, Any]]:
    # A cobrança (Mercado Pago / Stripe) entra na próxima etapa; ver docs/PRODUTO.md.
    return PLANOS


@app.get("/api/extrato")
def extrato(u: sqlite3.Row = Depends(usuario_atual)) -> list[dict[str, Any]]:
    rows = db().execute("SELECT delta, motivo, criado_em FROM extrato WHERE usuario_id = ? "
                        "ORDER BY id DESC LIMIT 50", (u["id"],)).fetchall()
    return [dict(r) for r in rows]


# -------------------------------------------------------------------- web ---

@app.get("/saude")
def saude() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def pagina() -> FileResponse:
    return FileResponse(BASE / "web" / "index.html")
