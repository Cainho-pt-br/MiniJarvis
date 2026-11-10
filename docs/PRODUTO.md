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

## 2. Onde hospedar: Vercel + Postgres (Neon)

A Vercel roda o app como função serverless. Como o disco dela é apagado a cada
execução, **o banco precisa ser externo**: o código usa Postgres sempre que
existe a variável `DATABASE_URL` (ou `POSTGRES_URL`). O jeito mais simples é o
**Neon**, que a própria Vercel cria e liga ao projeto em dois cliques, com
plano gratuito. Supabase também serve.

Arquivos que fazem isso funcionar: `index.py` na raiz (onde o preset FastAPI
da Vercel procura o `app`), `requirements.txt` e `.vercelignore` (só a versão
online sobe; broker e agente ficam de fora).

### Passo a passo (pelo painel, sem terminal)

1. Em vercel.com, **Add New → Project** e importe o repositório
   `Cainho-pt-br/MiniJarvis` do GitHub. Root Directory: `./`. Application
   Preset: **FastAPI** (detectado sozinho). Não precisa mudar o build.
2. Antes de publicar, em **Environment Variables**, adicione:
   - `ANTHROPIC_API_KEY`: gerada em console.anthropic.com (coloque um limite
     de gasto mensal lá também).
3. Clique **Deploy**. O primeiro deploy sobe, mas ainda sem banco.
4. No projeto, aba **Storage → Create Database → Neon (Postgres)** e conecte
   ao projeto. Isso cria `DATABASE_URL` sozinho.
5. Aba **Deployments → ⋯ → Redeploy**, para o app enxergar o banco.
6. Abra o link `.vercel.app`: a tela de cadastro aparece e as tabelas são
   criadas no primeiro acesso.
7. Domínio próprio: **Settings → Domains**.

A partir daí, cada `git push` na `main` publica sozinho, e cada PR ganha um
link de pré-visualização.

### Pelo terminal (alternativa)

```bash
npm i -g vercel
vercel login
vercel link
vercel env add ANTHROPIC_API_KEY
vercel --prod
```

### Limites a conhecer

- **Duração da função:** o padrão da Vercel (Fluid compute) cobre com folga
  o tempo de uma resposta de chat.
- **Streaming:** o app envia a resposta aos pedaços (SSE). Confira no
  primeiro deploy se o texto aparece aos poucos; se vier tudo de uma vez,
  funciona do mesmo jeito, só sem o efeito de digitação.
- **Limite de tentativas de login** fica na memória de cada instância. Em
  serverless isso é só uma proteção básica; o próximo passo é guardar no banco
  ou usar o Firewall da Vercel.
- **Plano Hobby (grátis) da Vercel é só para uso não comercial.** Quando
  começar a cobrar dos usuários, o projeto precisa estar no plano Pro.

Para testar no seu computador antes:
```bash
pip install -r nuvem/requirements.txt
COOKIE_SEGURO=0 IA_FAKE=1 uvicorn index:app --reload
```

O `Dockerfile` continua no repositório, caso um dia queira sair da Vercel
(Railway, Fly.io ou um servidor próprio).

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

### Modelos e a conta que precisa fechar

| Plano | Modelo | Custo por mensagem típica* | Mensagens que os créditos pagam |
|---|---|---|---|
| Grátis | `claude-haiku-5-5` | ~US$ 0,0006 (cobra o mínimo de 1 crédito) | ~300 |
| Básico | `claude-sonnet-5-5` | ~US$ 0,012 (12 créditos) | ~165 por mês |
| Pro | `claude-sonnet-5-5` | ~US$ 0,012 (12 créditos) | ~375 por mês |

\* ≈2 mil tokens de entrada e ≈800 de saída. A cobrança real usa os tokens de
cada resposta, com o desconto do cache de prompt.

- O **Haiku 5.5** deixa o plano grátis quase sem custo (300 créditos custam no
  máximo US$ 0,30 por pessoa), então dá para ser generoso e deixar a pessoa
  usar bastante antes de pagar.
- O **Sonnet 5.5** dá respostas melhores nos planos pagos, que é o motivo de
  assinar.
- No Básico, o custo de IA fica em ~US$ 2 (≈R$ 11), cerca de 37% do preço; o
  resto paga taxa de pagamento (~5%), imposto, Vercel e banco.
- Os modelos são trocáveis por variável de ambiente (`IA_MODELO_GRATIS`,
  `IA_MODELO_PAGO`), sem mexer no código.

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
| 1 | Nuvem: contas, chat em streaming, histórico, créditos | Feito |
| 2 | Deploy na Vercel + Neon | Feito |
| 3 | Parque por conta: pareamento por código, mapa, tarefas, cérebro, rotinas, sugestões, Jarvis com ferramentas | Feito |
| 4 | Segurança para leigos (e-mail confirmado, código em navegador novo, modo seguro, ações prontas) | Feito |
| 5 | Instalador de um comando para Windows, Mac e Linux | Feito |
| 6 | Pagamento (Mercado Pago) ligado aos botões de assinatura (hoje viram lista de espera) | Próximo |
| 7 | Login com Google, link compartilhável, indicação | |
| 8 | Termos de uso e política de privacidade (LGPD), domínio próprio | |

---

## 6. Como o parque funciona na nuvem

- **Conectar:** "Conectar máquina" gera um código de 6 letras (15 min) e um
  comando de instalação. O agente troca o código por um token próprio, gravado
  em `~/.minijarvis/agente.json` (permissão só do dono); o banco guarda só o
  hash. "Desconectar" invalida o token na hora.
- **Isolamento:** cada agente só vê e altera tarefas da própria conta.
- **Custo na Vercel:** o servidor dita o ritmo do agente (`PARQUE_HEARTBEAT_S`,
  padrão 60 s; espera longa `PARQUE_ESPERA_S`, 50 s): cerca de 3 requisições
  por minuto por máquina ligada.
- **Rotinas diárias:** a Vercel chama `/api/cron/rotinas` às 9h UTC (6h em
  Brasília), autenticada por `CRON_SECRET`.
- **Planos:** Grátis conecta 1 máquina, Básico 2, Pro 4. Rotinas diárias
  são do Pro.
- **Admin:** quem está em `ADMIN_EMAILS` vê a aba Admin (contas, lista de
  espera, troca de plano e créditos).

## 7. Segurança para leigos

- **Cadastro:** e-mail confirmado por código antes de ganhar créditos ou usar
  a IA; o mesmo e-mail escrito de outro jeito (pontos e +apelido do Gmail) não
  cria outra conta; e-mails descartáveis são recusados; no máximo 3 contas
  por IP por hora e 8 por dia. Os limites ficam no banco (valem em todas as
  instâncias da Vercel). Sem SMTP configurado, a confirmação fica desligada.
- **Login:** navegador novo pede um código enviado por e-mail (verificação em
  duas etapas sem aplicativo). 8 tentativas por conta a cada 15 min. "Sair de
  todos os dispositivos" em Planos > Sua conta.
- **Modo seguro (padrão de toda máquina):** só roda ações prontas e revisadas
  (`nuvem/acoes.py`): ver disco, informações, programas pesados, maiores
  arquivos, limpar temporários com mais de 7 dias, organizar Downloads (sem
  apagar nada) e testar a internet. Comandos livres e Claude Code só no modo
  livre, que exige a senha da conta para ligar.
- **Aprovação:** tudo que o Jarvis prepara no chat espera o clique em
  "Aprovar", com explicação em linguagem simples; comandos livres aparecem
  com aviso.
- **Painel:** não pode ser embutido em outro site (X-Frame-Options/CSP), o que
  impede enganar alguém para clicar em "Aprovar".
- **Agente:** só conversa com o Jarvis por HTTPS (exceto endereço local).

## 8. Instalação para leigos

Em "Conectar máquina" o painel mostra um comando só, para Windows
(PowerShell), Mac ou Linux (Terminal), e explica como abrir o terminal. O
script (`/instalar.ps1` e `/instalar.sh`, com o código embutido) instala o
Python se faltar (Windows, via winget; no Mac pede as ferramentas da Apple),
baixa o agente, conecta a máquina e deixa ligando sozinho com o computador
(tarefa agendada, launchd, systemd ou cron). Sem senha de administrador.
Para remover: `/desinstalar.ps1` ou `/desinstalar.sh`.

O workflow `.github/workflows/testes.yml` roda os testes (SQLite e Postgres)
e testa a instalação e todas as ações em Windows, macOS e Linux de verdade.
