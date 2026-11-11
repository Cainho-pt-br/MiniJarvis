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
- **Limites de tentativas** (cadastro, login, códigos, pagamentos) ficam no
  banco (`limites`), então valem entre todas as instâncias da Vercel.
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
| Grátis | R$ 0 | 120 (uma vez) | Experimentar sem cartão (1 computador) |
| Básico | R$ 29,90 | 240 | Uso diário (~120 mensagens, até 2 computadores) |
| Pro | R$ 59,90 | 4.500 + parque automático | Uso intenso (~375 mensagens com Sonnet, até 4 computadores) |
| Turbo | R$ 119,90 | 8.000 + parque automático | Quem tem muitos computadores ou quer o máximo (~335 mensagens com Opus, até 10 computadores) |

1 crédito = US$ 0,001 de custo de IA. A conta é feita com os tokens reais de
cada resposta (`nuvem/ia.py` → `custo_usd`), então o preço nunca fica abaixo
do custo.

### Modelos e a conta que precisa fechar

| Plano | Modelo (chat e tarefas) | Modelo (sugestões) | Custo médio por mensagem* | Mensagens estimadas |
|---|---|---|---|---|
| Grátis | `claude-haiku-5-5` | `claude-haiku-5-5` | ~2 créditos | ~60 |
| Básico | `claude-haiku-5-5` | `claude-haiku-5-5` | ~2 créditos | ~120 por mês |
| Pro | `claude-sonnet-5-5` | `claude-haiku-5-5` | ~12 créditos | ~375 por mês |
| Turbo | `claude-opus-5-5` | `claude-haiku-5-5` | ~24 créditos | ~335 por mês |

\* Mistura típica de perguntas curtas e tarefas com ferramentas. A cobrança real
usa os tokens de cada resposta, com o desconto do cache de prompt.

- O **Haiku 5.5** atende o plano Grátis (120 créditos custam no máximo US$ 0,12
  por conta nova), o plano Básico (240 créditos rendem ~120 mensagens/mês e
  custam no máximo US$ 0,24 ≈ R$ 1,32, ou 4,4% do preço) e as sugestões do parque
  em todos os planos.
- O **Sonnet 5.5** equipa o plano Pro no chat e nas tarefas.
- O **Opus 5.5** equipa o plano Turbo no chat e nas tarefas, mantendo 58% de
  margem líquida mesmo no pior caso (ver `docs/ECONOMIA.md`).
- Se a chamada ao modelo do plano falhar por erro de API ou sobrecarga, o Jarvis
  tenta uma única vez o modelo imediatamente abaixo (Opus → Sonnet; Sonnet → Haiku),
  registra `operacao.incidente("ia_fallback")` e cobra pelo modelo que respondeu.
- Os modelos são trocáveis por variável de ambiente (`IA_MODELO_HAIKU`,
  `IA_MODELO_SONNET`, `IA_MODELO_OPUS`), sem mexer no código.

### "Usar minhas IAs" (não implementado — depende de revisão de termos)

Muitos usuários avançados já pagam assinaturas pessoais (Claude Pro/Max com
Claude Code, ChatGPT Plus/Pro com Codex CLI, ou Google One AI Premium com
Gemini CLI) nas próprias máquinas. O agente do Jarvis já detecta se a máquina
tem `claude`, `codex` ou `gemini` instalados (`tem_claude`, `tem_codex`,
`tem_gemini` no heartbeat) e, no modo livre, já sabe rodar um job do tipo
`claude` localmente.

- **Desenho pensado (quando e se for liberado):**
  1. Na tela da máquina (ou no perfil), o usuário marca quais IAs instaladas no
     próprio computador deseja autorizar o Jarvis a acionar.
  2. Quando o usuário pede uma tarefa complexa no painel, o Jarvis no servidor
     (usando Haiku apenas para orquestrar, ~1–2 créditos) cria uma tarefa
     aguardando aprovação do usuário para delegar a execução ao CLI local
     (`claude`, `codex` ou `gemini`) na máquina do próprio usuário.
  3. O agente executa o CLI local com as credenciais que nunca saem da máquina
     do cliente e devolve apenas o texto do resultado ao painel.
- **Riscos:**
  1. **Restrição contratual dos fornecedores:** a Anthropic e outros provedores
     restringem o uso de assinaturas pessoais (OAuth / sessão do CLI) por
     produtos ou serviços de terceiros. Se o provedor interpretar que um painel
     web externo despachando comandos ao CLI local viola os Termos de Uso
     pessoais, a conta do usuário naquele fornecedor pode ser suspensa.
  2. **Experiência frágil para leigos:** o CLI local pode pedir login de novo,
     bater no limite de mensagens da assinatura pessoal do cliente ou mudar o
     formato de saída sem aviso, gerando falhas difíceis de explicar.
  3. **Segurança:** o CLI local roda com as permissões do usuário na máquina;
     por isso só poderia existir em máquinas no modo livre e sempre com aprovação
     explícita por tarefa.
- **Pergunta de termos que falta responder (para Anthropic, OpenAI e Google):**
  > *Os Termos de Uso da assinatura pessoal (Consumer / Pro / Max / Plus) permitem
  > que o próprio titular da conta autorize um agente instalado no computador dele
  > a invocar a ferramenta oficial de linha de comando (`claude` / `codex` / `gemini`)
  > em nome dele, a partir de um comando aprovado por ele num painel web de
  > terceiro, desde que as credenciais nunca saiam da máquina do usuário nem sejam
  > compartilhadas com terceiros?*

Enquanto essa resposta não estiver confirmada para cada fornecedor, o recurso
**não deve ser exposto** como benefício de plano nem automatizado no chat.

### Pagamento

- **Mercado Pago**: Pix e cartão, assinatura recorrente, mais conhecido pelo
  público leigo brasileiro. Recomendado para começar.
- **Stripe**: melhor se for vender fora do Brasil (também aceita Pix).

Já está no código (`nuvem/pagamento.py`): **assinatura mensal no cartão**
(preapproval do Mercado Pago, renovada pelo MP todo mês) e **Pix de um mês**
(QR code no painel). O webhook busca o pagamento na API do MP antes de dar
créditos e nunca credita duas vezes. Cada pagamento aprovado dá 31 dias do
plano e os créditos dele (`nuvem/cobranca.py`); o cron diário rebaixa para o
grátis quem venceu. Para ligar: `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET` e
`SITE_URL` na Vercel, e cadastrar `https://SEU-SITE/api/pagamentos/webhook`
nos Webhooks do Mercado Pago (eventos: Pagamentos e Planos e assinaturas).
Sem token, os botões de assinar voltam a ser lista de espera.

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

- **Termos de uso e política de privacidade** (LGPD): já existem em `/termos`
  e `/privacidade`, com aceite obrigatório no cadastro. Preencha
  `RESPONSAVEL_LEGAL` (nome/CNPJ) e `EMAIL_CONTATO` na Vercel.
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
| 6 | Pagamento (Mercado Pago: cartão recorrente e Pix), renovação e vencimento automáticos, "esqueci minha senha", termos e privacidade | Feito (falta o token do MP na Vercel) |
| 7 | Relatório diário ao dono, alertas, teto de gasto de IA, auto-atualização do agente, funil sem cookies | Feito |
| 8 | Indicação com créditos, link compartilhável de conversa, guias para SEO, sitemap | Feito |
| 9 | Login com Google, domínio próprio, afiliados | |

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
- **Planos:** Grátis conecta 1 máquina, Básico 2, Pro 4, Turbo 10. Rotinas
  diárias e sugestões automáticas (sem gastar créditos) estão incluídas no Pro e
  no Turbo.
- **Quem desce de plano** mantém as máquinas conectadas, mas só as mais
  antigas, até o limite do novo plano, continuam ativas; as outras ficam
  **bloqueadas** (seguem ligadas, não recebem tarefas, o painel avisa e
  convida a assinar). Desconectar uma libera a próxima; voltar ao plano maior
  libera todas, inclusive as tarefas que ficaram na fila.
- **Admin:** quem está em `ADMIN_EMAILS` vê a aba Admin (contas, lista de
  espera, troca de plano e créditos).

## 6.1 Operação sem o dono (`nuvem/operacao.py`)

- **Teto diário de IA** (`TETO_DIARIO_USD`, padrão US$ 10): somando todo
  mundo, passou do teto, a IA para até o dia virar e o dono recebe um e-mail.
- **Alertas:** 5 falhas da IA ou do e-mail em 10 minutos mandam um aviso
  (no máximo um por hora).
- **Relatório diário** (cron das 9h UTC) para `ADMIN_EMAILS`: contas, uso,
  custo, pagamentos, falhas e o funil. A aba Admin mostra o mesmo e tem o
  botão "mandar agora".
- **Funil sem cookies:** cadastro → e-mail confirmado → primeira mensagem →
  máquina conectada → pagamento, contado no banco (`eventos`).
- **Agente que se atualiza:** o heartbeat devolve a versão do `agente.py`
  que o site serve; o agente instalado, quando ocioso, baixa a versão nova,
  confere que compila, troca o arquivo e reinicia. Para publicar uma versão,
  basta aumentar `VERSAO` em `agente/agente.py` e fazer o deploy.
- **Saldo entre rodadas:** uma resposta com ferramentas não passa do saldo;
  o plano grátis usa `max_tokens` menor (`IA_MAX_TOKENS_GRATIS`).

## 6.2 Crescimento (`nuvem/crescimento.py`)

- **Indicação:** cada conta tem um link (`/?ref=código`). Quem entra por ele
  ganha `BONUS_INDICADO` créditos ao confirmar o e-mail; quem indicou ganha
  `BONUS_INDICADOR`, até `TETO_INDICADOR` no total.
- **Link compartilhável:** "Compartilhar" no chat publica a conversa em
  `/c/token` (sem índice no Google), com botão "criar meu Jarvis" que já leva
  o código de indicação de quem publicou. Dá para despublicar.
- **Guias** (`nuvem/web/guias/*.html`, servidos em `/guia/slug`): conteúdo
  para quem procura "computador lento", "organizar downloads" etc., listados
  na página inicial e no `sitemap.xml`. Para adicionar um guia, basta criar o
  arquivo HTML com `<title>` e `<meta name="description">`.

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
