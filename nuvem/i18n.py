"""Internacionalização (i18n) do Jarvis: pt-BR (padrão), en e es.

Ordem de escolha do idioma:
1. Preferência salva (`usuarios.idioma` no banco + `localStorage` via `X-Idioma` / `?lang=`)
2. `navigator.languages` (enviado pelo navegador em `X-Nav-Idiomas`)
3. Cabeçalho HTTP `Accept-Language`
4. Fallback padrão: `pt-BR`
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

IDIOMAS = ("pt-BR", "en", "es")
PADRAO = "pt-BR"

_idioma_ctx: ContextVar[str] = ContextVar("idioma_jarvis", default=PADRAO)


def normalizar_idioma(valor: str | None) -> str | None:
    if not valor:
        return None
    limpo = valor.strip().replace("_", "-").lower()
    if not limpo:
        return None
    primario = limpo.split("-", 1)[0]
    if primario == "pt":
        return "pt-BR"
    if primario == "en":
        return "en"
    if primario == "es":
        return "es"
    return None


def aceitar_idioma(cabecalho: str | None) -> str | None:
    """Extrai o primeiro idioma suportado de uma lista `Accept-Language` ou `navigator.languages`."""
    if not cabecalho:
        return None
    candidatos: list[tuple[float, int, str]] = []
    for idx, item in enumerate(cabecalho.split(",")):
        parte = item.strip()
        if not parte:
            continue
        pedacos = [p.strip() for p in parte.split(";")]
        tag = pedacos[0]
        peso = 1.0
        for p in pedacos[1:]:
            if p.lower().startswith("q="):
                try:
                    peso = float(p[2:])
                except ValueError:
                    peso = 0.0
        if peso <= 0:
            continue
        norm = normalizar_idioma(tag)
        if norm:
            candidatos.append((-peso, idx, norm))
    if not candidatos:
        return None
    candidatos.sort()
    return candidatos[0][2]


def resolver_idioma(request: Any = None, usuario: Any = None, explicito: str | None = None) -> str:
    """Resolve o idioma seguindo a ordem de prioridade."""
    if explicito:
        norm = normalizar_idioma(explicito)
        if norm:
            return norm
    if request is not None:
        qp = getattr(request, "query_params", None) or {}
        for chave in ("lang", "idioma"):
            norm = normalizar_idioma(qp.get(chave))
            if norm:
                return norm
        headers = getattr(request, "headers", None) or {}
        norm = normalizar_idioma(headers.get("x-idioma"))
        if norm:
            return norm
    if usuario is not None:
        try:
            u_idioma = usuario["idioma"] if "idioma" in usuario.keys() else None
        except Exception:
            u_idioma = getattr(usuario, "idioma", None)
        norm = normalizar_idioma(u_idioma)
        if norm:
            return norm
    if request is not None:
        headers = getattr(request, "headers", None) or {}
        norm = aceitar_idioma(headers.get("x-nav-idiomas"))
        if norm:
            return norm
        norm = aceitar_idioma(headers.get("accept-language"))
        if norm:
            return norm
    return PADRAO


def definir_idioma(idioma: str | None) -> str:
    resolvido = normalizar_idioma(idioma) or PADRAO
    _idioma_ctx.set(resolvido)
    return resolvido


def idioma_atual() -> str:
    return _idioma_ctx.get()


TEXTOS: dict[str, dict[str, str]] = {
    # --- Autenticação e contas ---
    "login_necessario": {
        "pt-BR": "Faça login para continuar.",
        "en": "Please sign in to continue.",
        "es": "Inicia sesión para continuar.",
    },
    "sessao_expirada": {
        "pt-BR": "Sua sessão expirou. Entre de novo.",
        "en": "Your session has expired. Please sign in again.",
        "es": "Tu sesión expiró. Vuelve a iniciar sesión.",
    },
    "muitas_tentativas": {
        "pt-BR": "Muitas tentativas. Espere alguns minutos.",
        "en": "Too many attempts. Please wait a few minutes.",
        "es": "Demasiados intentos. Espera unos minutos.",
    },
    "codigo_expirado": {
        "pt-BR": "Código expirado. Peça um novo.",
        "en": "Expired code. Request a new one.",
        "es": "Código vencido. Pide uno nuevo.",
    },
    "codigo_expirado_entrar": {
        "pt-BR": "Código expirado. Entre de novo.",
        "en": "Expired code. Please sign in again.",
        "es": "Código vencido. Vuelve a iniciar sesión.",
    },
    "tentativas_codigo": {
        "pt-BR": "Tentativas demais com este código. Peça um novo.",
        "en": "Too many attempts with this code. Request a new one.",
        "es": "Demasiados intentos con este código. Pide uno nuevo.",
    },
    "codigo_incorreto": {
        "pt-BR": "Código incorreto.",
        "en": "Incorrect code.",
        "es": "Código incorrecto.",
    },
    "erro_envio_email": {
        "pt-BR": "Não consegui enviar o e-mail agora. Tente de novo em instantes.",
        "en": "Could not send the email right now. Please try again in a moment.",
        "es": "No pude enviar el correo ahora. Inténtalo de nuevo en unos instantes.",
    },
    "confirme_email": {
        "pt-BR": "Confirme seu e-mail para continuar.",
        "en": "Confirm your email to continue.",
        "es": "Confirma tu correo para continuar.",
    },
    "muitas_contas_hora": {
        "pt-BR": "Muitas contas criadas desta rede. Tente de novo mais tarde.",
        "en": "Too many accounts created from this network. Please try again later.",
        "es": "Demasiadas cuentas creadas desde esta red. Inténtalo más tarde.",
    },
    "muitas_contas_dia": {
        "pt-BR": "Muitas contas criadas desta rede hoje. Tente amanhã.",
        "en": "Too many accounts created from this network today. Please try tomorrow.",
        "es": "Demasiadas cuentas creadas desde esta red hoy. Inténtalo mañana.",
    },
    "email_invalido": {
        "pt-BR": "E-mail inválido.",
        "en": "Invalid email address.",
        "es": "Correo electrónico inválido.",
    },
    "aceite_termos": {
        "pt-BR": "Para criar a conta, aceite os termos de uso e a política de privacidade.",
        "en": "To create an account, please accept the terms of use and privacy policy.",
        "es": "Para crear la cuenta, acepta los términos de uso y la política de privacidad.",
    },
    "email_descartavel": {
        "pt-BR": "Use um e-mail de verdade (endereços temporários não são aceitos).",
        "en": "Please use a real email address (temporary addresses are not accepted).",
        "es": "Usa un correo real (no se aceptan direcciones temporales).",
    },
    "email_ja_cadastrado": {
        "pt-BR": "Já existe uma conta com este e-mail.",
        "en": "An account with this email already exists.",
        "es": "Ya existe una cuenta con este correo.",
    },
    "email_admin_exige_confirmacao": {
        "pt-BR": "Este e-mail só pode ser cadastrado com a confirmação por e-mail ligada.",
        "en": "This email can only be registered when email confirmation is enabled.",
        "es": "Este correo solo se puede registrar con la confirmación por correo activada.",
    },
    "espere_reenviar_codigo": {
        "pt-BR": "Espere um minuto para pedir outro código.",
        "en": "Please wait a minute before requesting another code.",
        "es": "Espera un minuto para pedir otro código.",
    },
    "muitas_tentativas_conta": {
        "pt-BR": "Muitas tentativas nesta conta. Espere 15 minutos.",
        "en": "Too many attempts on this account. Please wait 15 minutes.",
        "es": "Demasiados intentos en esta cuenta. Espera 15 minutos.",
    },
    "credenciais_incorretas": {
        "pt-BR": "E-mail ou senha incorretos.",
        "en": "Incorrect email or password.",
        "es": "Correo o contraseña incorrectos.",
    },
    "recuperacao_indisponivel": {
        "pt-BR": "A recuperação de senha ainda não está disponível. Fale com o suporte.",
        "en": "Password recovery is not available yet. Please contact support.",
        "es": "La recuperación de contraseña aún no está disponible. Contacta al soporte.",
    },
    "muitos_codigos_email": {
        "pt-BR": "Já mandamos códigos demais para este e-mail. Espere uma hora.",
        "en": "We have already sent too many codes to this email. Please wait an hour.",
        "es": "Ya enviamos demasiados códigos a este correo. Espera una hora.",
    },
    "senha_incorreta": {
        "pt-BR": "Senha incorreta.",
        "en": "Incorrect password.",
        "es": "Contraseña incorrecta.",
    },
    "idioma_invalido": {
        "pt-BR": "Idioma inválido.",
        "en": "Invalid language.",
        "es": "Idioma inválido.",
    },
    # --- Conversas, IA, créditos e planos ---
    "conversa_nao_encontrada": {
        "pt-BR": "Conversa não encontrada.",
        "en": "Conversation not found.",
        "es": "Conversación no encontrada.",
    },
    "nova_conversa": {
        "pt-BR": "Nova conversa",
        "en": "New conversation",
        "es": "Nueva conversación",
    },
    "creditos_acabaram_chat": {
        "pt-BR": "Seus créditos acabaram. Recarregue para continuar conversando.",
        "en": "You are out of credits. Top up to continue chatting.",
        "es": "Se acabaron tus créditos. Recarga para seguir conversando.",
    },
    "creditos_acabaram": {
        "pt-BR": "Seus créditos acabaram.",
        "en": "You are out of credits.",
        "es": "Se acabaron tus créditos.",
    },
    "creditos_acabaram_meio": {
        "pt-BR": "\n\n(Seus créditos acabaram; recarregue para eu continuar.)",
        "en": "\n\n(You ran out of credits; top up so I can continue.)",
        "es": "\n\n(Se acabaron tus créditos; recarga para que pueda continuar.)",
    },
    "ia_nao_respondeu": {
        "pt-BR": "A IA não respondeu agora. Tente de novo em instantes.",
        "en": "The AI did not respond right now. Please try again in a moment.",
        "es": "La IA no respondió ahora. Inténtalo de nuevo en unos instantes.",
    },
    "ia_recusou": {
        "pt-BR": "Não posso ajudar com esse pedido. Que tal tentar de outro jeito?",
        "en": "I can't help with that request. How about trying another way?",
        "es": "No puedo ayudar con ese pedido. ¿Qué tal intentarlo de otra forma?",
    },
    "teto_diario": {
        "pt-BR": "O Jarvis atingiu o limite de uso de hoje. Volte amanhã.",
        "en": "Jarvis reached today's usage limit. Please come back tomorrow.",
        "es": "Jarvis alcanzó el límite de uso de hoy. Vuelve mañana.",
    },
    "sugestoes_recentes": {
        "pt-BR": "As sugestões foram atualizadas há pouco. Tente de novo em alguns minutos.",
        "en": "Suggestions were updated just now. Please try again in a few minutes.",
        "es": "Las sugerencias se actualizaron hace poco. Inténtalo de nuevo en unos minutos.",
    },
    "erro_sugestoes": {
        "pt-BR": "Não consegui pensar em sugestões agora. Tente de novo em instantes.",
        "en": "Could not generate suggestions right now. Please try again in a moment.",
        "es": "No pude pensar en sugerencias ahora. Inténtalo de nuevo en unos instantes.",
    },
    "plano_nao_encontrado": {
        "pt-BR": "Plano não encontrado.",
        "en": "Plan not found.",
        "es": "Plan no encontrado.",
    },
    "plano_invalido": {
        "pt-BR": "Plano inválido.",
        "en": "Invalid plan.",
        "es": "Plan inválido.",
    },
    "nao_encontrado": {
        "pt-BR": "não encontrado",
        "en": "not found",
        "es": "no encontrado",
    },
    "nao_autorizado": {
        "pt-BR": "não autorizado",
        "en": "unauthorized",
        "es": "no autorizado",
    },
    "nota_moeda": {
        "pt-BR": "",
        "en": "Billed in Brazilian reais (BRL)",
        "es": "Cobro en reales brasileños (BRL)",
    },
    # --- Pagamentos ---
    "pagamento_indisponivel": {
        "pt-BR": "O pagamento ainda não está disponível. Deixe seu interesse que avisamos.",
        "en": "Payments are not available yet. Leave your interest and we will notify you.",
        "es": "El pago aún no está disponible. Deja tu interés y te avisaremos.",
    },
    "pagamento_nao_encontrado": {
        "pt-BR": "Pagamento não encontrado.",
        "en": "Payment not found.",
        "es": "Pago no encontrado.",
    },
    "erro_criar_pagamento": {
        "pt-BR": "Não consegui abrir o pagamento no Mercado Pago agora. Tente de novo em instantes.",
        "en": "Could not start the payment on Mercado Pago right now. Please try again in a moment.",
        "es": "No pude iniciar el pago en Mercado Pago ahora. Inténtalo de nuevo en unos instantes.",
    },
    "erro_cancelar_assinatura": {
        "pt-BR": "Não consegui cancelar a assinatura no Mercado Pago agora. Tente de novo em instantes.",
        "en": "Could not cancel the subscription on Mercado Pago right now. Please try again in a moment.",
        "es": "No pude cancelar la suscripción en Mercado Pago ahora. Inténtalo de nuevo en unos instantes.",
    },
    "sem_assinatura_ativa": {
        "pt-BR": "Você não tem assinatura ativa.",
        "en": "You do not have an active subscription.",
        "es": "No tienes una suscripción activa.",
    },
    "assinatura_invalida": {
        "pt-BR": "assinatura inválida",
        "en": "invalid signature",
        "es": "firma inválida",
    },
    "erro_processar": {
        "pt-BR": "erro ao processar",
        "en": "processing error",
        "es": "error al procesar",
    },
    # --- Parque, máquinas, tarefas, cérebro e rotinas ---
    "agente_sem_token": {
        "pt-BR": "Agente sem token. Pareie esta máquina pelo painel.",
        "en": "Agent missing token. Pair this machine from the dashboard.",
        "es": "Agente sin token. Vincula este equipo desde el panel.",
    },
    "token_agente_invalido": {
        "pt-BR": "Token de agente inválido. Pareie esta máquina de novo.",
        "en": "Invalid agent token. Pair this machine again.",
        "es": "Token de agente inválido. Vincula este equipo de nuevo.",
    },
    "tarefa_nao_encontrada": {
        "pt-BR": "Tarefa não encontrada.",
        "en": "Task not found.",
        "es": "Tarea no encontrada.",
    },
    "job_nao_encontrado": {
        "pt-BR": "job não encontrado",
        "en": "job not found",
        "es": "job no encontrado",
    },
    "maquina_bloqueada_singular": {
        "pt-BR": "A máquina '{nome}' está bloqueada: o seu plano atual conecta 1 máquina. "
                 "Desconecte outra máquina ou assine um plano maior para voltar a usá-la.",
        "en": "Machine '{nome}' is locked: your current plan connects 1 machine. "
              "Disconnect another machine or upgrade your plan to use it again.",
        "es": "El equipo '{nome}' está bloqueado: tu plan actual conecta 1 equipo. "
              "Desconecta otro equipo o suscríbete a un plan superior para volver a usarlo.",
    },
    "maquina_bloqueada_plural": {
        "pt-BR": "A máquina '{nome}' está bloqueada: o seu plano atual conecta {n} máquinas. "
                 "Desconecte outra máquina ou assine um plano maior para voltar a usá-la.",
        "en": "Machine '{nome}' is locked: your current plan connects {n} machines. "
              "Disconnect another machine or upgrade your plan to use it again.",
        "es": "El equipo '{nome}' está bloqueado: tu plan actual conecta {n} equipos. "
              "Desconecta otro equipo o suscríbete a un plan superior para volver a usarlo.",
    },
    "limite_maquinas_singular": {
        "pt-BR": "Seu plano conecta até 1 máquina. Assine um plano maior para montar seu parque inteiro.",
        "en": "Your plan connects up to 1 machine. Upgrade your plan to connect all your computers.",
        "es": "Tu plan conecta hasta 1 equipo. Suscríbete a un plan superior para conectar todos tus equipos.",
    },
    "limite_maquinas_plural": {
        "pt-BR": "Seu plano conecta até {limite} máquinas. Assine um plano maior para montar seu parque inteiro.",
        "en": "Your plan connects up to {limite} machines. Upgrade your plan to connect all your computers.",
        "es": "Tu plan conecta hasta {limite} equipos. Suscríbete a un plan superior para conectar todos tus equipos.",
    },
    "codigo_pareamento_invalido": {
        "pt-BR": "Código inválido ou expirado. Gere outro no painel.",
        "en": "Invalid or expired code. Generate a new one in the dashboard.",
        "es": "Código inválido o vencido. Genera otro en el panel.",
    },
    "maquina_nao_encontrada": {
        "pt-BR": "Máquina não encontrada.",
        "en": "Machine not found.",
        "es": "Equipo no encontrado.",
    },
    "modo_invalido": {
        "pt-BR": "Modo inválido.",
        "en": "Invalid mode.",
        "es": "Modo inválido.",
    },
    "modo_seguro_msg": {
        "pt-BR": "A máquina '{maquina}' está no modo seguro: ela só roda ações prontas. "
                 "Para rodar comandos livres, mude a máquina para o modo livre no painel.",
        "en": "Machine '{maquina}' is in safe mode: it only runs ready-made actions. "
              "To run custom commands, switch the machine to unrestricted mode in the dashboard.",
        "es": "El equipo '{maquina}' está en modo seguro: solo ejecuta acciones listas. "
              "Para ejecutar comandos libres, cambia el equipo a modo libre en el panel.",
    },
    "tipo_tarefa_invalido": {
        "pt-BR": "Tipo de tarefa inválido.",
        "en": "Invalid task type.",
        "es": "Tipo de tarea inválido.",
    },
    "diga_o_que_fazer": {
        "pt-BR": "Diga o que a máquina deve fazer.",
        "en": "Specify what the machine should do.",
        "es": "Indica qué debe hacer el equipo.",
    },
    "maquina_fora_do_parque": {
        "pt-BR": "Máquina '{maquina}' não está no seu parque.",
        "en": "Machine '{maquina}' is not in your park.",
        "es": "El equipo '{maquina}' no está en tu parque.",
    },
    "acao_desconhecida": {
        "pt-BR": "Ação desconhecida.",
        "en": "Unknown action.",
        "es": "Acción desconocida.",
    },
    "ainda_nao_da_so": {
        "pt-BR": "Ainda não dá: {motivo}.",
        "en": "Not ready yet: {motivo}.",
        "es": "Aún no se puede: {motivo}.",
    },
    "maquina_sem_so": {
        "pt-BR": "a máquina ainda não informou o sistema operacional; espere ela conectar",
        "en": "the machine has not reported its operating system yet; wait for it to connect",
        "es": "el equipo aún no informó su sistema operativo; espera a que se conecte",
    },
    "nota_nao_encontrada": {
        "pt-BR": "Nota não encontrada.",
        "en": "Note not found.",
        "es": "Nota no encontrada.",
    },
    "de_nome_nota": {
        "pt-BR": "Dê um nome para a nota.",
        "en": "Give the note a name.",
        "es": "Dale un nombre a la nota.",
    },
    "rotina_invalida": {
        "pt-BR": "Rotina inválida.",
        "en": "Invalid routine.",
        "es": "Rutina inválida.",
    },
    "rotinas_diarias_nao_incluidas": {
        "pt-BR": "Rotinas diárias automáticas não estão incluídas no seu plano atual.",
        "en": "Automatic daily routines are not included in your current plan.",
        "es": "Las rutinas diarias automáticas no están incluidas en tu plan actual.",
    },
    "rotina_nao_encontrada": {
        "pt-BR": "Rotina não encontrada.",
        "en": "Routine not found.",
        "es": "Rutina no encontrada.",
    },
    # --- E-mails (códigos e recibos) ---
    "email_confirmar_assunto": {
        "pt-BR": "{codigo} é o seu código do Jarvis",
        "en": "{codigo} is your Jarvis verification code",
        "es": "{codigo} es tu código de Jarvis",
    },
    "email_confirmar_frase": {
        "pt-BR": "Para ativar sua conta e ganhar seus créditos, digite este código:",
        "en": "To activate your account and get your free credits, enter this code:",
        "es": "Para activar tu cuenta y recibir tus créditos, ingresa este código:",
    },
    "email_senha_assunto": {
        "pt-BR": "{codigo} · redefinir a senha do Jarvis",
        "en": "{codigo} · reset your Jarvis password",
        "es": "{codigo} · restablecer la contraseña de Jarvis",
    },
    "email_senha_frase": {
        "pt-BR": "Você pediu para redefinir sua senha. Digite este código na tela de nova senha:",
        "en": "You requested a password reset. Enter this code on the new password screen:",
        "es": "Pediste restablecer tu contraseña. Ingresa este código en la pantalla de nueva contraseña:",
    },
    "email_entrar_assunto": {
        "pt-BR": "{codigo} · confirme que é você entrando no Jarvis",
        "en": "{codigo} · confirm your sign-in to Jarvis",
        "es": "{codigo} · confirma que eres tú iniciando sesión en Jarvis",
    },
    "email_entrar_frase": {
        "pt-BR": "Alguém (provavelmente você) está entrando na sua conta por um navegador novo. "
                 "Se foi você, digite este código:",
        "en": "Someone (probably you) is signing in to your account from a new browser. "
              "If it was you, enter this code:",
        "es": "Alguien (probablemente tú) está entrando a tu cuenta desde un navegador nuevo. "
              "Si fuiste tú, ingresa este código:",
    },
    "email_ola": {
        "pt-BR": "Olá, {nome}!",
        "en": "Hi, {nome}!",
        "es": "¡Hola, {nome}!",
    },
    "email_validade": {
        "pt-BR": "Ele vale por 15 minutos.",
        "en": "It is valid for 15 minutes.",
        "es": "Es válido por 15 minutos.",
    },
    "email_aviso_seguranca": {
        "pt-BR": "Se não foi você, ignore este e-mail e troque sua senha.",
        "en": "If it wasn't you, ignore this email and change your password.",
        "es": "Si no fuiste tú, ignora este correo y cambia tu contraseña.",
    },
    "email_recibo_assunto": {
        "pt-BR": "Recibo do Jarvis · plano {plano}",
        "en": "Jarvis receipt · {plano} plan",
        "es": "Recibo de Jarvis · plan {plano}",
    },
    "email_recibo_corpo": {
        "pt-BR": "Recebemos seu pagamento de R$ {valor} (BRL) pelo plano {plano}. "
                 "Foram adicionados {creditos} créditos à sua conta.",
        "en": "We received your payment of R$ {valor} (BRL) for the {plano} plan. "
              "{creditos} credits have been added to your account.",
        "es": "Recibimos tu pago de R$ {valor} (BRL) por el plan {plano}. "
              "Se agregaron {creditos} créditos a tu cuenta.",
    },
    # --- Página pública de conversa compartilhada (/c/TOKEN) ---
    "compartilhada_nao_publicada": {
        "pt-BR": "Esta conversa não está mais publicada.",
        "en": "This conversation is no longer published.",
        "es": "Esta conversación ya no está publicada.",
    },
    "compartilhada_sufixo_titulo": {
        "pt-BR": "conversa com o Jarvis",
        "en": "chat with Jarvis",
        "es": "conversación con Jarvis",
    },
    "compartilhada_topo": {
        "pt-BR": "conversa compartilhada",
        "en": "shared conversation",
        "es": "conversación compartida",
    },
    "compartilhada_cta_titulo": {
        "pt-BR": "Quer um Jarvis assim para você?",
        "en": "Want a Jarvis like this for yourself?",
        "es": "¿Quieres un Jarvis así para ti?",
    },
    "compartilhada_cta_texto": {
        "pt-BR": "Ele conversa em português, lembra de tudo e cuida dos seus computadores. Grátis, sem cartão.",
        "en": "It chats in your language, remembers everything, and takes care of your computers. Free, no card required.",
        "es": "Conversa en tu idioma, lo recuerda todo y cuida tus computadoras. Gratis, sin tarjeta.",
    },
    "compartilhada_cta_botao": {
        "pt-BR": "Criar meu Jarvis grátis →",
        "en": "Create my free Jarvis →",
        "es": "Crear mi Jarvis gratis →",
    },
}


PLANOS_TEXTOS: dict[str, dict[str, dict[str, Any]]] = {
    "gratis": {
        "pt-BR": {
            "nome": "Grátis",
            "descricao": "Para conhecer o Jarvis.",
            "itens": [
                "Chat com o Jarvis",
                "Conecte 1 computador",
                "Cérebro com memória",
                "{creditos} créditos para começar",
            ],
        },
        "en": {
            "nome": "Free",
            "descricao": "To get to know Jarvis.",
            "itens": [
                "Chat with Jarvis",
                "Connect 1 computer",
                "Brain with long-term memory",
                "{creditos} credits to get started",
            ],
        },
        "es": {
            "nome": "Gratis",
            "descricao": "Para conocer a Jarvis.",
            "itens": [
                "Chat con Jarvis",
                "Conecta 1 computadora",
                "Cerebro con memoria",
                "{creditos} créditos para empezar",
            ],
        },
    },
    "basico": {
        "pt-BR": {
            "nome": "Básico",
            "descricao": "Seu assistente no dia a dia.",
            "itens": [
                "Chat e tarefas no computador",
                "Conecte até 2 computadores",
                "{creditos} créditos por mês",
                "Rotinas manuais",
            ],
        },
        "en": {
            "nome": "Basic",
            "descricao": "Your everyday assistant.",
            "itens": [
                "Chat and tasks on your computer",
                "Connect up to 2 computers",
                "{creditos} credits per month",
                "Manual routines",
            ],
        },
        "es": {
            "nome": "Básico",
            "descricao": "Tu asistente para el día a día.",
            "itens": [
                "Chat y tareas en tu computadora",
                "Conecta hasta 2 computadoras",
                "{creditos} créditos por mes",
                "Rutinas manuales",
            ],
        },
    },
    "pro": {
        "pt-BR": {
            "nome": "Pro",
            "descricao": "O parque inteiro trabalhando por você.",
            "itens": [
                "Jarvis mais esperto para pedidos difíceis",
                "Conecte até 4 computadores",
                "4.500 créditos por mês",
                "Rotinas diárias automáticas",
                "Sugestões do parque todo dia",
            ],
        },
        "en": {
            "nome": "Pro",
            "descricao": "Your whole computer fleet working for you.",
            "itens": [
                "Smarter Jarvis for complex requests",
                "Connect up to 4 computers",
                "4,500 credits per month",
                "Automatic daily routines",
                "Daily park suggestions",
            ],
        },
        "es": {
            "nome": "Pro",
            "descricao": "Todo tu parque trabajando por ti.",
            "itens": [
                "Jarvis más inteligente para pedidos difíciles",
                "Conecta hasta 4 computadoras",
                "4.500 créditos por mes",
                "Rutinas diarias automáticas",
                "Sugerencias del parque todos los días",
            ],
        },
    },
    "turbo": {
        "pt-BR": {
            "nome": "Turbo",
            "descricao": "Para quem tem muitos computadores ou quer o máximo.",
            "itens": [
                "O Jarvis mais inteligente",
                "Conecte até 10 computadores",
                "8.000 créditos por mês",
                "Rotinas diárias automáticas",
                "Sugestões do parque todo dia",
            ],
        },
        "en": {
            "nome": "Turbo",
            "descricao": "For power users or anyone with many computers.",
            "itens": [
                "The smartest Jarvis",
                "Connect up to 10 computers",
                "8,000 credits per month",
                "Automatic daily routines",
                "Daily park suggestions",
            ],
        },
        "es": {
            "nome": "Turbo",
            "descricao": "Para quien tiene muchas computadoras o quiere el máximo.",
            "itens": [
                "El Jarvis más inteligente",
                "Conecta hasta 10 computadoras",
                "8.000 créditos por mes",
                "Rutinas diarias automáticas",
                "Sugerencias del parque todos los días",
            ],
        },
    },
}


def t(chave: str, idioma: str | None = None, **vars: Any) -> str:
    """Traduz `chave` para `idioma` (ou o idioma da requisição atual)."""
    lang = normalizar_idioma(idioma) or idioma_atual()
    entrada = TEXTOS[chave]
    texto = entrada.get(lang, entrada[PADRAO])
    return texto.format(**vars) if vars else texto


def planos_localizados(planos_base: list[dict[str, Any]], idioma: str | None = None) -> list[dict[str, Any]]:
    lang = normalizar_idioma(idioma) or idioma_atual()
    nota = t("nota_moeda", lang)
    saida = []
    for p in planos_base:
        copia = dict(p)
        tr = PLANOS_TEXTOS.get(p["id"], {}).get(lang) or PLANOS_TEXTOS.get(p["id"], {}).get(PADRAO)
        if tr:
            copia["nome"] = tr["nome"]
            copia["descricao"] = tr["descricao"]
            copia["itens"] = [i.format(creditos=p["creditos"]) for i in tr["itens"]]
        copia["nota_moeda"] = nota
        saida.append(copia)
    return saida
