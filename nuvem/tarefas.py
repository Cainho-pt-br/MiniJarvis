"""Tarefas prontas: o primeiro contato do usuário leigo com a IA.

Cada tarefa vira um cartão na tela inicial. Ao clicar, o texto de `prompt`
entra na caixa de mensagem com `{...}` para a pessoa completar.
"""

TAREFAS = [
    {"id": "email", "icone": "✉️", "titulo": "Escrever um e-mail",
     "descricao": "Diga para quem e o assunto; eu escrevo.",
     "prompt": "Escreva um e-mail educado para {destinatário} sobre {assunto}."},
    {"id": "resumo", "icone": "📝", "titulo": "Resumir um texto",
     "descricao": "Cole um texto longo e receba os pontos principais.",
     "prompt": "Resuma em tópicos curtos o texto abaixo:\n\n{cole o texto aqui}"},
    {"id": "semana", "icone": "📅", "titulo": "Organizar minha semana",
     "descricao": "Liste o que precisa fazer; eu monto um plano.",
     "prompt": "Monte um plano para minha semana com estas tarefas: {liste suas tarefas}"},
    {"id": "whatsapp", "icone": "💬", "titulo": "Responder uma mensagem",
     "descricao": "Cole a mensagem que recebeu e diga o tom da resposta.",
     "prompt": "Me ajude a responder esta mensagem de forma {educada/direta/carinhosa}:\n\n{cole a mensagem}"},
    {"id": "post", "icone": "📣", "titulo": "Post para redes sociais",
     "descricao": "Legendas para Instagram, LinkedIn ou WhatsApp.",
     "prompt": "Crie 3 opções de legenda para {rede social} divulgando {o que quer divulgar}."},
    {"id": "explicar", "icone": "💡", "titulo": "Explicar algo difícil",
     "descricao": "Contrato, boleto, termo técnico… em palavras simples.",
     "prompt": "Explique em palavras simples, como para alguém leigo: {o que quer entender}"},
    {"id": "receita", "icone": "🍳", "titulo": "O que cozinhar hoje",
     "descricao": "Diga o que tem na geladeira.",
     "prompt": "Tenho {ingredientes}. Sugira 3 receitas fáceis com o passo a passo."},
    {"id": "curriculo", "icone": "💼", "titulo": "Melhorar currículo",
     "descricao": "Cole seu currículo e a vaga desejada.",
     "prompt": "Melhore meu currículo para a vaga de {vaga}:\n\n{cole o currículo}"},
]

TAREFAS_POR_IDIOMA: dict[str, list[dict[str, str]]] = {
    "pt-BR": TAREFAS,
    "en": [
        {"id": "email", "icone": "✉️", "titulo": "Write an email",
         "descricao": "Tell me who it's for and the subject; I'll write it.",
         "prompt": "Write a polite email to {recipient} about {subject}."},
        {"id": "resumo", "icone": "📝", "titulo": "Summarize a text",
         "descricao": "Paste a long text and get the key points.",
         "prompt": "Summarize the text below in short bullet points:\n\n{paste text here}"},
        {"id": "semana", "icone": "📅", "titulo": "Plan my week",
         "descricao": "List what you need to do; I'll build a plan.",
         "prompt": "Build a weekly plan for me with these tasks: {list your tasks}"},
        {"id": "whatsapp", "icone": "💬", "titulo": "Reply to a message",
         "descricao": "Paste the message you received and choose the tone.",
         "prompt": "Help me reply to this message in a {polite/direct/warm} tone:\n\n{paste message}"},
        {"id": "post", "icone": "📣", "titulo": "Social media post",
         "descricao": "Captions for Instagram, LinkedIn, or WhatsApp.",
         "prompt": "Create 3 caption options for {social network} promoting {what you want to promote}."},
        {"id": "explicar", "icone": "💡", "titulo": "Explain something complex",
         "descricao": "Contract, bill, technical term… in plain words.",
         "prompt": "Explain in simple words, as if to a layperson: {what you want to understand}"},
        {"id": "receita", "icone": "🍳", "titulo": "What to cook today",
         "descricao": "Tell me what's in your fridge.",
         "prompt": "I have {ingredients}. Suggest 3 easy recipes with step-by-step instructions."},
        {"id": "curriculo", "icone": "💼", "titulo": "Improve my resume",
         "descricao": "Paste your resume and the target role.",
         "prompt": "Improve my resume for the {job title} position:\n\n{paste resume}"},
    ],
    "es": [
        {"id": "email", "icone": "✉️", "titulo": "Escribir un correo",
         "descricao": "Dime para quién es y el asunto; yo lo escribo.",
         "prompt": "Escribe un correo amable para {destinatario} sobre {asunto}."},
        {"id": "resumo", "icone": "📝", "titulo": "Resumir un texto",
         "descricao": "Pega un texto largo y recibe los puntos principales.",
         "prompt": "Resume en puntos cortos el siguiente texto:\n\n{pega el texto aquí}"},
        {"id": "semana", "icone": "📅", "titulo": "Organizar mi semana",
         "descricao": "Lista lo que necesitas hacer; yo armo un plan.",
         "prompt": "Arma un plan para mi semana con estas tareas: {lista tus tareas}"},
        {"id": "whatsapp", "icone": "💬", "titulo": "Responder un mensaje",
         "descricao": "Pega el mensaje que recibiste y dime el tono de la respuesta.",
         "prompt": "Ayúdame a responder este mensaje de forma {amable/directa/cariñosa}:\n\n{pega el mensaje}"},
        {"id": "post", "icone": "📣", "titulo": "Publicación para redes",
         "descricao": "Textos para Instagram, LinkedIn o WhatsApp.",
         "prompt": "Crea 3 opciones de texto para {red social} promocionando {lo que quieres promocionar}."},
        {"id": "explicar", "icone": "💡", "titulo": "Explicar algo difícil",
         "descricao": "Contrato, factura, término técnico… en palabras simples.",
         "prompt": "Explica en palabras simples, como para alguien sin experiencia técnica: {lo que quieres entender}"},
        {"id": "receita", "icone": "🍳", "titulo": "Qué cocinar hoy",
         "descricao": "Dime qué tienes en el refrigerador.",
         "prompt": "Tengo {ingredientes}. Sugiere 3 recetas fáciles paso a paso."},
        {"id": "curriculo", "icone": "💼", "titulo": "Mejorar currículum",
         "descricao": "Pega tu currículum y el puesto deseado.",
         "prompt": "Mejora mi currículum para el puesto de {puesto}:\n\n{pega tu currículum}"},
    ],
}


def catalogo_tarefas(idioma: str | None = None) -> list[dict[str, str]]:
    from .i18n import PADRAO, idioma_atual, normalizar_idioma

    lang = normalizar_idioma(idioma) or idioma_atual()
    return TAREFAS_POR_IDIOMA.get(lang, TAREFAS_POR_IDIOMA[PADRAO])

