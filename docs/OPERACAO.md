# Operação do Jarvis: o que fazer quando algo foge do normal

Resumo para quem opera o serviço (hoje, o dono). O dia a dia não exige nada:
o cron diário rebaixa planos vencidos, renova sugestões do Pro, dispara as
rotinas e manda o relatório por e-mail (quando o SMTP está configurado).

## Onde olhar

| O quê | Onde |
|---|---|
| Contas, uso, custo, funil, pagamentos | aba **Admin** do painel (ou o e-mail diário) |
| Erros do servidor | Vercel → projeto → **Logs** |
| Banco de dados | Neon → projeto `ancient-cell-67338910` (histórico de 6 h a 30 dias para voltar no tempo, conforme o plano) |
| Pagamentos | Mercado Pago → Atividade; webhooks em Suas integrações → Webhooks |
| Custo de IA | console da Anthropic (compare com "custo de IA" do relatório) |

## Alertas que você pode receber por e-mail

- **Teto diário de IA atingido:** o Jarvis parou de responder até o dia virar
  (UTC). Se o uso é legítimo, suba `TETO_DIARIO_USD` na Vercel e faça Redeploy.
- **N falhas de IA/e-mail em 10 minutos:** veja os Logs. Causas comuns: chave
  da Anthropic sem saldo ou revogada; senha do SMTP trocada.
- **Pagamento estornado:** o plano e os créditos do mês já foram retirados.

## Variáveis de ambiente (Vercel)

Lista completa e comentada em `.env.example`. Depois de mudar qualquer uma,
é preciso um novo deploy (Deployments → ⋯ → Redeploy).

## Quando o deploy da Vercel falha sem erro de código

Em 10/11/2026 a Vercel recusou seis deploys seguidos (inclusive de um commit
que funcionava) com `BUILD_FAILED: Resource provisioning timed out`, sem log
de build. O código estava bom: o build local passava. Se acontecer de novo:

1. Confirme que não é o código: `python -m pytest -q nuvem/tests` e o CI do
   GitHub verdes. Veja a causa real com
   `vercel api /v13/deployments/<dpl_...> --scope <time>` (campo `errorMessage`).
2. Tente **Redeploy** pelo painel da Vercel; espere uma meia hora e tente de novo.
3. Se persistir, publique o build local (a produção continua na versão anterior
   enquanto isso, nada fica fora do ar):

```bash
# numa cópia limpa do commit que está na main, sem as pastas que o .vercelignore exclui
git archive origin/main | tar -x -C /tmp/jarvis-deploy && cd /tmp/jarvis-deploy
rm -rf broker deploy docs nuvem/tests .github
vercel link --yes --project mini-jarvis --scope <time>
rm -f .env.local                       # o link cria este arquivo com um token: NUNCA publique
vercel build --prod --yes --scope <time>
rm -f .vercel/.env.production.local .env.local
vercel deploy --prebuilt --prod --scope <time>
```

   Antes de publicar, confira que `.vercel/output/functions/fastapi.func/.vc-config.json`
   não lista nenhum `.env*` além de `.env.example`. Depois de publicar, teste
   `/saude`, `/termos` e um login com senha errada (deve dar 401, não 500).
4. Para **voltar** à versão anterior se algo der errado:
   `vercel rollback <url-do-deploy-anterior> --scope <time>`.

## Se um cliente reclamar

- **Pagou e o plano não ativou:** Admin → ache a conta → troque o plano (dá os
  créditos e 31 dias). Depois confira no Mercado Pago se o webhook chegou.
- **Esqueceu a senha:** "Esqueci minha senha" na tela de entrada (precisa do
  SMTP). Sem SMTP, não há como recuperar sem mexer no banco.
- **Quer apagar a conta:** Planos → Sua conta → Apagar minha conta (pede a
  senha e cancela a assinatura). Pedido por e-mail: faça o mesmo pelo banco e
  cancele a assinatura no Mercado Pago.
- **Máquina "bloqueada":** o plano dele conecta menos máquinas do que ele tem.
  Desconectar uma libera a próxima, ou assinar um plano maior libera todas.
- **Computador não conecta:** o painel já mostra o passo a passo e o comando
  `agente.py --diagnostico` depois de 90 s.

## Rotina mensal (15 minutos)

1. Compare a fatura da Anthropic com o "custo de IA" somado dos relatórios.
2. Veja no Admin o funil: onde as pessoas desistem (cadastro → e-mail →
   primeira mensagem → máquina → pagamento).
3. Confira se Vercel, Neon e e-mail ainda cabem no plano gratuito.
