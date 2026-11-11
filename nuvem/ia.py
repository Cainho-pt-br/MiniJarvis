"""Camada de IA do MiniJarvis Nuvem.

Isola a chamada ao Claude num único lugar: o resto do app só conhece
`Resposta` e o gerador de pedaços de texto. Nos testes, `FakeIA` toma o lugar
do cliente real, então nenhum teste gasta dinheiro com a API.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# Modelos por faixa: Haiku no Grátis/Básico e nas sugestões; Sonnet no Pro; Opus no Turbo.
MODELO_HAIKU = os.environ.get("IA_MODELO_HAIKU", os.environ.get("IA_MODELO_GRATIS", "claude-haiku-5-5"))
MODELO_SONNET = os.environ.get("IA_MODELO_SONNET", os.environ.get("IA_MODELO_PAGO", "claude-sonnet-5-5"))
MODELO_OPUS = os.environ.get("IA_MODELO_OPUS", "claude-opus-5-5")
MODELO_GRATIS = MODELO_HAIKU
MODELO_PAGO = MODELO_SONNET
ESFORCO = os.environ.get("IA_ESFORCO", "medium")  # low | medium | high
MAX_TOKENS = int(os.environ.get("IA_MAX_TOKENS", "16000"))
MAX_TOKENS_GRATIS = int(os.environ.get("IA_MAX_TOKENS_GRATIS", "4000"))  # limita o custo de uma resposta

# Preço em US$ por 1 milhão de tokens (entrada, saída). Usado para cobrar
# créditos proporcionais ao custo real de cada resposta.
PRECOS = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),  # até 100 mil tokens de entrada por pedido
}
# Custo médio estimado por mensagem em créditos (1 crédito = US$ 0,001),
# considerando uma mistura típica de conversa e ferramentas (ver docs/ECONOMIA.md).
CUSTO_MEDIO_MENSAGEM = {
    "claude-haiku-5-5": 2,
    "claude-sonnet-5-5": 12,
    "claude-opus-5-5": 24,
}
# Modelos que aceitam `fallbacks: "default"` (refazer em outro modelo se recusar).
COM_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}


def mensagens_estimadas(creditos: int, modelo: str) -> int:
    custo = CUSTO_MEDIO_MENSAGEM.get(modelo, 12)
    return max(1, round(creditos / custo / 5) * 5)


def modelo_abaixo(modelo: str) -> str | None:
    """Modelo imediatamente abaixo para fallback em erro de API ou sobrecarga."""
    mapa = {
        MODELO_OPUS: MODELO_SONNET,
        MODELO_SONNET: MODELO_HAIKU,
        "claude-opus-5-5": "claude-sonnet-5-5",
        "claude-sonnet-5-5": "claude-haiku-5-5",
    }
    abaixo = mapa.get(modelo)
    return abaixo if abaixo and abaixo != modelo else None


def modelo_do_plano(plano: str) -> str:
    from . import app as nucleo

    return str(nucleo.info_plano(plano)["modelo"])


def modelo_sugestoes_do_plano(plano: str) -> str:
    from . import app as nucleo

    return str(nucleo.info_plano(plano)["modelo_sugestoes"])


def max_tokens_do_plano(plano: str) -> int:
    from . import app as nucleo

    return int(nucleo.info_plano(plano)["max_tokens"])


SISTEMA = """Você é o Jarvis, o assistente pessoal do usuário, simpático e direto.
Fale em português do Brasil, com linguagem simples, sem jargão técnico.
Respostas curtas por padrão; use listas quando ajudarem a ler.

Você cuida do "parque": os computadores que o usuário conectou a você. Cada um
roda um agente que executa tarefas. Use as ferramentas:
- ver_parque: para saber quais máquinas existem, se estão ligadas, o que têm
  instalado e o que rodou por último. Consulte antes de propor tarefas.
- criar_tarefa: para fazer algo numa máquina. A tarefa só roda depois que o
  usuário aprovar no painel. Preencha "explicacao" em linguagem simples, para
  um leigo entender o que vai acontecer e se há algum risco.
  Prefira sempre tipo "acao" (ações prontas e revisadas, listadas em
  ver_parque). Máquinas no "modo seguro" SÓ aceitam ações prontas: se o pedido
  não couber em nenhuma, explique isso e diga que o dono pode liberar o modo
  livre no painel. Em máquinas no "modo livre", tipo "shell" roda um comando
  (no Windows, do cmd; no Linux/macOS, sh) e tipo "claude" manda um pedido ao
  Claude Code da máquina, se ela tiver.
  Nunca apague, formate ou sobrescreva nada sem o usuário pedir com todas as
  letras.
- ver_tarefa: para acompanhar uma tarefa e contar o resultado.
- ler_cerebro e salvar_no_cerebro: o cérebro é a sua memória de longo prazo,
  em notas ligadas por [[links]]. Quando o usuário pedir para lembrar algo, ou
  contar algo importante sobre si, salve numa nota. Prefira completar uma nota
  que já existe (leia antes; "Sobre mim" guarda o que ele conta de si). Toda
  nota nova precisa citar ao menos uma nota existente com [[Nome]], para
  aparecer ligada no mapa do cérebro. Antes de responder sobre preferências
  dele, leia o cérebro.

Se o usuário não tiver máquinas conectadas e pedir algo que precise de uma,
explique que basta clicar em "Conectar máquina" no painel."""


def _ferramenta(nome: str, descricao: str, props: dict[str, Any], obrigatorios: list[str]) -> dict[str, Any]:
    return {"name": nome, "description": descricao, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": props, "required": obrigatorios,
                             "additionalProperties": False}}


FERRAMENTAS = [
    _ferramenta("ver_parque", "Mostra as máquinas do usuário (ligadas ou não, hardware, IAs instaladas, uso) "
                "e as tarefas e rotinas recentes.", {}, []),
    _ferramenta("criar_tarefa", "Prepara uma tarefa para rodar numa máquina do parque. Ela fica aguardando "
                "a aprovação do usuário no painel.", {
                    "maquina": {"type": "string", "description": "Nome da máquina, como aparece em ver_parque"},
                    "tipo": {"type": "string", "enum": ["acao", "shell", "claude"],
                             "description": "'acao' (preferido): uma ação pronta. 'shell'/'claude': só no modo livre."},
                    "acao": {"type": "string", "description": "Id da ação pronta, quando tipo='acao'"},
                    "comando": {"type": "string", "description": "Comando (shell) ou pedido (claude), quando não é ação"},
                    "titulo": {"type": "string", "description": "Título curto, para leigos"},
                    "explicacao": {"type": "string",
                                   "description": "Em 1 ou 2 frases simples: o que vai acontecer e se há risco"},
                }, ["maquina", "tipo", "titulo", "explicacao"]),
    _ferramenta("ver_tarefa", "Mostra o estado, o log e o resultado de uma tarefa.",
                {"id": {"type": "integer"}}, ["id"]),
    _ferramenta("ler_cerebro", "Lê uma nota do cérebro. Sem nome, lista as notas existentes.",
                {"nome": {"type": "string"}}, []),
    _ferramenta("salvar_no_cerebro", "Cria ou substitui uma nota do cérebro (markdown). Use [[Nome]] para "
                "ligar a outras notas.", {"nome": {"type": "string"}, "conteudo": {"type": "string"}},
                ["nome", "conteudo"]),
]


@dataclass
class Resposta:
    modelo: str = ""
    texto: str = ""
    conteudo: list[dict[str, Any]] = field(default_factory=list)  # blocos para reenviar no histórico
    tokens_entrada: int = 0
    tokens_saida: int = 0
    recusada: bool = False
    stop_reason: str = ""
    chamadas: list[dict[str, Any]] = field(default_factory=list)  # tool_use: id, name, input


def custo_usd(modelo: str, entrada: int, saida: int) -> float:
    # Modelo desconhecido cobra pelo preço mais alto da tabela, nunca abaixo do custo.
    pin, pout = PRECOS.get(modelo, max(PRECOS.values()))
    return (entrada * pin + saida * pout) / 1_000_000


def _uso(final: Resposta, msg: Any, modelo: str) -> None:
    final.modelo = getattr(msg, "model", None) or modelo  # com fallback, cobra pelo modelo que respondeu
    u = msg.usage
    # Tokens de entrada equivalentes a preço cheio: leitura de cache custa 0,1x
    # e escrita de cache 1,25x.
    final.tokens_entrada = round((u.input_tokens or 0) + 0.1 * (u.cache_read_input_tokens or 0)
                                 + 1.25 * (u.cache_creation_input_tokens or 0))
    final.tokens_saida = u.output_tokens or 0
    final.stop_reason = msg.stop_reason or ""
    final.recusada = msg.stop_reason == "refusal"


INSTRUCAO_IDIOMA: dict[str, str] = {
    "en": (
        "IMPORTANT: The user's interface is in English. Always respond in simple, layperson-friendly English "
        "(overrides the Portuguese default above), including tool titles (`titulo`) and explanations (`explicacao`)."
    ),
    "es": (
        "IMPORTANTE: La interfaz del usuario está en español. Responde siempre en español simple y claro para "
        "personas no técnicas (reemplaza el portugués por defecto anterior), incluidos los títulos (`titulo`) "
        "y las explicaciones (`explicacao`) de las herramientas."
    ),
}


def sistema_para_idioma(idioma: str | None = None) -> list[dict[str, Any]]:
    """Monta os blocos `system` preservando o bloco principal com `cache_control` intacto."""
    from .i18n import idioma_atual, normalizar_idioma

    lang = normalizar_idioma(idioma) or idioma_atual()
    blocos: list[dict[str, Any]] = [
        {"type": "text", "text": SISTEMA, "cache_control": {"type": "ephemeral"}},
    ]
    if lang in INSTRUCAO_IDIOMA:
        blocos.append({"type": "text", "text": INSTRUCAO_IDIOMA[lang]})
    return blocos


class ClaudeIA:
    """Cliente real. Lê ANTHROPIC_API_KEY do ambiente."""

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self.cliente = anthropic.Anthropic()

    @staticmethod
    def _extra(modelo: str) -> dict[str, Any]:
        if modelo in COM_FALLBACK:
            # Se o modelo recusar, a API refaz o pedido em outro modelo sozinha.
            return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        return {}

    def _chamar_stream(self, historico: list[dict[str, Any]], final: Resposta, modelo: str,
                       ferramentas: list[dict[str, Any]] | None, max_tokens: int) -> Iterator[str]:
        final.modelo = modelo
        extra = self._extra(modelo)
        if ferramentas:
            extra["tools"] = ferramentas
        with self.cliente.beta.messages.stream(
            model=modelo,
            max_tokens=max_tokens,
            system=sistema_para_idioma(),
            messages=historico,
            output_config={"effort": ESFORCO},
            **extra,
        ) as stream:
            for pedaco in stream.text_stream:
                final.texto += pedaco
                yield pedaco
            msg = stream.get_final_message()
        _uso(final, msg, modelo)
        # Os blocos voltam inteiros no histórico (incluindo os de raciocínio),
        # como a API exige para continuar a conversa.
        final.conteudo = [b.model_dump(exclude_none=True) for b in msg.content]
        final.chamadas = [{"id": b.id, "name": b.name, "input": b.input}
                          for b in msg.content if b.type == "tool_use"]

    def responder(self, historico: list[dict[str, Any]], final: Resposta, modelo: str,
                  ferramentas: list[dict[str, Any]] | None = None, max_tokens: int = MAX_TOKENS) -> Iterator[str]:
        """Gera pedaços de texto e preenche `final` ao terminar.
        Se a chamada ao modelo do plano der erro de API ou sobrecarga antes de escrever,
        tenta UMA vez o modelo logo abaixo e cobra pelo modelo que realmente respondeu."""
        try:
            yield from self._chamar_stream(historico, final, modelo, ferramentas, max_tokens)
        except Exception:
            abaixo = modelo_abaixo(modelo)
            if not abaixo or final.texto:
                raise
            from . import operacao

            operacao.incidente("ia_fallback")
            yield from self._chamar_stream(historico, final, abaixo, ferramentas, max_tokens)

    def _chamar_json(self, prompt: str, schema: dict[str, Any], modelo: str) -> tuple[Any, Resposta]:
        final = Resposta(modelo=modelo)
        msg = self.cliente.beta.messages.create(
            model=modelo, max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **self._extra(modelo),
        )
        _uso(final, msg, modelo)
        if final.recusada:
            raise RuntimeError("a IA recusou gerar sugestões")
        texto = next(b.text for b in msg.content if b.type == "text")
        return json.loads(texto), final

    def gerar_json(self, prompt: str, schema: dict[str, Any], modelo: str) -> tuple[Any, Resposta]:
        """Uma chamada com saída em JSON garantida pelo schema."""
        try:
            return self._chamar_json(prompt, schema, modelo)
        except Exception:
            abaixo = modelo_abaixo(modelo)
            if not abaixo:
                raise
            from . import operacao

            operacao.incidente("ia_fallback")
            return self._chamar_json(prompt, schema, abaixo)


class FakeIA:
    """IA de mentira para testes e para rodar sem chave de API.

    Entende um pedido no formato  execute `comando` em maquina  e responde com
    uma chamada à ferramenta criar_tarefa, para dar para testar o fluxo inteiro.
    """

    def responder(self, historico: list[dict[str, Any]], final: Resposta, modelo: str,
                  ferramentas: list[dict[str, Any]] | None = None, max_tokens: int = MAX_TOKENS) -> Iterator[str]:
        from .i18n import idioma_atual

        lang = idioma_atual()
        final.modelo = modelo
        final.tokens_entrada, final.tokens_saida = 1000, 500
        ultima = historico[-1]["content"]
        prefixo_pronto = {"en": "Done! ", "es": "¡Listo! "}.get(lang, "Pronto! ")
        prefixo_entendi = {"en": "Understood: ", "es": "Entendido: "}.get(lang, "Entendi: ")
        frase_preparar = {"en": "I will prepare that.", "es": "Voy a preparar eso."}.get(lang, "Vou preparar isso.")
        if isinstance(ultima, list) and ultima and ultima[0].get("type") == "tool_result":
            texto = prefixo_pronto + str(ultima[0].get("content"))[:300]
        else:
            if isinstance(ultima, list):
                ultima = " ".join(b.get("text", "") for b in ultima if isinstance(b, dict))
            pedido = re.search(r"execute `(.+?)` em (\S+)", str(ultima))
            if pedido and ferramentas:
                cmd = pedido.group(1)
                entrada = ({"tipo": "acao", "acao": cmd[1:]} if cmd.startswith(":") else
                           {"tipo": "shell", "comando": cmd})
                final.conteudo = [{"type": "text", "text": frase_preparar},
                                  {"type": "tool_use", "id": "toolu_fake1", "name": "criar_tarefa",
                                   "input": {"maquina": pedido.group(2), "titulo": "Comando pedido no chat",
                                             "explicacao": "Roda o que você pediu.", **entrada}}]
                final.chamadas = [final.conteudo[1]]
                final.stop_reason = "tool_use"
                final.texto = frase_preparar
                yield final.texto
                return
            texto = prefixo_entendi + str(ultima)[:200]
        for pedaco in (texto[:9], texto[9:]):
            final.texto += pedaco
            yield pedaco
        final.conteudo = [{"type": "text", "text": final.texto}]
        final.stop_reason = "end_turn"

    def gerar_json(self, prompt: str, schema: dict[str, Any], modelo: str) -> tuple[Any, Resposta]:
        from .i18n import idioma_atual

        lang = idioma_atual()
        final = Resposta(modelo=modelo, tokens_entrada=800, tokens_saida=300)
        if lang == "en" or "in simple English" in prompt:
            return {"sugestoes": [
                {"tag": "sistema", "titulo": "Connect your first machine",
                 "descricao": "With a connected computer I can work for you.",
                 "motivo": "Your park is empty.",
                 "maquina": "", "pergunta": "How do I connect my computer?"},
            ]}, final
        if lang == "es" or "en español simple" in prompt:
            return {"sugestoes": [
                {"tag": "sistema", "titulo": "Conecta tu primer equipo",
                 "descricao": "Con una computadora conectada puedo trabajar por ti.",
                 "motivo": "Tu parque está vacío.",
                 "maquina": "", "pergunta": "¿Cómo conecto mi computadora?"},
            ]}, final
        return {"sugestoes": [
            {"tag": "sistema", "titulo": "Conecte sua primeira máquina", "descricao": "Com um computador "
             "conectado eu consigo trabalhar por você.", "motivo": "Seu parque está vazio.",
             "maquina": "", "pergunta": "Como conecto meu computador?"},
        ]}, final


def criar_ia() -> Any:
    # Só usa a IA de mentira quando pedido explicitamente: em produção, a falta
    # de chave tem que aparecer como erro, não como respostas falsas.
    if os.environ.get("IA_FAKE") == "1":
        return FakeIA()
    return ClaudeIA()

