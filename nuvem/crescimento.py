"""O que faz o Jarvis crescer sozinho: indicação, link compartilhável e guias.

- **Indicação:** cada conta tem um código; quem entra pelo link ganha créditos
  extras ao confirmar o e-mail, e quem indicou também (com teto, para não
  valer a pena criar contas falsas).
- **Link compartilhável:** a pessoa publica uma conversa numa página pública
  ("olha o que o Jarvis fez") com um botão "experimente grátis". Dá para
  despublicar a qualquer momento.
- **Guias:** páginas de conteúdo (`nuvem/web/guias/*.html`) para o Google
  trazer gente que procura "computador lento", "organizar downloads" etc.,
  mais `robots.txt` e `sitemap.xml`.
"""
from __future__ import annotations

import html
import os
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from . import app as nucleo

router = APIRouter()

BONUS_INDICADO = int(os.environ.get("BONUS_INDICADO", "100"))
BONUS_INDICADOR = int(os.environ.get("BONUS_INDICADOR", "100"))
TETO_INDICADOR = int(os.environ.get("TETO_INDICADOR", "1000"))  # créditos por indicações, no total
_ALFABETO = "abcdefghjkmnpqrstuvwxyz23456789"


# ---------------------------------------------------------------- indicação ---

def gerar_codigo_indicacao() -> str:
    return "".join(secrets.choice(_ALFABETO) for _ in range(8))


def codigo_de(uid: int) -> str:
    u = nucleo.db().execute("SELECT codigo_indicacao FROM usuarios WHERE id=?", (uid,)).fetchone()
    if u["codigo_indicacao"]:
        return u["codigo_indicacao"]
    for _ in range(5):  # conta antiga, de antes da indicação existir
        codigo = gerar_codigo_indicacao()
        try:
            with nucleo.transacao() as con:
                con.execute("UPDATE usuarios SET codigo_indicacao=? WHERE id=?", (codigo, uid))
            return codigo
        except nucleo.ErroDuplicado:
            continue
    raise RuntimeError("não consegui gerar um código de indicação")


def quem_indicou(codigo: str | None) -> int | None:
    codigo = (codigo or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9]{8}", codigo):
        return None
    r = nucleo.db().execute("SELECT id FROM usuarios WHERE codigo_indicacao=?", (codigo,)).fetchone()
    return r["id"] if r else None


def premiar_indicacao(uid: int) -> None:
    """Chamado quando a conta nova confirma o e-mail (uma vez por conta)."""
    u = nucleo.db().execute("SELECT indicado_por FROM usuarios WHERE id=?", (uid,)).fetchone()
    ind = u["indicado_por"] if u else None
    if not ind or ind == uid:
        return
    if nucleo.db().execute("SELECT 1 FROM extrato WHERE usuario_id=? AND motivo='indicação: bônus de boas-vindas'",
                           (uid,)).fetchone():
        return
    ganho = nucleo.db().execute("SELECT COALESCE(SUM(delta),0) AS g FROM extrato WHERE usuario_id=? "
                                "AND motivo LIKE ?", (ind, "indicação: você indicou%")).fetchone()["g"]
    bonus_ind = max(0, min(BONUS_INDICADOR, TETO_INDICADOR - int(ganho)))
    with nucleo.transacao() as con:
        con.execute("UPDATE usuarios SET creditos=creditos+? WHERE id=?", (BONUS_INDICADO, uid))
        con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                    (uid, BONUS_INDICADO, "indicação: bônus de boas-vindas", nucleo.agora()))
        if bonus_ind:
            con.execute("UPDATE usuarios SET creditos=creditos+? WHERE id=?", (bonus_ind, ind))
            con.execute("INSERT INTO extrato (usuario_id, delta, motivo, criado_em) VALUES (?, ?, ?, ?)",
                        (ind, bonus_ind, "indicação: você indicou alguém", nucleo.agora()))


@router.get("/api/indicacao")
def minha_indicacao(request: Request, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, Any]:
    from .pagamento import site_url

    codigo = codigo_de(u["id"])
    db = nucleo.db()
    n = db.execute("SELECT COUNT(*) AS n FROM usuarios WHERE indicado_por=? AND email_verificado=1",
                   (u["id"],)).fetchone()["n"]
    ganho = db.execute("SELECT COALESCE(SUM(delta),0) AS g FROM extrato WHERE usuario_id=? "
                       "AND motivo LIKE ?", (u["id"], "indicação: você indicou%")).fetchone()["g"]
    return {"codigo": codigo, "link": f"{site_url(request)}/?ref={codigo}", "indicados": n,
            "creditos_ganhos": int(ganho), "bonus": BONUS_INDICADOR, "bonus_indicado": BONUS_INDICADO,
            "teto": TETO_INDICADOR}


# ------------------------------------------------------- link compartilhável ---

@router.post("/api/conversas/{cid}/compartilhar")
def compartilhar(cid: int, request: Request, u: Any = Depends(nucleo.usuario_verificado)) -> dict[str, str]:
    from .pagamento import site_url

    nucleo._conversa_do_usuario(cid, u["id"])
    r = nucleo.db().execute("SELECT token FROM compartilhamentos WHERE conversa_id=?", (cid,)).fetchone()
    token = r["token"] if r else secrets.token_urlsafe(12)
    if r is None:
        with nucleo.transacao() as con:
            con.execute("INSERT INTO compartilhamentos (token, conversa_id, usuario_id, criado_em) VALUES (?, ?, ?, ?)",
                        (token, cid, u["id"], nucleo.agora()))
    return {"url": f"{site_url(request)}/c/{token}"}


@router.delete("/api/conversas/{cid}/compartilhar")
def despublicar(cid: int, u: Any = Depends(nucleo.usuario_atual)) -> dict[str, bool]:
    with nucleo.transacao() as con:
        con.execute("DELETE FROM compartilhamentos WHERE conversa_id=? AND usuario_id=?", (cid, u["id"]))
    return {"ok": True}


_PAGINA = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} · {sufixo_titulo}</title><meta name="description" content="{descricao}">
<meta property="og:title" content="{titulo} · Jarvis"><meta property="og:description" content="{descricao}">
<meta name="robots" content="noindex"><link rel="icon" href="/favicon.ico">
<style>body{{margin:0;background:#05080f;color:#e8eef8;font:15px/1.6 Inter,system-ui,sans-serif}}main{{max-width:720px;margin:0 auto;padding:32px 16px 80px}}
.topo{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;font-size:14px;color:#9aa8bf}}.topo a{{color:#7ae5df;text-decoration:none}}
h1{{font-size:22px;margin:0 0 20px}}.m{{display:flex;gap:12px;margin:0 0 16px}}.m .q{{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;background:#131c2c;border:1px solid #2a3a57}}
.m.assistant .q{{background:linear-gradient(135deg,#3fd0c9,#8b7bff);color:#04121a;border:0}}.c{{white-space:pre-wrap;word-break:break-word;padding:12px 14px;border-radius:14px;background:#0e1522;border:1px solid #1d2940;flex:1}}
.m.user .c{{background:#131c2c}}.cta{{margin-top:36px;padding:24px;border-radius:16px;text-align:center;background:linear-gradient(120deg,rgba(63,208,201,.12),rgba(139,123,255,.12));border:1px solid #2a3a57}}
.cta a{{display:inline-block;margin-top:12px;padding:12px 22px;border-radius:12px;background:linear-gradient(135deg,#3fd0c9,#8b7bff);color:#04121a;font-weight:700;text-decoration:none}}.cta p{{margin:0;color:#9aa8bf}}</style></head>
<body><main><div class="topo"><a href="/">Jarvis</a><span>{topo_texto}</span></div><h1>{titulo}</h1>{mensagens}
<div class="cta"><b>{cta_titulo}</b><p>{cta_texto}</p><a href="/?ref={ref}">{cta_botao}</a></div></main></body></html>"""


@router.get("/c/{token}")
def ver_compartilhada(token: str, request: Request) -> Response:
    from .i18n import resolver_idioma, t

    idioma = resolver_idioma(request)
    r = nucleo.db().execute(
        "SELECT c.id, c.titulo, s.usuario_id FROM compartilhamentos s JOIN conversas c ON c.id=s.conversa_id "
        "WHERE s.token=?", (token,)).fetchone()
    if r is None:
        return HTMLResponse(f"<h1 style='font-family:sans-serif'>{t('compartilhada_nao_publicada', idioma)}</h1>", 404)
    msgs = nucleo.db().execute("SELECT papel, texto FROM mensagens WHERE conversa_id=? AND oculta=0 AND texto<>'' "
                               "ORDER BY id LIMIT 200", (r["id"],)).fetchall()
    corpo = "".join(f'<div class="m {m["papel"]}"><div class="q">{"J" if m["papel"] == "assistant" else "•"}</div>'
                    f'<div class="c">{html.escape(m["texto"])}</div></div>' for m in msgs)
    primeira = next((m["texto"] for m in msgs if m["papel"] == "assistant"), "")
    return HTMLResponse(_PAGINA.format(
        lang=idioma,
        titulo=html.escape(r["titulo"]),
        sufixo_titulo=html.escape(t("compartilhada_sufixo_titulo", idioma)),
        descricao=html.escape(primeira[:150]),
        topo_texto=html.escape(t("compartilhada_topo", idioma)),
        mensagens=corpo,
        cta_titulo=html.escape(t("compartilhada_cta_titulo", idioma)),
        cta_texto=html.escape(t("compartilhada_cta_texto", idioma)),
        cta_botao=html.escape(t("compartilhada_cta_botao", idioma)),
        ref=codigo_de(r["usuario_id"]),
    ))


# ---------------------------------------------------------------------- guias ---

GUIAS_DIR = nucleo.BASE / "web" / "guias"


def _arquivo_guia(slug: str, idioma: str) -> Any:
    if idioma in ("en", "es"):
        arq_id = GUIAS_DIR / idioma / f"{slug}.html"
        if arq_id.is_file():
            return arq_id
    return GUIAS_DIR / f"{slug}.html"


def guias(idioma: str | None = None) -> list[dict[str, str]]:
    from .i18n import idioma_atual

    id_ef = idioma or idioma_atual()
    saida = []
    for arq_base in sorted(GUIAS_DIR.glob("*.html")):
        slug = arq_base.stem
        arq = _arquivo_guia(slug, id_ef)
        texto = arq.read_text(encoding="utf-8")
        t_m = re.search(r"<title>(.*?)</title>", texto)
        d_m = re.search(r'name="description" content="(.*?)"', texto)
        saida.append({"slug": slug, "titulo": html.unescape(t_m.group(1).split(" · ")[0]) if t_m else slug,
                      "descricao": html.unescape(d_m.group(1)) if d_m else ""})
    return saida


@router.get("/api/guias")
def listar_guias() -> list[dict[str, str]]:
    return guias()


def _renderizar_guia(slug: str, request: Request, idioma_forcado: str | None = None) -> Response:
    from .i18n import idioma_atual, t
    from .pagamento import site_url

    if not re.fullmatch(r"[a-z0-9-]{1,80}", slug) or not (GUIAS_DIR / f"{slug}.html").is_file():
        raise HTTPException(404, t("nao_encontrado"))
    idioma = idioma_forcado or idioma_atual()
    arq = _arquivo_guia(slug, idioma)
    texto = arq.read_text(encoding="utf-8")
    base = site_url(request)
    url_can = f"{base}/guia/{slug}{('?lang=' + idioma) if idioma in ('en', 'es') else ''}"
    hreflang = (f'<link rel="canonical" href="{url_can}">'
                f'<link rel="alternate" hreflang="pt-BR" href="{base}/guia/{slug}?lang=pt-BR">'
                f'<link rel="alternate" hreflang="en" href="{base}/guia/{slug}?lang=en">'
                f'<link rel="alternate" hreflang="es" href="{base}/guia/{slug}?lang=es">'
                f'<link rel="alternate" hreflang="x-default" href="{base}/guia/{slug}">')
    texto = texto.replace("<!--HREFLANG-->", hreflang)
    return HTMLResponse(texto, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/guia/{slug}")
def ver_guia(slug: str, request: Request) -> Response:
    return _renderizar_guia(slug, request)


@router.get("/{idioma_rota}/guia/{slug}")
def ver_guia_idioma(idioma_rota: str, slug: str, request: Request) -> Response:
    from .i18n import t

    if idioma_rota not in ("en", "es", "pt-BR", "pt"):
        raise HTTPException(404, t("nao_encontrado"))
    return _renderizar_guia(slug, request, idioma_forcado="pt-BR" if idioma_rota == "pt" else idioma_rota)


@router.get("/robots.txt")
def robots(request: Request) -> PlainTextResponse:
    from .pagamento import site_url

    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /c/\nSitemap: {site_url(request)}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap(request: Request) -> Response:
    from .pagamento import site_url

    base = site_url(request)
    rotas = [f"{base}/", f"{base}/termos", f"{base}/privacidade"] + [f"{base}/guia/{g['slug']}" for g in guias("pt-BR")]
    idiomas = ("pt-BR", "en", "es")
    blocos = []
    for u in rotas:
        alternates = "".join(
            f'<xhtml:link rel="alternate" hreflang="{lang}" href="{html.escape(f"{u}?lang={lang}")}"/>'
            for lang in idiomas
        ) + f'<xhtml:link rel="alternate" hreflang="x-default" href="{html.escape(u)}"/>'
        blocos.append(f"<url><loc>{html.escape(u)}</loc>{alternates}</url>")
        for lang in idiomas:
            blocos.append(f"<url><loc>{html.escape(f'{u}?lang={lang}')}</loc>{alternates}</url>")
    corpo = "".join(blocos)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        f"{corpo}</urlset>",
        media_type="application/xml",
    )
