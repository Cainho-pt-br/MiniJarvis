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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .banco import Banco, ErroDuplicado, url_postgres
from .ia import (
    MAX_TOKENS,
    MAX_TOKENS_GRATIS,
    MODELO_HAIKU,
    MODELO_OPUS,
    MODELO_SONNET,
    Resposta,
    criar_ia,
    custo_usd,
    max_tokens_do_plano,
    mensagens_estimadas,
    modelo_do_plano,
)
from .tarefas import TAREFAS

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NUVEM_DB", BASE / "nuvem.db"))
CREDITOS_INICIAIS = int(os.environ.get("CREDITOS_INICIAIS", "120"))
CREDITOS_POR_DOLAR = int(os.environ.get("CREDITOS_POR_DOLAR", "1000"))  # 1 crédito = US$ 0,001 de IA
SESSAO_DIAS = 30
COOKIE = "mj_sessao"
COOKIE_SEGURO = os.environ.get("COOKIE_SEGURO", "1") != "0"  # desligue só em http local
MAX_MENSAGEM = 20_000
HISTORICO_MAX_MSGS = 40

log = logging.getLogger("minijarvis")
app = FastAPI(title="MiniJarvis Nuvem", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def cabecalhos_de_seguranca(request: Request, call_next: Any) -> Any:
    resp = await call_next(request)
    # Ninguém pode embutir o painel noutro site (evita enganar o clique em "Aprovar").
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if COOKIE_SEGURO:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp
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


# ----------------------------------------------------------------- limites ---

def ip_cliente(request: Request) -> str:
    """IP de quem chamou. Na Vercel, o cabeçalho é posto pela própria Vercel e
    não dá para falsificar; fora dela só vale o endereço da conexão."""
    if os.environ.get("VERCEL"):
        ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if ip.strip():
            return ip.strip()
    return request.client.host if request.client else "?"


def limitar(chave: str, maximo: int, janela_s: int, msg: str = "Muitas tentativas. Espere alguns minutos.") -> None:
    """Conta tentativas no banco (vale entre instâncias). Estoura com 429."""
    desde = (datetime.now(timezone.utc) - timedelta(seconds=janela_s)).isoformat(timespec="seconds")
    n = db().execute("SELECT COUNT(*) AS n FROM limites WHERE chave = ? AND criado_em > ?",
                     (chave, desde)).fetchone()["n"]
    if n >= maximo:
        raise HTTPException(429, msg)
    with transacao() as con:
        con.execute("INSERT INTO limites (chave, criado_em) VALUES (?, ?)", (chave, agora()))
        if secrets.randbelow(50) == 0:  # faxina de vez em quando
            velho = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
            con.execute("DELETE FROM limites WHERE criado_em < ?", (velho,))


def limitar_tentativas(chave: str, maximo: int = 10, janela: float = 300) -> None:
    limitar(chave, maximo, int(janela))


# ------------------------------------------------------------------ contas ---

class Cadastro(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=200)
    senha: str = Field(min_length=8, max_length=200)
    aceito: bool = False  # termos de uso e política de privacidade
    indicacao: str = Field(default="", max_length=20)  # código de quem indicou (?ref=)


class Entrada(BaseModel):
    email: str = Field(max_length=200)
    senha: str = Field(max_length=200)


_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Provedores de e-mail descartável mais usados para criar contas em massa.
DESCARTAVEIS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com", "10minutemail.com",
    "10minutemail.net", "tempmail.com", "temp-mail.org", "tempmail.net", "tempmailo.com", "yopmail.com",
    "yopmail.net", "getnada.com", "nada.email", "trashmail.com", "dispostable.com", "maildrop.cc",
    "mailnesia.com", "mohmal.com", "emailondeck.com", "fakeinbox.com", "throwawaymail.com",
    "mintemail.com", "mytemp.email", "tempinbox.com", "spamgourmet.com", "burnermail.io", "inboxkitten.com",
    "minuteinbox.com", "tmail.ws", "tmpmail.org", "tmpmail.net", "emailfake.com", "crazymailing.com",
    "mail.tm", "1secmail.com", "1secmail.net", "1secmail.org", "dropmail.me", "linshiyouxiang.net",
}


def email_canonico(email: str) -> str:
    """O mesmo e-mail escrito de jeitos diferentes vira um só (caio+1@gmail.com,
    c.a.i.o@gmail.com e caio@gmail.com são a mesma caixa)."""
    local, _, dominio = email.lower().partition("@")
    local = local.split("+", 1)[0]
    if dominio in ("gmail.com", "googlemail.com"):
        local, dominio = local.replace(".", ""), "gmail.com"
    return f"{local}@{dominio}"


def _publico(u: Any) -> dict[str, Any]:
    return {"id": u["id"], "nome": u["nome"], "email": u["email"], "creditos": u["creditos"],
            "plano": u["plano"], "plano_ate": u["plano_ate"], "assinatura": bool(u["assinatura_id"]),
            "email_verificado": bool(u["email_verificado"]), "admin": eh_admin(u),
            "codigo_indicacao": u["codigo_indicacao"]}


def eh_admin(u: Any) -> bool:
    return bool(u["email_verificado"]) and u["email"].lower() in _admins()


def _admins() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


CODIGO_MIN = 15
COOKIE_DISP = "mj_disp"


def _criar_codigo(uid: int, finalidade: str) -> tuple[int, str]:
    codigo = f"{secrets.randbelow(1_000_000):06d}"
    expira = (datetime.now(timezone.utc) + timedelta(minutes=CODIGO_MIN)).isoformat(timespec="seconds")
    with transacao() as con:
        con.execute("DELETE FROM codigos WHERE usuario_id = ? AND finalidade = ?", (uid, finalidade))
        cid = con.inserir("INSERT INTO codigos (usuario_id, finalidade, codigo_hash, expira_em, criado_em) "
                          "VALUES (?, ?, ?, ?, ?)", (uid, finalidade, _hash_token(codigo), expira, agora()))
    return cid, codigo


def _conferir_codigo(uid: int, finalidade: str, codigo: str, cid: int | None = None) -> None:
    sql = "SELECT * FROM codigos WHERE usuario_id = ? AND finalidade = ?"
    args: list[Any] = [uid, finalidade]
    if cid is not None:
        sql += " AND id = ?"
        args.append(cid)
    c = db().execute(sql, args).fetchone()
    if c is None or c["expira_em"] < agora():
        raise HTTPException(400, "Código expirado. Peça um novo.")
    if c["tentativas"] >= 5:
        raise HTTPException(429, "Tentativas demais com este código. Peça um novo.")
    if not hmac.compare_digest(c["codigo_hash"], _hash_token(codigo.strip())):
        with transacao() as con:
            con.execute("UPDATE codigos SET tentativas = tentativas + 1 WHERE id = ?", (c["id"],))
        raise HTTPException(400, "Código incorreto.")
    with transacao() as con:
        con.execute("DELETE FROM codigos WHERE id = ?", (c["id"],))


def _mandar_codigo(u: Any, finalidade: str) -> int:
    from . import correio

    cid, codigo = _criar_codigo(u["id"], finalidade)
    motivo = {"verificar": "confirmar", "entrar": "entrar", "senha": "senha"}[finalidade]
    try:
        correio.enviar_codigo(u["email"], u["nome"], codigo, motivo)
    except Exception as exc:  # noqa: BLE001
        log.exception("Falha ao enviar e-mail: %s", type(exc).__name__)
        operacao.incidente("email")
        raise HTTPException(503, "Não consegui enviar o e-mail agora. Tente de novo em instantes.")
    return cid


def confiar_dispositivo(resp: Response, uid: int) -> None:
    token = secrets.token_urlsafe(32)
    with transacao() as con:
        con.execute("INSERT INTO dispositivos VALUES (?, ?, ?)", (_hash_token(token), uid, agora()))
    resp.set_cookie(COOKIE_DISP, token, max_age=365 * 86400, httponly=True, secure=COOKIE_SEGURO, samesite="lax")


def dispositivo_confiavel(request: Request, uid: int) -> bool:
    token = request.cookies.get(COOKIE_DISP)
    return bool(token) and db().execute("SELECT 1 FROM dispositivos WHERE token_hash = ? AND usuario_id = ?",
                                        (_hash_token(token), uid)).fetchone() is not None


def usuario_verificado(u: Any = Depends(usuario_atual)) -> Any:
    if not u["email_verificado"]:
        raise HTTPException(403, "Confirme seu e-mail para continuar.")
    return u


def _dar_boas_vindas(uid: int) -> None:
    with transacao() as con:
        con.execute("UPDATE usuarios SET email_verificado = 1, creditos = creditos + ? WHERE id = ?",
                    (CREDITOS_INICIAIS, uid))
        con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                    (uid, CREDITOS_INICIAIS, "boas-vindas", agora()))
    operacao.primeira_vez("email_confirmado", uid)
    crescimento.premiar_indicacao(uid)


@app.post("/api/cadastro")
def cadastrar(c: Cadastro, request: Request, resp: Response) -> dict[str, Any]:
    from . import correio

    ip = ip_cliente(request)
    limitar(f"cad-h:{ip}", 3, 3600, "Muitas contas criadas desta rede. Tente de novo mais tarde.")
    limitar(f"cad-d:{ip}", 8, 86400, "Muitas contas criadas desta rede hoje. Tente amanhã.")
    email = c.email.strip().lower()
    if not _RE_EMAIL.match(email):
        raise HTTPException(400, "E-mail inválido.")
    if not c.aceito:
        raise HTTPException(400, "Para criar a conta, aceite os termos de uso e a política de privacidade.")
    canon = email_canonico(email)
    if canon.split("@")[1] in DESCARTAVEIS:
        raise HTTPException(400, "Use um e-mail de verdade (endereços temporários não são aceitos).")
    if db().execute("SELECT 1 FROM usuarios WHERE email_canonico = ? OR email = ?", (canon, email)).fetchone():
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    precisa_confirmar = correio.configurado()
    if not precisa_confirmar and email in _admins():
        # Sem confirmação por e-mail, qualquer um poderia se cadastrar com o
        # e-mail do dono e virar administrador.
        raise HTTPException(403, "Este e-mail só pode ser cadastrado com a confirmação por e-mail ligada.")
    indicador = crescimento.quem_indicou(c.indicacao)
    try:
        with transacao() as con:
            uid = con.inserir(
                "INSERT INTO usuarios (email, email_canonico, nome, senha_hash, creditos, email_verificado, "
                "aceitou_termos_em, codigo_indicacao, indicado_por, criado_em) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
                (email, canon, c.nome.strip(), gerar_hash(c.senha), agora(), crescimento.gerar_codigo_indicacao(),
                 indicador, agora()))
    except ErroDuplicado:
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    parque._semear_cerebro(uid)  # o cérebro já nasce com as notas de boas-vindas
    operacao.registrar("cadastro", uid)
    u = db().execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
    if precisa_confirmar:
        _mandar_codigo(u, "verificar")
    else:
        log.warning("SMTP não configurado: conta %s criada sem confirmação de e-mail", uid)
        _dar_boas_vindas(uid)
    abrir_sessao(resp, uid)
    return _publico(db().execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone())


class Codigo(BaseModel):
    codigo: str = Field(min_length=4, max_length=12)


@app.post("/api/verificar")
def verificar_email(c: Codigo, resp: Response, u: Any = Depends(usuario_atual)) -> dict[str, Any]:
    if u["email_verificado"]:
        return _publico(u)
    _conferir_codigo(u["id"], "verificar", c.codigo)
    _dar_boas_vindas(u["id"])
    confiar_dispositivo(resp, u["id"])
    return _publico(db().execute("SELECT * FROM usuarios WHERE id = ?", (u["id"],)).fetchone())


@app.post("/api/verificar/reenviar")
def reenviar_codigo(u: Any = Depends(usuario_atual)) -> dict[str, bool]:
    if u["email_verificado"]:
        return {"ok": True}
    limitar(f"reenv-m:{u['id']}", 1, 60, "Espere um minuto para pedir outro código.")
    limitar(f"reenv-h:{u['id']}", 6, 3600)
    _mandar_codigo(u, "verificar")
    return {"ok": True}


def _mascarar(email: str) -> str:
    local, _, dominio = email.partition("@")
    return local[:2] + "•" * max(1, len(local) - 2) + "@" + dominio


@app.post("/api/entrar")
def entrar(e: Entrada, request: Request, resp: Response) -> dict[str, Any]:
    from . import correio

    email = e.email.strip().lower()
    limitar(f"login-ip:{ip_cliente(request)}", 15, 900)
    limitar(f"login-conta:{email_canonico(email)}", 8, 900,
            "Muitas tentativas nesta conta. Espere 15 minutos.")
    u = db().execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
    if u is None or not conferir_senha(e.senha, u["senha_hash"]):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    # Navegador novo numa conta confirmada: pede o código mandado por e-mail
    # (verificação em duas etapas sem precisar de aplicativo).
    if u["email_verificado"] and correio.configurado() and not dispositivo_confiavel(request, u["id"]):
        cid = _mandar_codigo(u, "entrar")
        return {"precisa_codigo": True, "desafio": cid, "email": _mascarar(u["email"])}
    abrir_sessao(resp, u["id"])
    return _publico(u)


class CodigoEntrada(BaseModel):
    desafio: int
    codigo: str = Field(min_length=4, max_length=12)


@app.post("/api/entrar/codigo")
def entrar_com_codigo(c: CodigoEntrada, request: Request, resp: Response) -> dict[str, Any]:
    limitar(f"login-cod:{ip_cliente(request)}", 20, 900)
    r = db().execute("SELECT usuario_id FROM codigos WHERE id = ? AND finalidade = 'entrar'", (c.desafio,)).fetchone()
    if r is None:
        raise HTTPException(400, "Código expirado. Entre de novo.")
    _conferir_codigo(r["usuario_id"], "entrar", c.codigo, c.desafio)
    confiar_dispositivo(resp, r["usuario_id"])
    abrir_sessao(resp, r["usuario_id"])
    return _publico(db().execute("SELECT * FROM usuarios WHERE id = ?", (r["usuario_id"],)).fetchone())


class EsqueciSenha(BaseModel):
    email: str = Field(max_length=200)


@app.post("/api/senha/esquecer")
def esquecer_senha(e: EsqueciSenha, request: Request) -> dict[str, bool]:
    """Manda um código por e-mail para redefinir a senha. Responde igual
    exista a conta ou não (não revela quem tem conta)."""
    from . import correio

    if not correio.configurado():
        raise HTTPException(503, "A recuperação de senha ainda não está disponível. Fale com o suporte.")
    limitar(f"esq-ip:{ip_cliente(request)}", 10, 3600)
    email = e.email.strip().lower()
    u = db().execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
    if u is not None:
        limitar(f"esq-conta:{u['id']}", 3, 3600, "Já mandamos códigos demais para este e-mail. Espere uma hora.")
        _mandar_codigo(u, "senha")
    return {"ok": True}


class NovaSenha(BaseModel):
    email: str = Field(max_length=200)
    codigo: str = Field(min_length=4, max_length=12)
    senha: str = Field(min_length=8, max_length=200)


@app.post("/api/senha/redefinir")
def redefinir_senha(n: NovaSenha, request: Request, resp: Response) -> dict[str, Any]:
    limitar(f"redef-ip:{ip_cliente(request)}", 20, 900)
    u = db().execute("SELECT * FROM usuarios WHERE email = ?", (n.email.strip().lower(),)).fetchone()
    if u is None:
        raise HTTPException(400, "Código incorreto.")
    _conferir_codigo(u["id"], "senha", n.codigo)
    with transacao() as con:
        # Quem provou que tem o e-mail também confirmou a conta.
        con.execute("UPDATE usuarios SET senha_hash = ?, email_verificado = 1 WHERE id = ?",
                    (gerar_hash(n.senha), u["id"]))
        con.execute("DELETE FROM sessoes WHERE usuario_id = ?", (u["id"],))  # derruba quem estiver usando
    if not u["email_verificado"]:
        _dar_boas_vindas(u["id"])
    confiar_dispositivo(resp, u["id"])
    abrir_sessao(resp, u["id"])
    return _publico(db().execute("SELECT * FROM usuarios WHERE id = ?", (u["id"],)).fetchone())


@app.post("/api/sair")
def sair(request: Request, resp: Response) -> dict[str, bool]:
    token = request.cookies.get(COOKIE)
    if token:
        with transacao() as con:
            con.execute("DELETE FROM sessoes WHERE token_hash = ?", (_hash_token(token),))
    resp.delete_cookie(COOKIE)
    return {"ok": True}


@app.post("/api/sair-de-todos")
def sair_de_todos(resp: Response, u: Any = Depends(usuario_atual)) -> dict[str, bool]:
    """Encerra todas as sessões e esquece os navegadores confiáveis."""
    with transacao() as con:
        con.execute("DELETE FROM sessoes WHERE usuario_id = ?", (u["id"],))
        con.execute("DELETE FROM dispositivos WHERE usuario_id = ?", (u["id"],))
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(COOKIE_DISP)
    return {"ok": True}


@app.get("/api/eu")
def eu(u: Any = Depends(usuario_atual)) -> dict[str, Any]:
    return _publico(u)


class ApagarConta(BaseModel):
    senha: str = Field(max_length=200)


@app.post("/api/eu/apagar")
def apagar_conta(a: ApagarConta, resp: Response, u: Any = Depends(usuario_atual)) -> dict[str, bool]:
    """Direito de exclusão (LGPD): apaga a conta e tudo ligado a ela. Pede a
    senha (não dá para desfazer) e antes cancela a assinatura, para a pessoa
    não continuar sendo cobrada por uma conta que não existe mais."""
    limitar(f"apagar:{u['id']}", 5, 900)
    if not conferir_senha(a.senha, u["senha_hash"]):
        raise HTTPException(403, "Senha incorreta.")
    pagamento.parar_cobranca(u)
    with transacao() as con:
        con.execute("DELETE FROM usuarios WHERE id = ?", (u["id"],))
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(COOKIE_DISP)
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
def enviar_mensagem(m: NovaMensagem, u: Any = Depends(usuario_verificado)) -> StreamingResponse:
    from .ia import FERRAMENTAS
    from .jarvis import EntradaInvalida, executar_ferramenta

    if u["creditos"] <= 0:
        raise HTTPException(402, "Seus créditos acabaram. Recarregue para continuar conversando.")
    operacao.conferir_teto()
    operacao.primeira_vez("primeira_mensagem", u["id"])
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
    max_tokens = max_tokens_do_plano(u["plano"])

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
                for pedaco in motor.responder(historico, final, modelo, FERRAMENTAS, max_tokens):
                    if separar:
                        yield _sse("texto", "\n\n")
                        separar = False
                    ja_escreveu = True
                    yield _sse("texto", pedaco)
            except Exception as exc:  # noqa: BLE001 - a pessoa precisa ver algo amigável
                # Vai para os logs (Vercel → Logs); a pessoa vê só a mensagem amigável.
                log.exception("Falha ao chamar a IA (conversa %s, modelo %s): %s",
                              cid, final.modelo, type(exc).__name__)
                operacao.incidente("ia")
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
            if custo_total >= u["creditos"]:  # o saldo acabou no meio: não começa outra rodada
                yield _sse("texto", "\n\n(Seus créditos acabaram; recarregue para eu continuar.)")
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
def gerar_sugestoes(u: Any = Depends(usuario_verificado)) -> dict[str, Any]:
    from .jarvis import atualizar_sugestoes

    incluido = bool(info_plano(u["plano"]).get("sugestoes_auto"))  # nos planos com sugestões automáticas não gasta créditos
    if not incluido and u["creditos"] <= 0:
        raise HTTPException(402, "Seus créditos acabaram.")
    atual = db().execute("SELECT gerado_em FROM sugestoes WHERE usuario_id = ?", (u["id"],)).fetchone()
    if atual and (datetime.now(timezone.utc) - datetime.fromisoformat(atual["gerado_em"])).total_seconds() < 600:
        raise HTTPException(429, "As sugestões foram atualizadas há pouco. Tente de novo em alguns minutos.")
    sugestoes, saldo = atualizar_sugestoes(u, cobrar=not incluido)
    return {"sugestoes": sugestoes, "gerado_em": agora(), "creditos": saldo}


# ------------------------------------------------------------------ planos ---

PLANOS = [
    {"id": "gratis", "nome": "Grátis", "preco_brl": 0, "creditos": CREDITOS_INICIAIS, "maquinas": 1,
     "modelo": MODELO_HAIKU, "modelo_sugestoes": MODELO_HAIKU, "max_tokens": MAX_TOKENS_GRATIS,
     "rotinas_diarias": False, "sugestoes_auto": False,
     "mensagens_estimadas": mensagens_estimadas(CREDITOS_INICIAIS, MODELO_HAIKU),
     "descricao": "Para conhecer o Jarvis.",
     "itens": ["Chat com o Jarvis", "Conecte 1 computador", "Cérebro com memória",
               f"{CREDITOS_INICIAIS} créditos para começar"]},
    # Créditos calibrados para ~120 mensagens/mês e IA ≤ 45% do preço (ver docs/ECONOMIA.md).
    {"id": "basico", "nome": "Básico", "preco_brl": 29.90, "creditos": 240, "maquinas": 2,
     "modelo": MODELO_HAIKU, "modelo_sugestoes": MODELO_HAIKU, "max_tokens": MAX_TOKENS,
     "rotinas_diarias": False, "sugestoes_auto": False,
     "mensagens_estimadas": mensagens_estimadas(240, MODELO_HAIKU),
     "descricao": "Seu assistente no dia a dia.",
     "itens": ["Chat e tarefas no computador", "Conecte até 2 computadores", "240 créditos por mês",
               "Rotinas manuais"]},
    {"id": "pro", "nome": "Pro", "preco_brl": 59.90, "creditos": 4500, "maquinas": 4, "destaque": True,
     "modelo": MODELO_SONNET, "modelo_sugestoes": MODELO_HAIKU, "max_tokens": MAX_TOKENS,
     "rotinas_diarias": True, "sugestoes_auto": True,
     "mensagens_estimadas": mensagens_estimadas(4500, MODELO_SONNET),
     "descricao": "O parque inteiro trabalhando por você.",
     "itens": ["Jarvis mais esperto para pedidos difíceis", "Conecte até 4 computadores",
               "4.500 créditos por mês", "Rotinas diárias automáticas", "Sugestões do parque todo dia"]},
    {"id": "turbo", "nome": "Turbo", "preco_brl": 119.90, "creditos": 8000, "maquinas": 10,
     "modelo": MODELO_OPUS, "modelo_sugestoes": MODELO_HAIKU, "max_tokens": MAX_TOKENS,
     "rotinas_diarias": True, "sugestoes_auto": True,
     "mensagens_estimadas": mensagens_estimadas(8000, MODELO_OPUS),
     "descricao": "Para quem tem muitos computadores ou quer o máximo.",
     "itens": ["O Jarvis mais inteligente", "Conecte até 10 computadores", "8.000 créditos por mês",
               "Rotinas diárias automáticas", "Sugestões do parque todo dia"]},
]


def info_plano(plano: str) -> dict[str, Any]:
    return next((p for p in PLANOS if p["id"] == plano), PLANOS[0])


@app.get("/api/planos")
def planos() -> list[dict[str, Any]]:
    return PLANOS


@app.post("/api/planos/{plano}/interesse")
def registrar_interesse(plano: str, u: Any = Depends(usuario_verificado)) -> dict[str, bool]:
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


# ------------------------------------------------------------------- admin ---

def usuario_admin(u: Any = Depends(usuario_atual)) -> Any:
    if not eh_admin(u):
        raise HTTPException(404, "não encontrado")
    return u


@app.get("/api/admin/resumo")
def admin_resumo(_: Any = Depends(usuario_admin)) -> dict[str, Any]:
    us = db().execute(
        "SELECT u.id, u.email, u.nome, u.plano, u.creditos, u.email_verificado, u.criado_em, "
        "(SELECT COUNT(*) FROM agentes a WHERE a.usuario_id = u.id) AS maquinas, "
        "(SELECT COUNT(*) FROM conversas c WHERE c.usuario_id = u.id) AS conversas "
        "FROM usuarios u ORDER BY u.id DESC LIMIT 500").fetchall()
    interesses = db().execute(
        "SELECT u.email, i.plano, i.criado_em FROM interesses i JOIN usuarios u ON u.id = i.usuario_id "
        "ORDER BY i.id DESC LIMIT 200").fetchall()
    return {"usuarios": [dict(r) for r in us], "interesses": [dict(r) for r in interesses],
            "relatorio": operacao.relatorio()}


@app.post("/api/admin/relatorio")
def admin_mandar_relatorio(_: Any = Depends(usuario_admin)) -> dict[str, bool]:
    return {"enviado": operacao.mandar_relatorio()}


class TrocaPlano(BaseModel):
    plano: str
    meses: int = Field(default=1, ge=0, le=24)  # 0: só troca o rótulo, sem créditos nem prazo
    creditos: int = Field(default=0, ge=0, le=100_000)


@app.post("/api/admin/usuarios/{uid}/plano")
def admin_trocar_plano(uid: int, t: TrocaPlano, _: Any = Depends(usuario_admin)) -> dict[str, bool]:
    from . import cobranca

    if t.plano not in {p["id"] for p in PLANOS}:
        raise HTTPException(400, "Plano inválido.")
    if t.plano == "gratis":
        with transacao() as con:
            con.execute("UPDATE usuarios SET plano = 'gratis', plano_ate = NULL WHERE id = ?", (uid,))
    elif t.meses:
        cobranca.ativar_plano(uid, t.plano, f"plano {t.plano} (cortesia)", t.meses)
    if t.creditos:
        with transacao() as con:
            con.execute("UPDATE usuarios SET creditos = creditos + ? WHERE id = ?", (t.creditos, uid))
            con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                        (uid, t.creditos, "créditos de cortesia", agora()))
    return {"ok": True}


# -------------------------------------------------------------------- web ---

@app.get("/saude")
def saude() -> dict[str, str]:
    return {"status": "ok"}


_INDEX: list[str] = []


@app.get("/")
def pagina(request: Request) -> Any:
    """A página inicial, com o endereço do site e a imagem de compartilhamento
    (o WhatsApp e as redes só aceitam endereços completos nessas tags)."""
    from fastapi.responses import HTMLResponse

    from .pagamento import site_url

    if not _INDEX:
        _INDEX.append((BASE / "web" / "index.html").read_text(encoding="utf-8"))
    base = site_url(request)
    seo = (f'<link rel="canonical" href="{base}/">'
           f'<meta property="og:type" content="website"><meta property="og:url" content="{base}/">'
           f'<meta property="og:image" content="{base}/icones/icone-512.png">'
           f'<meta property="og:locale" content="pt_BR"><meta name="twitter:card" content="summary">')
    return HTMLResponse(_INDEX[0].replace("<!--SEO-->", seo), headers={"Cache-Control": "public, max-age=300"})


@app.get("/termos")
def termos() -> Any:
    return _pagina_legal("termos.html")


@app.get("/privacidade")
def privacidade() -> Any:
    return _pagina_legal("privacidade.html")


def _pagina_legal(nome: str) -> Any:
    from fastapi.responses import HTMLResponse

    texto = (BASE / "web" / nome).read_text(encoding="utf-8")
    texto = texto.replace("{{CONTATO}}", os.environ.get("EMAIL_CONTATO") or os.environ.get("EMAIL_REMETENTE", "")
                          .split("<")[-1].rstrip(">") or "o e-mail de contato informado no site")
    return HTMLResponse(texto.replace("{{RESPONSAVEL}}", os.environ.get("RESPONSAVEL_LEGAL", "o responsável pelo Jarvis")))


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


from . import crescimento, instaladores, operacao, pagamento, parque  # noqa: E402  (precisam das funções acima)

app.include_router(parque.router)
app.include_router(crescimento.router)
app.include_router(instaladores.router)
app.include_router(pagamento.router)
