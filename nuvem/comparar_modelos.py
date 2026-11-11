"""Compara Haiku 5.5 e Sonnet 5.5 em 20 pedidos típicos do Jarvis.

Avalia qualidade de resposta, uso correto das ferramentas, latência e custo real
em créditos (1 crédito = US$ 0,001) nas três categorias do produto:
- Pergunta curta (conversa direta e memória)
- Tarefa com ferramenta (ações prontas, modo seguro vs. livre, cérebro)
- Analisar o parque (diagnóstico de múltiplas máquinas e tarefas recentes)

ATENÇÃO: rodar contra a API real gasta cerca de US$ 0,30 a US$ 0,50.
Só rode com autorização do dono:

    ANTHROPIC_API_KEY=sk-ant-... python -m nuvem.comparar_modelos --executar --saida comparacao_haiku_sonnet.md

Para testar o script sem gastar nada (usando FakeIA):

    python -m nuvem.comparar_modelos --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any

from .ia import (
    FERRAMENTAS,
    MAX_TOKENS,
    MODELO_HAIKU,
    MODELO_SONNET,
    ClaudeIA,
    FakeIA,
    Resposta,
    custo_usd,
)

CREDITOS_POR_DOLAR = 1000
MAX_RODADAS = 4

PARQUE_EXEMPLO = """MÁQUINAS DO PARQUE:
- Notebook da Sala (notebook-sala) [modo seguro]: LIGADA · Windows 11 · CPU Intel i5 (4 núcleos), 8 GB RAM · uso agora: CPU 74%, RAM 86%, disco 91% cheio (18 GB livres)
  Ações prontas disponíveis (id: título):
  - espaco_disco: Ver espaço livre no disco
  - top_processos: Ver o que está deixando o computador lento
  - testar_rede: Testar se a internet está boa
  - limpar_temp: Limpar arquivos temporários
  - tamanho_downloads: Ver o peso da pasta Downloads
  - info_sistema: Ver resumo do computador
  - atualizar_sistema: Procurar atualizações do sistema
- PC do Escritório (pc-escritorio) [modo livre]: LIGADA · Ubuntu 24.04 · CPU Ryzen 7 (8 núcleos), 32 GB RAM · IAs instaladas: Claude Code · uso agora: CPU 8%, RAM 31%, disco 42% cheio (290 GB livres)

TAREFAS RECENTES:
- #14 [concluido] Ver espaço livre no disco em notebook-sala: C: 18 GB livres de 238 GB (91% usado)
- #15 [falhou] Limpar arquivos temporários em notebook-sala: Permissão negada em C:\\Windows\\Temp"""

CEREBRO_EXEMPLO = {
    "Sobre mim": "# Sobre mim\n\nTrabalho com contabilidade, prefiro explicações diretas e uso bastante a planilha de clientes. Veja [[Jarvis]].",
    "Jarvis": "# Jarvis\n\nO Jarvis cuida dos meus computadores ([[Sobre mim]]).",
}

PEDIDOS: list[dict[str, str]] = [
    # --- 1. Perguntas curtas (7) ---
    {"categoria": "pergunta_curta", "id": "p01",
     "pedido": "Oi Jarvis! Escreva uma mensagem curta e educada para cobrar um cliente que atrasou o boleto em 5 dias."},
    {"categoria": "pergunta_curta", "id": "p02",
     "pedido": "O Windows mostrou 'Seu disco está quase cheio'. O que isso quer dizer na prática? Corro risco de perder meus arquivos?"},
    {"categoria": "pergunta_curta", "id": "p03",
     "pedido": "Resuma em 3 tópicos simples a diferença entre memória RAM e espaço no disco (HD/SSD)."},
    {"categoria": "pergunta_curta", "id": "p04",
     "pedido": "Tenho R$ 1.450 de despesas fixas e recebo R$ 4.200. Quanto sobra se eu guardar 15% do que recebo?"},
    {"categoria": "pergunta_curta", "id": "p05",
     "pedido": "Recebi um e-mail dizendo que minha conta do banco será bloqueada hoje se eu não clicar num link. Como sei se é golpe?"},
    {"categoria": "pergunta_curta", "id": "p06",
     "pedido": "Me dê uma ideia rápida de organização para não misturar documentos pessoais com os da contabilidade no computador."},
    {"categoria": "pergunta_curta", "id": "p07",
     "pedido": "Como faço para tirar print apenas de um pedaço da tela no Windows e no Mac?"},

    # --- 2. Tarefas com ferramenta (8) ---
    {"categoria": "tarefa_ferramenta", "id": "t01",
     "pedido": "Veja quanto espaço livre tem no disco do meu Notebook da Sala."},
    {"categoria": "tarefa_ferramenta", "id": "t02",
     "pedido": "O Notebook da Sala está muito lento hoje. Descubra quais programas estão pesando nele."},
    {"categoria": "tarefa_ferramenta", "id": "t03",
     "pedido": "Limpe os arquivos temporários do Notebook da Sala para liberar espaço."},
    {"categoria": "tarefa_ferramenta", "id": "t04",
     "pedido": "A internet parece ruim no PC do Escritório. Faça um teste de conexão nele."},
    {"categoria": "tarefa_ferramenta", "id": "t05",
     "pedido": "Quero saber quanto está pesando a pasta Downloads no Notebook da Sala."},
    {"categoria": "tarefa_ferramenta", "id": "t06",
     "pedido": "Rode o comando `df -h /home` no PC do Escritório para eu ver o tamanho da pasta home."},
    {"categoria": "tarefa_ferramenta", "id": "t07",
     "pedido": "Lembre no seu cérebro que todo dia 5 eu preciso fechar a folha dos clientes da contabilidade."},
    {"categoria": "tarefa_ferramenta", "id": "t08",
     "pedido": "O que você já sabe sobre mim no seu cérebro?"},

    # --- 3. Analisar o parque (5) ---
    {"categoria": "analisar_parque", "id": "a01",
     "pedido": "Como estão meus computadores agora? Algum precisa de atenção?"},
    {"categoria": "analisar_parque", "id": "a02",
     "pedido": "Por que a última tarefa no Notebook da Sala falhou e o que podemos fazer?"},
    {"categoria": "analisar_parque", "id": "a03",
     "pedido": "Qual dos meus computadores é melhor para rodar uma tarefa pesada agora?"},
    {"categoria": "analisar_parque", "id": "a04",
     "pedido": "Quero apagar a pasta Documentos inteira do Notebook da Sala agora mesmo."},
    {"categoria": "analisar_parque", "id": "a05",
     "pedido": "Olhe meu parque e prepare a tarefa mais urgente para melhorar o Notebook da Sala."},
]


def _simular_ferramenta(nome: str, entrada: dict[str, Any]) -> str:
    if nome == "ver_parque":
        return PARQUE_EXEMPLO
    if nome == "criar_tarefa":
        maq = entrada.get("maquina", "")
        tipo = entrada.get("tipo", "")
        if "notebook" in maq.lower() and tipo != "acao":
            return "ERRO: A máquina 'Notebook da Sala' está no modo seguro e só aceita ações prontas."
        return json.dumps({
            "id": 99,
            "status": "aguardando",
            "maquina": maq,
            "tipo": tipo,
            "acao": entrada.get("acao"),
            "comando": entrada.get("comando"),
            "titulo": entrada.get("titulo"),
            "aviso": "Tarefa criada e aguardando a aprovação do usuário no painel.",
        }, ensure_ascii=False)
    if nome == "ver_tarefa":
        return json.dumps({
            "id": entrada.get("id", 15),
            "status": "falhou",
            "titulo": "Limpar arquivos temporários",
            "log": "Permissão negada em alguns arquivos em uso do sistema.",
        }, ensure_ascii=False)
    if nome == "ler_cerebro":
        alvo = entrada.get("nome")
        if alvo and alvo in CEREBRO_EXEMPLO:
            return CEREBRO_EXEMPLO[alvo]
        return "Notas existentes: " + ", ".join(CEREBRO_EXEMPLO.keys()) + "\n\n" + CEREBRO_EXEMPLO["Sobre mim"]
    if nome == "salvar_no_cerebro":
        return f"Nota '{entrada.get('nome', 'Nova')}' salva no cérebro."
    return "OK"


def rodar_pedido(motor: Any, modelo: str, texto_pedido: str) -> dict[str, Any]:
    historico: list[dict[str, Any]] = [{"role": "user", "content": texto_pedido}]
    partes_texto: list[str] = []
    ferramentas_usadas: list[str] = []
    tokens_in = 0
    tokens_out = 0
    usd_total = 0.0
    t0 = time.monotonic()

    for _ in range(MAX_RODADAS):
        final = Resposta()
        trecho = "".join(motor.responder(historico, final, modelo, FERRAMENTAS, MAX_TOKENS))
        if trecho:
            partes_texto.append(trecho)
        tokens_in += final.tokens_entrada
        tokens_out += final.tokens_saida
        usd_total += custo_usd(final.modelo, final.tokens_entrada, final.tokens_saida)
        historico.append({"role": "assistant", "content": final.conteudo})
        if final.stop_reason != "tool_use" or not final.chamadas:
            break
        resultados = []
        for ch in final.chamadas:
            ferramentas_usadas.append(ch["name"])
            resultados.append({
                "type": "tool_result",
                "tool_use_id": ch["id"],
                "content": _simular_ferramenta(ch["name"], ch.get("input") or {}),
            })
        historico.append({"role": "user", "content": resultados})

    duracao_s = round(time.monotonic() - t0, 2)
    creditos = max(1, math.ceil(usd_total * CREDITOS_POR_DOLAR))
    return {
        "modelo": modelo,
        "resposta": "\n\n".join(partes_texto).strip(),
        "ferramentas": ferramentas_usadas,
        "tokens_entrada": tokens_in,
        "tokens_saida": tokens_out,
        "custo_usd": round(usd_total, 6),
        "creditos": creditos,
        "segundos": duracao_s,
    }


def comparar(motor: Any, modelos: tuple[str, str] = (MODELO_HAIKU, MODELO_SONNET)) -> list[dict[str, Any]]:
    resultados = []
    for item in PEDIDOS:
        par: dict[str, Any] = {"id": item["id"], "categoria": item["categoria"], "pedido": item["pedido"], "modelos": {}}
        for m in modelos:
            par["modelos"][m] = rodar_pedido(motor, m, item["pedido"])
        resultados.append(par)
    return resultados


def formatar_markdown(resultados: list[dict[str, Any]], modelos: tuple[str, str] = (MODELO_HAIKU, MODELO_SONNET)) -> str:
    m1, m2 = modelos
    linhas = [
        "# Comparação Haiku 5.5 vs. Sonnet 5.5 (20 pedidos típicos)",
        "",
        "| ID | Categoria | Ferramentas (Haiku / Sonnet) | Créditos (Haiku / Sonnet) | Tempo s (Haiku / Sonnet) |",
        "|---|---|---|---|---|",
    ]
    tot_cred = {m1: 0, m2: 0}
    tot_usd = {m1: 0.0, m2: 0.0}
    for r in resultados:
        r1, r2 = r["modelos"][m1], r["modelos"][m2]
        tot_cred[m1] += r1["creditos"]
        tot_cred[m2] += r2["creditos"]
        tot_usd[m1] += r1["custo_usd"]
        tot_usd[m2] += r2["custo_usd"]
        f1 = ", ".join(r1["ferramentas"]) or "—"
        f2 = ", ".join(r2["ferramentas"]) or "—"
        linhas.append(
            f"| `{r['id']}` | {r['categoria']} | {f1} / {f2} | "
            f"{r1['creditos']} / {r2['creditos']} | {r1['segundos']}s / {r2['segundos']}s |"
        )
    linhas.extend([
        "",
        "## Totais nos 20 pedidos",
        f"- **{m1}**: {tot_cred[m1]} créditos (US$ {tot_usd[m1]:.4f}) · média de {tot_cred[m1] / len(resultados):.1f} créditos/pedido",
        f"- **{m2}**: {tot_cred[m2]} créditos (US$ {tot_usd[m2]:.4f}) · média de {tot_cred[m2] / len(resultados):.1f} créditos/pedido",
        "",
        "## Respostas completas",
    ])
    for r in resultados:
        linhas.extend([
            "",
            f"### {r['id']} ({r['categoria']}): {r['pedido']}",
            f"**{m1}** ({r['modelos'][m1]['creditos']} cr, ferramentas: {r['modelos'][m1]['ferramentas']}):",
            "",
            r["modelos"][m1]["resposta"],
            "",
            f"**{m2}** ({r['modelos'][m2]['creditos']} cr, ferramentas: {r['modelos'][m2]['ferramentas']}):",
            "",
            r["modelos"][m2]["resposta"],
        ])
    return "\n".join(linhas) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compara Haiku 5.5 e Sonnet 5.5 em 20 pedidos típicos.")
    parser.add_argument("--executar", action="store_true",
                        help="Chama a API real da Anthropic (gasta ~US$ 0,50; requer autorização do dono).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Executa os 20 pedidos com FakeIA sem gastar API.")
    parser.add_argument("--saida", type=str, default="",
                        help="Arquivo markdown onde salvar o relatório completo.")
    args = parser.parse_args(argv)

    if not args.executar and not args.dry_run:
        print("Este script faz 40 chamadas à API da Anthropic (~US$ 0,50).\n"
              "Para rodar de verdade (com autorização do dono), passe --executar e defina ANTHROPIC_API_KEY.\n"
              "Para validar o fluxo sem custo, passe --dry-run.", file=sys.stderr)
        return 2

    if args.dry_run:
        motor: Any = FakeIA()
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("Defina ANTHROPIC_API_KEY antes de rodar com --executar.", file=sys.stderr)
            return 1
        motor = ClaudeIA()

    resultados = comparar(motor)
    relatorio = formatar_markdown(resultados)
    if args.saida:
        with open(args.saida, "w", encoding="utf-8") as f:
            f.write(relatorio)
        print(f"Relatório salvo em {args.saida}")
    else:
        print(relatorio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
