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
