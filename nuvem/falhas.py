"""Traduz o erro técnico de uma tarefa numa explicação que um leigo entende.

Ferramentas de IA local são muito criticadas por erros genéricos ("failed
loading model", "all backends returned error") que não dizem o que fazer.
Aqui, cada causa comum vira uma frase com o próximo passo.
"""
from __future__ import annotations

import re

# (padrão no erro/log, explicação). A primeira que casar vence.
_REGRAS: list[tuple[str, str]] = [
    (r"claude n[ãa]o encontrado|claude: command not found",
     "Esta máquina não tem o Claude Code instalado. Use uma das ações prontas, ou instale o Claude Code nela."),
    (r"gemini.*n[ãa]o encontrad|antigravity.*n[ãa]o encontrad",
     "Esta máquina não tem o Gemini instalado. Use uma das ações prontas, ou instale o Gemini nela."),
    (r"n[ãa]o est[áa] autenticado|authentication required|not logged in|n[ãa]o fez login",
     "O programa de IA desta máquina precisa de login. Abra-o uma vez no computador e entre na sua conta."),
    (r"hit your (weekly|usage) limit|usage limit|quota|limite (semanal|de uso)|rate limit",
     "A conta de IA usada por esta máquina atingiu o limite de uso. Espere ela renovar ou troque de conta."),
    (r"permission denied|acesso negado|access is denied|operation not permitted|permiss[ãa]o negada",
     "O computador negou o acesso. Algumas pastas e arquivos exigem permissão de administrador, "
     "e o Jarvis roda só com as permissões do seu usuário."),
    (r"no space left|espa[çc]o insuficiente|not enough space|disk full|disco cheio",
     "O disco está cheio. Rode a ação \"Maiores arquivos em Downloads\" para ver o que ocupa espaço."),
    (r"command not found|n[ãa]o [ée] reconhecido|is not recognized|n[ãa]o foi encontrado|"
     r"no such file or directory|cannot find the (path|file)|c[óo]digo 127",
     "O programa ou a pasta pedida não existe neste computador. Confira o nome, ou use uma ação pronta."),
    (r"timed out|timeout|demorou demais",
     "A tarefa demorou mais do que o permitido. Tente de novo; se repetir, o computador pode estar muito ocupado."),
    (r"name or service not known|temporary failure in name resolution|no route to host|network is unreachable|"
     r"connection (refused|reset)|getaddrinfo|sem internet",
     "Faltou internet ou o endereço não respondeu. Teste com a ação \"Testar a internet\"."),
    (r"parou de responder",
     "O computador parou de falar com o Jarvis no meio da tarefa: desligou, entrou em repouso ou ficou sem "
     "internet. Quando ele voltar, a tarefa pode ser repetida."),
    (r"modo seguro",
     "Esta máquina está no modo seguro e só roda ações prontas. Para comandos livres, libere o modo livre no "
     "cartão da máquina (pede sua senha)."),
    (r"c[óo]digo [1-9]\d*",
     "O comando terminou com erro. A saída abaixo mostra o motivo; se não ficar claro, peça ao Jarvis para explicar."),
]


def explicar_falha(status: str, mensagem: str | None, log: str | None) -> str | None:
    """Devolve a explicação para tarefas que falharam, ou None se não há o que explicar."""
    if status != "falhou":
        return None
    texto = f"{mensagem or ''}\n{(log or '')[-2000:]}".lower()
    for padrao, explicacao in _REGRAS:
        if re.search(padrao, texto, re.I):
            return explicacao
    return "Algo deu errado e a máquina não disse o motivo. Veja a saída abaixo ou peça ao Jarvis para investigar."
