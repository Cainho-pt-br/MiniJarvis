"""Traduz o erro técnico de uma tarefa numa explicação que um leigo entende.

Ferramentas de IA local são muito criticadas por erros genéricos ("failed
loading model", "all backends returned error") que não dizem o que fazer.
Aqui, cada causa comum vira uma frase com o próximo passo.
"""
from __future__ import annotations

import re

from .i18n import PADRAO, idioma_atual, normalizar_idioma

# (padrão no erro/log, explicações por idioma). A primeira que casar vence.
_REGRAS: list[tuple[str, dict[str, str]]] = [
    (r"claude n[ãa]o encontrado|claude: command not found", {
        "pt-BR": "Esta máquina não tem o Claude Code instalado. Use uma das ações prontas, ou instale o Claude Code nela.",
        "en": "This machine does not have Claude Code installed. Use one of the ready-made actions, or install Claude Code on it.",
        "es": "Este equipo no tiene Claude Code instalado. Usa una de las acciones listas o instala Claude Code en él.",
    }),
    (r"gemini.*n[ãa]o encontrad|antigravity.*n[ãa]o encontrad", {
        "pt-BR": "Esta máquina não tem o Gemini instalado. Use uma das ações prontas, ou instale o Gemini nela.",
        "en": "This machine does not have Gemini installed. Use one of the ready-made actions, or install Gemini on it.",
        "es": "Este equipo no tiene Gemini instalado. Usa una de las acciones listas o instala Gemini en él.",
    }),
    (r"n[ãa]o est[áa] autenticado|authentication required|not logged in|n[ãa]o fez login", {
        "pt-BR": "O programa de IA desta máquina precisa de login. Abra-o uma vez no computador e entre na sua conta.",
        "en": "The AI program on this machine requires sign-in. Open it once on the computer and log in to your account.",
        "es": "El programa de IA de este equipo necesita iniciar sesión. Ábrelo una vez en la computadora y entra en tu cuenta.",
    }),
    (r"hit your (weekly|usage) limit|usage limit|quota|limite (semanal|de uso)|rate limit", {
        "pt-BR": "A conta de IA usada por esta máquina atingiu o limite de uso. Espere ela renovar ou troque de conta.",
        "en": "The AI account used by this machine reached its usage limit. Wait for it to renew or switch accounts.",
        "es": "La cuenta de IA usada por este equipo alcanzó su límite de uso. Espera a que se renueve o cambia de cuenta.",
    }),
    (r"permission denied|acesso negado|access is denied|operation not permitted|permiss[ãa]o negada", {
        "pt-BR": "O computador negou o acesso. Algumas pastas e arquivos exigem permissão de administrador, "
                 "e o Jarvis roda só com as permissões do seu usuário.",
        "en": "The computer denied access. Some folders and files require administrator permission, "
              "and Jarvis only runs with your user permissions.",
        "es": "La computadora denegó el acceso. Algunas carpetas y archivos requieren permisos de administrador, "
              "y Jarvis funciona solo con los permisos de tu usuario.",
    }),
    (r"no space left|espa[çc]o insuficiente|not enough space|disk full|disco cheio", {
        "pt-BR": "O disco está cheio. Rode a ação \"Maiores arquivos em Downloads\" para ver o que ocupa espaço.",
        "en": "The disk is full. Run the \"Largest files in Downloads\" action to see what is taking up space.",
        "es": "El disco está lleno. Ejecuta la acción \"Archivos más grandes en Descargas\" para ver qué ocupa espacio.",
    }),
    (r"command not found|n[ãa]o [ée] reconhecido|is not recognized|n[ãa]o foi encontrado|"
     r"no such file or directory|cannot find the (path|file)|c[óo]digo 127", {
        "pt-BR": "O programa ou a pasta pedida não existe neste computador. Confira o nome, ou use uma ação pronta.",
        "en": "The requested program or folder does not exist on this computer. Check the name, or use a ready-made action.",
        "es": "El programa o la carpeta solicitada no existe en esta computadora. Verifica el nombre o usa una acción lista.",
    }),
    (r"timed out|timeout|demorou demais", {
        "pt-BR": "A tarefa demorou mais do que o permitido. Tente de novo; se repetir, o computador pode estar muito ocupado.",
        "en": "The task took longer than allowed. Try again; if it happens again, the computer may be very busy.",
        "es": "La tarea tardó más de lo permitido. Inténtalo de nuevo; si se repite, la computadora puede estar muy ocupada.",
    }),
    (r"name or service not known|temporary failure in name resolution|no route to host|network is unreachable|"
     r"connection (refused|reset)|getaddrinfo|sem internet", {
        "pt-BR": "Faltou internet ou o endereço não respondeu. Teste com a ação \"Testar a internet\".",
        "en": "No internet connection or the address did not respond. Test with the \"Test internet\" action.",
        "es": "Faltó internet o la dirección no respondió. Prueba con la acción \"Probar internet\".",
    }),
    (r"parou de responder", {
        "pt-BR": "O computador parou de falar com o Jarvis no meio da tarefa: desligou, entrou em repouso ou ficou sem "
                 "internet. Quando ele voltar, a tarefa pode ser repetida.",
        "en": "The computer stopped communicating with Jarvis mid-task: it turned off, went to sleep, or lost "
              "internet. When it comes back, you can repeat the task.",
        "es": "La computadora dejó de comunicarse con Jarvis en medio de la tarea: se apagó, entró en reposo o se quedó sin "
              "internet. Cuando vuelva, la tarea se puede repetir.",
    }),
    (r"modo seguro", {
        "pt-BR": "Esta máquina está no modo seguro e só roda ações prontas. Para comandos livres, libere o modo livre no "
                 "cartão da máquina (pede sua senha).",
        "en": "This machine is in safe mode and only runs ready-made actions. For custom commands, enable unrestricted mode on "
              "the machine card (requires your password).",
        "es": "Este equipo está en modo seguro y solo ejecuta acciones listas. Para comandos libres, activa el modo libre en "
              "la tarjeta del equipo (pide tu contraseña).",
    }),
    (r"c[óo]digo [1-9]\d*", {
        "pt-BR": "O comando terminou com erro. A saída abaixo mostra o motivo; se não ficar claro, peça ao Jarvis para explicar.",
        "en": "The command finished with an error. The output below shows why; if it's unclear, ask Jarvis to explain.",
        "es": "El comando terminó con error. La salida abajo muestra el motivo; si no queda claro, pídele a Jarvis que lo explique.",
    }),
]

_PADRAO_FALHA: dict[str, str] = {
    "pt-BR": "Algo deu errado e a máquina não disse o motivo. Veja a saída abaixo ou peça ao Jarvis para investigar.",
    "en": "Something went wrong and the machine did not report the reason. Check the output below or ask Jarvis to investigate.",
    "es": "Algo salió mal y el equipo no indicó el motivo. Mira la salida abajo o pídele a Jarvis que investigue.",
}


def explicar_falha(status: str, mensagem: str | None, log: str | None, idioma: str | None = None) -> str | None:
    """Devolve a explicação para tarefas que falharam, ou None se não há o que explicar."""
    if status != "falhou":
        return None
    lang = normalizar_idioma(idioma) or idioma_atual()
    texto = f"{mensagem or ''}\n{(log or '')[-2000:]}".lower()
    for padrao, explicacoes in _REGRAS:
        if re.search(padrao, texto, re.I):
            return explicacoes.get(lang, explicacoes[PADRAO])
    return _PADRAO_FALHA.get(lang, _PADRAO_FALHA[PADRAO])

