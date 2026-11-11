# Economia: o preço dos planos cobre o custo de IA?

Análise de 11/11/2026. Refaça quando mudar preço de plano, modelo, preço da
API ou o câmbio. Os números de consumo são **estimativas** (tokens medidos no
código, não em produção): confirme com o relatório diário depois do primeiro mês.

## Por que o custo de IA não passa do que o cliente paga

O Jarvis desconta créditos pelos **tokens reais** de cada resposta
(`nuvem/app.py::_custo`, `nuvem/ia.py::custo_usd`), inclusive o raciocínio da IA,
que vem contado como saída. 1 crédito = US$ 0,001. Logo o custo de IA de um
cliente é no máximo `créditos do plano ÷ 1000` dólares. Detalhes:

- A tabela `PRECOS` (`nuvem/ia.py`) confere com a documentação da API em
  11/11/2026: Haiku 5.5 US$ 0,10 / 0,50; Sonnet 5.5 US$ 2 / 10; Opus 5.5 US$ 4 / 20
  por milhão de tokens (entrada / saída). **Atualize se a Anthropic mudar.**
- O código conta a leitura de cache a 0,1× da entrada; no Sonnet 5.5 o real é 0,05×.
  Ou seja, cobra-se um pouco a mais, nunca a menos.
- Mínimo de 1 crédito por mensagem (no Haiku o custo real é menor que isso).
- Uma mensagem pode estourar o saldo em até uma resposta (a cobrança é feita ao
  fim e limitada ao saldo): perda de centavos por cliente por mês.

## Consumo por mensagem (estimado)

| Tipo | Haiku (grátis) | Sonnet (pagos) |
|---|---|---|
| Pergunta curta | 1 crédito | 5 |
| Pedido de tarefa (1 ferramenta) | 2 | 12 |
| Analisar o parque (2 ferramentas) | 3 | 22 |

Isso rende cerca de 170 a 400 mensagens por mês no Básico e 375 a 900 no Pro.
`IA_ESFORCO` (padrão `medium`) controla quanto a IA raciocina: `low` baixa o custo
por mensagem no chat.

## Margem por plano no pior caso (cliente gasta 100% dos créditos)

Câmbio de referência: US$ 1 = R$ 5,51 (9/11/2026). Taxa do Mercado Pago estimada
em 4,98% + R$ 0,49 no cartão (**confirmar na conta**).

| | Básico | Pro |
|---|---|---|
| Preço | R$ 29,90 | R$ 59,90 |
| IA no pior caso | R$ 11,02 (37%) | R$ 24,80 (41%) |
| Taxa do pagamento | R$ 1,98 | R$ 3,47 |
| **Sobra** | **R$ 16,90 (57%)** | **R$ 31,63 (53%)** |
| Sobra com 50% de uso | R$ 22,41 | R$ 44,03 |

Dólar a R$ 6,50: sobra R$ 14,92 (Básico) e R$ 27,18 (Pro).

Custos fixos aproximados: Vercel Pro US$ 20 (cerca de R$ 110), MEI (cerca de
R$ 80, confirmar com contador), domínio. Total perto de R$ 195 por mês: o ponto
de equilíbrio é de uns 12 assinantes Básico, 7 Pro ou 9 numa mistura.

## Onde ainda se perde dinheiro

1. **Contas grátis em massa:** até US$ 0,30 por conta. `TETO_DIARIO_USD` limita o
   gasto de IA do dia inteiro; deixe em 3 até ter receita e ligue o e-mail
   (confirmação de conta) antes de divulgar.
2. **Chargeback / estorno:** perde-se o valor e a IA já gasta. Há estorno
   automático de plano e créditos (`cobranca.estornar_plano`) e aviso ao dono.
3. **Câmbio:** o custo é em dólar e a receita em real.
4. **Sugestões automáticas do Pro:** cerca de US$ 0,15 por mês por Pro, não cobradas
   (não entram no teto diário).
5. **Plano gratuito sem confirmação de e-mail:** sem SMTP não há barreira além do
   limite por IP.

## Alavancas

- Pacote avulso de créditos (não implementado): R$ 20 por cerca de 1.300 créditos
  daria sobra parecida (cerca de 57%).
- `IA_ESFORCO=low` para o chat; Haiku para mais casos; revisar `MAX_RODADAS`.
- Preço em dólar para estrangeiros (Stripe), se houver demanda.
- "Usar minhas IAs" (delegar ao Claude Code/Codex/Gemini do cliente) tiraria custo
  de API do Jarvis, mas depende de revisar os termos de uso de cada fornecedor.
