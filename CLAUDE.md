# MiniJarvis: guia para agentes

Leia este arquivo inteiro antes de mexer. Ele é o ponto de partida; os
detalhes estão em `docs/`.

## O que é

**Jarvis** é um assistente de IA em português para pessoas comuns: conversa,
lembra (o "cérebro") e cuida dos computadores da pessoa (o "parque"), que
conecta com um comando colado no terminal. Produto online, cobrado por
assinatura com créditos. Produção: https://mini-jarvis-jet.vercel.app

O repositório tem duas gerações:

| Pasta | O que é | Mexer? |
|---|---|---|
| `nuvem/` | **O produto** (FastAPI, Vercel, Postgres/Neon). Quase tudo acontece aqui | Sim |
| `agente/agente.py` | Agente que roda no computador do cliente. Também serve o broker legado | Com cuidado |
| `index.py` | Ponto de entrada da Vercel (`from nuvem.app import app`) | Raramente |
| `broker/`, `deploy/`, `orquestrador-spec.md` | Projeto original local (Raspberry Pi + Tailscale). **Legado**, fora da Vercel (`.vercelignore`) | Não, salvo pedido |

## Rodar e testar

```bash
pip install -r nuvem/requirements.txt pytest httpx pyflakes
python -m pyflakes nuvem
python -m pytest -q nuvem/tests                       # SQLite
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres python -m pytest -q nuvem/tests   # Postgres
COOKIE_SEGURO=0 IA_FAKE=1 EMAIL_FAKE=1 MP_FAKE=1 uvicorn index:app --reload   # servidor local sem custo
```

**Rode os testes nos dois bancos antes de abrir PR**: o Postgres já pegou
erros que o SQLite deixa passar. O CI (`.github/workflows/testes.yml`) roda os
dois e ainda instala o agente de verdade em Windows, macOS e Linux
(`nuvem/tests/e2e_instalador.py`). Para o Postgres local, suba um cluster
próprio e use a URL acima.

## Mapa de `nuvem/`

| Arquivo | Responsabilidade |
|---|---|
| `app.py` | Contas (cadastro, e-mail confirmado, login, "esqueci a senha", apagar conta), chat com streaming e ferramentas, planos, admin, páginas legais e página inicial |
| `parque.py` | Máquinas, pareamento por código, protocolo do agente, tarefas (jobs), cérebro (notas), rotinas, cron diário, limite de máquinas por plano |
| `jarvis.py` | Ferramentas que a IA usa no chat; sugestões do parque (`atualizar_sugestoes`, `sugestoes_automaticas`) |
| `i18n.py` | Internacionalização (`pt-BR`, `en`, `es`), resolução de idioma por requisição e textos da API/planos |
| `ia.py` | Cliente da Anthropic, modelos por plano, **tabela de preços** (`PRECOS`), `FakeIA` para testes |
| `acoes.py` | As 7 ações prontas do modo seguro (comando Windows em PowerShell codificado e unix) |
| `falhas.py` | Explica em linguagem simples (`pt-BR`, `en`, `es`) por que uma tarefa falhou |
| `instaladores.py` | Scripts `/instalar.ps1`, `/instalar.sh`, `/desinstalar.*` servidos pelo site |
| `pagamento.py` | Mercado Pago: assinatura no cartão, Pix, webhook (`FakeMP` nos testes) |
| `cobranca.py` | Ativar plano (31 dias + créditos), rebaixar vencidos, estorno |
| `operacao.py` | Teto diário de gasto de IA, funil, relatório diário, alertas |
| `crescimento.py` | Indicação, conversa compartilhada (`/c/token`), guias de SEO, `robots.txt`, `sitemap.xml` |
| `correio.py` | E-mail por SMTP (biblioteca padrão) |
| `banco.py` | Camada de banco única para Postgres e SQLite, schema e migrações |
| `web/index.html` | **Todo o front-end** (arquivo único, JavaScript puro). `web/guias/*.html`, `termos.html`, `privacidade.html` |
| `tests/` | Testes (SQLite e Postgres). Fixture `cliente` em `conftest.py` |

## Regras de negócio (não quebre)

- **Planos:** Grátis 1 máquina e 120 créditos iniciais (modelo Haiku 5.5, ~60 mensagens); Básico R$ 29,90, 2 máquinas, 240 créditos/mês (modelo Haiku 5.5, ~120 mensagens/mês); Pro R$ 59,90, 4 máquinas, 4.500 créditos/mês (modelo Sonnet 5.5 no chat e tarefas, ~375 mensagens/mês), rotinas diárias e sugestões automáticas **sem gastar créditos**; Turbo R$ 119,90, 10 máquinas, 8.000 créditos/mês (modelo Opus 5.5 no chat e tarefas, ~335 mensagens/mês), rotinas diárias e sugestões automáticas **sem gastar créditos**. Sugestões do parque usam Haiku 5.5 em todos os planos. Todas as regras por plano (`modelo`, `modelo_sugestoes`, `maquinas`, `creditos`, `rotinas_diarias`, `sugestoes_auto`, `max_tokens`, `mensagens_estimadas`) ficam em `PLANOS` (`app.py`).
- **Fallback de modelo:** se a chamada ao modelo do plano falhar por erro de API ou sobrecarga antes de escrever, tenta uma única vez o modelo imediatamente abaixo (Opus → Sonnet; Sonnet → Haiku; Haiku não tem fallback), registra `operacao.incidente("ia_fallback")` e cobra pelo modelo que realmente respondeu.
- **Créditos:** 1 crédito = US$ 0,001, descontado pelos **tokens reais** de cada resposta. O custo de IA nunca pode passar do crédito cobrado. Conta completa em `docs/ECONOMIA.md`.
- **Quem desce de plano** mantém as máquinas, mas só as mais antigas, até o limite, seguem ativas; as outras ficam **bloqueadas** (sem tarefas) até desconectar uma ou assinar de novo (`parque.liberadas`).
- **Plano pago** vale 31 dias por pagamento aprovado (`usuarios.plano_ate`); o cron diário rebaixa os vencidos mantendo os créditos que sobraram.
- **Segurança:** toda máquina nasce no **modo seguro** (só ações prontas); modo livre exige a senha da conta. Tarefas criadas pelo Jarvis nascem "aguardando" e só rodam depois do "Aprovar". Admin exige e-mail confirmado (`ADMIN_EMAILS`). O token do agente é guardado só como hash.
- **Pagamento:** o webhook nunca confia no aviso: busca o pagamento na API do Mercado Pago. `pagamentos.referencia` é UNIQUE e a troca para "aprovado" é atômica (não credite fora desse caminho).
- **Teto de gasto:** `TETO_DIARIO_USD` para a IA do dia inteiro e avisa o dono por e-mail.
- **Idioma:** suporta `pt-BR` (padrão e fallback), `en` e `es`, sempre em linguagem simples e sem jargão. Ordem de escolha: preferência salva (`usuarios.idioma` + `localStorage`) > `navigator.languages` > cabeçalho `Accept-Language` > `pt-BR`. Preços continuam em reais (BRL), com aviso claro para quem usa em `en` ou `es`.

## Armadilhas já pisadas

- O SQL é escrito com `?` e `Banco._sql` troca por `%s` no Postgres. **Nunca ponha `%` literal no SQL** (nem em `LIKE`); passe o padrão como parâmetro.
- `_TABELAS` usa `str.format`: chaves `{}` em comentários ou valores do schema quebram (escreva `{{}}`).
- SQLite não aceita `ADD COLUMN ... UNIQUE`: na migração, crie a coluna e depois um `CREATE UNIQUE INDEX`.
- Leia colunas **por nome** (`row["x"]`), nunca por posição. Use `con.inserir(...)` para obter o id novo.
- Alterar `agente/agente.py`: aumente `VERSAO`; os agentes instalados se atualizam sozinhos quando o site serve uma versão maior. Mantenha o agente compatível com o broker legado.
- A IA com `fallbacks: "default"` só vale para Sonnet/Opus (não para Haiku). Testes usam `IA_FAKE=1`, e-mail usa `correio.CAIXA_DE_TESTE`, pagamento usa `MP_FAKE=1`.
- Front-end: confira a sintaxe do script depois de editar (`node -e "new Function(...)"`) e teste de verdade com Playwright, inclusive em tela de celular (390 px).

## Deploy e produção

- `git push` na `main` publica na Vercel (preview em PR). O cron diário (`vercel.json`, 09:00 UTC) chama `/api/cron/rotinas` com `CRON_SECRET`: rebaixa planos vencidos, renova sugestões do Pro, dispara rotinas e manda o relatório.
- Variáveis de ambiente: `.env.example` é a lista completa e comentada. Quem configura é o dono, na Vercel.
- **Se a Vercel falhar sem erro de código** (já aconteceu: `Resource provisioning timed out`), siga `docs/OPERACAO.md`. Cuidado: `vercel link` cria um `.env.local` com token e `vercel build` local **ignora o `.vercelignore`**; nunca publique esses arquivos.
- Banco: Neon (Postgres). Alterações de schema entram em `banco.py` como `CREATE ... IF NOT EXISTS` ou na lista `_MIGRACOES` (idempotentes, rodam na partida).

## Como trabalhar neste repositório

- Desenvolva na branch que a sessão designar, abra o PR **como rascunho** e espere o CI ficar verde. **Quem mescla é o dono**; só mescle com o pedido explícito dele.
- Commits em português, no formato `tipo(escopo): resumo` com explicação curta do porquê.
- **Nunca** coloque segredos, tokens, e-mails ou dados pessoais do dono em código, commit, PR ou issue. O dono digita segredos direto na Vercel.
- Ações que mudam o mundo fora do repositório (deploy em produção, banco de produção, cobranças) pedem confirmação do dono primeiro.
- Todo bug corrigido ganha um teste que falha sem a correção.

## Estado e próximos passos

Código completo até o PR #6. **Falta só configuração do dono** (não é código): credenciais do Mercado Pago, e-mail de envio (SMTP), Vercel Pro e domínio, MEI, limite de gasto na Anthropic. Sem credenciais do Mercado Pago os botões de assinar viram lista de espera; sem SMTP a confirmação de e-mail, o "esqueci a senha" e o relatório ficam desligados.

Ideias já discutidas, ainda não feitas: login com Google (precisa de credencial do dono), pacote avulso de créditos, programa de afiliados, preços em dólar e Stripe para estrangeiros, e "usar minhas IAs" (delegar tarefas ao Claude Code, Codex ou Gemini que o cliente já assina; depende de revisar os termos de uso de cada fornecedor antes).

## Documentos

- `docs/PRODUTO.md`: estratégia, hospedagem, monetização, parque e segurança
- `docs/ECONOMIA.md`: custo de IA por mensagem, margem por plano, riscos
- `docs/OPERACAO.md`: o que olhar, alertas, atendimento, deploy quando a Vercel falha
- `docs/CONCORRENTES.md`: análise do LocalAI e o que as pessoas reclamam
- `docs/REVISAO.md`: retrato da revisão de 10/11/2026 (histórico)
