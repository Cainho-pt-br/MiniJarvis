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
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .banco import Banco, ErroDuplicado, url_postgres
from .ia import Resposta, criar_ia, custo_usd, modelo_do_plano
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

log = logging.getLogger("minijarvis")
app = FastAPI(title="MiniJarvis Nuvem")
_ia = None


def ia() -> Any:
    global _ia
    if _ia is None:
        _ia = criar_ia()
    return _ia


# ----------------------------------------------------------------- banco ---

_bancos: dict[tuple[str, str | None], Banco] = {}


def db() -> Banco:
    chave = (str(DB_PATH), url_postgres())
    if chave not in _bancos:
        _bancos[chave] = Banco(DB_PATH, chave[1])
    return _bancos[chave]


def transacao() -> Any:
    return db().transacao()


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


def usuario_atual(request: Request) -> Any:
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


def _publico(u: Any) -> dict[str, Any]:
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
            uid = con.inserir(
                "INSERT INTO usuarios (email, nome, senha_hash, creditos, criado_em) VALUES (?, ?, ?, ?, ?)",
                (email, c.nome.strip(), gerar_hash(c.senha), CREDITOS_INICIAIS, agora()))
            con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                        (uid, CREDITOS_INICIAIS, "boas-vindas", agora()))
    except ErroDuplicado:
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    parque._semear_cerebro(uid)  # o cérebro já nasce com as notas de boas-vindas
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
def eu(u: Any = Depends(usuario_atual)) -> dict[str, Any]:
    return _publico(u)


@app.delete("/api/eu")
def apagar_conta(resp: Response, u: Any = Depends(usuario_atual)) -> dict[str, bool]:
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
def listar_conversas(u: Any = Depends(usuario_atual)) -> list[dict[str, Any]]:
    rows = db().execute("SELECT id, titulo, atualizado_em FROM conversas WHERE usuario_id = ? "
                        "ORDER BY atualizado_em DESC LIMIT 100", (u["id"],)).fetchall()
    return [dict(r) for r in rows]


def _conversa_do_usuario(cid: int, uid: int) -> Any:
    c = db().execute("SELECT * FROM conversas WHERE id = ? AND usuario_id = ?", (cid, uid)).fetchone()
    if c is None:
        raise HTTPException(404, "Conversa não encontrada.")
    return c


@app.get("/api/conversas/{cid}")
def ver_conversa(cid: int, u: Any = Depends(usuario_atual)) -> dict[str, Any]:
    c = _conversa_do_usuario(cid, u["id"])
    msgs = db().execute("SELECT papel, texto, criado_em FROM mensagens WHERE conversa_id = ? AND oculta = 0 "
                        "AND texto <> '' ORDER BY id", (cid,)).fetchall()
    return {"id": c["id"], "titulo": c["titulo"], "mensagens": [dict(m) for m in msgs]}


@app.delete("/api/conversas/{cid}")
def apagar_conversa(cid: int, u: Any = Depends(usuario_atual)) -> dict[str, bool]:
    _conversa_do_usuario(cid, u["id"])
    with transacao() as con:
        con.execute("DELETE FROM conversas WHERE id = ?", (cid,))
    return {"ok": True}


class NovaMensagem(BaseModel):
    texto: str = Field(min_length=1, max_length=MAX_MENSAGEM)
    conversa_id: int | None = None


def _historico_api(cid: int) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT papel, texto, conteudo_api, oculta FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT ?",
        (cid, HISTORICO_MAX_MSGS)).fetchall()[::-1]
    # A janela tem que começar por uma mensagem que a pessoa escreveu (não por
    # um resultado de ferramenta, que precisa vir colado no pedido dele).
    while rows and (rows[0]["papel"] != "user" or rows[0]["oculta"]):
        rows = rows[1:]
    return [{"role": r["papel"],
             "content": json.loads(r["conteudo_api"]) if r["conteudo_api"] else r["texto"]} for r in rows]


def _sse(evento: str, dados: Any) -> str:
    return f"event: {evento}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"


def _cobrar(uid: int, custo: int, motivo: str) -> int:
    with transacao() as con:
        con.execute("UPDATE usuarios SET creditos = CASE WHEN creditos > ? THEN creditos - ? ELSE 0 END "
                    "WHERE id = ?", (custo, custo, uid))
        con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                    (uid, -custo, motivo, agora()))
        return con.execute("SELECT creditos FROM usuarios WHERE id = ?", (uid,)).fetchone()["creditos"]


def _custo(final: Resposta) -> int:
    return max(1, math.ceil(custo_usd(final.modelo, final.tokens_entrada, final.tokens_saida) * CREDITOS_POR_DOLAR))


MAX_RODADAS = 6  # quantas idas e vindas com ferramentas uma mensagem pode ter


@app.post("/api/mensagens")
def enviar_mensagem(m: NovaMensagem, u: Any = Depends(usuario_atual)) -> StreamingResponse:
    from .ia import FERRAMENTAS
    from .jarvis import EntradaInvalida, executar_ferramenta

    if u["creditos"] <= 0:
        raise HTTPException(402, "Seus créditos acabaram. Recarregue para continuar conversando.")
    texto = m.texto.strip()
    with transacao() as con:
        if m.conversa_id is None:
            titulo = texto.splitlines()[0][:60] or "Nova conversa"
            cid = con.inserir("INSERT INTO conversas (usuario_id, titulo, criado_em, atualizado_em) "
                              "VALUES (?, ?, ?, ?)", (u["id"], titulo, agora(), agora()))
        else:
            cid = _conversa_do_usuario(m.conversa_id, u["id"])["id"]
        con.execute("INSERT INTO mensagens (conversa_id, papel, texto, criado_em) VALUES (?, 'user', ?, ?)",
                    (cid, texto, agora()))
    historico = _historico_api(cid)
    motor = ia()
    modelo = modelo_do_plano(u["plano"])

    def guardar(papel: str, visivel: str, conteudo: Any, oculta: int) -> None:
        with transacao() as con:
            con.execute("INSERT INTO mensagens (conversa_id, papel, texto, conteudo_api, oculta, criado_em) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (cid, papel, visivel, json.dumps(conteudo, ensure_ascii=False), oculta, agora()))

    def gerar() -> Iterator[str]:
        yield _sse("conversa", {"id": cid})
        custo_total = 0
        ja_escreveu = False
        for _rodada in range(MAX_RODADAS):
            final = Resposta()
            try:
                separar = ja_escreveu  # quebra de linha só entre trechos de texto
                for pedaco in motor.responder(historico, final, modelo, FERRAMENTAS):
                    if separar:
                        yield _sse("texto", "\n\n")
                        separar = False
                    ja_escreveu = True
                    yield _sse("texto", pedaco)
            except Exception as exc:  # noqa: BLE001 - a pessoa precisa ver algo amigável
                # Vai para os logs (Vercel → Logs); a pessoa vê só a mensagem amigável.
                log.exception("Falha ao chamar a IA (conversa %s, modelo %s): %s",
                              cid, final.modelo, type(exc).__name__)
                yield _sse("erro", "A IA não respondeu agora. Tente de novo em instantes.")
                break
            custo_total += _custo(final)
            if final.recusada and not final.texto:
                final.texto = "Não posso ajudar com esse pedido. Que tal tentar de outro jeito?"
                yield _sse("texto", final.texto)
            guardar("assistant", final.texto, final.conteudo, 0 if final.texto else 1)
            historico.append({"role": "assistant", "content": final.conteudo})
            # Só executa ferramentas de uma resposta que terminou pedindo por elas:
            # cortada (max_tokens) ou recusada, a entrada pode estar incompleta.
            if final.stop_reason != "tool_use" or not final.chamadas:
                break
            resultados = []
            for ch in final.chamadas:
                try:
                    texto_res, evento = executar_ferramenta(u["id"], ch["name"], ch["input"])
                    resultados.append({"type": "tool_result", "tool_use_id": ch["id"], "content": texto_res})
                    if evento and "nota" in evento:
                        yield _sse("nota", evento)
                    elif evento:
                        yield _sse("tarefa", evento)
                except EntradaInvalida as exc:
                    resultados.append({"type": "tool_result", "tool_use_id": ch["id"], "is_error": True,
                                       "content": json.dumps({"INVALID_INPUT": str(exc)}, ensure_ascii=False)})
            guardar("user", "", resultados, 1)
            historico.append({"role": "user", "content": resultados})
        saldo = _cobrar(u["id"], custo_total, f"conversa {cid}") if custo_total else u["creditos"]
        with transacao() as con:
            con.execute("UPDATE conversas SET atualizado_em = ? WHERE id = ?", (agora(), cid))
        yield _sse("fim", {"custo": custo_total, "creditos": saldo})

    return StreamingResponse(gerar(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------- sugestões ---

@app.get("/api/sugestoes")
def ver_sugestoes(u: Any = Depends(usuario_atual)) -> dict[str, Any]:
    r = db().execute("SELECT conteudo, gerado_em FROM sugestoes WHERE usuario_id = ?", (u["id"],)).fetchone()
    return {"sugestoes": json.loads(r["conteudo"]) if r else [], "gerado_em": r["gerado_em"] if r else None}


@app.post("/api/sugestoes/gerar")
def gerar_sugestoes(u: Any = Depends(usuario_atual)) -> dict[str, Any]:
    from .jarvis import SCHEMA_SUGESTOES, guardar_sugestoes, prompt_sugestoes, validar_sugestoes

    if u["creditos"] <= 0:
        raise HTTPException(402, "Seus créditos acabaram.")
    atual = db().execute("SELECT gerado_em FROM sugestoes WHERE usuario_id = ?", (u["id"],)).fetchone()
    if atual and (datetime.now(timezone.utc) - datetime.fromisoformat(atual["gerado_em"])).total_seconds() < 600:
        raise HTTPException(429, "As sugestões foram atualizadas há pouco. Tente de novo em alguns minutos.")
    try:
        dados, final = ia().gerar_json(prompt_sugestoes(u["id"]), SCHEMA_SUGESTOES, modelo_do_plano(u["plano"]))
    except Exception as exc:  # noqa: BLE001
        log.exception("Falha ao gerar sugestões: %s", type(exc).__name__)
        raise HTTPException(503, "Não consegui pensar em sugestões agora. Tente de novo em instantes.")
    sugestoes = validar_sugestoes(dados)
    guardar_sugestoes(u["id"], sugestoes)
    saldo = _cobrar(u["id"], _custo(final), "sugestões do parque")
    return {"sugestoes": sugestoes, "gerado_em": agora(), "creditos": saldo}


# ------------------------------------------------------------------ planos ---

PLANOS = [
    {"id": "gratis", "nome": "Grátis", "preco_brl": 0, "creditos": CREDITOS_INICIAIS, "maquinas": 1,
     "descricao": "Para conhecer o Jarvis.",
     "itens": ["Chat com o Jarvis", "1 máquina conectada", "Cérebro com memória", "300 créditos para começar"]},
    # Créditos calibrados para a IA custar no máximo ~35% do preço (ver docs/PRODUTO.md).
    {"id": "basico", "nome": "Básico", "preco_brl": 29.90, "creditos": 2000, "maquinas": 3,
     "descricao": "Seu assistente no dia a dia.",
     "itens": ["IA mais inteligente (Sonnet)", "Até 3 máquinas", "2.000 créditos por mês", "Rotinas manuais"]},
    {"id": "pro", "nome": "Pro", "preco_brl": 59.90, "creditos": 4500, "maquinas": None, "destaque": True,
     "descricao": "O parque inteiro trabalhando por você.",
     "itens": ["IA mais inteligente (Sonnet)", "Máquinas ilimitadas", "4.500 créditos por mês",
               "Rotinas diárias automáticas", "Sugestões do parque todo dia"]},
]


@app.get("/api/planos")
def planos() -> list[dict[str, Any]]:
    # A cobrança (Mercado Pago / Stripe) entra na próxima etapa; ver docs/PRODUTO.md.
    return PLANOS


@app.post("/api/planos/{plano}/interesse")
def registrar_interesse(plano: str, u: Any = Depends(usuario_atual)) -> dict[str, bool]:
    """Enquanto o pagamento não está ligado, guarda quem quis assinar (lista de espera)."""
    if plano not in {p["id"] for p in PLANOS} - {"gratis"}:
        raise HTTPException(404, "Plano não encontrado.")
    with transacao() as con:
        con.execute("INSERT INTO interesses (usuario_id, plano, criado_em) VALUES (?, ?, ?)", (u["id"], plano, agora()))
    return {"ok": True}


@app.get("/api/extrato")
def extrato(u: Any = Depends(usuario_atual)) -> list[dict[str, Any]]:
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


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse(BASE / "web" / "icones" / "favicon.ico")


@app.get("/icones/{nome}")
def icone(nome: str) -> FileResponse:
    caminho = (BASE / "web" / "icones" / nome).resolve()
    if caminho.parent != (BASE / "web" / "icones").resolve() or not caminho.is_file():
        raise HTTPException(404, "não encontrado")
    return FileResponse(caminho, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/agente.py")
def baixar_agente() -> FileResponse:
    """O agente que a pessoa baixa para conectar um computador ao parque."""
    return FileResponse(BASE.parent / "agente" / "agente.py", media_type="text/x-python",
                        filename="agente.py")


from . import parque  # noqa: E402  (precisa das funções acima já definidas)

app.include_router(parque.router)
