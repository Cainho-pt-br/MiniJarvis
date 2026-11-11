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

| Tipo | Haiku (Grátis / Básico / sugestões) | Sonnet (Pro) |
|---|---|---|
| Pergunta curta | 1 crédito | 5 |
| Pedido de tarefa (1 ferramenta) | 2 | 12 |
| Analisar o parque (2 ferramentas) | 3 | 22 |
| **Custo médio típico** | **2 créditos** | **12 créditos** |

Com 1 crédito = US$ 0,001 para qualquer modelo, 1 crédito no Haiku rende de 10 a
20 vezes mais tokens do que no Sonnet:

- **Grátis (200 créditos, Haiku 5.5):** rende cerca de **100 mensagens** (entre 65 e 200),
  custando no máximo US$ 0,20 (R$ 1,10) por conta nova.
- **Básico (800 créditos/mês, Haiku 5.5):** com média de 2 créditos por mensagem,
  800 créditos rendem **cerca de 400 mensagens por mês** (entre 265 e 800), superando
  com folga a meta mínima de 300 mensagens/mês. No pior caso (100% dos créditos gastos),
  800 créditos custam US$ 0,80 = **R$ 4,41** (14,7% do preço de R$ 29,90, bem abaixo do
  teto de 45%). Mesmo que no futuro os créditos do Básico subam até 2.400 créditos
  (US$ 2,40 = R$ 13,22, ou 44,2% do preço), o custo de pior caso ainda respeita o limite
  de 45%.
- **Pro (4.500 créditos/mês, Sonnet 5.5):** com média de 12 créditos por mensagem,
  rende **cerca de 375 mensagens por mês** (entre 200 e 900). As sugestões do parque
  rodam em Haiku 5.5 em todos os planos e seguem gratuitas no Pro (~US$ 0,01/mês por
  assinante ativo).

`IA_ESFORCO` (padrão `medium`) controla quanto a IA raciocina: `low` baixa o custo
por mensagem no chat.

## Margem por plano no pior caso (cliente gasta 100% dos créditos)

Câmbio de referência: US$ 1 = R$ 5,51 (9/11/2026). Taxa do Mercado Pago estimada
em 4,98% + R$ 0,49 no cartão (**confirmar na conta**).

| | Básico (800 cr · Haiku) | Pro (4.500 cr · Sonnet) |
|---|---|---|
| Preço | R$ 29,90 | R$ 59,90 |
| Mensagens estimadas | ~400 / mês | ~375 / mês |
| IA no pior caso (100% dos créditos) | R$ 4,41 (15%) | R$ 24,80 (41%) |
| Taxa do pagamento | R$ 1,98 | R$ 3,47 |
| **Sobra no pior caso** | **R$ 23,51 (79%)** | **R$ 31,63 (53%)** |
| Sobra com 50% de uso | R$ 25,71 | R$ 44,03 |

Dólar a R$ 6,50: sobra R$ 22,72 (Básico) e R$ 27,18 (Pro).

Custos fixos aproximados: Vercel Pro US$ 20 (cerca de R$ 110), MEI (cerca de
R$ 80, confirmar com contador), domínio. Total perto de R$ 195 por mês: o ponto
de equilíbrio é de uns 9 assinantes Básico, 7 Pro ou 8 numa mistura.

## Onde ainda se perde dinheiro

1. **Contas grátis em massa:** até US$ 0,20 por conta. `TETO_DIARIO_USD` limita o
   gasto de IA do dia inteiro; deixe em 3 até ter receita e ligue o e-mail
   (confirmação de conta) antes de divulgar.
2. **Chargeback / estorno:** perde-se o valor e a IA já gasta. Há estorno
   automático de plano e créditos (`cobranca.estornar_plano`) e aviso ao dono.
3. **Câmbio:** o custo é em dólar e a receita em real.
4. **Sugestões automáticas do Pro:** usando Haiku 5.5 em todos os planos, custam
   cerca de US$ 0,01 por mês por assinante Pro (não cobradas dos créditos).
5. **Plano gratuito sem confirmação de e-mail:** sem SMTP não há barreira além do
   limite por IP.

## Alavancas

- Pacote avulso de créditos (não implementado): R$ 20 por cerca de 1.300 créditos
  daria sobra parecida (cerca de 57%).
- `IA_ESFORCO=low` para o chat; Haiku para mais casos; revisar `MAX_RODADAS`.
- Preço em dólar para estrangeiros (Stripe), se houver demanda.
- "Usar minhas IAs" (delegar ao Claude Code/Codex/Gemini do cliente) tiraria custo
  de API do Jarvis, mas depende de revisar os termos de uso de cada fornecedor.
