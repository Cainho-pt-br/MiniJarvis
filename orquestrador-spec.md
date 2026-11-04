# Orquestrador de máquinas — especificação

Painel local para enfileirar, distribuir e acompanhar tarefas entre máquinas
heterogêneas conectadas por Tailscale. Acesso de qualquer lugar pelo tailnet,
sem expor nada na internet pública.

Construção começa no Raspberry Pi 4, em Bragança Paulista.

---

## Estado do parque

| Máquina | Hardware | Papel | Disponibilidade |
|---|---|---|---|
| Raspberry Pi 4 | ARM64, 4GB, cartão SD | **Broker + UI** (fase 1) | 24/7 |
| Desktop principal | Ryzen 5 8500G, RTX 5060 Ti 16GB, 32GB DDR5 | Worker GPU | Ligado sob demanda |
| Acer Aspire | i5-7200U, 16GB, NVMe | Worker móvel | Intermitente (viagem) |
| Lenovo IdeaPad | Celeron N2840, 8GB, SSD SATA | Worker leve / cliente | Intermitente |
| Desktop H61 | i3-3220, 4GB DDR3 | **Broker** (fase 2) | Ainda não montado |

Todas no Tailscale. Endereçamento sempre por hostname MagicDNS, nunca por IP.

---

## Fase 0 — preparar o Raspberry Pi

### 1. Claude Code

Instalador nativo, binário autocontido — não precisa de Node.js. Detecta arm64
sozinho.

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

O binário vai para `~/.local/bin`, que não está no PATH por padrão no Raspberry
Pi OS. Se `claude` não for encontrado:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
claude --version
```

Nada de `sudo` na instalação.

### 2. Tailscale com SSH

```bash
sudo tailscale up --ssh
```

O `--ssh` é o que garante acesso quando o Pi estiver em Monte Alegre sem
monitor. Confirmar que funciona de outra máquina **antes** de qualquer mudança
física.

### 3. Watchdog de hardware

O Pi 4 tem `bcm2835_wdt` nativo. Em `/etc/systemd/system.conf`:

```
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
```

Isso reseta a placa se o kernel parar de escalonar — panic, deadlock de driver.
Processo lento ou job de 6 horas com CPU a 100% não dispara nada: o systemd
continua sendo escalonado e continua alimentando o contador.

**Armadilha a evitar:** `WatchdogSec=` dentro de um bloco `[Service]` é outro
mecanismo — reinicia aquele serviço se ele parar de mandar `sd_notify`. Isso
mataria job longo no meio. **Não usar `WatchdogSec` nos units do broker nem do
agente.** Só o `RuntimeWatchdogSec` global.

### 4. Reduzir escrita no cartão SD

Decisão tomada: fica no cartão SD, sem adaptador USB-SATA. Em troca, atacar o
volume de escrita, que é a causa real de corrupção.

- `log2ram` para o journal do systemd
- `noatime` nas opções de montagem em `/etc/fstab`
- SQLite em modo WAL
- **Métricas de agente só em memória.** Heartbeat de 10s × 5 agentes seriam
  ~43 mil escritas por dia. Persistir no banco apenas mudança de estado de job;
  CPU/RAM/GPU e `ultimo_ping` vivem num dict em memória do processo do broker
- **Backup noturno do `.db`** via Tailscale para o Ryzen ou o Aspire. É isso que
  substitui o SSD: cartão morto vira R$50 e um restore, com perda máxima de um
  dia de histórico

### Nota sobre desenvolver no Pi

O Claude Code roda bem (binário nativo, ~300MB de RAM), mas instalar
dependências e rodar testes num Pi 4 de 4GB é lento. Alternativa se incomodar:
rodar o Claude Code no Ryzen e fazer deploy no Pi por Tailscale SSH. Decisão
atual é construir no Pi mesmo.

---

## Fase 1 — o broker

### Stack

- Python 3.11+, FastAPI, Uvicorn
- SQLite (arquivo único, WAL habilitado)
- UI: página única servida pelo próprio FastAPI (HTML + JS puro, sem build step)
- Deploy: systemd unit, sem Docker (Pi 4 com 4GB — evitar overhead)
- Exposição: `tailscale serve` para HTTPS dentro do tailnet

### Schema

**jobs**
```
id              INTEGER PK
tipo            TEXT      -- ex: "inferencia", "scraping", "render", "shell"
payload         TEXT      -- JSON com os parâmetros do job
tags            TEXT      -- CSV de capacidades exigidas, ex: "gpu-5060ti"
prioridade      INTEGER   -- menor = mais urgente, default 100
status          TEXT      -- pendente | executando | concluido | falhou | cancelado
agente          TEXT      -- hostname que pegou o job
progresso       INTEGER   -- 0-100
mensagem        TEXT      -- linha de status corrente
resultado       TEXT      -- JSON de saída
log             TEXT      -- stdout/stderr acumulado (limitar a ~64KB)
tentativas      INTEGER
max_tentativas  INTEGER   -- default 3
criado_em       TIMESTAMP
iniciado_em     TIMESTAMP
terminado_em    TIMESTAMP
```

**agentes** — em memória, não no SQLite (ver Fase 0.4)
```
hostname        -- chave
tags            -- capacidades que oferece
ultimo_ping     -- timestamp
cpu_pct, ram_pct, gpu_pct, vram_livre_mb
job_atual       -- id do job, None se ocioso
versao_agente
```

Consequência aceita: reiniciar o broker zera a lista de agentes. Eles voltam a
aparecer no próximo heartbeat, em até 10s.

### Endpoints

```
POST   /api/jobs                    cria job
GET    /api/jobs?status=&tag=       lista com filtro
GET    /api/jobs/{id}               detalhe + log
POST   /api/jobs/{id}/cancelar      cancela (pendente ou executando)
POST   /api/jobs/{id}/reenfileirar  volta pra fila

POST   /api/agentes/heartbeat       registra/atualiza agente e métricas
GET    /api/agentes                 lista com status online/offline

GET    /api/trabalho?tags=a,b       agente pede próximo job compatível
POST   /api/trabalho/{id}/progresso agente reporta progresso e log
POST   /api/trabalho/{id}/concluir  agente entrega resultado
POST   /api/trabalho/{id}/falhar    agente reporta erro
```

`GET /api/trabalho` **precisa ser atômico**: seleciona e marca `executando` na
mesma transação, senão dois agentes pegam o mesmo job. É o único ponto onde uma
corrida quebra o sistema, e com SQLite em WAL é fácil errar.

### UI

Página única, auto-refresh a cada 5s, layout que funcione em tela de celular.

1. **Linha de agentes** — card por máquina: nome, online/offline, tags, CPU/RAM/GPU, job atual
2. **Fila** — jobs pendentes ordenados por prioridade, com aviso quando nenhum agente online atende as tags exigidas
3. **Em execução** — barra de progresso + mensagem corrente
4. **Histórico** — últimos 50 concluídos/falhados, clicável para ver o log
5. **Formulário de envio** — tipo, payload, tags, prioridade

### Agente

Daemon Python de arquivo único, roda em qualquer máquina do tailnet.

- `BROKER_URL` e `TAGS` vêm de variáveis de ambiente
- Heartbeat a cada 10s, mesmo ocioso
- Loop: pede trabalho → executa → reporta progresso → entrega
- Executa um job por vez (paralelismo vem depois, se precisar)
- Deve sobreviver a broker offline: retry com backoff, sem morrer

### Regras de comportamento que precisam estar no código desde já

1. **Máquina desligada é estado normal, não erro.** Job com tag `gpu-5060ti`
   fica `pendente` indefinidamente enquanto o Ryzen estiver desligado. Nunca
   marcar como falha por falta de agente. A UI mostra "aguardando agente com
   tag X", não um alerta vermelho.
2. **Agente sumido devolve o job.** Sem heartbeat por 90s com `job_atual`
   preenchido, o broker devolve o job para `pendente` e incrementa `tentativas`.
3. **Jobs devem ser idempotentes.** Reexecução após queda não pode duplicar
   efeito colateral.
4. **Escrita em disco é recurso escasso.** O broker roda em cartão SD. Nada de
   polling que escreva, nada de log verboso em disco, métricas em memória.
5. **Portabilidade do broker é requisito, não bônus.** Nada de path absoluto,
   nada de dependência de ARM, banco num arquivo só. Migrar do Pi para o i3
   deve ser: parar serviço, copiar o `.db`, subir no novo host, trocar
   `BROKER_URL` nos agentes.
6. **Zero exposição pública.** Bind em `127.0.0.1` + `tailscale serve`. Sem
   port forwarding, sem autenticação própria — a identidade é o tailnet.

### Tags por máquina

```
Ryzen 5 8500G   → gpu-5060ti, cpu-forte
Aspire i5       → cpu-movel
IdeaPad Celeron → cpu-leve
Raspberry Pi 4  → edge, sempre-ligado
i3-3220 (fase 2)→ cpu, gpu-leve (se a placa entrar)
```

### Ordem de construção

Não construir abstração antes de ter carga real.

1. **Broker + UI + agente, com job tipo `shell`** — executa comando arbitrário e
   devolve stdout. Valida o ciclo inteiro em poucas horas: fila, heartbeat,
   progresso, log, painel.
2. **Agente no Ryzen e no Aspire** — valida roteamento por tag e o
   comportamento de fila com máquina desligada.
3. **Job `inferencia`** — dispara um lote do pipeline de sementes na 5060 Ti,
   com progresso por imagem processada. Valida job longo.
4. **Job `render-carrossel`** — pipeline Playwright + Pillow de export. Valida
   job com artefato de saída.

---

## Fase 2 — quando o i3-3220 estiver pronto

Gatilho: i3 limpo, com 8GB de RAM e disco. A placa de vídeo é opcional e não
bloqueia nada.

1. **Migrar o broker** do Pi para o i3 (cópia do `.db` + troca de `BROKER_URL`)
2. **Pi vai para Monte Alegre**, acoplado à máquina de protótipo, virando
   worker de borda
3. **Se a GPU entrar no i3**: nova tag `gpu-leve` para inferência contínua de
   baixo volume, sem depender do Ryzen estar ligado

### Pré-requisitos antes de levar o Pi para Monte Alegre

- Wi-Fi de Monte Alegre já cadastrado no Pi (ou levar cabo de rede)
- `tailscale up --ssh` testado de outra máquina
- `tailscale up --advertise-routes=192.168.x.0/24` — dá acesso à rede local
  inteira de lá pelo tailnet, incluindo a página de admin do modem. Com isso o
  reboot do modem deixa de depender da visita semanal
- Parte de potência da máquina de protótipo (esteira, motores) atrás de relé
  normalmente desligado

## Fora de escopo

- **Câmeras / Frigate.** Adiado por decisão: o app YooSee é usado por familiar e
  não há material sensível. As câmeras têm RTSP em `rtsp://IP:554/onvif1`
  (principal) e `onvif2` (sub-stream), habilitável no app — anotado para o dia
  em que fizer sentido.
- **i3-330M avulso.** Descartado — custo de recuperação supera o valor.
- **Wake-on-LAN.** Descartado por decisão: o Ryzen fica desligado da tomada por
  segurança elétrica. O modelo de fila substitui.
- **Adaptador USB-SATA para o Pi.** Descartado — watchdog + redução de escrita +
  backup noturno cobrem o risco a custo zero.
- **Nobreak.** Meia fase é problema de Bragança e é raro. Resolver depois, fora
  deste projeto.
