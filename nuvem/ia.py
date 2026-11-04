"""Camada de IA do MiniJarvis Nuvem.

Isola a chamada ao Claude num único lugar: o resto do app só conhece
`Resposta` e o gerador de pedaços de texto. Nos testes, `FakeIA` toma o lugar
do cliente real, então nenhum teste gasta dinheiro com a API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterator

# Plano grátis usa o modelo mais barato; planos pagos, o mais capaz.
MODELO_GRATIS = os.environ.get("IA_MODELO_GRATIS", "claude-haiku-5-5")
MODELO_PAGO = os.environ.get("IA_MODELO_PAGO", "claude-sonnet-5-5")
ESFORCO = os.environ.get("IA_ESFORCO", "medium")  # low | medium | high
MAX_TOKENS = int(os.environ.get("IA_MAX_TOKENS", "16000"))

# Preço em US$ por 1 milhão de tokens (entrada, saída). Usado para cobrar
# créditos proporcionais ao custo real de cada resposta.
PRECOS = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),  # até 100 mil tokens de entrada por pedido
}
# Modelos que aceitam `fallbacks: "default"` (refazer em outro modelo se recusar).
COM_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}


def modelo_do_plano(plano: str) -> str:
    return MODELO_GRATIS if plano == "gratis" else MODELO_PAGO


SISTEMA = """Você é o Jarvis, um assistente pessoal simpático e direto.
Fale em português do Brasil, com linguagem simples, sem jargão técnico.
Respostas curtas por padrão; use listas quando ajudarem a ler.
Quando o pedido for vago, faça uma pergunta curta antes de responder."""


@dataclass
class Resposta:
    modelo: str = ""
    texto: str = ""
    conteudo: list[dict[str, Any]] = field(default_factory=list)  # blocos para reenviar no histórico
    tokens_entrada: int = 0
    tokens_saida: int = 0
    recusada: bool = False


def custo_usd(modelo: str, entrada: int, saida: int) -> float:
    # Modelo desconhecido cobra pelo preço mais alto da tabela, nunca abaixo do custo.
    pin, pout = PRECOS.get(modelo, max(PRECOS.values()))
    return (entrada * pin + saida * pout) / 1_000_000


class ClaudeIA:
    """Cliente real. Lê ANTHROPIC_API_KEY do ambiente."""

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self.cliente = anthropic.Anthropic()

    def responder(self, historico: list[dict[str, Any]], final: Resposta, modelo: str) -> Iterator[str]:
        """Gera pedaços de texto e preenche `final` ao terminar."""
        final.modelo = modelo
        extra: dict[str, Any] = {}
        if modelo in COM_FALLBACK:
            # Se o modelo recusar, a API refaz o pedido em outro modelo sozinha.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        with self.cliente.beta.messages.stream(
            model=modelo,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SISTEMA, "cache_control": {"type": "ephemeral"}}],
            messages=historico,
            output_config={"effort": ESFORCO},
            **extra,
        ) as stream:
            for pedaco in stream.text_stream:
                final.texto += pedaco
                yield pedaco
            msg = stream.get_final_message()
        final.modelo = msg.model or modelo  # com fallback, cobra pelo modelo que respondeu
        # Os blocos voltam inteiros no histórico (incluindo os de raciocínio),
        # como a API exige para continuar a conversa.
        final.conteudo = [b.model_dump(exclude_none=True) for b in msg.content]
        # Tokens de entrada equivalentes a preço cheio: leitura de cache custa 0,1x
        # e escrita de cache 1,25x.
        u = msg.usage
        final.tokens_entrada = round((u.input_tokens or 0) + 0.1 * (u.cache_read_input_tokens or 0)
                                     + 1.25 * (u.cache_creation_input_tokens or 0))
        final.tokens_saida = msg.usage.output_tokens or 0
        final.recusada = msg.stop_reason == "refusal"


class FakeIA:
    """IA de mentira para testes e para rodar sem chave de API."""

    def responder(self, historico: list[dict[str, Any]], final: Resposta, modelo: str) -> Iterator[str]:
        final.modelo = modelo
        ultima = historico[-1]["content"]
        if isinstance(ultima, list):
            ultima = " ".join(b.get("text", "") for b in ultima if isinstance(b, dict))
        for pedaco in ("Entendi: ", str(ultima)[:200]):
            final.texto += pedaco
            yield pedaco
        final.conteudo = [{"type": "text", "text": final.texto}]
        final.tokens_entrada = 1000
        final.tokens_saida = 500


def criar_ia() -> Any:
    # Só usa a IA de mentira quando pedido explicitamente: em produção, a falta
    # de chave tem que aparecer como erro, não como respostas falsas.
    if os.environ.get("IA_FAKE") == "1":
        return FakeIA()
    return ClaudeIA()
