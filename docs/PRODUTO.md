# MiniJarvis como produto: estratégia

Este documento responde três perguntas: **qual formato** (online, instalador
ou híbrido), **onde e como hospedar**, e **como ganhar dinheiro**.

---

## 1. Formato: híbrido, com o online como porta de entrada

| | Só online | Só instalador | **Híbrido (escolhido)** |
|---|---|---|---|
| Primeiro uso | Abre um link | Baixa, instala, libera antivírus | Abre um link |
| Viralização | Alta: link no WhatsApp funciona na hora | Baixa: cada passo perde gente | Alta |
| Funciona no celular | Sim | Não | Sim |
| Acesso ao computador da pessoa | Não | Sim | Sim, para quem instalar |
| Cobrança | Simples (assinatura) | Difícil (licença, pirataria) | Simples |

**Decisão:** o produto nasce **online** (é o que viraliza e cobra fácil) e o
**instalador** vira o recurso premium: "conecte seu computador e o Jarvis
organiza arquivos, roda tarefas e trabalha enquanto você dorme". É exatamente
o que o orquestrador atual (`broker/` + `agente/`) já sabe fazer; ele passa a
falar com a nuvem em vez de um Raspberry Pi.

```
 Celular / navegador ──► Jarvis Nuvem (nuvem/) ──► API do Claude
                               ▲
                               │ conexão de saída, por código de pareamento
                               │
                  Instalador no PC (agente/agente.py empacotado)
```

O agente instalado sempre **abre a conexão de dentro para fora** (como já faz
hoje com long-polling). Assim não precisa de Tailscale, porta aberta nem
configuração de roteador: a pessoa digita um código de 6 dígitos que aparece
no site e pronto.

---

## 2. Onde hospedar

### Fase 1: começar (0 a ~1.000 usuários) — custo ~R$ 30 a 60/mês

**Recomendação: [Railway](https://railway.app) ou [Fly.io](https://fly.io)**,
usando o `Dockerfile` da raiz do repositório.

- Deploy: conecta o repositório do GitHub, cada `git push` publica sozinho.
- Banco: SQLite num volume persistente (`/dados`). Aguenta bem milhares de
  usuários com a carga de um chat.
- HTTPS e domínio próprio (`jarvis.com.br`, por exemplo) inclusos.
- Variáveis a configurar no painel: `ANTHROPIC_API_KEY`, `IA_MODELO`,
  `CREDITOS_INICIAIS`.

Passo a passo (Railway):
1. Criar conta em railway.app e "New Project → Deploy from GitHub repo".
2. Adicionar um **Volume** montado em `/dados`.
3. Em **Variables**, colocar `ANTHROPIC_API_KEY` (gerada em
   console.anthropic.com).
4. Em **Settings → Domains**, gerar um domínio ou apontar o seu.
5. Abrir o link: a tela de cadastro já aparece.

Para testar no seu computador antes:
```bash
pip install -r nuvem/requirements.txt
COOKIE_SEGURO=0 IA_FAKE=1 uvicorn nuvem.app:app --reload
```

### Fase 2: crescer (milhares de usuários pagantes)

- Trocar SQLite por **Postgres** (Supabase ou Neon, plano gratuito no começo).
  O código já concentra o acesso ao banco em `db()`/`transacao()` para
  facilitar a troca.
- Login com Google (Supabase Auth ou OAuth direto), que reduz muito o
  abandono no cadastro.
- Mais de uma instância atrás do balanceador do próprio Railway/Fly.

O Raspberry Pi continua útil como **seu** agente e ambiente de testes, mas não
deve hospedar o produto: queda de energia ou internet derruba todo mundo.

---

## 3. Monetização

### Modelo: grátis para provar + assinatura com créditos

| Plano | Preço | Créditos/mês | Para quem |
|---|---|---|---|
| Grátis | R$ 0 | 300 (uma vez) | Experimentar sem cartão |
| Básico | R$ 29,90 | 2.000 | Uso diário |
| Pro | R$ 59,90 | 4.500 + computador conectado | Uso intenso |

1 crédito = US$ 0,001 de custo de IA. A conta é feita com os tokens reais de
cada resposta (`nuvem/ia.py` → `custo_usd`), então o preço nunca fica abaixo
do custo.

### A conta que precisa fechar

Com o modelo padrão (`claude-opus-5-5`, US$ 4 / US$ 20 por milhão de tokens),
uma mensagem típica (≈2 mil tokens de entrada, ≈800 de saída) custa cerca de
**US$ 0,02 = 20 créditos**. Então:

- Grátis: ~15 mensagens. Suficiente para a pessoa sentir o valor.
- Básico: ~100 mensagens/mês. O custo de IA fica em ~US$ 2 (≈R$ 11), ou
  ~37% do preço; o resto paga taxa de pagamento (~5%), imposto e servidor.

**Alavanca de custo (decisão sua):** o modelo é configurável por
`IA_MODELO`. `claude-sonnet-5-5` custa metade, e `claude-haiku-5-5` é ~40 vezes
mais barato e serve bem para tarefas simples. Trocar o modelo permite dar
muito mais mensagens pelo mesmo preço, mas vale comparar a qualidade das
respostas antes. Um caminho comum é usar o mais barato no plano grátis e o
melhor nos pagos.

### Pagamento

- **Mercado Pago**: Pix e cartão, assinatura recorrente, mais conhecido pelo
  público leigo brasileiro. Recomendado para começar.
- **Stripe**: melhor se for vender fora do Brasil (também aceita Pix).

A integração (checkout + webhook que soma créditos em `extrato`) é a
próxima etapa do código.

### O que faz viralizar

1. **Link compartilhável de resposta**: "olha o que o Jarvis fez" com a
   resposta e um botão "experimente grátis".
2. **Indicação**: quem indica e quem é indicado ganham créditos.
3. **Tarefas prontas** para o dia a dia brasileiro (responder cliente no
   WhatsApp, explicar boleto, legenda de Instagram). Elas já estão em
   `nuvem/tarefas.py`.
4. **Cadastro em 10 segundos** (Google) e nada de cartão no plano grátis.

---

## 4. Obrigações antes de abrir ao público

- **Termos de uso e política de privacidade** (LGPD): o que é guardado, por
  quanto tempo, e que as mensagens passam pela API da Anthropic.
- **Exclusão de conta**: já existe (`DELETE /api/eu`, apaga tudo).
- **CNPJ (MEI serve no início)** para emitir nota e receber pelo Mercado Pago.
- **Limite de gastos** no console da Anthropic, para um bug não virar fatura
  alta.

---

## 5. Roteiro

| Etapa | Entrega | Situação |
|---|---|---|
| 1 | Nuvem: contas, chat em streaming, histórico, créditos, tarefas prontas, Docker | **Feito** (`nuvem/`) |
| 2 | Deploy no Railway + domínio + termos/privacidade | Próximo |
| 3 | Pagamento (Mercado Pago) e tela de planos | |
| 4 | Login com Google, link compartilhável, indicação | |
| 5 | Instalador (.exe/.dmg) do agente com código de pareamento | |
| 6 | Tarefas no computador conectado (o orquestrador atual) | |
