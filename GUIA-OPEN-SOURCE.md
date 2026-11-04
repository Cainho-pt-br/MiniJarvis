# Guia de Preparação Open Source: MiniJarvis / Orquestrador

Este documento serve como mapa de bordo para transformar o código copiado nesta pasta em um produto de código aberto de alta qualidade, limpo de informações pessoais e pronto para ser publicado no GitHub.

---

## 1. Visão Geral da Arquitetura Copiada

A pasta foi isolada com o motor essencial do orquestrador distribuído:

- **`broker/`**: Servidor central (FastAPI) que gerencia filas de tarefas com persistência leve em SQLite, expõe websockets/streaming e serve o painel web PWA.
- **`agente/`**: Worker universal em Python (`agente.py`), escrito exclusivamente com a **biblioteca padrão (stdlib)** — roda em Linux, Windows e Mac sem necessidade de `pip install`.
- **`deploy/`**: Scripts de inicialização como serviço (`systemd` no Linux, Tarefas Agendadas no Windows) e ferramentas de linha de comando (`criar-job.py`, `lote.py`, `agendador.py`, etc.).
- **`orquestrador-spec.md`**: Especificação técnica original da arquitetura (pode ser consultada para enriquecer a documentação).

---

## 2. Checklist de Despersonalização (Limpeza de PII e Regras Privadas)

Antes de publicar o repositório como público, faça as seguintes substituições nos arquivos:

### 2.1. No arquivo `broker/ui.html`

- [ ] **Máquinas fixas de conversa (Linha ~1969):**
  - **Como está:**
    ```javascript
    const MAQUINAS_CONVERSA = [
      { id: "raspberrypi", nome: "Pi" },
      { id: "boss", nome: "Boss" },
      { id: "aspire5", nome: "Aspire 5" }
    ];
    ```
  - **Como deve ficar (Dinâmico):**
    Em vez de uma lista estática, monte o seletor baseado nos agentes que realmente se registraram no broker:
    ```javascript
    function obterMaquinasConversa() {
      if (!agentes || !agentes.length) return [{ id: "local", nome: "Local" }];
      return agentes.map(a => ({ id: a.hostname, nome: a.nome || a.hostname }));
    }
    ```
- [ ] **Termos "Caiosnet" e Sons:**
  - Substitua ocorrências de `caiosnet_som_ativado` por `minijarvis_som_ativado` no `localStorage`.
  - No SVG/Badge central (Linha ~758), renomeie a classe e o texto de `Caiosnet` para `MiniJarvis` ou `Orquestrador`.
- [ ] **Títulos e Meta tags:**
  - Ajuste a tag `<title>` e as descrições no manifesto para o nome público escolhido.

---

### 2.2. No arquivo `broker/app.py`

- [ ] **PIN de Administração:**
  - Localize: `CAIO_PIN = os.environ.get("CAIO_PIN", "2026")`
  - Altere para: `ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")` e atualize as referências onde `CAIO_PIN` é checado.
- [ ] **Repositório Git Hardcoded (Linha ~328):**
  - Localize: `_repo = Path(os.environ.get("ORQ_REPO_GIT", str(Path.home() / "git" / "caiosnet.git")))`
  - Altere para:
    ```python
    _repo_env = os.environ.get("ORQ_REPO_GIT")
    _repo = Path(_repo_env) if _repo_env else None
    if _repo and _repo.exists():
        app.mount("/git", StaticFiles(directory=str(_repo)), name="git")
    ```
- [ ] **Remoção de referências a CRM e tarefas pessoais:**
  - Remova trechos que mencionam `dossie_crm`, `Ajustes no CRM`, ou menções nos prompts automáticos de correção (ex: `"pedida pelo Caio"` -> altere para `"solicitada pelo administrador"`).

---

### 2.3. No arquivo `agente/agente.py`

- [ ] **Docstrings e Comentários:**
  - Remova comentários históricos que citam o nome do desenvolvedor anterior ("Decisão do Caio em...").
- [ ] **Configuração Padrão do Broker:**
  - Confirme que `BROKER_URL` aponta por padrão para `http://127.0.0.1:8080`, configurável via variável de ambiente.

---

### 2.4. Na pasta `deploy/`

- [ ] **Nomes de Serviço:**
  - Nos arquivos `orquestrador-broker.service` e `orquestrador-agente.service`, verifique se os caminhos apontam para `/opt/minijarvis` ou tornem-se templates com variáveis como `/opt/orquestrador`.
- [ ] **Script de instalação Windows (`instalar-agente-windows.ps1`):**
  - Revise as URLs de download de fallback para que apontem para o seu futuro repositório do GitHub.

---

## 3. Arquivos Criados para Estruturar como Produto

Já deixamos criados nesta pasta os arquivos essenciais de suporte:

1. **`requirements.txt`**: Dependências Python necessárias apenas para rodar o Broker (`fastapi`, `uvicorn`, `pydantic`). O agente continua com zero dependências externas.
2. **`.env.example`**: Modelo pronto de variáveis de ambiente tanto para o servidor quanto para os workers.
3. **`.gitignore`**: Proteção para não commitar acidentalmente bancos `.db`, arquivos de upload, logs ou pastas virtuais `.venv`.
4. **`LICENSE`**: Licença MIT padrão para software livre comercial e comunitário.

---

## 4. Sugestão de `README.md` para o GitHub

Quando for publicar, crie um `README.md` destacando os seguintes pontos fortes que tornam este projeto especial:

1. **Arquitetura Distribuída Leve:** Funciona perfeitamente em rede mesh segura (como Tailscale) entre PCs Windows, servidores Linux e Raspberry Pi.
2. **Zero Dependências no Worker:** O agente não exige compilação nem pacotes pip; basta copiar `agente.py` e rodar em qualquer máquina com Python 3.9+.
3. **Resiliência a Desconexões:** Máquina desligada não gera erro falso; o broker mantém o job pendente até um agente compatível aparecer.
4. **Roteamento Inteligente por Tags:** Agendamento por capacidades de hardware (`gpu`, `cpu-forte`, `claude`, `gemini`, etc.).
5. **Multi-Modelos com Fallback:** Integração pronta para Claude Code, Gemini CLI (Antigravity) e OpenAI Codex.

---

## 5. Como Testar Localmente Antes de Publicar

1. **Criar e ativar ambiente virtual para o Broker:**
   ```bash
   python -m venv .venv
   # No Windows:
   .venv\Scripts\activate
   # No Linux/Mac:
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

2. **Iniciar o Broker:**
   ```bash
   cd broker
   uvicorn app:app --host 127.0.0.1 --port 8080 --reload
   ```
   Acesse no navegador: `http://localhost:8080`

3. **Iniciar um Agente de teste (em outro terminal):**
   ```bash
   cd agente
   python agente.py
   ```
   Ele se conectará ao broker local e aparecerá no painel como agente online.
