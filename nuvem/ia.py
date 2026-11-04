"""Camada de IA do MiniJarvis Nuvem.

Isola a chamada ao Claude num único lugar: o resto do app só conhece
`Resposta` e o gerador de pedaços de texto. Nos testes, `FakeIA` toma o lugar
do cliente real, então nenhum teste gasta dinheiro com a API.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterator

MODELO = os.environ.get("IA_MODELO", "claude-opus-5-5")
ESFORCO = os.environ.get("IA_ESFORCO", "medium")  # low | medium | high
MAX_TOKENS = int(os.environ.get("IA_MAX_TOKENS", "16000"))

# Preço em US$ por 1 milhão de tokens (entrada, saída). Usado para cobrar
# créditos proporcionais ao custo real de cada resposta.
PRECOS = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

SISTEMA = """Você é o Jarvis, um assistente pessoal simpático e direto.
Fale em português do Brasil, com linguagem simples, sem jargão técnico.
Respostas curtas por padrão; use listas quando ajudarem a ler.
Quando o pedido for vago, faça uma pergunta curta antes de responder."""


@dataclass
class Resposta:
    texto: str = ""
    conteudo: list[dict[str, Any]] = field(default_factory=list)  # blocos para reenviar no histórico
    tokens_entrada: int = 0
    tokens_saida: int = 0
    recusada: bool = False


def custo_usd(modelo: str, entrada: int, saida: int) -> float:
    pin, pout = PRECOS.get(modelo, PRECOS["claude-opus-5-5"])
    return (entrada * pin + saida * pout) / 1_000_000


class ClaudeIA:
    """Cliente real. Lê ANTHROPIC_API_KEY do ambiente."""

    def __init__(self) -> None:
        import anthropic

        self._anthropic = anthropic
        self.cliente = anthropic.Anthropic()
        self.modelo = MODELO

    def responder(self, historico: list[dict[str, Any]], final: Resposta) -> Iterator[str]:
        """Gera pedaços de texto e preenche `final` ao terminar."""
        with self.cliente.beta.messages.stream(
            model=self.modelo,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SISTEMA, "cache_control": {"type": "ephemeral"}}],
            messages=historico,
            output_config={"effort": ESFORCO},
            betas=["server-side-fallback-2026-07-01"],
            # Se o modelo principal recusar, a API tenta outro modelo sozinha.
            fallbacks="default",
        ) as stream:
            for pedaco in stream.text_stream:
                final.texto += pedaco
                yield pedaco
            msg = stream.get_final_message()
        # Os blocos voltam inteiros no histórico (incluindo os de raciocínio),
        # como a API exige para continuar a conversa.
        final.conteudo = [b.model_dump(exclude_none=True) for b in msg.content]
        final.tokens_entrada = (msg.usage.input_tokens or 0) + (msg.usage.cache_read_input_tokens or 0) \
            + (msg.usage.cache_creation_input_tokens or 0)
        final.tokens_saida = msg.usage.output_tokens or 0
        final.recusada = msg.stop_reason == "refusal"


class FakeIA:
    """IA de mentira para testes e para rodar sem chave de API."""

    modelo = "claude-haiku-5-5"

    def responder(self, historico: list[dict[str, Any]], final: Resposta) -> Iterator[str]:
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
