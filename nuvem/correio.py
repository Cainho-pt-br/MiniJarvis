"""Envio de e-mail (códigos de confirmação) por SMTP, só com a biblioteca padrão.

Funciona com qualquer provedor SMTP. O mais simples para começar é o Gmail
com uma "senha de app" (https://myaccount.google.com/apppasswords):

    SMTP_HOST=smtp.gmail.com  SMTP_PORTA=587
    SMTP_USUARIO=voce@gmail.com  SMTP_SENHA=<senha de app de 16 letras>
    EMAIL_REMETENTE="Jarvis <voce@gmail.com>"

Sem SMTP_HOST configurado, a confirmação por e-mail fica desligada (o app
continua funcionando, só sem essa proteção) e o motivo vai para o log.
"""
from __future__ import annotations

import html as _html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("minijarvis")

# Quando os testes rodam, os e-mails vão para cá em vez de sair.
CAIXA_DE_TESTE: list[EmailMessage] | None = None


def configurado() -> bool:
    return CAIXA_DE_TESTE is not None or bool(os.environ.get("SMTP_HOST")) or os.environ.get("EMAIL_FAKE") == "1"


def enviar(para: str, assunto: str, texto: str, html: str) -> None:
    msg = EmailMessage()
    remetente = os.environ.get("EMAIL_REMETENTE") or os.environ.get("SMTP_USUARIO", "jarvis@localhost")
    msg["From"], msg["To"], msg["Subject"] = remetente, para, assunto
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    if CAIXA_DE_TESTE is not None:
        CAIXA_DE_TESTE.append(msg)
        return
    if os.environ.get("EMAIL_FAKE") == "1":  # desenvolvimento: o e-mail vai só para o log
        log.warning("EMAIL_FAKE para %s: %s", para, assunto)
        return
    host, porta = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORTA", "587"))
    contexto = ssl.create_default_context()
    if porta == 465:
        servidor = smtplib.SMTP_SSL(host, porta, context=contexto, timeout=20)
    else:
        servidor = smtplib.SMTP(host, porta, timeout=20)
        servidor.starttls(context=contexto)
    with servidor:
        if os.environ.get("SMTP_USUARIO"):
            servidor.login(os.environ["SMTP_USUARIO"], os.environ.get("SMTP_SENHA", ""))
        servidor.send_message(msg)


def enviar_codigo(para: str, nome: str, codigo: str, motivo: str) -> None:
    """motivo: 'confirmar' (conta nova) ou 'entrar' (dispositivo novo)."""
    if motivo == "confirmar":
        assunto = f"{codigo} é o seu código do Jarvis"
        frase = "Para ativar sua conta e ganhar seus créditos, digite este código:"
    else:
        assunto = f"{codigo} · confirme que é você entrando no Jarvis"
        frase = ("Alguém (provavelmente você) está entrando na sua conta por um navegador novo. "
                 "Se foi você, digite este código:")
    texto = (f"Olá, {nome}!\n\n{frase}\n\n    {codigo}\n\nEle vale por 15 minutos.\n"
             "Se não foi você, ignore este e-mail e troque sua senha.\n\nJarvis")
    html = f"""<div style="font-family:system-ui,sans-serif;max-width:480px;margin:auto;padding:24px;color:#1c1f2a">
<h2 style="margin:0 0 12px">Olá, {_html.escape(nome)}!</h2><p>{frase}</p>
<p style="font-size:34px;letter-spacing:8px;font-weight:700;font-family:monospace;background:#eefbfa;
padding:14px;text-align:center;border-radius:12px;color:#0b6b66">{codigo}</p>
<p style="color:#667085">Ele vale por 15 minutos. Se não foi você, ignore este e-mail e troque sua senha.</p>
<p style="color:#667085">— Jarvis</p></div>"""
    enviar(para, assunto, texto, html)
