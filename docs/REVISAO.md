# Revisão do projeto: o que falta para virar renda recorrente

Revisão feita em 10/11/2026 sobre o código do PR #4 (branch
`claude/modest-brahmagupta-po9asl`), o banco de produção (Neon) e o deploy na
Vercel.

## Onde estamos

| Fato | Situação |
|---|---|
| Contas no banco de produção | 1 (a sua). 0 máquinas, 0 conversas, 0 tarefas, 0 na lista de espera |
| Site no ar | Código do PR #3: **ninguém consegue criar conta nem entrar** (bug do modal, corrigido no PR #4, ainda não mesclado) |
| Cobrança | Não existe. Os botões "Quero o Pro" só guardam quem clicou |
| E-mail transacional | Não configurado (confirmação de e-mail fica desligada) |
| Plano da Vercel | Hobby: proibido para uso comercial |
| Termos de uso / privacidade | Não existem |
| Testes | 46 unitários (SQLite e Postgres) + instalador e 7 ações testados de verdade em Windows, macOS e Linux a cada PR |

Resumo: o produto está tecnicamente pronto para alguém usar, mas **ainda não
tem como receber um real**, e o site no ar está quebrado para cadastro.

## Uma palavra sobre "renda passiva e garantida"

Não existe renda garantida em software: receita depende de gente chegar,
ficar e pagar, e nenhum código garante isso. O que **dá** para garantir, e é
o objetivo deste documento, são três coisas:

1. **Que o sistema cobre e entregue sozinho**, sem você mexer no banco para
   trocar plano ou somar créditos.
2. **Que ele nunca gaste mais do que arrecada**: teto de custo de IA,
   limites contra abuso, custo fixo baixo.
3. **Que ele não gere trabalho manual**: senha esquecida, máquina que não
   conecta, agente desatualizado e erro silencioso são as fontes clássicas de
   suporte. Cada uma precisa de resposta automática.

O "passivo" vem de automatizar essas três. O "garantido" não existe; o que
existe é não perder dinheiro enquanto o produto cresce.

## Nota por área

| Área | Nota | Por quê |
|---|---|---|
| Produto para leigos | 8 | Chat, parque, cérebro, ações prontas, instalador de um comando, erros explicados. Falta "esqueci minha senha" e login com Google |
| Segurança | 8 | E-mail confirmado, limites no banco, modo seguro, aprovação, tokens com hash, cabeçalhos. Falta teto global de gasto e reset de senha seguro |
| Custos | 7 | Haiku no grátis e Sonnet nos pagos, cobrança por token real. Falta renovação mensal automática e expiração do plano |
| Operação sem você | 4 | Nenhum alerta, nenhum relatório, agente não se atualiza, só o painel Admin |
| Cobrança | 0 | Não existe |
| Aquisição | 3 | Landing boa e com SEO básico, mas sem analytics, indicação, link compartilhável ou domínio próprio |
| Jurídico | 2 | Sem termos, sem política de privacidade, Vercel Hobby, sem CNPJ para emitir nota |
| Código e testes | 9 | Pequeno, legível, testado em três sistemas |

## Lacunas concretas encontradas no código

1. **Não há "esqueci minha senha"** (`nuvem/app.py`). Hoje quem esquece a senha
   só recupera a conta falando com você. É a maior fonte de suporte de qualquer
   SaaS. Precisa de código por e-mail + nova senha, com os mesmos limites dos
   códigos atuais.
2. **Créditos "por mês" não renovam** e **plano nunca expira**. `PLANOS`
   promete "2.000 créditos por mês", mas o único jeito de somar créditos é a
   aba Admin. Falta uma coluna `plano_ate` em `usuarios`, uma rotina diária
   (no cron que já existe) que renova créditos de quem pagou e rebaixa quem
   não pagou, e o extrato mostrando isso.
3. **Sem pagamento.** `POST /api/planos/{id}/interesse` só guarda intenção.
4. **Sem teto global de gasto de IA.** Um bug ou abuso só é contido pelo limite
   no console da Anthropic. Falta um teto diário no app (soma de `custo_usd`
   por dia em `extrato`; passou, a IA responde "voltamos amanhã" e você recebe
   um e-mail).
5. **Nenhum alerta.** Erro 500, IA fora do ar, SMTP falhando, agente velho:
   tudo vai só para os logs da Vercel. Falta um e-mail diário ao dono (novos
   cadastros, mensagens, custo, falhas, lista de espera) e um aviso imediato
   em erro repetido.
6. **O agente não se atualiza.** `agente/agente.py` 0.3.0 é baixado uma vez
   pelo instalador e nunca mais. Toda correção futura exigiria pedir a cada
   pessoa que rode o comando de novo. O heartbeat já manda a versão; falta a
   resposta trazer "versão mínima" e o agente baixar `/agente.py` e reiniciar.
7. **Saldo só é checado antes da mensagem.** Com 1 crédito dá para gastar até
   ~100 numa resposta com 6 rodadas de ferramentas e 16 mil tokens
   (`MAX_RODADAS`, `IA_MAX_TOKENS`). Perda pequena, mas corrigível: conferir
   o saldo entre rodadas e usar `max_tokens` menor no plano grátis.
8. **Espera longa na Vercel.** Cada máquina ligada mantém uma função presa até
   50 s, 3 vezes por minuto. Com Fluid Compute o tempo ocioso custa pouco, mas
   é preciso acompanhar o uso quando houver dezenas de máquinas; no plano
   grátis dá para usar 30 s de espera e heartbeat de 90 s.
9. **E-mail.** Gmail com senha de app serve para começar, mas tem limite de
   ~500 e-mails/dia e cai em spam com facilidade. Um serviço transacional
   (Resend, Brevo, Postmark) com domínio próprio resolve os dois e tem plano
   gratuito suficiente para o início.
10. **Sem analytics.** Não dá para saber onde as pessoas desistem (landing →
    cadastro → confirmação → primeira mensagem → máquina conectada). Vercel
    Analytics ou Umami resolvem sem cookies.
11. **Sem termos de uso e política de privacidade.** Obrigatório para cobrar
    (LGPD, CDC) e para o Mercado Pago aprovar a conta.
12. **Vercel Hobby** não permite uso comercial; ao ligar o pagamento, passar
    para o Pro (US$ 20/mês).

## Conta de padaria

| Item | Valor |
|---|---|
| Custo fixo | Vercel Pro ≈ R$ 110/mês + domínio ≈ R$ 4/mês. Neon e e-mail no gratuito até centenas de contas |
| Custo de IA por assinante Básico | ≈ US$ 2 (R$ 11) se usar todos os 2.000 créditos |
| Custo de IA por assinante Pro | ≈ US$ 4,5 (R$ 25) se usar todos os 4.500 |
| Taxa do Mercado Pago | cartão ≈ 5% + R$ 0,49; Pix ≈ 1% |
| Margem por assinante | Básico ≈ R$ 17; Pro ≈ R$ 31 |
| Ponto de equilíbrio | entre 4 (Pro) e 7 (Básico) assinantes |
| Custo de um usuário grátis | no máximo US$ 0,30 (300 créditos de Haiku), na prática bem menos |

Ou seja: a partir de ~7 assinantes o projeto se paga; cada assinante além
disso é margem. Mil usuários grátis custam no pior caso US$ 300 uma única
vez, por isso o teto diário de gasto (item 4) importa.

## Plano em três fases

### Fase A: poder receber (1 a 2 semanas de trabalho)

1. Mesclar o PR #4 e conferir cadastro/login no ar.
2. "Esqueci minha senha" por código no e-mail.
3. E-mail transacional com domínio próprio (ou Gmail do projeto para começar).
4. **Pagamento pelo Mercado Pago:** assinatura mensal no cartão (Pro e Básico)
   e **pacote avulso de créditos por Pix** (quem não quer assinar compra
   R$ 20 de créditos). O webhook soma créditos, grava `plano_ate` e tudo
   aparece no extrato. Nada passa pela sua mão.
5. Renovação e rebaixamento automáticos no cron diário.
6. Termos de uso e política de privacidade (páginas no site, aceite no
   cadastro). MEI para emitir nota.
7. Vercel Pro e domínio próprio.

### Fase B: rodar sem você (1 semana)

8. Relatório diário por e-mail ao dono + alerta imediato em falha repetida.
9. Teto diário de gasto de IA com desligamento automático.
10. Auto-atualização do agente.
11. Analytics do funil (landing → cadastro → confirmação → primeira mensagem
    → máquina conectada).
12. Saldo conferido entre rodadas; `max_tokens` menor no grátis.

### Fase C: crescer sozinho (contínuo)

13. Login com Google (cadastro em 10 segundos).
14. Indicação: quem indica e quem entra ganham créditos.
15. Link compartilhável de uma resposta do Jarvis com "experimente grátis".
16. Conteúdo para SEO: "como limpar o PC", "organizar downloads", "o que
    está deixando o computador lento", cada um terminando no Jarvis.
17. Programa de afiliados (20% recorrente) para criadores de conteúdo.

## Decisões que só você pode tomar

- Mesclar o PR #4 (sem isso, nada do resto acontece).
- Nome e domínio do produto (afeta e-mail, termos e Mercado Pago).
- Conta no Mercado Pago (precisa de CPF ou CNPJ; para nota fiscal, MEI).
- Qual serviço de e-mail (recomendação: Resend ou Brevo com o domínio).
