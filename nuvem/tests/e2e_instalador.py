"""Teste de ponta a ponta do instalador, rodado pelo GitHub Actions em Windows,
macOS e Linux de verdade (ver .github/workflows/testes.yml).

Com o servidor já de pé em JARVIS_URL (IA de mentira), ele:
cria uma conta, pede um código de pareamento, roda o comando de instalação
exatamente como a pessoa colaria no terminal, espera a máquina aparecer
online, roda cada ação pronta e confere que terminou bem, confere que um
comando livre é recusado no modo seguro, e por fim desinstala.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("JARVIS_URL", "http://127.0.0.1:8000")
jarra = http.cookiejar.CookieJar()
abridor = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jarra))


def api(metodo: str, caminho: str, corpo: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + caminho, method=metodo,
                                 data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with abridor.open(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def esperar(cond, segundos: int, oque: str):
    fim = time.time() + segundos
    while time.time() < fim:
        v = cond()
        if v:
            return v
        time.sleep(3)
    raise SystemExit(f"FALHOU: esperei {segundos}s por {oque}")


def rodar_comando(cmd: str) -> None:
    print("$", cmd, flush=True)
    if platform.system() == "Windows":
        # Windows PowerShell 5.1, o que vem em todo Windows, como a pessoa usaria.
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd])
    else:
        r = subprocess.run(["sh", "-c", cmd])
    if r.returncode != 0:
        raise SystemExit(f"FALHOU: comando saiu com {r.returncode}")


def main() -> None:
    st, u = api("POST", "/api/cadastro", {"nome": "CI", "email": "ci@exemplo.com", "senha": "senha-forte-1"})
    assert st == 200, u
    st, p = api("POST", "/api/parque/parear")
    assert st == 200, p
    so = {"Windows": "windows", "Darwin": "mac"}.get(platform.system(), "linux")
    rodar_comando(p["comandos"][so])

    maq = esperar(lambda: next((m for m in api("GET", "/api/parque")[1]["maquinas"]
                               if m["online"] and m["info"].get("so")), None),
                  90, "a máquina aparecer online")
    print("online:", maq["hostname"], maq["info"].get("so"), "modo", maq["modo"], flush=True)
    assert maq["modo"] == "seguro"

    st, r = api("POST", "/api/jobs", {"maquina": maq["hostname"], "tipo": "shell", "texto": "whoami"})
    assert st == 403, (st, r)

    st, acoes = api("GET", "/api/acoes")
    falhas = []
    for a in acoes:
        st, j = api("POST", "/api/jobs", {"maquina": maq["hostname"], "tipo": "acao", "texto": a["id"]})
        assert st == 200, j
        d = esperar(lambda: (lambda d: d if d["status"] in ("concluido", "falhou") else None)(
            api("GET", f"/api/jobs/{j['id']}")[1]), 120, f"a ação {a['id']}")
        print(f"\n== {a['id']}: {d['status']} ({d.get('mensagem')})\n{(d['log'] or '').strip()[:600]}", flush=True)
        if d["status"] != "concluido" or not (d["log"] or "").strip():
            falhas.append(a["id"])

    desinstalar = (f'irm "{BASE}/desinstalar.ps1" | iex' if so == "windows"
                   else f'curl -fsSL "{BASE}/desinstalar.sh" | sh')
    rodar_comando(desinstalar)
    if falhas:
        raise SystemExit(f"FALHOU: ações com problema: {falhas}")
    print("\nOK: instalador, agente e todas as ações funcionaram em", platform.platform())


if __name__ == "__main__":
    sys.exit(main())
