#!/usr/bin/env python3
"""Agente do MiniJarvis. Arquivo único, roda em qualquer computador.

Conectar este computador ao seu Jarvis na nuvem (uma vez só):
    python agente.py --parear CODIGO --broker https://seu-jarvis.vercel.app
O código aparece no painel, em "Conectar máquina". Depois disso basta rodar
    python agente.py

Configuração (variáveis de ambiente, opcionais; valem mais que o arquivo
salvo pelo pareamento em ~/.minijarvis/agente.json):
    BROKER_URL    endereço do Jarvis (nuvem) ou do broker local
    AGENTE_TOKEN  token deste agente (o pareamento grava sozinho)
    TAGS          ex: gpu-5060ti,cpu-forte
    AGENTE_NOME   default: hostname da máquina

Só depende da stdlib: nada de pip na máquina worker.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Subir a cada mudança que o painel precise distinguir: é por ela que o quadro
# de sugestões acusa máquina rodando agente velho (0.2.0 = Codex).
VERSAO = "0.3.0"

# Configuração gravada pelo pareamento. Variável de ambiente vale mais.
CONFIG_ARQUIVO = Path(os.environ.get("AGENTE_CONFIG", str(Path.home() / ".minijarvis" / "agente.json")))


def _ler_config() -> dict:
    try:
        return json.loads(CONFIG_ARQUIVO.read_text(encoding="utf-8"))
    except Exception:
        return {}


_CONFIG = _ler_config()
BROKER = (os.environ.get("BROKER_URL") or _CONFIG.get("broker") or "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("AGENTE_TOKEN") or _CONFIG.get("token") or ""
NOME = os.environ.get("AGENTE_NOME") or _CONFIG.get("nome") or socket.gethostname()
# O nome da máquina é sempre uma tag. Assim "rodar no celeron" não precisa de
# uma tag inventada para representar o celeron, e as tags configuradas ficam
# só para capacidade de verdade (ter GPU, ter Claude Code, viver ligado).
TAGS = ",".join(dict.fromkeys(
    [t.strip() for t in os.environ.get("TAGS", "").split(",") if t.strip()] + [NOME]))

HEARTBEAT_S = 10
POLL_OCIOSO_S = 5
# Quanto a busca por trabalho espera no servidor. Na nuvem o próprio servidor
# diz os valores na resposta do heartbeat (menos requisições = custo menor).
ESPERA_S = 20
# De quanto em quanto tempo o agente puxa o cérebro mesmo sem job. Antes o pull
# só acontecia antes de raciocinar, e uma máquina que passava o dia sem job
# `claude` ficava dias atrás — e o painel mostrava o commit da partida, que era
# mentira. 15 min é o bastante para o parque andar junto sem martelar o cartão
# SD do Pi (um pull sem novidade escreve só o FETCH_HEAD).
SINCRONIZAR_CEREBRO_S = int(os.environ.get("SINCRONIZAR_CEREBRO_S", "900"))
PROGRESSO_S = 1          # frequência de envio de log/progresso durante o job (1s para streaming responsivo)

_estado = {"job_atual": None, "cancelar": False}
_lock = threading.Lock()


def log(*a: object) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def sem_janela() -> dict:
    """No Windows o agente roda sob pythonw, sem console.

    Cada subprocesso criado nessa situação abre uma janela própria, que pisca na
    tela do usuário — e o heartbeat chama nvidia-smi a cada 10s, então a piscada
    é constante. CREATE_NO_WINDOW resolve.
    """
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


# ------------------------------------------------------------------- HTTP
def chamar(metodo: str, caminho: str, corpo: dict | None = None,
           timeout: int = 20) -> tuple[int, dict]:
    url = BROKER + caminho
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo)
    if dados:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("User-Agent", "MiniJarvis-Agente/" + VERSAO)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        bruto = r.read()
        if r.status == 204 or not bruto:
            return r.status, {}
        return r.status, json.loads(bruto)


# -------------------------------------------------------------- métricas
_cpu_windows_anterior: list[int] = []


def discos() -> list[dict]:
    """Todos os discos FIXOS da máquina, não só o do sistema.

    O boss tem um HDD de 1 TB além do NVMe, e olhar só a raiz escondia ele do
    painel. Fica em métrica, não em inventário, porque o espaço livre muda o
    tempo todo — e o custo é uma chamada de sistema por disco a cada 10s.
    """
    alvos: list[str] = []
    if os.name == "nt":
        import ctypes
        bits = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if not (bits >> i) & 1:
                continue
            letra = chr(ord("A") + i) + ":\\"
            # 3 = DRIVE_FIXED. Sem o filtro entrariam pendrive, leitor de
            # cartão, unidade de rede e CD — que aparecem e somem, e poluiriam
            # o painel com discos fantasmas.
            if ctypes.windll.kernel32.GetDriveTypeW(letra) != 3:
                continue
            alvos.append(letra)
    else:
        alvos.append("/")

    saida = []
    for p in alvos:
        try:
            u = shutil.disk_usage(p)
            saida.append({"unidade": p.rstrip("\\") or "/",
                          "total_gb": round(u.total / (1024 ** 3), 1),
                          "livre_gb": round(u.free / (1024 ** 3), 1)})
        except Exception:
            pass          # disco existe mas não respondeu: ignora, sem quebrar
    return saida


def _metricas_windows(m: dict) -> None:
    """RAM e CPU no Windows via ctypes: sem /proc e sem os.getloadavg()."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        m["ram_pct"] = float(st.dwMemoryLoad)

    # CPU: GetSystemTimes devolve contadores acumulados, então a porcentagem sai
    # da diferença entre esta leitura e a anterior (a primeira sai como None).
    class FILETIME(ctypes.Structure):
        _fields_ = [("baixo", ctypes.c_ulong), ("alto", ctypes.c_ulong)]

    ocioso, kernel, usuario = FILETIME(), FILETIME(), FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(ocioso), ctypes.byref(kernel), ctypes.byref(usuario)):
        return
    junta = lambda f: (f.alto << 32) | f.baixo   # noqa: E731
    atual = [junta(ocioso), junta(kernel), junta(usuario)]
    if _cpu_windows_anterior:
        d_ocioso = atual[0] - _cpu_windows_anterior[0]
        d_total = (atual[1] - _cpu_windows_anterior[1]) + \
                  (atual[2] - _cpu_windows_anterior[2])
        if d_total > 0:
            m["cpu_pct"] = round(100.0 * (d_total - d_ocioso) / d_total, 1)
    _cpu_windows_anterior[:] = atual


def metricas() -> dict:
    """Best-effort: se não der para medir, manda None. Sem dependências."""
    m: dict[str, object] = {"cpu_pct": None, "ram_pct": None,
                            "gpu_pct": None, "vram_livre_mb": None,
                            "disco_livre_gb": None, "discos": []}
    try:
        m["discos"] = discos()
        # O disco do sistema continua num campo próprio: é ele que o cartão de
        # máquina mostra, e ele não pode depender da ordem da lista.
        raiz = os.path.abspath(os.sep).rstrip("\\") or "/"
        for d in m["discos"]:
            if d["unidade"] == raiz:
                m["disco_livre_gb"] = d["livre_gb"]
                break
    except Exception:
        pass
    if os.name == "nt":
        try:
            _metricas_windows(m)
        except Exception:
            pass
    else:
        try:  # Linux: /proc
            with open("/proc/meminfo") as f:
                mi = {}
                for linha in f:
                    k, _, v = linha.partition(":")
                    mi[k] = float(v.strip().split()[0])
            total, disp = mi["MemTotal"], mi.get("MemAvailable", mi["MemFree"])
            m["ram_pct"] = round(100.0 * (total - disp) / total, 1)
            m["cpu_pct"] = round(100.0 * min(os.getloadavg()[0] /
                                             (os.cpu_count() or 1), 1.0), 1)
        except Exception:
            pass
    if shutil.which("nvidia-smi"):
        try:
            saida = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                **sem_janela()).stdout.strip()
            gpu, vram = saida.splitlines()[0].split(",")
            m["gpu_pct"] = float(gpu.strip())
            m["vram_livre_mb"] = int(vram.strip())
        except Exception:
            pass
    return m


# ------------------------------------------------------------- inventário
def _cpu_nome() -> str:
    if os.name == "nt":
        try:
            import winreg
            chave = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, chave) as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except Exception:
            return platform.processor() or ""
    try:
        # Em ARM não existe "model name"; o Pi identifica a placa em "Model".
        achados = {}
        with open("/proc/cpuinfo") as f:
            for linha in f:
                k, _, v = linha.partition(":")
                k = k.strip().lower()
                if k in ("model name", "model", "hardware") and v.strip():
                    achados.setdefault(k, v.strip())
        return achados.get("model name") or achados.get("model") \
            or achados.get("hardware") or platform.machine()
    except Exception:
        return platform.machine()


def _so_nome() -> str:
    if os.name == "nt":
        return "Windows " + platform.release()
    try:
        with open("/etc/os-release") as f:
            for linha in f:
                if linha.startswith("PRETTY_NAME="):
                    return linha.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return platform.system() + " " + platform.release()


def _ram_total_mb() -> int | None:
    try:
        if os.name == "nt":
            import ctypes

            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEM()
            st.dwLength = ctypes.sizeof(MEM)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys / (1024 * 1024))
        else:
            with open("/proc/meminfo") as f:
                for linha in f:
                    if linha.startswith("MemTotal:"):
                        return int(float(linha.split()[1]) / 1024)
    except Exception:
        pass
    return None


def _gpu_nome() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        saida = subprocess.run(["nvidia-smi", "--query-gpu=name",
                                "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=8,
                               **sem_janela()).stdout.strip()
        return saida.splitlines()[0].strip() or None
    except Exception:
        return None


def _localizar_binario_claude() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA") or ""
    candidatos = [
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        shutil.which("claude.exe"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        str(Path.home() / ".local" / "bin" / "claude"),
        str(Path(local_app_data) / "Programs" / "claude" / "claude.exe") if local_app_data else None,
        "/usr/local/bin/claude",
    ]
    for c in candidatos:
        if c and (os.path.exists(c) or shutil.which(c)):
            return c
    return ""


def _tem_claude_local() -> bool:
    b = _localizar_binario_claude()
    return bool(b)


def _claude_logado(binario: str) -> bool | None:
    """Se o Claude Code está autenticado. None = não deu para saber.

    Ter o binário instalado não é o mesmo que poder usá-lo: sem login, um job
    `claude` falha na hora. O painel precisa da distinção, senão anuncia uma
    máquina como cérebro e ela quebra no primeiro pedido.

    O código de saída não serve de sinal — `auth status` sai com 1 quando não
    há login, mas imprime o JSON de qualquer jeito. Quem responde é o texto.
    """
    if not (shutil.which(binario) or os.path.exists(binario)):
        return None
    try:
        r = subprocess.run([binario, "auth", "status"], capture_output=True,
                           text=True, timeout=25, encoding="utf-8",
                           errors="replace", **sem_janela())
        bruto = (r.stdout or "") + (r.stderr or "")
        i, f = bruto.find("{"), bruto.rfind("}")
        if i < 0 or f <= i:
            return None
        return bool(json.loads(bruto[i:f + 1]).get("loggedIn"))
    except Exception:
        return None


def _localizar_binario_gemini() -> tuple[str, bool]:
    """Retorna (caminho_binario, eh_chave_prepaga)."""
    arquivo_chave = Path.home() / ".config" / "orq" / "gemini.chave"
    if not os.environ.get("GEMINI_API_KEY") and arquivo_chave.exists():
        bin_chave = os.environ.get("GEMINI_BIN", "gemini")
        if shutil.which(bin_chave):
            return bin_chave, True

    local_app_data = os.environ.get("LOCALAPPDATA") or ""
    candidatos = [
        os.environ.get("AGY_BIN"),
        shutil.which("agy"),
        shutil.which("agy.exe"),
        str(Path(local_app_data) / "agy" / "bin" / "agy.exe") if local_app_data else None,
        str(Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"),
        str(Path.home() / ".local" / "bin" / "agy"),
        "/home/pi/.local/bin/agy",
        "/usr/local/bin/agy",
    ]
    for c in candidatos:
        if c and os.path.exists(c):
            return c, False
    return "", False


def _tem_gemini_local() -> bool:
    binario, eh_chave = _localizar_binario_gemini()
    return bool(binario and (eh_chave or os.path.exists(binario) or shutil.which(binario)))


def _localizar_codex() -> list[str]:
    """Começo do argv para chamar o Codex CLI, ou [] se esta máquina não tem.

    No Windows o npm instala `codex.cmd`, e um .cmd recebendo o texto do
    usuário como argumento passa pelo cmd.exe — que interpreta &, | e ^ dentro
    do prompt. Por isso lá o agente chama `node codex.js` direto, ou o
    codex.exe nativo que o app do ChatGPT traz, e o argumento chega intacto.
    """
    if os.environ.get("CODEX_BIN"):
        return [os.environ["CODEX_BIN"]]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        js = Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = (shutil.which("node") or
                next((c for c in (r"C:\Program Files\nodejs\node.exe",) if os.path.exists(c)), None))
        # Só vale se o binário nativo do pacote chegou: um `npm install`
        # interrompido deixa o codex.js sem ele, e aí o app serve melhor.
        nativo = list(js.parent.parent.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
        if js.is_file() and node and nativo:
            return [node, str(js)]
        # O app do ChatGPT guarda o próprio codex.exe numa pasta com hash de
        # versão, que muda a cada atualização: pega o mais novo.
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        try:
            achados = sorted(Path(local, "OpenAI", "Codex", "bin").glob("*/codex.exe"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            achados = []
        return [str(achados[0])] if achados else []
    for c in (shutil.which("codex"), "/usr/local/bin/codex",
              str(Path.home() / ".local" / "bin" / "codex")):
        if c and os.path.exists(c):
            return [c]
    return []


def _codex_logado(argv: list[str]) -> bool | None:
    """Se o Codex tem login (ChatGPT ou chave). None = não deu para saber."""
    if not argv:
        return None
    try:
        r = subprocess.run(argv + ["login", "status"], capture_output=True,
                           text=True, timeout=25, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL,
                           **sem_janela())
        texto = ((r.stdout or "") + (r.stderr or "")).lower()
        if "not logged in" in texto:
            return False
        if "logged in" in texto:
            return True
    except Exception:
        pass
    return None


# Marca de "o Codex precisa de login", gravada quando uma chamada falha por
# token vencido. `codex login status` responde "logado" mesmo com o token
# morto (aspire5, 2026-09-25), então sem a marca o painel voltava a mostrar o
# Codex como pronto a cada reinício do agente, e o botão de login sumia.
MARCA_CODEX_LOGIN = Path.home() / ".config" / "orq" / "codex-precisa-login"


def marcar_codex_login(precisa: bool) -> None:
    INVENTARIO["codex_logado"] = not precisa
    try:
        if precisa:
            MARCA_CODEX_LOGIN.parent.mkdir(parents=True, exist_ok=True)
            MARCA_CODEX_LOGIN.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        elif MARCA_CODEX_LOGIN.exists():
            MARCA_CODEX_LOGIN.unlink()
    except OSError:
        pass


def estado_ias() -> dict:
    """O que esta máquina consegue usar para raciocinar, e com login ou não.

    Separado do inventário porque muda sem o agente reiniciar: instalar o Codex
    ou entrar numa conta é coisa que se faz com a máquina ligada, e o painel
    ficaria anunciando o estado da partida até o próximo boot.
    """
    codex = _localizar_codex()
    return {
        # Sem Claude Code instalado, a máquina executa mas não raciocina. E
        # instalado sem login também não raciocina — daí os dois campos.
        "tem_claude": _tem_claude_local(),
        "claude_logado": _claude_logado(_localizar_binario_claude() or os.environ.get("CLAUDE_BIN", "claude")),
        "tem_gemini": _tem_gemini_local(),
        "tem_codex": bool(codex),
        "codex_logado": (False if MARCA_CODEX_LOGIN.exists() else _codex_logado(codex)) if codex else None,
    }




def estado_cerebro(cwd: str | None) -> dict:
    """Como está a cópia local do cérebro nesta máquina.

    Vai no heartbeat para o painel poder dizer "cérebro ausente" ou "atrasado"
    no cartão da máquina. Sem isto uma cópia apagada passa despercebida: o
    `git pull` do agente ignora falha de propósito, para falta de rede não
    impedir de responder — e foi assim que a aspire5 passou horas respondendo
    sem cérebro nenhum, em 2026-09-07, sem ninguém ter como notar.
    """
    e: dict = {"cwd": cwd or "", "git": False, "commit": None,
               "notas": 0, "erro": None}
    if not cwd:
        e["erro"] = "sem CLAUDE_CWD"
        return e
    if not os.path.isdir(cwd):
        e["erro"] = "diretório não existe"
        return e
    if not os.path.isdir(os.path.join(cwd, ".git")):
        e["erro"] = "não é repositório git"
        return e
    e["git"] = True
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", cwd, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=15,
                           **sem_janela())
        if r.returncode == 0:
            e["commit"] = r.stdout.strip()
        else:
            e["erro"] = (r.stderr or "").strip()[:120]
    except Exception as ex:
        e["erro"] = str(ex)[:120]
    try:
        pasta = os.path.join(cwd, "cerebro")
        if os.path.isdir(pasta):
            e["notas"] = len([f for f in os.listdir(pasta) if f.endswith(".md")])
    except Exception:
        pass
    # Mudança no cérebro que ficou só nesta máquina: editada e não commitada,
    # ou commitada e não empurrada. As outras máquinas nunca a veem, e nada
    # avisava — o modelo escrevia a nota, esquecia o push, e a descoberta
    # sumia. Só olha o cérebro e os arquivos de instrução, não o repositório
    # todo: no boss o clone é a pasta de trabalho, cheia de arquivo solto.
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", cwd, "status", "--porcelain",
                            "--", "cerebro", "CLAUDE.md", "AGENTS.md", "GEMINI.md"],
                           capture_output=True, text=True, timeout=15, **sem_janela())
        if r.returncode == 0:
            e["pendentes"] = len([l for l in r.stdout.splitlines() if l.strip()])
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", cwd, "rev-list", "--count",
                            "@{u}..HEAD"], capture_output=True, text=True, timeout=15, **sem_janela())
        if r.returncode == 0:
            e["a_frente"] = int(r.stdout.strip() or 0)
    except Exception:
        pass
    return e


# Estado do cérebro local. Recalculado na partida e depois de cada `git pull`,
# não a cada batida: seriam quatro processos `git` por máquina por minuto para
# um dado que só muda quando alguém empurra alguma coisa.
CEREBRO: dict = {}


def atualizar_cerebro(pull_ok: bool | None = None) -> None:
    """Recalcula o estado. `pull_ok` só é passado logo após uma tentativa de
    pull; nos outros momentos o resultado anterior é preservado.

    Estar atrás do repositório central por alguns minutos NÃO é defeito: o
    agente puxa na hora de raciocinar e a cada SINCRONIZAR_CEREBRO_S ocioso,
    não a cada batida. O que é defeito é o pull ter FALHADO — aí a máquina vai
    raciocinar com contexto velho sem que ninguém perceba.
    """
    e = estado_cerebro(os.environ.get("CLAUDE_CWD") or None)
    if pull_ok is None:
        e["pull_ok"] = CEREBRO.get("pull_ok")
        e["pull_em"] = CEREBRO.get("pull_em")
    else:
        e["pull_ok"] = pull_ok
        e["pull_em"] = time.strftime("%Y-%m-%d %H:%M:%S")
    CEREBRO.clear()
    CEREBRO.update(e)


def puxar_cerebro(cwd: str | None, timeout: int = 45) -> bool | None:
    """`git pull --ff-only` no clone e recalcula o estado do cérebro.

    Devolve True/False conforme o pull aplicou, ou None se `cwd` não é um clone
    (aí nem tenta e não mexe no estado). Falha é ignorada de propósito: ficar
    sem rede atrasa o contexto, mas não pode impedir de responder — o que o
    painel usa para acusar contexto velho é o `pull_ok` que fica em CEREBRO.
    """
    if not (cwd and os.path.isdir(os.path.join(cwd, ".git"))):
        return None
    ok = False
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", cwd, "pull", "--ff-only", "--quiet"],
                           capture_output=True, text=True, timeout=timeout,
                           **sem_janela())
        ok = (r.returncode == 0)
        if not ok:
            log("git pull não aplicou (segue assim mesmo):",
                (r.stderr or "").strip()[:200])
    except Exception as e:
        log("git pull falhou (segue assim mesmo):", e)
    # Recalcula depois do pull: é o único momento em que o commit muda.
    atualizar_cerebro(ok)
    return ok


_ultimo_pull_ok = [0.0]
PULL_FRESCO_S = int(os.environ.get("PULL_FRESCO_S", "120"))


def puxar_antes_de_raciocinar(cwd: str | None) -> None:
    """O pull de antes de cada job de modelo, pulado se o último deu certo há
    menos de PULL_FRESCO_S. Numa conversa, cada mensagem pagava 1 a 3 s de
    `git pull` pelo Wi-Fi do Pi para trazer um cérebro que não mudou."""
    if time.monotonic() - _ultimo_pull_ok[0] < PULL_FRESCO_S:
        return
    if puxar_cerebro(cwd):
        _ultimo_pull_ok[0] = time.monotonic()


def sincronizador_cerebro() -> None:
    """Puxa o cérebro periodicamente enquanto a máquina está ociosa.

    Só ocioso: durante um job o pull já aconteceu na largada (tipos `claude` e
    `gemini`), e mexer no clone debaixo de um job `shell` que roda script do
    repositório seria pedir problema. Se estiver ocupado, espera o próximo ciclo.
    """
    cwd = os.environ.get("CLAUDE_CWD") or None
    if not (cwd and os.path.isdir(os.path.join(cwd, ".git"))):
        return
    while True:
        # Pull que falhou tenta de novo em 1 min, não em 15: a falha costuma
        # ser passageira (roteador reiniciando, Pi reiniciando), e o painel
        # mostrava "pull falhou" em amarelo por 15 min à toa (aspire5,
        # 2026-09-25). Falha que persiste continua aparecendo.
        time.sleep(60 if CEREBRO.get("pull_ok") is False else SINCRONIZAR_CEREBRO_S)
        with _lock:
            ocupado = _estado["job_atual"] is not None
        if ocupado:
            continue
        antes = CEREBRO.get("commit")
        if puxar_cerebro(cwd) and CEREBRO.get("commit") != antes:
            log("cérebro sincronizado:", antes, "->", CEREBRO.get("commit"))
            enviar_cerebro_nuvem()


def inventario() -> dict:
    """Descrição fixa da máquina, calculada UMA vez na partida.

    Vai junto no heartbeat de propósito: o broker guarda agente só em memória
    (o banco fica num cartão SD e só recebe mudança de estado de job), então
    ele precisa reaprender quem é cada máquina sozinho depois de reiniciar.
    São ~200 bytes a cada 10s e nada disso toca o disco.
    """
    inv: dict[str, object] = {
        "so": _so_nome(),
        "cpu": _cpu_nome(),
        "nucleos": os.cpu_count() or 0,
        "arquitetura": platform.machine(),
        "ram_total_mb": _ram_total_mb(),
        "gpu": _gpu_nome(),
        "python": platform.python_version(),
        "disco_total_gb": None,
    }
    inv.update(estado_ias())
    try:
        # Só o total entra aqui: o espaço LIVRE muda o tempo todo e por isso é
        # métrica, medida a cada heartbeat, não inventário fixo.
        inv["disco_total_gb"] = round(
            shutil.disk_usage(os.path.abspath(os.sep)).total / (1024 ** 3), 1)
    except Exception:
        pass
    return inv


# Preenchido em main(), depois da trava de instância única: um agente duplicado
# sai em seguida e não tem por que sondar nvidia-smi e registro do Windows.
INVENTARIO: dict = {}

REAVALIAR_IAS_S = int(os.environ.get("REAVALIAR_IAS_S", "1200"))


def reavaliador_ias() -> None:
    """Refaz de tempos em tempos só a parte do inventário que muda com a
    máquina ligada (o que está instalado e com login). Numa thread própria
    porque `claude auth status` pode levar segundos, e o heartbeat não espera.
    Ocupado, pula: o job em andamento já está usando a CLI."""
    while True:
        time.sleep(REAVALIAR_IAS_S)
        with _lock:
            ocupado = _estado["job_atual"] is not None
        if ocupado:
            continue
        try:
            INVENTARIO.update(estado_ias())
        except Exception as e:
            log("reavaliar IAs falhou:", e)


def batedor() -> None:
    """Heartbeat a cada 10s, mesmo ocioso. Nunca morre por broker offline."""
    while True:
        try:
            with _lock:
                job = _estado["job_atual"]
            corpo = {"hostname": NOME, "tags": TAGS, "job_atual": job,
                     "versao_agente": VERSAO + "/" + platform.machine(),
                     "info": INVENTARIO, "cerebro": CEREBRO}
            corpo.update(metricas())
            _, resp = chamar("POST", "/api/agentes/heartbeat", corpo)
            if resp.get("cancelar"):
                with _lock:
                    _estado["cancelar"] = True
            _ajustar_ritmo(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log("o Jarvis recusou este agente (token inválido ou máquina removida). "
                    "Gere um código no painel e rode: python agente.py --parear CODIGO")
            else:
                log("heartbeat falhou:", e)
        except Exception as e:
            log("heartbeat falhou:", e)
        time.sleep(HEARTBEAT_S)


def _ajustar_ritmo(resp: dict) -> None:
    """A nuvem diz de quanto em quanto tempo bater e quanto esperar por trabalho."""
    global HEARTBEAT_S, ESPERA_S
    try:
        if resp.get("heartbeat_s"):
            HEARTBEAT_S = max(5, min(int(resp["heartbeat_s"]), 300))
        if resp.get("espera_s"):
            ESPERA_S = max(5, min(int(resp["espera_s"]), 120))
    except (TypeError, ValueError):
        pass


def enviar_cerebro_nuvem() -> None:
    """Na nuvem, manda as notas .md do cérebro local para aparecerem no painel."""
    cwd = os.environ.get("CLAUDE_CWD") or None
    pasta = os.path.join(cwd, "cerebro") if cwd else None
    if not (TOKEN and pasta and os.path.isdir(pasta)):
        return
    notas = []
    for arq in sorted(os.listdir(pasta))[:300]:
        if arq.endswith(".md"):
            try:
                with open(os.path.join(pasta, arq), encoding="utf-8", errors="replace") as f:
                    notas.append({"nome": arq[:-3], "conteudo": f.read(100_000)})
            except OSError:
                continue
    try:
        _, r = chamar("POST", "/api/agente/cerebro", {"notas": notas}, timeout=60)
        log("cérebro enviado ao painel:", r.get("gravadas"), "notas")
    except Exception as e:
        log("não consegui enviar o cérebro (segue o jogo):", e)


# ------------------------------------------------------------- execução
def rodar(job_id: int, comando: str | list[str], shell: bool,
          cwd: str | None = None, exigir_sucesso: bool = True,
          codificacao: str | None = None,
          ambiente: dict | None = None,
          transformar_linha: Any = None) -> dict:
    """Roda um processo streamando log e respeitando cancelamento.

    `comando` é string quando shell=True e lista de argumentos quando shell=False
    — a segunda forma evita ter que escapar aspas, o que importa para o job
    `claude`, cujo prompt é texto livre digitado pelo usuário.
    """
    with _lock:
        if _estado.get("cancelar"):
            raise RuntimeError("cancelado")

    proc = subprocess.Popen(
        comando, shell=shell, cwd=cwd, env=ambiente,
        # Ninguém digita nada num agente: um comando que pare esperando a
        # entrada ficaria preso até alguém cancelar. Com DEVNULL ele recebe
        # fim de arquivo na hora. O Codex, em particular, lê a entrada padrão
        # quando ela está aberta e anexa o que vier ao prompt.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        # Não existe uma codificação certa para tudo no Windows: o cmd.exe
        # escreve na codepage OEM do console (cp850 aqui) e o claude escreve
        # UTF-8. Decodificar tudo como um só corrompe o outro, então quem chama
        # diz qual esperar; o default é o do console, que serve para job shell.
        encoding=(codificacao or ("oem" if os.name == "nt" else "utf-8")),
        errors="replace",
        # grupo próprio para conseguir matar filhos no cancelamento
        start_new_session=(os.name != "nt"),
        **sem_janela(),
    )

    buffer: list[str] = []
    saida_total: list[str] = []
    buf_lock = threading.Lock()

    def ler() -> None:
        """Thread só de leitura: nunca bloqueia o envio de progresso."""
        assert proc.stdout is not None
        for linha in proc.stdout:
            texto = transformar_linha(linha) if transformar_linha else linha
            if texto:
                with buf_lock:
                    buffer.append(texto)
                    saida_total.append(texto)

    leitor = threading.Thread(target=ler, daemon=True)
    leitor.start()

    def enviar(mensagem: str | None = None) -> bool:
        """Manda o acumulado; devolve True se o broker pediu cancelamento."""
        with buf_lock:
            texto = "".join(buffer)
            buffer.clear()
        try:
            _, r = chamar("POST", "/api/trabalho/" + str(job_id) + "/progresso",
                          {"agente": NOME, "log": texto, "mensagem": mensagem})
            return bool(r.get("cancelar"))
        except Exception as e:
            log("progresso falhou (segue o jogo):", e)
            return False

    # O envio é por relógio, não por linha lida: um job que fica minutos calado
    # precisa continuar publicando log e, principalmente, continuar percebendo
    # um pedido de cancelamento.
    inicio = time.time()
    while proc.poll() is None:
        time.sleep(PROGRESSO_S)
        with buf_lock:
            linhas = len(saida_total)
        with _lock:
            pedido = _estado["cancelar"]
        # A mensagem carrega o tempo decorrido: em job sem total conhecido, é o
        # que mostra que a coisa está viva. Só a contagem de linhas fica parada
        # quando o processo está calado, e parece travamento.
        msg = "executando há " + str(int(time.time() - inicio)) + "s"
        if linhas:
            msg += " · " + str(linhas) + " linha" + ("s" if linhas > 1 else "")
        if enviar(msg) or pedido:
            matar(proc)
            raise RuntimeError("cancelado")

    leitor.join(timeout=10)     # drena o que sobrou no cano
    proc.wait()
    linhas = len(saida_total)
    if enviar("finalizando") or _estado.get("cancelar"):
        raise RuntimeError("cancelado")

    saida = "".join(saida_total)
    if exigir_sucesso and proc.returncode != 0:
        raise RuntimeError("comando saiu com código " + str(proc.returncode) +
                           ": " + saida.strip()[-300:])
    return {"returncode": proc.returncode, "linhas": linhas,
            "stdout": saida[-8192:], "saida_completa": saida}


# Frases com que a CLI do Claude recusa por cota, saindo com codigo 0. A lista
# e curta de proposito: casar demais transformaria uma resposta legitima que
# fale de "limite" numa falha inventada. O tamanho da resposta entra na conta
# junto — resposta de verdade nao cabe em 400 caracteres.
RECUSA_DE_COTA = re.compile(
    r"(hit your (weekly|usage) limit|limite (semanal|de uso)"
    r"|usage limit reached|rate limit(ed)? exceeded"
    r"|out of (credits|tokens)|upgrade to continue"
    r"|resets? \d{1,2}(:\d{2})?\s*(am|pm)?)", re.I)


# Erro de API por cota ou saldo, que a CLI imprime dentro de um rastro de
# pilha e termina com codigo 0. Diferente da recusa em frase, este vem
# longo -- por isso NAO olha o tamanho da resposta. Em 2026-09-11 a fila
# de leads das 8h 'concluiu' com 'Your prepayment credits are depleted'
# enterrado em 40 linhas de JavaScript.
ERRO_DE_API = re.compile(
    r'("code":\s*429|RESOURCE_EXHAUSTED|credits are depleted'
    r'|insufficient[_ ]quota|quota exceeded|quota reached|billing)', re.I)


def rodar_preparo(job: dict, cwd: str | None, ambiente: dict) -> None:
    """Roda o campo `antes` do payload — um comando shell — ANTES do modelo.

    POR QUE EXISTE
    A regra do parque é que o mecânico roda como shell, sem gastar token, e o
    modelo só faz o que precisa de julgamento. Até 2026-09-12 a fila de leads
    pedia ao modelo que rodasse `montar-fila.py`, e o agy em modo -p pôs o
    script como tarefa de fundo, esperou 5 minutos (`--print-timeout`) e saiu
    com código 0 dizendo "terminating 1 background task(s) on exit" — sem
    dossiê, sem fila, e o job verde. Com `antes`, o script roda aqui, com o log
    no painel, e falha do script é falha do job antes de gastar um token.
    """
    antes = job["payload"].get("antes")
    if not antes:
        return
    ambiente.setdefault("PYTHONUTF8", "1")
    ambiente.setdefault("PYTHONIOENCODING", "utf-8")
    rodar(job["id"], antes, shell=True, cwd=cwd, exigir_sucesso=True,
          ambiente=ambiente)


def executar_shell(job: dict) -> dict:
    comando = job["payload"].get("comando")
    if not comando:
        raise ValueError("payload sem campo 'comando'")
    # Um script precisa poder saber que está rodando DENTRO de um job, e qual.
    # O `deploy.sh` usa isso para não reiniciar o agente que o está executando —
    # em 2026-09-09 ele se matou no meio e o job voltou para a fila. Serve
    # também para um job criar outro job marcado como filho dele.
    ambiente = dict(os.environ)
    ambiente["ORQ_JOB_ID"] = str(job["id"])
    ambiente["ORQ_BROKER"] = BROKER
    ambiente.setdefault("PYTHONUTF8", "1")
    ambiente.setdefault("PYTHONIOENCODING", "utf-8")
    r = rodar(job["id"], comando, shell=True, ambiente=ambiente)
    r.pop("saida_completa", None)
    return r


def preparar_anexo_local(anexo_dado: Any, nome_padrao: str = "anexo.bin") -> str | None:
    """Garante que o anexo (imagem, áudio, etc.) referenciado no job exista no disco local deste agente."""
    if not anexo_dado:
        return None
    if isinstance(anexo_dado, str) and anexo_dado.strip().startswith("{"):
        try:
            anexo_dado = json.loads(anexo_dado)
        except Exception:
            try:
                import ast
                anexo_dado = ast.literal_eval(anexo_dado)
            except Exception:
                pass
    url = ""
    caminho_remoto = ""
    nome = nome_padrao
    if isinstance(anexo_dado, dict):
        url = anexo_dado.get("url") or ""
        caminho_remoto = anexo_dado.get("caminho") or ""
        nome = anexo_dado.get("nome") or (Path(url).name if url else nome_padrao)
    elif isinstance(anexo_dado, str):
        if anexo_dado.startswith("/uploads/") or anexo_dado.startswith("http"):
            url = anexo_dado
            nome = Path(url).name
        else:
            caminho_remoto = anexo_dado
            nome = Path(caminho_remoto).name

    # Se o caminho já existe localmente (ex: agente rodando no mesmo host do broker):
    if caminho_remoto and Path(caminho_remoto).is_file():
        return str(Path(caminho_remoto).resolve())

    # Se temos URL, baixa para a pasta temporária local do agente
    if url:
        pasta_temp = Path(tempfile.gettempdir()) / "orq_uploads"
        pasta_temp.mkdir(parents=True, exist_ok=True)
        nome_limpo = re.sub(r"[^a-zA-Z0-9_.-]", "_", nome)
        destino_local = pasta_temp / nome_limpo
        if not destino_local.is_file() or destino_local.stat().st_size == 0:
            url_completa = urllib.parse.urljoin(BROKER, url)
            try:
                req = urllib.request.Request(url_completa)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    destino_local.write_bytes(resp.read())
            except Exception as e:
                log(f"Falha ao baixar anexo do broker ({url_completa}): {e}")
                return None
        return str(destino_local.resolve())
    return None


preparar_imagem_local = preparar_anexo_local


def transcrever_audio_local(caminho_audio: str) -> str:
    """Transcreve áudio localmente via Gemini Flash / agy para servir ao Claude Code sem gastar tokens caros."""
    if not caminho_audio or not Path(caminho_audio).is_file():
        return ""
    binario, _ = _localizar_binario_gemini()
    if not binario:
        binario = shutil.which("gemini") or shutil.which("agy") or shutil.which("gemini.exe") or shutil.which("agy.exe")
        if not binario and Path("/usr/local/bin/gemini").exists():
            binario = "/usr/local/bin/gemini"
    if not binario or not (shutil.which(binario) or os.path.exists(binario)):
        return ""

    prompt_transc = (
        f"[Transcreva a fala contida no arquivo de áudio: {caminho_audio}]\n"
        "Transcreva com exatidão a mensagem de voz gravada pelo usuário. "
        "Devolva apenas e exclusivamente o texto falado, sem aspas, explicações, introduções ou notas."
    )
    nome_bin = Path(binario).name.lower()
    if "agy" in nome_bin:
        cmd = [binario, "-p", prompt_transc, "--dangerously-skip-permissions", "--output-format", "text"]
    else:
        cmd = [binario, "--yolo", "-p", prompt_transc]

    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=50, encoding="utf-8", errors="replace", **sem_janela())
        if r.returncode == 0 and r.stdout.strip():
            texto = r.stdout.strip()
            if texto.startswith('"') and texto.endswith('"') and len(texto) >= 2:
                texto = texto[1:-1].strip()
            return texto
    except Exception as e:
        log("transcrição de áudio local falhou (seguindo sem transcrição):", e)
    return ""


def texto_do_audio(p: dict) -> str | None:
    """O aviso de mensagem de voz que vai na frente do prompt (Claude e Codex).

    Sem transcrição feita no navegador, tenta a local pelo Gemini: nenhum dos
    dois ouve áudio, e mandar só o caminho do arquivo não diz nada a eles.
    """
    caminho_audio = preparar_anexo_local(p.get("audio"), nome_padrao="audio.webm")
    if not caminho_audio:
        return None
    dado_audio = p.get("audio")
    transcricao = ""
    if isinstance(dado_audio, dict):
        transcricao = (dado_audio.get("transcricao") or "").strip()
    if not transcricao:
        log("Áudio recebido sem transcrição do navegador. Acionando transcritor local...")
        transcricao = transcrever_audio_local(caminho_audio)
        if transcricao:
            log(f"Transcrição local gerada com sucesso: {transcricao[:80]}...")
    if transcricao:
        return (f"[O usuário enviou uma mensagem de voz gravada. Arquivo salvo em: {caminho_audio}]\n"
                f"Transcrição da mensagem de voz do usuário:\n\"{transcricao}\"")
    return (f"[O usuário enviou uma mensagem de voz gravada salva localmente em: {caminho_audio}]\n"
            f"Responda à mensagem de voz considerando o contexto da conversa.")


class ErroDeIA(RuntimeError):
    """Falha de modelo que o broker pode resolver trocando de modelo.

    `motivo` vai junto no relatório de falha ("cota" ou "login"), para o broker
    decidir sem adivinhar pelo texto da mensagem — adivinhar pelo texto é o que
    ele ainda faz com agente antigo, que não manda o campo.
    """

    def __init__(self, mensagem: str, motivo: str) -> None:
        super().__init__(mensagem)
        self.motivo = motivo


ORDEM_IA = ("claude", "gemini", "codex")
NOME_IA = {"claude": "Claude", "gemini": "Gemini", "codex": "Codex"}


def ia_disponivel(tipo: str) -> bool:
    if tipo == "claude":
        return _tem_claude_local() and INVENTARIO.get("claude_logado") is not False
    if tipo == "gemini":
        return _tem_gemini_local()
    if tipo == "codex":
        return bool(_localizar_codex()) and INVENTARIO.get("codex_logado") is not False
    return False


def alternar_ia(job: dict, tipo_atual: str, motivo: str) -> dict | None:
    """Refaz o pedido com a próxima IA desta máquina que ainda não foi tentada.

    Claude → Gemini → Codex → Claude. A lista `_tentados` vai no payload e
    acompanha o job até o broker, que faz a mesma troca quando a máquina não
    tem alternativa local — assim nenhum pedido dá volta infinita.
    """
    p = job["payload"]
    tentados = set(p.get("_tentados") or []) | {tipo_atual}
    # marcas da versão anterior, que ainda podem vir em job reenfileirado
    if p.get("_fallback_gemini"):
        tentados.add("gemini")
    if p.get("_fallback_claude"):
        tentados.add("claude")
    # A cota do Antigravity é compartilhada com o uso pessoal do Caio. Não
    # consumir essa cota como fallback automático do Claude no orquestrador.
    if tipo_atual == "claude":
        tentados.add("gemini")
    i = ORDEM_IA.index(tipo_atual)
    for alvo in ORDEM_IA[i + 1:] + ORDEM_IA[:i]:
        if alvo in tentados or not ia_disponivel(alvo):
            continue
        log(motivo + " — alternando para " + NOME_IA[alvo])
        p["_tentados"] = sorted(tentados | {alvo})
        # A sessão pertence ao modelo que falhou: passar o id de uma conversa do
        # Claude ao agy ou ao Codex faria o outro tentar retomar uma conversa
        # que não é dele.
        # o modelo também é do que falhou: "gemini-..." no pedido do Codex ou do Claude daria erro
        alt = dict(job, payload=dict(p, sessao=None, modelo=None))
        res = EXECUTORES[alvo](alt)
        p["_tentados"] = alt["payload"].get("_tentados", p["_tentados"])
        res.setdefault("aviso", motivo + " — respondido via " + NOME_IA[alvo])
        res["modelo"] = (str(res.get("modelo") or alvo) +
                         " [fallback do " + NOME_IA[tipo_atual] + "]")
        res["ia"] = res.get("ia") or alvo
        # Conversa que já existia continua agrupada onde estava no painel; a
        # próxima mensagem volta a tentar o modelo escolhido.
        if p.get("sessao"):
            res["sessao_fallback"] = res.get("sessao")
            res["sessao"] = p["sessao"]
        return res
    return None


def executar_claude(job: dict) -> dict:
    """Manda um prompt para o Claude Code e devolve a resposta.

    Roda com `--permission-mode bypassPermissions` por decisão explícita do
    dono do parque: o chat tem acesso total à máquina. Vale lembrar que o
    orquestrador não tem autenticação própria, então qualquer um no tailnet
    alcança isto.

    A conversa continua entre mensagens pelo id de sessão: a primeira mensagem
    cria um `--session-id` e as seguintes usam `--resume` com o mesmo id, que
    volta no resultado para a UI reaproveitar.
    """
    p = job["payload"]
    prompt = p.get("prompt")
    if not prompt:
        raise ValueError("payload sem campo 'prompt'")

    # Se o job tem anexo de imagem ou áudio, localiza/baixa e avisa o modelo
    caminho_img = preparar_imagem_local(p.get("imagem"))
    prefixos_anexos = []
    if caminho_img:
        prefixos_anexos.append(
            f"[O usuário anexou uma imagem salva localmente em: {caminho_img}]\n"
            f"Inspecione a imagem com suas ferramentas visuais/leitura de arquivo e responda."
        )
    texto_audio = texto_do_audio(p)
    if texto_audio:
        prefixos_anexos.append(texto_audio)

    prompt_exec = prompt
    if prefixos_anexos:
        prompt_exec = "\n\n".join(prefixos_anexos) + f"\n\n{prompt}"

    binario = _localizar_binario_claude() or os.environ.get("CLAUDE_BIN", "claude")
    if not (shutil.which(binario) or os.path.exists(binario)):
        raise RuntimeError("claude não encontrado no PATH deste agente "
                           "(defina CLAUDE_BIN se estiver fora do PATH)")

    sessao = p.get("sessao") or str(uuid.uuid4())
    continuando = bool(p.get("sessao"))
    argv = [binario, "-p", prompt_exec, "--permission-mode", "bypassPermissions"]
    argv += ["--resume", sessao] if continuando else ["--session-id", sessao]
    # Modelo: o payload manda, senão MODELO_CLAUDE do ambiente, senão o padrão
    # da CLI. Deixar isso no ambiente permite cada máquina ter o seu — o Pi
    # pode usar um modelo e o boss outro, sem mexer em código.
    modelo = p.get("modelo") or os.environ.get("MODELO_CLAUDE")
    if modelo:
        argv += ["--model", str(modelo)]

    # O diretório de trabalho é o que faz o Claude Code carregar o CLAUDE.md do
    # projeto — é assim que ele já chega sabendo do parque, em vez de começar do
    # zero a cada mensagem.
    cwd = p.get("cwd") or os.environ.get("CLAUDE_CWD") or None
    if cwd and not os.path.isdir(cwd):
        raise RuntimeError("diretório de trabalho não existe: " + cwd)

    # O cérebro do parque é compartilhado por git: cada máquina puxa antes de
    # raciocinar, senão responde com um contexto que já mudou noutra máquina —
    # foi o que aconteceu com o Pi, que passou dois dias com o CLAUDE.md velho.
    # Falha aqui é ignorada de propósito: ficar sem rede atrasa o contexto, mas
    # não pode impedir de responder.
    puxar_antes_de_raciocinar(cwd)

    # Sob pythonw, sem console, a CLI às vezes encerra com código diferente de
    # zero mesmo tendo respondido por inteiro (visto: 129). O que interessa aqui
    # é o texto: sem resposta é falha, com resposta é sucesso e o código fica
    # registrado no resultado.
    # O Claude precisa saber em qual job ele está rodando para marcar os jobs que
    # criar como filhos desta conversa — sem isso o painel não tem como ligar o
    # que foi executado à mensagem que pediu.
    ambiente = dict(os.environ)
    ambiente["ORQ_JOB_ID"] = str(job["id"])
    ambiente["ORQ_BROKER"] = BROKER
    rodar_preparo(job, cwd, ambiente)

    r = rodar(job["id"], argv, shell=False, cwd=cwd, exigir_sucesso=False,
              codificacao="utf-8", ambiente=ambiente)
    resposta = (r.get("saida_completa") or "").strip()
    if continuando and ("No conversation found with session ID" in resposta or
                        "No conversation found with session ID" in str(r.get("erro") or "")):
        # O id de sessão foi pré-alocado pela UI/broker para a conversa antes do Claude criá-lo
        argv_novo = [a if a != "--resume" else "--session-id" for a in argv]
        r = rodar(job["id"], argv_novo, shell=False, cwd=cwd, exigir_sucesso=False,
                  codificacao="utf-8", ambiente=ambiente)
        resposta = (r.get("saida_completa") or "").strip()
    if not resposta:
        raise RuntimeError("o Claude não devolveu texto algum (código " +
                           str(r["returncode"]) + ")")

    # A CLI sai com código 0 e uma FRASE quando a assinatura bate no teto. Sem
    # esta checagem o job fica verde no painel tendo feito nada: em 2026-09-10 a
    # fila de leads das 8h "concluiu" devolvendo só "You've hit your weekly
    # limit", e ninguém soube até alguém abrir a pasta do Drive e estranhar.
    bateu_cota = (len(resposta) < 400 and RECUSA_DE_COTA.search(resposta)) or bool(ERRO_DE_API.search(resposta))
    if bateu_cota:
        motivo = "a assinatura do Claude bateu no limite" if RECUSA_DE_COTA.search(resposta) else "a API do Claude recusou por cota ou saldo"
        res = alternar_ia(job, "claude", motivo)
        if res:
            return res
        raise ErroDeIA(motivo + ": " + " ".join(resposta.split())[:200], "cota")

    return {"resposta": resposta, "sessao": sessao, "cwd": cwd or "",
            "modelo": modelo or "(padrão da CLI)", "cli": os.path.basename(binario),
            "continuou": continuando, "returncode": r["returncode"],
            "linhas": r["linhas"], "ia": "claude"}


def matar(proc: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                proc.wait(timeout=4)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        else:
            # No Windows, shell=True significa cmd.exe: terminate() mataria só o
            # cmd e deixaria o processo de verdade rodando órfão. taskkill /T
            # derruba a árvore inteira.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15, **sem_janela())
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _conversa_gemini_existe(cid: Any) -> bool:
    """Verifica se o ID de conversa do agy realmente existe localmente.

    Evita passar --conversation com ID que não existe (ex: UUID gerado no
    broker/UI para agrupar o chat, ID de conversa do Claude, ou conversa de
    outra máquina). Sem esta checagem o agy imprime no stderr a mensagem:
    'warning: conversation "<id>" not found' e inicia nova conversa do mesmo jeito.
    """
    if not cid:
        return False
    c = str(cid).strip()
    caminhos = [
        Path.home() / ".gemini" / "antigravity-cli",
        Path("/home/pi/.gemini/antigravity-cli"),
    ]
    for base in caminhos:
        try:
            if (base / "conversations" / f"{c}.db").exists() or (base / "brain" / c).is_dir():
                return True
        except Exception:
            pass
    return False


def executar_gemini(job: dict) -> dict:
    """Manda um prompt para o Gemini CLI. Mesmo contrato do `claude`.

    POR QUE EXISTE
    Decisão do Caio em 2026-09-10: trabalho simples e repetitivo não deve
    gastar a assinatura do Claude, que é para o que é difícil. Tarefa agendada
    é exatamente isso — redigir 15 ganchos a partir de um dossiê pronto não
    precisa do modelo mais caro da casa.

    Serve também de reserva: quando o `claude` falha por cota, o mesmo trabalho
    pode ser reenfileirado com a tag `gemini` e o dia não fica sem fila.

    DIFERENÇA QUE IMPORTA: o Gemini CLI lê `GEMINI.md`, não `CLAUDE.md`. O
    parque resolve isso com um `GEMINI.md` na raiz do repositório apontando
    para o mesmo conteúdo — sem ele, o modelo chega sem saber o que é o parque.
    """
    p = job["payload"]
    prompt = p.get("prompt")
    if not prompt:
        raise ValueError("payload sem campo 'prompt'")

    # Se o job tem anexo de imagem ou áudio, localiza/baixa e avisa o modelo
    caminho_img = preparar_anexo_local(p.get("imagem"), nome_padrao="imagem.png")
    caminho_audio = preparar_anexo_local(p.get("audio"), nome_padrao="audio.webm")
    prefixos_anexos = []
    if caminho_img:
        prefixos_anexos.append(
            f"[O usuário anexou uma imagem salva localmente em: {caminho_img}]\n"
            f"Inspecione a imagem com suas ferramentas visuais/leitura de arquivo e responda."
        )
    if caminho_audio:
        transcricao = ""
        dado_audio = p.get("audio")
        if isinstance(dado_audio, dict):
            transcricao = (dado_audio.get("transcricao") or "").strip()
        if transcricao:
            prefixos_anexos.append(
                f"[O usuário enviou uma gravação de áudio salva localmente em: {caminho_audio}]\n"
                f"Transcrição da mensagem de voz:\n\"{transcricao}\"\n"
                f"Ouça/analise também o arquivo de áudio gravado e responda à mensagem."
            )
        else:
            prefixos_anexos.append(
                f"[O usuário enviou uma gravação de áudio salva localmente em: {caminho_audio}]\n"
                f"Ouça ou inspecione o arquivo de áudio gravado pelo usuário para compreender sua solicitação e responda."
            )
    prompt_exec = prompt
    if prefixos_anexos:
        prompt_exec = "\n\n".join(prefixos_anexos) + f"\n\n{prompt}"

    # A chave da API, quando existe, vem de um arquivo fora do repositório —
    # mesmo padrão da chave de busca. Fica numa variável local e entra só no
    # ambiente DESTE processo. Escrever em os.environ passaria a chave para
    # todo job `shell` que o agente rodar depois, e segredo que viaja para
    # onde não precisa é segredo a caminho de vazar.
    arquivo_chave = Path.home() / ".config" / "orq" / "gemini.chave"
    chave = ""
    if not os.environ.get("GEMINI_API_KEY") and arquivo_chave.exists():
        chave = arquivo_chave.read_text(encoding="utf-8").strip()

    # DUAS CLIs, UM TIPO DE JOB. Em 2026-06-18 o Google cortou o `gemini` para
    # conta individual (grátis, AI Pro e Ultra): só chave de API paga entra
    # nele. O que a assinatura inclui hoje é o Antigravity CLI (`agy`), com o
    # mesmo login Google e um modo -p para rodar sem terminal. Descoberto em
    # 2026-09-12, quando o "Sign in with Google" do `gemini` respondeu "This
    # client is no longer supported for Gemini Code Assist for individuals".
    #
    # Então: com chave, é o `gemini` (pré-pago); sem chave, é o `agy` com a
    # conta do dono (assinatura). A decisão do Caio é gastar a assinatura.
    modelo = p.get("modelo") or os.environ.get("MODELO_GEMINI")
    binario, eh_chave = _localizar_binario_gemini()
    if chave or eh_chave:
        bin_chave = binario or os.environ.get("GEMINI_BIN", "gemini")
        if not (shutil.which(bin_chave) or os.path.exists(bin_chave)):
            raise RuntimeError("gemini não encontrado no PATH deste agente "
                               "(defina GEMINI_BIN se estiver fora do PATH)")
        # --yolo é o equivalente do bypassPermissions: sem ele a CLI para
        # pedindo confirmação e o job trava até o tempo acabar, sem dizer
        # por quê.
        argv = [bin_chave, "--yolo", "-p", prompt_exec]
        if modelo:
            argv += ["-m", str(modelo)]
    else:
        if not binario or not (shutil.which(binario) or os.path.exists(binario)):
            raise RuntimeError(
                "sem chave em ~/.config/orq/gemini.chave e sem `agy` nesta "
                "máquina. Instale com "
                "`irm https://antigravity.google/cli/install.ps1 | iex` (Windows) ou "
                "`curl -fsSL https://antigravity.google/cli/install.sh | bash` (Linux)")
        # O padrão de --print-timeout é 5 minutos, e ao estourar o agy sai com
        # código 0 e mata o que estiver rodando. Redigir 15 ganchos leva mais
        # que isso num Pi.
        sessao = p.get("sessao")
        argv = [binario, "-p", prompt_exec, "--dangerously-skip-permissions",
                "--print-timeout", os.environ.get("AGY_TIMEOUT", "40m"),
                "--output-format", "stream-json"]
        if sessao and _conversa_gemini_existe(sessao):
            argv += ["--conversation", str(sessao)]
        if modelo:
            argv += ["--model", str(modelo)]

    cwd = p.get("cwd") or os.environ.get("CLAUDE_CWD") or None
    if cwd and not os.path.isdir(cwd):
        raise RuntimeError("diretório de trabalho não existe: " + cwd)

    # Mesma regra do cérebro compartilhado: puxa antes de raciocinar, e falha
    # de rede não impede de responder.
    puxar_antes_de_raciocinar(cwd)

    ambiente = dict(os.environ)
    ambiente["ORQ_JOB_ID"] = str(job["id"])
    ambiente["ORQ_BROKER"] = BROKER
    # Se a home atual não tiver .gemini, mas /home/pi/.gemini existir (ex: rodando sob root no Pi),
    # ajusta HOME para que o agy localize a autenticação da conta Google AI Pro
    if not (Path.home() / ".gemini").exists() and Path("/home/pi/.gemini").exists():
        ambiente["HOME"] = "/home/pi"
    # O Gemini CLI recusa trabalhar num diretório que não foi "confiado" em
    # modo interativo — e um agente nunca é interativo. Sem isto o job morre
    # com uma mensagem sobre pastas confiáveis que não parece erro de
    # autenticação nem de prompt, e custa tempo para entender.
    ambiente.setdefault("GEMINI_CLI_TRUST_WORKSPACE", "true")
    if chave:
        ambiente["GEMINI_API_KEY"] = chave
    rodar_preparo(job, cwd, ambiente)

    transformador = None
    sessao_valida = str(sessao) if (sessao and _conversa_gemini_existe(sessao)) else ""
    sessao_stream = [sessao_valida]
    resposta_stream = [""]
    deltas_stream: list[str] = []

    if not (chave or eh_chave):
        def _parse_agy(linha: str) -> str | None:
            l = linha.strip()
            if not l:
                return None
            try:
                data = json.loads(l)
                event = data.get("event")
                if event == "init":
                    cid = data.get("conversation_id")
                    if cid:
                        sessao_stream[0] = cid
                    return None
                elif event == "step_update":
                    su = data.get("step_update", {})
                    stype = su.get("step_type")
                    state = su.get("state")
                    if stype == "tool":
                        tname = su.get("tool_name") or (su.get("tool_info") or {}).get("name", "ferramenta")
                        if state == "ACTIVE":
                            params = (su.get("tool_info") or {}).get("parameters", {})
                            desc = ""
                            if tname == "generate_image":
                                p = " ".join(str(params.get('Prompt', '')).split())
                                desc = f" (prompt: {p[:60]}...)" if p else ""
                            elif tname == "run_command":
                                c = " ".join(str(params.get('CommandLine', '')).split())
                                desc = f" ({c[:60]})" if c else ""
                            elif tname == "write_to_file":
                                f = " ".join(str(params.get('TargetFile', '')).split())
                                desc = f" ({f[:60]})" if f else ""
                            return f"\n\n> ⚙️ **Executando `{tname}`**{desc}...\n\n"
                        elif state == "DONE":
                            dur = su.get("duration_seconds")
                            dur_s = f" ({dur:.1f}s)" if dur else ""
                            return f"> ✅ `{tname}` concluído{dur_s}\n\n"
                        return None
                    elif stype == "agent_response":
                        delta = su.get("text_delta")
                        if delta:
                            deltas_stream.append(delta)
                            return delta
                        return None
                elif event == "result":
                    res = data.get("result", {})
                    resp = res.get("response")
                    if resp:
                        resposta_stream[0] = resp
                    cid = res.get("conversation_id")
                    if cid:
                        sessao_stream[0] = cid
                    return None
            except Exception:
                l_lower = l.lower()
                if "conversation" in l_lower and "not found" in l_lower:
                    return None
                return linha
            return None
        transformador = _parse_agy

    r = rodar(job["id"], argv, shell=False, cwd=cwd, exigir_sucesso=False,
              codificacao="utf-8", ambiente=ambiente, transformar_linha=transformador)
    saida_bruta = (r.get("saida_completa") or "").strip()
    if not saida_bruta and not resposta_stream[0] and not deltas_stream:
        raise RuntimeError("o Gemini não devolveu texto algum (código " +
                           str(r["returncode"]) + ")")

    resposta = (resposta_stream[0].strip() or
                "".join(deltas_stream).strip() or
                saida_bruta)
    sessao_retornada = sessao_stream[0] or sessao_valida

    sem_auth = ("FatalAuthenticationError" in resposta or "set an Auth method" in resposta
            or "authentication required" in resposta
            or "authentication failed" in resposta)
    cota_estourada = (len(resposta) < 400 and RECUSA_DE_COTA.search(resposta))
    erro_api = bool(ERRO_DE_API.search(resposta))

    # O agy estourou o --print-timeout: matou o que estava fazendo e saiu com 0.
    if ("bounded by --print-timeout" in resposta
            or re.search(r"terminating \d+ background task", resposta)):
        raise RuntimeError("o agy estourou o tempo de resposta e abandonou o "
                           "trabalho no meio (ponha o mecânico em `antes`, "
                           "ou aumente AGY_TIMEOUT): " +
                           " ".join(resposta.split())[:200])

    if sem_auth or cota_estourada or erro_api:
        motivo = "a cota do Gemini acabou" if cota_estourada else ("o Gemini não está autenticado nesta máquina" if sem_auth else "a API do Gemini recusou")
        res = alternar_ia(job, "gemini", motivo)
        if res:
            return res
        if sem_auth:
            raise ErroDeIA(
                "o Gemini não está autenticado nesta máquina. Rode `agy` (ou "
                "`gemini`, se for usar chave) num terminal para entrar com a conta Google",
                "login")
        if cota_estourada:
            raise ErroDeIA("a cota do Gemini acabou: " +
                           " ".join(resposta.split())[:200], "cota")
        m = re.search(r'"message":\s*"([^"]{10,200})', resposta)
        raise ErroDeIA("a API do Gemini recusou: " +
                       (m.group(1) if m else " ".join(resposta.split())[:200]), "cota")
    return {"resposta": resposta, "sessao": sessao_retornada, "cwd": cwd or "",
            "modelo": modelo or "(padrão da CLI)",
            "returncode": r["returncode"], "linhas": r["linhas"], "ia": "gemini"}


def executar_codex(job: dict) -> dict:
    """Manda um prompt para o Codex CLI (OpenAI). Mesmo contrato do `claude`.

    POR QUE EXISTE
    Terceira reserva do parque, pela assinatura do ChatGPT do Caio
    (2026-09-24). O Codex lê `AGENTS.md`, que na raiz do repositório aponta
    para o mesmo `CLAUDE.md` — é assim que ele chega sabendo do parque.

    PERMISSÕES: diferente do Claude (bypassPermissions), o padrão aqui é o
    sandbox `workspace-write` com rede: escreve no clone e fala com o broker,
    mas não mexe fora da pasta do projeto. CODEX_SANDBOX=danger-full-access
    no ambiente da máquina libera tudo, se o dono decidir assim.

    A conversa continua pelo id da thread (`codex exec resume`), que vem no
    primeiro evento da saída JSON e volta no resultado como `sessao`.
    """
    p = job["payload"]
    prompt = p.get("prompt")
    if not prompt:
        raise ValueError("payload sem campo 'prompt'")
    base = _localizar_codex()
    if not base:
        raise RuntimeError("codex não encontrado nesta máquina. Instale com "
                           "`npm install -g @openai/codex` e entre com `codex login`")

    # Imagem vai como anexo de verdade (-i): o Codex vê os pixels sem precisar
    # abrir o arquivo com uma ferramenta, o que o Claude e o agy precisam.
    caminho_img = preparar_anexo_local(p.get("imagem"), nome_padrao="imagem.png")
    prompt_exec = prompt
    texto_audio = texto_do_audio(p)
    if texto_audio:
        prompt_exec = texto_audio + "\n\n" + prompt

    cwd = p.get("cwd") or os.environ.get("CLAUDE_CWD") or None
    if cwd and not os.path.isdir(cwd):
        raise RuntimeError("diretório de trabalho não existe: " + cwd)
    puxar_antes_de_raciocinar(cwd)

    sandbox = os.environ.get("CODEX_SANDBOX", "workspace-write")
    argv = base + ["exec", "--json", "--skip-git-repo-check", "-s", sandbox]
    if sandbox == "workspace-write":
        # sem rede, o chat não consegue criar job no broker nem dar push
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    if cwd:
        argv += ["-C", cwd]
    # Modelo sempre explícito: sem ele o Codex usa o padrão da instalação, e
    # no Pi o padrão era o gpt-6-astra, que a conta do ChatGPT recusa
    # (2026-09-25). Decisão do Caio: só o GPT-6 Luna, nas três máquinas.
    modelo = p.get("modelo") or os.environ.get("MODELO_CODEX") or "gpt-6-luna"
    argv += ["-m", str(modelo)]
    # Esforço médio no chat: o boss herdava o `xhigh` do app do ChatGPT, e um
    # "olá" levava dezenas de segundos pensando.
    esforco = p.get("esforco") or os.environ.get("CODEX_ESFORCO") or "medium"
    argv += ["-c", 'model_reasoning_effort="%s"' % esforco]
    # O CLAUDE.md como instrução do projeto, carregado direto no contexto, como
    # o Claude faz. Com um AGENTS.md mandando "leia o CLAUDE.md", o Codex
    # gastava uma rodada inteira do modelo só para abrir o arquivo antes de
    # responder até um "olá" (2026-09-25). E sem o conector de apps do
    # ChatGPT, que no Wi-Fi do Pi estourava o tempo a cada mensagem.
    # Também sem a atualização da lista de modelos nem a busca de versão nova:
    # no Pi cada uma estourava o tempo pelo Wi-Fi e somava ~45 s a um "olá".
    argv += ["-c", 'project_doc_fallback_filenames=["CLAUDE.md"]', "--disable", "apps",
             "--disable", "remote_models", "-c", "check_for_update_on_startup=false"]
    sessao = p.get("sessao")
    retomar_codex = bool(sessao and str(sessao).startswith("thread_"))
    if retomar_codex:
        argv += ["resume"]
    if caminho_img:
        argv += ["-i", caminho_img]
    # `--` antes dos posicionais: -i aceita vários arquivos e engoliria o
    # prompt, e um prompt que comece com "-" viraria opção.
    argv += ["--"] + ([str(sessao)] if retomar_codex else []) + [prompt_exec]

    ambiente = dict(os.environ)
    ambiente["ORQ_JOB_ID"] = str(job["id"])
    ambiente["ORQ_BROKER"] = BROKER
    rodar_preparo(job, cwd, ambiente)

    estado = {"sessao": sessao or "", "mensagens": [], "erro": "", "uso": None}

    def ler_evento(linha: str) -> str | None:
        l = linha.strip()
        if not l:
            return None
        try:
            ev = json.loads(l)
        except ValueError:
            # Aviso solto da CLI fica visível no log — menos este, que ela
            # imprime sempre que a entrada não é um terminal, e não diz nada.
            if l.startswith("Reading additional input from stdin"):
                return None
            return linha
        tipo = ev.get("type")
        item = ev.get("item") or {}
        it = item.get("type")
        if tipo == "thread.started":
            estado["sessao"] = ev.get("thread_id") or estado["sessao"]
        elif tipo == "item.started" and it == "command_execution":
            cmd = " ".join(str(item.get('command', '')).split())
            return f"\n\n> ⚙️ **Executando** `{cmd[:90]}`...\n\n"
        elif tipo == "item.completed":
            if it == "agent_message" and item.get("text"):
                estado["mensagens"].append(item["text"])
                return item["text"] + "\n\n"
            if it == "command_execution":
                cmd = " ".join(str(item.get('command', '')).split())
                cod = item.get("exit_code")
                sinal = "✅" if cod in (0, None) else "❌"
                return f"> {sinal} `{cmd[:90]}`" + \
                       (f" (código {cod})" if cod not in (0, None) else "") + "\n\n"
            if it == "file_change":
                caminhos = ", ".join(str(c.get("path", "")) for c in item.get("changes") or [])
                return f"> ✏️ alterou {caminhos[:160]}\n\n"
            if it == "web_search":
                q = " ".join(str(item.get('query', '')).split())
                return f"> 🔎 buscou `{q[:90]}`\n\n"
            if it == "error":
                estado["erro"] = str(item.get("message") or "")
        elif tipo == "turn.completed":
            estado["uso"] = ev.get("usage")
        elif tipo == "turn.failed":
            estado["erro"] = str((ev.get("error") or {}).get("message") or "")
        elif tipo == "error":
            estado["erro"] = str(ev.get("message") or "")
        return None

    r = rodar(job["id"], argv, shell=False, cwd=cwd, exigir_sucesso=False,
              codificacao="utf-8", ambiente=ambiente, transformar_linha=ler_evento)
    # A resposta é a última mensagem: as anteriores são o Codex narrando o que
    # vai fazer, e já apareceram no log ao vivo.
    resposta = (estado["mensagens"][-1] if estado["mensagens"] else "").strip()
    erro = estado["erro"] or ("" if resposta else (r.get("saida_completa") or "").strip())

    if not resposta:
        baixo = erro.lower()
        sem_login = any(t in baixo for t in ("not logged in", "401", "unauthorized",
                                              "codex login", "please log in",
                                              "could not be refreshed", "sign in again"))
        sem_cota = bool(RECUSA_DE_COTA.search(erro) or ERRO_DE_API.search(erro)
                        or "usage limit" in baixo)
        if sem_login:
            marcar_codex_login(True)
        if sem_login or sem_cota:
            motivo = ("o Codex precisa de login nesta máquina (use \"Entrar com código\" no quadro)"
                      if sem_login
                      else "o Codex bateu no limite da assinatura")
            res = alternar_ia(job, "codex", motivo)
            if res:
                return res
            raise ErroDeIA(motivo + ": " + " ".join(erro.split())[:200],
                           "login" if sem_login else "cota")
        raise RuntimeError("o Codex não devolveu resposta (código " +
                           str(r["returncode"]) + "): " + " ".join(erro.split())[:300])

    marcar_codex_login(False)
    return {"resposta": resposta, "sessao": estado["sessao"], "cwd": cwd or "",
            "modelo": modelo or "(padrão do Codex)", "cli": "codex",
            "continuou": bool(sessao), "returncode": r["returncode"],
            "linhas": r["linhas"], "uso": estado["uso"], "ia": "codex"}


def executar_codex_login(job: dict) -> dict:
    """Refaz o login do Codex por código de dispositivo: logout e
    `login --device-auth`. O endereço e o código aparecem no log do job; o
    job espera até o Caio confirmar no navegador (o código vale minutos).

    Existe como tipo próprio, e não como job shell, para usar o mesmo binário
    que o agente usa: no Windows o `codex` pode nem estar no PATH, e o
    `codex.cmd` do npm interrompido fica sem o executável nativo.
    """
    base = _localizar_codex()
    if not base:
        raise RuntimeError("codex não encontrado nesta máquina")
    rodar(job["id"], base + ["logout"], shell=False, exigir_sucesso=False,
          codificacao="utf-8")
    r = rodar(job["id"], base + ["login", "--device-auth"], shell=False,
              exigir_sucesso=True, codificacao="utf-8")
    marcar_codex_login(not _codex_logado(base))
    return {"logado": INVENTARIO["codex_logado"], "linhas": r["linhas"]}


EXECUTORES = {"shell": executar_shell, "claude": executar_claude,
              "gemini": executar_gemini, "codex": executar_codex,
              "codex_login": executar_codex_login}


# ------------------------------------------------------------------ laço
def rodar_job(job: dict) -> None:
    job_id = job["id"]
    with _lock:
        _estado["job_atual"] = job_id
        _estado["cancelar"] = False
    log("job", job_id, "(" + job["tipo"] + ") tentativa", job.get("tentativa"))
    try:
        executor = EXECUTORES.get(job["tipo"])
        if executor is None:
            raise ValueError("tipo de job não suportado: " + job["tipo"])
        resultado = executor(job)
        chamar("POST", "/api/trabalho/" + str(job_id) + "/concluir",
               {"agente": NOME, "resultado": resultado})
        log("job", job_id, "concluído")
    except Exception as e:
        log("job", job_id, "falhou:", e)
        corpo = {"agente": NOME, "erro": str(e)}
        if isinstance(e, ErroDeIA):
            corpo["motivo"] = e.motivo
        try:
            chamar("POST", "/api/trabalho/" + str(job_id) + "/falhar", corpo)
        except Exception as e2:
            # Broker fora do ar: o zelador devolve o job depois de 90s sem ping.
            log("não consegui reportar a falha:", e2)
    finally:
        with _lock:
            _estado["job_atual"] = None
            _estado["cancelar"] = False


def instancia_unica() -> socket.socket:
    """Impede dois agentes com o mesmo nome na mesma máquina.

    Duplicatas são silenciosamente destrutivas: as duas reivindicam jobs, mas o
    broker indexa agentes por hostname, então `job_atual` de uma sobrescreve o
    da outra e o zelador pode devolver à fila um job que está rodando.

    A trava é um socket em porta derivada do nome: se já existe instância, o
    bind falha. Diferente de arquivo de lock, não deixa resíduo se o processo
    morrer de forma abrupta.
    """
    porta = 49200 + (sum(NOME.encode()) * 7919) % 300
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", porta))
        s.listen(1)
    except OSError:
        log("já existe um agente '" + NOME + "' nesta máquina (porta " +
            str(porta) + "); saindo")
        raise SystemExit(0)
    return s


def _exigir_https(url: str) -> None:
    """O token do agente dá acesso a esta máquina: nunca trafega sem criptografia,
    a não ser para um broker local (desenvolvimento ou rede da casa)."""
    p = urllib.parse.urlparse(url)
    local = p.hostname in ("localhost", "127.0.0.1", "::1")
    if p.scheme != "https" and not local and os.environ.get("AGENTE_PERMITIR_HTTP") != "1":
        raise SystemExit("Recusado: o endereço do Jarvis precisa começar com https:// (" + url + ")")


def parear(codigo: str, broker: str, nome: str) -> None:
    """Troca o código mostrado no painel por um token, e grava a configuração."""
    global BROKER, TOKEN, NOME, TAGS
    _exigir_https(broker)
    BROKER, TOKEN, NOME = broker.rstrip("/"), "", nome
    try:
        _, r = chamar("POST", "/api/agente/parear", {"codigo": codigo, "hostname": nome})
    except urllib.error.HTTPError as e:
        try:
            detalhe = json.loads(e.read()).get("detail")
        except Exception:
            detalhe = str(e)
        raise SystemExit("Não deu para conectar: " + str(detalhe))
    TOKEN, NOME = r["token"], r.get("hostname") or nome
    TAGS = ",".join(dict.fromkeys([t for t in TAGS.split(",") if t and t != socket.gethostname()] + [NOME]))
    CONFIG_ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_ARQUIVO.write_text(json.dumps({"broker": BROKER, "token": TOKEN, "nome": NOME}), encoding="utf-8")
    try:
        os.chmod(CONFIG_ARQUIVO, 0o600)   # o token dá acesso a esta máquina
    except OSError:
        pass
    log("conectado ao Jarvis como", NOME, "| configuração salva em", CONFIG_ARQUIVO)


def _argumentos() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Agente do MiniJarvis")
    ap.add_argument("--parear", metavar="CODIGO", help="código mostrado no painel em 'Conectar máquina'")
    ap.add_argument("--broker", metavar="URL", help="endereço do Jarvis (ex: https://seu-jarvis.vercel.app)")
    ap.add_argument("--nome", help="nome desta máquina no painel (padrão: hostname)")
    ap.add_argument("--so-parear", action="store_true", help="só conecta e sai (usado pelo instalador)")
    a = ap.parse_args()
    if a.parear:
        parear(a.parear, a.broker or BROKER, a.nome or NOME)
        if a.so_parear:
            raise SystemExit(0)
    if TOKEN:
        _exigir_https(BROKER)


def main() -> None:
    _argumentos()
    trava = instancia_unica()   # precisa continuar viva enquanto o agente roda
    INVENTARIO.update(inventario())
    # Puxa já na partida: o estado que vai no primeiro heartbeat é o real, não
    # o do último boot. Se o clone não existir, só registra o estado.
    if puxar_cerebro(os.environ.get("CLAUDE_CWD") or None) is None:
        atualizar_cerebro()
    log("agente", NOME, "->", BROKER, "| tags:", TAGS or "(nenhuma)",
        "| trava:", trava.getsockname()[1])
    log("  ", INVENTARIO.get("cpu"), "|", INVENTARIO.get("nucleos"), "nucleos |",
        INVENTARIO.get("ram_total_mb"), "MB |", INVENTARIO.get("so"))
    threading.Thread(target=batedor, daemon=True).start()
    threading.Thread(target=sincronizador_cerebro, daemon=True).start()
    threading.Thread(target=enviar_cerebro_nuvem, daemon=True).start()
    threading.Thread(target=reavaliador_ias, daemon=True).start()
    espera = POLL_OCIOSO_S
    while True:
        try:
            # Espera longa: o broker segura o pedido até aparecer job para
            # esta máquina (ou 20 s), e responde na hora em que ele chega.
            # Antes o agente perguntava de 5 em 5 s, e todo pedido do chat
            # esperava até 5 s só para ser notado.
            t0 = time.monotonic()
            status, job = chamar(
                "GET", "/api/trabalho?agente=" + urllib.parse.quote(NOME) +
                "&tags=" + urllib.parse.quote(TAGS) + "&espera=" + str(ESPERA_S),
                timeout=ESPERA_S + 30)
            espera = POLL_OCIOSO_S          # broker respondeu: zera o backoff
            if status == 204 or not job:
                # Broker anterior ignora `espera` e responde na hora: aí o
                # agente volta ao ritmo antigo em vez de martelar o Pi.
                if time.monotonic() - t0 < 2:
                    time.sleep(POLL_OCIOSO_S)
                continue
            rodar_job(job)
        except KeyboardInterrupt:
            log("saindo")
            return
        except Exception as e:
            # Broker offline é estado normal: backoff até 60s, sem morrer.
            log("sem broker (" + str(e) + "); nova tentativa em", espera, "s")
            time.sleep(espera)
            espera = min(espera * 2, 60)


if __name__ == "__main__":
    sys.exit(main())
