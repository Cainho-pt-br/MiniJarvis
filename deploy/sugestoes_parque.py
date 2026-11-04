#!/usr/bin/env python3
"""Gera sugestões operacionais para o parque de máquinas usando Gemini Flash.

FILOSOFIA (Regra do Parque)
"Mecânico no script, IA só no julgamento."
Este script coleta métricas em milissegundos via API local do broker e monta um
resumo ultracompacto (~25 linhas, ~150 tokens). O Gemini é chamado com o modelo
Flash (incluso na assinatura Google AI Pro) e responde estritamente em JSON.

CONTROLE DE COTA E RATE LIMIT
- Frequência básica: 1x ao dia (disparo matinal).
- Frequência máxima: 2x ao dia (se houver alteração significativa no cluster,
  ex: máquina ligou/desligou ou job falhou).
- Se já rodou hoje e o parque não mudou: aborta em < 0.1s com ZERO tokens gastos.
- Flag --forcar ignora o rate limit para execuções manuais sob demanda.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
DADOS_DIR = RAIZ / "dados"
ARQUIVO_SUGESTOES = DADOS_DIR / "sugestoes-gemini.json"

BROKER = os.environ.get("BROKER_URL", "http://127.0.0.1:8080").rstrip("/")


def ctx_ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_get(url: str, timeout: int = 15) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "Orquestrador-Sugestoes/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx_ssl()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url: str, dados: dict, timeout: int = 15) -> dict | None:
    req = urllib.request.Request(
        url,
        data=json.dumps(dados).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Orquestrador-Sugestoes/1.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx_ssl()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print("Aviso: falha ao postar no broker:", e, file=sys.stderr)
        return None


def carregar_sugestoes_anteriores() -> dict:
    if ARQUIVO_SUGESTOES.is_file():
        try:
            return json.loads(ARQUIVO_SUGESTOES.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        d = http_get(f"{BROKER}/api/sugestoes", timeout=5)
        if isinstance(d, dict) and "sugestoes" in d:
            return d
    except Exception:
        pass
    return {}


def calcular_fingerprint(agentes: list[dict], jobs: list[dict]) -> str:
    on = sorted([a.get("hostname", "") for a in agentes if a.get("online")])
    falhas = len([j for j in jobs if j.get("status") == "falhou"])
    gpus = sorted([a.get("hostname", "") for a in agentes if a.get("online") and (a.get("info") or {}).get("gpu")])
    texto = f"on:{','.join(on)}|gpu:{','.join(gpus)}|falhas:{falhas}"
    return hashlib.md5(texto.encode("utf-8")).hexdigest()[:12]


def catalogo_rotinas() -> dict[str, str]:
    """As rotinas que existem de verdade (deploy/agendadas/*.json): nome -> descrição.

    É a única lista de ações que o modelo pode sugerir rodar. Até 2026-09-24 ele
    escrevia o comando livremente e inventava scripts (scripts/prospectar_leads.py,
    data/broker.db) — o botão "Aplicar" do painel rodaria coisa que não existe.
    """
    rotinas = {}
    for arq in sorted((AQUI / "agendadas").glob("*.json")):
        try:
            t = json.loads(arq.read_text(encoding="utf-8"))
            if t.get("nome"):
                rotinas[t["nome"]] = t.get("descricao", "")
        except Exception:
            pass
    return rotinas


def consertar_acentos(texto):
    """Desfaz UTF-8 lido como Latin-1 ("estÃ¡" -> "está").

    O agy, rodando sob systemd sem LANG, devolveu texto assim em 2026-09-24.
    Só troca quando a volta dá UTF-8 válido: texto já correto fica como está.
    """
    if not isinstance(texto, str) or not any(c in texto for c in "ÃÂ"):
        return texto
    try:
        return texto.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return texto


def montar_resumo_parque(agentes: list[dict], jobs: list[dict],
                         rotinas: list[dict] | None = None) -> str:
    linhas = ["ESTADO ATUAL DO PARQUE:"]
    for a in agentes:
        h = a.get("hostname", "desconhecido")
        on = a.get("online", False)
        if not on:
            linhas.append(f"- {h}: OFFLINE")
            continue
        info = a.get("info") or {}
        so = info.get("so", "SO?")
        cpu = info.get("cpu", "CPU?")
        ram = f"{round(info.get('ram_total_mb', 0) / 1024)}GB" if info.get("ram_total_mb") else "RAM?"
        gpu = info.get("gpu")
        gpu_str = f", GPU: {gpu}" if gpu else ""
        ias = [n for n, k in (("Claude", "tem_claude"), ("Gemini", "tem_gemini"),
                              ("Codex", "tem_codex")) if info.get(k)]
        job_atual = a.get("job_atual")
        ocupado = f"[rodando job #{job_atual}]" if job_atual else "[ocioso]"
        discos = ", ".join(f"{d.get('unidade')} {d.get('livre_gb')}GB livres"
                           for d in (a.get("discos") or []))
        linhas.append(f"- {h}: {so}, {cpu}, {ram}{gpu_str} (IA: {'/'.join(ias) or 'nenhuma'}) "
                      f"{ocupado}{' · ' + discos if discos else ''}")

    concluidos = len([j for j in jobs if j.get("status") == "concluido"])
    # Apenas falhas ativas onde o mesmo agente NÃO executou outros jobs com sucesso depois
    falhas_todas = [j for j in jobs if j.get("status") == "falhou"]
    falhas_ativas = []
    for f in falhas_todas:
        ag = f.get("agente")
        f_id = f.get("id", 0)
        superada = any(x.get("status") == "concluido" and x.get("agente") == ag and x.get("id", 0) > f_id for x in jobs)
        if not superada:
            falhas_ativas.append(f)

    executando = len([j for j in jobs if j.get("status") == "executando"])
    linhas.append(f"\nJOBS RECENTES (últimas 24h): {concluidos} concluídos, {executando} em execução, {len(falhas_ativas)} falha(s) ativa(s).")
    if falhas_ativas:
        ult = falhas_ativas[0]
        linhas.append(f"Última falha não resolvida: Job #{ult.get('id')} no agente {ult.get('agente')} ({ult.get('mensagem') or 'sem msg'}).")
    else:
        linhas.append("Status de estabilidade: Todos os nós estão saudáveis e operacionais (sem falhas pendentes).")

    if rotinas:
        linhas.append("\nROTINAS (última execução):")
        for r in rotinas:
            u = r.get("ultimo") or {}
            quando = u.get("terminado_em") or u.get("criado_em") or "nunca rodou"
            linhas.append(f"- {r['nome']}: {u.get('status', 'nunca')} em {quando}")

    linhas.append("\nOBJETIVOS E DIRETRIZES DO PARQUE:")
    linhas.append("1. Prospecção de Leads (CRM de sementes): meta diária de 10 contatos.")
    linhas.append("2. Produção de Mídia: aproveitar a GPU RTX 5060 Ti no Boss para vídeo, render ou inferência local pesada.")
    linhas.append("3. Celeron: worker leve para raspagem web ou download via Pi (sem IA local).")
    linhas.append("4. Proteção do Pi: manutenção do SQLite e prevenção de desgaste do SD card.")
    return "\n".join(linhas)


def localizar_binario_gemini() -> tuple[str, str]:
    # Prioriza o Antigravity CLI (`agy`), que utiliza a conta Google AI Pro (sem custo de API)
    candidatos_agy = [
        os.environ.get("AGY_BIN"),
        shutil.which("agy"),
        shutil.which("agy.exe"),
        "/usr/local/bin/agy",
        "/home/pi/.local/bin/agy",
        str(Path.home() / ".local" / "bin" / "agy"),
        r"C:\Users\Pichau\AppData\Local\agy\bin\agy.EXE",
    ]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidatos_agy.append(os.path.join(local_app, "agy", "bin", "agy.exe"))
        candidatos_agy.append(os.path.join(local_app, "Programs", "Antigravity", "bin", "agy.exe"))

    for c in candidatos_agy:
        if c and (os.path.exists(c) or shutil.which(c)):
            mod = os.environ.get("MODELO_GEMINI", "gemini-3.8-flash-low")
            return c, mod

    # Fallback legado: gemini CLI com chave pré-paga se agy não existir
    candidatos_gemini = [
        os.environ.get("GEMINI_BIN"),
        shutil.which("gemini"),
        "/usr/local/bin/gemini",
        str(Path.home() / ".local" / "bin" / "gemini"),
    ]
    for c in candidatos_gemini:
        if c and (os.path.exists(c) or shutil.which(c)):
            return c, "gemini-2.5-flash"

    return "", ""


def chamar_gemini(resumo_parque: str, rotinas: dict[str, str]) -> tuple[list[dict], str]:
    binario, modelo = localizar_binario_gemini()
    if not binario:
        raise RuntimeError("Nenhum binário do Gemini/Antigravity (`agy` ou `gemini`) encontrado nesta máquina.")

    catalogo = "\n".join(f"- {n}: {d}" for n, d in rotinas.items())
    prompt = (
        "Você é o conselheiro operacional do parque de máquinas do Caio.\n"
        "Com base no estado abaixo, sugira de 2 a 4 ações úteis para HOJE. Cada uma precisa "
        "de um motivo concreto tirado do estado (um número, um nome de máquina, uma rotina "
        "que falhou ou não rodou) — nada genérico como 'nenhum job em execução'.\n"
        "Responda ESTRITAMENTE em JSON (uma lista de objetos), sem markdown e sem texto em volta.\n\n"
        f"{resumo_parque}\n\n"
        "ROTINAS QUE EXISTEM (as ÚNICAS que você pode mandar rodar, pelo nome exato):\n"
        f"{catalogo}\n\n"
        "REGRAS:\n"
        "- Nunca escreva comando de terminal nem invente script. Ou você aponta uma rotina "
        "do catálogo (campo rotina), ou escreve uma pergunta para o chat do parque (campo "
        "pergunta), que um modelo com acesso às máquinas vai responder.\n"
        "- Para o boss (GPU RTX): imagens para posts, carrosséis ou montagem de vídeo com "
        "clipes existentes. NUNCA vídeo por difusão do zero.\n"
        "- Nunca sugira VACUUM nem nada que reescreva o banco do Pi: o cartão SD se desgasta.\n"
        "- Máquina desligada é estado normal, não problema.\n\n"
        "SCHEMA:\n"
        "[\n"
        "  {\n"
        '    "tag": "gpu | leads | conteudo | sistema",\n'
        '    "tit": "Título curto (até 5 palavras)",\n'
        '    "desc": "Benefício prático em 1 frase",\n'
        '    "motivo": "O dado do estado que justifica agora",\n'
        '    "destino": "boss | raspberrypi | celeron | aspire5",\n'
        '    "rotina": "nome exato do catálogo, ou null",\n'
        '    "pergunta": "pergunta pronta para o chat, ou null"\n'
        "  }\n"
        "]"
    )

    if "agy" in Path(binario).name.lower():
        argv = [binario, "-p", prompt, "--dangerously-skip-permissions", "--model", modelo]
    else:
        argv = [binario, "-p", prompt, "--yolo", "-m", modelo]

    # Garante acesso às credenciais do Google AI Pro em /home/pi/.gemini mesmo quando executado sob root
    env = dict(os.environ)
    # Sob systemd não há LANG, e o agy devolveu os acentos corrompidos.
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    if Path("/home/pi/.gemini/antigravity-cli").is_dir():
        env["ANTIGRAVITY_APP_DATA_DIR"] = "/home/pi/.gemini/antigravity-cli"
        env["HOME"] = "/home/pi"

    print(f"Chamando Gemini ({modelo}) via {Path(binario).name}...", flush=True)
    ini = time.time()
    r = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CLI Gemini saiu com código {r.returncode}: {r.stderr.strip()[:300]}")

    duracao = round(time.time() - ini, 1)
    print(f"Gemini respondeu em {duracao}s.", flush=True)

    bruto = r.stdout.strip()
    match_bloco = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", bruto)
    conteudo = match_bloco.group(1).strip() if match_bloco else bruto
    i = conteudo.find("[")
    f = conteudo.rfind("]")
    if i >= 0 and f > i:
        conteudo = conteudo[i:f+1]

    sugestoes = json.loads(conteudo)
    if not isinstance(sugestoes, list):
        raise ValueError("A resposta da IA não é uma lista JSON")
    return validar(sugestoes, rotinas), modelo


def validar(sugestoes: list, rotinas: dict[str, str]) -> list[dict]:
    """Fica só o que dá para executar: rotina que existe, ou pergunta para o chat."""
    boas = []
    for s in sugestoes:
        if not isinstance(s, dict):
            continue
        s = {k: consertar_acentos(v) for k, v in s.items()}
        if s.get("rotina") not in rotinas:
            s["rotina"] = None
        if not (s.get("rotina") or (s.get("pergunta") or "").strip()):
            print("descartada (sem rotina válida nem pergunta):", s.get("tit"), file=sys.stderr)
            continue
        s.pop("comando", None)
        boas.append(s)
    return boas


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera sugestões para o parque com Gemini")
    parser.add_argument("--forcar", action="store_true", help="Ignora rate limit e fingerprint")
    args = parser.parse_args()

    hoje = time.strftime("%Y-%m-%d")
    agora_str = time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{agora_str}] Iniciando verificação de sugestões operacionais...")

    # 1. Coleta estado do cluster
    try:
        agentes = http_get(f"{BROKER}/api/agentes", timeout=10)
        jobs = http_get(f"{BROKER}/api/jobs?ordem=recentes&limite=40", timeout=10)
        try:
            estado_rotinas = http_get(f"{BROKER}/api/rotinas", timeout=10)
        except Exception:
            estado_rotinas = []     # broker anterior a 2026-09-24
    except Exception as e:
        print(f"Erro ao consultar broker em {BROKER}: {e}", file=sys.stderr)
        return 1

    fp_atual = calcular_fingerprint(agentes, jobs)
    antigas = carregar_sugestoes_anteriores()

    data_anterior = antigas.get("data", "")
    execucoes_hoje = antigas.get("execucoes_hoje", 0) if data_anterior == hoje else 0
    fp_anterior = antigas.get("fingerprint", "")

    # 2. Avalia Rate Limit e Alterações
    if not args.forcar:
        if execucoes_hoje >= 2:
            print(f"Teto diário de 2 execuções já atingido para hoje ({hoje}). Abortando sem gastar tokens.")
            return 0
        if execucoes_hoje == 1:
            if fp_atual == fp_anterior:
                print(f"Parque inalterado desde a execução da manhã ({antigas.get('gerado_em')}). Abortando sem gastar tokens.")
                return 0
            print(f"Alteração detectada no parque (fingerprint {fp_anterior} -> {fp_atual}). Prosseguindo com a 2ª rodada do dia.")
        else:
            print(f"Primeira execução do dia ({hoje}). Prosseguindo com geração...")
    else:
        print("Modo --forcar ativo: ignorando rate limit e fingerprint.")

    # 3. Monta resumo do parque e chama o Gemini
    resumo = montar_resumo_parque(agentes, jobs, estado_rotinas)
    try:
        sugestoes, modelo_usado = chamar_gemini(resumo, catalogo_rotinas())
    except Exception as e:
        print(f"Falha ao consultar o Gemini: {e}", file=sys.stderr)
        return 2

    # 4. Salva dados locais e atualiza broker
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    resultado = {
        "gerado_em": agora_str,
        "data": hoje,
        "execucoes_hoje": execucoes_hoje + 1,
        "fingerprint": fp_atual,
        "modelo": modelo_usado,
        "sugestoes": sugestoes
    }
    ARQUIVO_SUGESTOES.write_text(json.dumps(resultado, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Salvo com sucesso em {ARQUIVO_SUGESTOES} ({len(sugestoes)} sugestões).")

    # Notifica broker
    http_post(f"{BROKER}/api/sugestoes", resultado, timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
