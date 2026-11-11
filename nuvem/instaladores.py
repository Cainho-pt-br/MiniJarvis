"""Instaladores de um comando só, servidos pelo próprio site.

A pessoa copia uma linha do painel ("Conectar máquina") e cola no terminal:

    Windows:     irm "https://.../instalar.ps1?c=CODIGO" | iex
    Mac/Linux:   curl -fsSL "https://.../instalar.sh?c=CODIGO" | sh

O script já vem com o endereço do Jarvis e o código de pareamento embutidos.
Ele confere (e no Windows instala) o Python, baixa o agente para uma pasta
do usuário, faz o pareamento e deixa o agente ligando sozinho com o
computador (tarefa agendada no Windows, launchd no Mac, systemd ou cron no
Linux). Nada pede senha de administrador.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()
_RE_CODIGO = re.compile(r"^[A-Z2-9]{6}$")


def _base(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        base = base.replace("http://", "https://", 1)
    return base


def _texto(conteudo: str) -> PlainTextResponse:
    return PlainTextResponse(conteudo, media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store"})


SH = r'''#!/bin/sh
# Instalador do agente do Jarvis (Mac e Linux). Não precisa de sudo.
set -eu
JARVIS="__BASE__"
CODIGO="__CODIGO__"
DIR="$HOME/.minijarvis"
diga() { printf '%s\n' "$*"; }

diga ""
diga "  Jarvis · conectando este computador"
diga ""

if [ -z "$CODIGO" ]; then
  diga "Este comando está sem o código. Gere outro no painel, em 'Conectar máquina'."
  exit 1
fi

# 1. Python 3.9 ou mais novo
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY=$(command -v "$c"); break
  fi
done
if [ -z "$PY" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    diga "• Este Mac precisa das Ferramentas de Linha de Comando da Apple (elas trazem o Python)."
    diga "  Vai abrir uma janela: clique em 'Instalar', espere terminar e cole este comando de novo."
    xcode-select --install >/dev/null 2>&1 || true
  else
    diga "• Falta o Python 3 neste computador. Instale e cole este comando de novo:"
    diga "    Ubuntu/Debian/Mint:  sudo apt install -y python3"
    diga "    Fedora:              sudo dnf install -y python3"
    diga "    Arch:                sudo pacman -S python"
  fi
  exit 1
fi
diga "✓ Python encontrado ($("$PY" --version 2>&1))"

# 2. Baixa o agente
mkdir -p "$DIR"
curl -fsSL "$JARVIS/agente.py" -o "$DIR/agente.py.novo"
mv "$DIR/agente.py.novo" "$DIR/agente.py"
diga "✓ Agente baixado em $DIR"

# 3. Conecta à sua conta
"$PY" "$DIR/agente.py" --parear "$CODIGO" --broker "$JARVIS" --so-parear
diga "✓ Computador conectado à sua conta"

# 4. Liga sozinho com o computador
ARRANQUE=""
if [ "$(uname -s)" = "Darwin" ]; then
  PL="$HOME/Library/LaunchAgents/com.minijarvis.agente.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.minijarvis.agente</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/agente.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/agente.log</string>
  <key>StandardErrorPath</key><string>$DIR/agente.log</string>
</dict></plist>
PLIST
  launchctl unload "$PL" >/dev/null 2>&1 || true
  if launchctl load -w "$PL" >/dev/null 2>&1; then
    ARRANQUE="launchd"
  else
    pkill -f "$DIR/agente.py" >/dev/null 2>&1 || true
    ( cd "$DIR" && nohup "$PY" "$DIR/agente.py" >> "$DIR/agente.log" 2>&1 & )
  fi
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UD="$HOME/.config/systemd/user"
  mkdir -p "$UD"
  cat > "$UD/minijarvis-agente.service" <<UNIT
[Unit]
Description=Agente do Jarvis
After=network-online.target

[Service]
ExecStart="$PY" "$DIR/agente.py"
WorkingDirectory=$DIR
Environment=PATH=$PATH
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable minijarvis-agente.service >/dev/null 2>&1
  systemctl --user restart minijarvis-agente.service
  # Deixa rodar mesmo sem ninguém logado (quando o sistema permite sem sudo).
  loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
  ARRANQUE="systemd"
else
  # Sem systemd de usuário (contêiner, WSL antigo...): cron, se houver.
  LINHA="cd \"$DIR\" && \"$PY\" \"$DIR/agente.py\" >> \"$DIR/agente.log\" 2>&1"
  if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v '# minijarvis' || true
      echo "@reboot $LINHA # minijarvis"
      echo "*/5 * * * * $LINHA # minijarvis" ) | crontab - && ARRANQUE="cron"
  fi
  pkill -f "$DIR/agente.py" >/dev/null 2>&1 || true
  ( cd "$DIR" && nohup "$PY" "$DIR/agente.py" >> "$DIR/agente.log" 2>&1 & )
fi

diga ""
if [ -n "$ARRANQUE" ]; then
  diga "✓ Pronto! O agente já está rodando e liga sozinho com o computador ($ARRANQUE)."
else
  diga "✓ Pronto! O agente está rodando."
  diga "  Atenção: não achei como ligá-lo sozinho depois de reiniciar; rode este comando de novo se precisar."
fi
diga "  Volte ao navegador: este computador já aparece no seu parque."
diga ""
diga "  Para desconectar e remover: curl -fsSL \"$JARVIS/desinstalar.sh\" | sh"
diga ""
'''


PS1 = r'''# Instalador do agente do Jarvis (Windows). Não precisa ser administrador.
function Instalar-Jarvis {
  $ErrorActionPreference = 'Stop'
  $ProgressPreference = 'SilentlyContinue'
  $Jarvis = '__BASE__'
  $Codigo = '__CODIGO__'
  $Dir = Join-Path $env:LOCALAPPDATA 'MiniJarvis'

  Write-Host ''
  Write-Host '  Jarvis - conectando este computador' -ForegroundColor Cyan
  Write-Host ''
  if (-not $Codigo) { Write-Host 'Este comando esta sem o codigo. Gere outro no painel, em "Conectar maquina".' -ForegroundColor Yellow; return }

  # 1. Python (o "python" da Microsoft Store nao conta: e so um atalho para a loja)
  function Achar-Python {
    foreach ($nome in @('python', 'py')) {
      $c = Get-Command $nome -ErrorAction SilentlyContinue
      if ($c -and $c.Source -notlike '*WindowsApps*') {
        try {
          $exe = & $c.Source -c 'import sys; print(sys.executable) if sys.version_info >= (3, 9) else None' 2>$null
          if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe.Trim() }
        } catch {}
      }
    }
    foreach ($raiz in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
      foreach ($d in (Get-ChildItem $raiz -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
        $exe = Join-Path $d.FullName 'python.exe'
        if (Test-Path $exe) { return $exe }
      }
    }
    return $null
  }
  $py = Achar-Python
  if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      Write-Host '- Instalando o Python (leva 1 ou 2 minutos)...'
      winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
      $py = Achar-Python
    }
  }
  if (-not $py) {
    Write-Host 'Nao consegui instalar o Python sozinho.' -ForegroundColor Yellow
    Write-Host 'Baixe em https://www.python.org/downloads/ , marque "Add python.exe to PATH" ao instalar e cole este comando de novo.'
    return
  }
  $pyw = Join-Path (Split-Path $py) 'pythonw.exe'
  if (-not (Test-Path $pyw)) { $pyw = $py }
  Write-Host "OK Python encontrado ($py)"

  # 2. Baixa o agente
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  Invoke-WebRequest -Uri "$Jarvis/agente.py" -OutFile "$Dir\agente.py" -UseBasicParsing
  Write-Host "OK Agente baixado em $Dir"

  # 3. Conecta a sua conta
  & $py "$Dir\agente.py" --parear $Codigo --broker $Jarvis --so-parear
  if ($LASTEXITCODE -ne 0) { Write-Host 'Nao deu para conectar. Gere um codigo novo no painel e tente de novo.' -ForegroundColor Yellow; return }
  Write-Host 'OK Computador conectado a sua conta'

  # 4. Liga sozinho: tarefa agendada no logon e a cada 5 min (se ja estiver
  #    rodando, a copia nova sai sozinha: o agente so permite uma instancia).
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*MiniJarvis*agente.py*' -and $_.CommandLine -notlike '*--parear*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  $tarefaOk = $false
  try {
    $acao = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Dir\agente.py`"" -WorkingDirectory $Dir
    $g1 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $g2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $cfg = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
             -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName 'Jarvis agente' -Action $acao -Trigger $g1,$g2 -Settings $cfg `
      -Description 'Mantem o agente do Jarvis ligado' -Force -ErrorAction Stop | Out-Null
    $tarefaOk = $true
  } catch {
    # Sem permissao para tarefa agendada: usa a chave Run do usuario.
    Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'JarvisAgente' `
      -Value "`"$pyw`" `"$Dir\agente.py`""
  }
  Start-Process -FilePath $pyw -ArgumentList "`"$Dir\agente.py`"" -WorkingDirectory $Dir -WindowStyle Hidden

  Write-Host ''
  Write-Host 'Pronto! O agente ja esta rodando e liga sozinho com o computador.' -ForegroundColor Green
  Write-Host 'Volte ao navegador: este computador ja aparece no seu parque.'
  Write-Host ''
  Write-Host "Para desconectar e remover: irm `"$Jarvis/desinstalar.ps1`" | iex"
  Write-Host ''
}
Instalar-Jarvis
'''


DESINSTALAR_SH = r'''#!/bin/sh
# Remove o agente do Jarvis deste computador (Mac e Linux).
DIR="$HOME/.minijarvis"
if [ "$(uname -s)" = "Darwin" ]; then
  PL="$HOME/Library/LaunchAgents/com.minijarvis.agente.plist"
  launchctl unload "$PL" >/dev/null 2>&1; rm -f "$PL"
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now minijarvis-agente.service >/dev/null 2>&1
  rm -f "$HOME/.config/systemd/user/minijarvis-agente.service"
  systemctl --user daemon-reload >/dev/null 2>&1
fi
if command -v crontab >/dev/null 2>&1; then
  crontab -l 2>/dev/null | grep -v '# minijarvis' | crontab - 2>/dev/null
fi
pkill -f "$DIR/agente.py" >/dev/null 2>&1
rm -rf "$DIR"
echo "Agente do Jarvis removido. Remova também a máquina no painel (Parque > Desconectar)."
'''

DESINSTALAR_PS1 = r'''# Remove o agente do Jarvis deste computador (Windows).
function Remover-Jarvis {
  $Dir = Join-Path $env:LOCALAPPDATA 'MiniJarvis'
  Unregister-ScheduledTask -TaskName 'Jarvis agente' -Confirm:$false -ErrorAction SilentlyContinue
  Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'JarvisAgente' -ErrorAction SilentlyContinue
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*MiniJarvis*agente.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force (Join-Path $env:USERPROFILE '.minijarvis') -ErrorAction SilentlyContinue
  Write-Host 'Agente do Jarvis removido. Remova tambem a maquina no painel (Parque > Desconectar).'
}
Remover-Jarvis
'''


_TRADUCOES_INSTALADOR: dict[str, list[tuple[str, str]]] = {
    "en": [
        ("Jarvis · conectando este computador", "Jarvis · connecting this computer"),
        ("Este comando está sem o código. Gere outro no painel, em 'Conectar máquina'.",
         "This command is missing the pairing code. Generate a new one in the dashboard under 'Connect machine'."),
        ("• Este Mac precisa das Ferramentas de Linha de Comando da Apple (elas trazem o Python).",
         "• This Mac needs the Apple Command Line Tools (they include Python)."),
        ("  Vai abrir uma janela: clique em 'Instalar', espere terminar e cole este comando de novo.",
         "  A window will open: click 'Install', wait for it to finish, and paste this command again."),
        ("• Falta o Python 3 neste computador. Instale e cole este comando de novo:",
         "• Python 3 is missing on this computer. Install it and paste this command again:"),
        ("✓ Python encontrado", "✓ Python found"),
        ("✓ Agente baixado em", "✓ Agent downloaded to"),
        ("✓ Computador conectado à sua conta", "✓ Computer connected to your account"),
        ("✓ Pronto! O agente já está rodando e liga sozinho com o computador",
         "✓ Done! The agent is running and starts automatically with the computer"),
        ("✓ Pronto! O agente está rodando.", "✓ Done! The agent is running."),
        ("  Atenção: não achei como ligá-lo sozinho depois de reiniciar; rode este comando de novo se precisar.",
         "  Note: could not configure auto-start on reboot; run this command again if needed."),
        ("  Volte ao navegador: este computador já aparece no seu parque.",
         "  Return to your browser: this computer now appears in your park."),
        ("  Para desconectar e remover:", "  To disconnect and remove:"),
        ("Jarvis - conectando este computador", "Jarvis - connecting this computer"),
        ('Este comando esta sem o codigo. Gere outro no painel, em "Conectar maquina".',
         'This command is missing the code. Generate a new one in the dashboard under "Connect machine".'),
        ("- Instalando o Python (leva 1 ou 2 minutos)...", "- Installing Python (takes 1 or 2 minutes)..."),
        ("Nao consegui instalar o Python sozinho.", "Could not install Python automatically."),
        ('Baixe em https://www.python.org/downloads/ , marque "Add python.exe to PATH" ao instalar e cole este comando de novo.',
         'Download from https://www.python.org/downloads/ , check "Add python.exe to PATH" during setup, and paste this command again.'),
        ("OK Python encontrado", "OK Python found"),
        ("OK Agente baixado em", "OK Agent downloaded to"),
        ("Nao deu para conectar. Gere um codigo novo no painel e tente de novo.",
         "Could not connect. Generate a new code in the dashboard and try again."),
        ("OK Computador conectado a sua conta", "OK Computer connected to your account"),
        ("Pronto! O agente ja esta rodando e liga sozinho com o computador.",
         "Done! The agent is running and starts automatically with the computer."),
        ("Volte ao navegador: este computador ja aparece no seu parque.",
         "Return to your browser: this computer now appears in your park."),
        ("Para desconectar e remover:", "To disconnect and remove:"),
        ("Agente do Jarvis removido. Remova também a máquina no painel (Parque > Desconectar).",
         "Jarvis agent removed. Also remove the machine in the dashboard (Park > Disconnect)."),
        ("Agente do Jarvis removido. Remova tambem a maquina no painel (Parque > Desconectar).",
         "Jarvis agent removed. Also remove the machine in the dashboard (Park > Disconnect)."),
    ],
    "es": [
        ("Jarvis · conectando este computador", "Jarvis · conectando esta computadora"),
        ("Este comando está sem o código. Gere outro no painel, em 'Conectar máquina'.",
         "A este comando le falta el código. Genera otro en el panel, en 'Conectar equipo'."),
        ("• Este Mac precisa das Ferramentas de Linha de Comando da Apple (elas trazem o Python).",
         "• Esta Mac necesita las Herramientas de Línea de Comandos de Apple (incluyen Python)."),
        ("  Vai abrir uma janela: clique em 'Instalar', espere terminar e cole este comando de novo.",
         "  Se abrirá una ventana: haz clic en 'Instalar', espera a que termine y pega este comando de nuevo."),
        ("• Falta o Python 3 neste computador. Instale e cole este comando de novo:",
         "• Falta Python 3 en esta computadora. Instálalo y pega este comando de nuevo:"),
        ("✓ Python encontrado", "✓ Python encontrado"),
        ("✓ Agente baixado em", "✓ Agente descargado en"),
        ("✓ Computador conectado à sua conta", "✓ Computadora conectada a tu cuenta"),
        ("✓ Pronto! O agente já está rodando e liga sozinho com o computador",
         "✓ ¡Listo! El agente ya está funcionando y se inicia solo con la computadora"),
        ("✓ Pronto! O agente está rodando.", "✓ ¡Listo! El agente está funcionando."),
        ("  Atenção: não achei como ligá-lo sozinho depois de reiniciar; rode este comando de novo se precisar.",
         "  Atención: no pude configurar el inicio automático al reiniciar; ejecuta este comando de nuevo si lo necesitas."),
        ("  Volte ao navegador: este computador já aparece no seu parque.",
         "  Vuelve al navegador: esta computadora ya aparece en tu parque."),
        ("  Para desconectar e remover:", "  Para desconectar y eliminar:"),
        ("Jarvis - conectando este computador", "Jarvis - conectando esta computadora"),
        ('Este comando esta sem o codigo. Gere outro no painel, em "Conectar maquina".',
         'A este comando le falta el codigo. Genera otro en el panel, en "Conectar equipo".'),
        ("- Instalando o Python (leva 1 ou 2 minutos)...", "- Instalando Python (tarda 1 o 2 minutos)..."),
        ("Nao consegui instalar o Python sozinho.", "No pude instalar Python automáticamente."),
        ('Baixe em https://www.python.org/downloads/ , marque "Add python.exe to PATH" ao instalar e cole este comando de novo.',
         'Descárgalo en https://www.python.org/downloads/ , marca "Add python.exe to PATH" al instalar y pega este comando de nuevo.'),
        ("OK Python encontrado", "OK Python encontrado"),
        ("OK Agente baixado em", "OK Agente descargado en"),
        ("Nao deu para conectar. Gere um codigo novo no painel e tente de novo.",
         "No se pudo conectar. Genera un código nuevo en el panel e inténtalo de nuevo."),
        ("OK Computador conectado a sua conta", "OK Computadora conectada a tu cuenta"),
        ("Pronto! O agente ja esta rodando e liga sozinho com o computador.",
         "¡Listo! El agente ya está funcionando y se inicia solo con la computadora."),
        ("Volte ao navegador: este computador ja aparece no seu parque.",
         "Vuelve al navegador: esta computadora ya aparece en tu parque."),
        ("Para desconectar e remover:", "Para desconectar y eliminar:"),
        ("Agente do Jarvis removido. Remova também a máquina no painel (Parque > Desconectar).",
         "Agente de Jarvis eliminado. Elimina también el equipo en el panel (Parque > Desconectar)."),
        ("Agente do Jarvis removido. Remova tambem a maquina no painel (Parque > Desconectar).",
         "Agente de Jarvis eliminado. Elimina también el equipo en el panel (Parque > Desconectar)."),
    ],
}


def _traduzir_script(script: str, idioma: str | None) -> str:
    from .i18n import idioma_atual, normalizar_idioma

    lang = normalizar_idioma(idioma) or idioma_atual()
    for orig, trad in _TRADUCOES_INSTALADOR.get(lang, []):
        script = script.replace(orig, trad)
    return script


def _codigo(c: str) -> str:
    c = (c or "").strip().upper()
    return c if _RE_CODIGO.match(c) else ""


@router.get("/instalar.sh")
def instalar_sh(request: Request, c: str = "", lang: str = "") -> PlainTextResponse:
    from .i18n import resolver_idioma

    idioma = resolver_idioma(request, explicito=lang)
    s = SH.replace("__BASE__", _base(request)).replace("__CODIGO__", _codigo(c))
    return _texto(_traduzir_script(s, idioma))


@router.get("/instalar.ps1")
def instalar_ps1(request: Request, c: str = "", lang: str = "") -> PlainTextResponse:
    from .i18n import resolver_idioma

    idioma = resolver_idioma(request, explicito=lang)
    s = PS1.replace("__BASE__", _base(request)).replace("__CODIGO__", _codigo(c))
    return _texto(_traduzir_script(s, idioma))


@router.get("/desinstalar.sh")
def desinstalar_sh(request: Request, lang: str = "") -> PlainTextResponse:
    from .i18n import resolver_idioma

    idioma = resolver_idioma(request, explicito=lang)
    return _texto(_traduzir_script(DESINSTALAR_SH, idioma))


@router.get("/desinstalar.ps1")
def desinstalar_ps1(request: Request, lang: str = "") -> PlainTextResponse:
    from .i18n import resolver_idioma

    idioma = resolver_idioma(request, explicito=lang)
    return _texto(_traduzir_script(DESINSTALAR_PS1, idioma))


def comandos(base: str, codigo: str, idioma: str | None = None) -> dict[str, str]:
    from .i18n import PADRAO, idioma_atual, normalizar_idioma

    lang = normalizar_idioma(idioma) or idioma_atual()
    sufixo = f"&lang={lang}" if lang != PADRAO else ""
    return {
        "windows": f'irm "{base}/instalar.ps1?c={codigo}{sufixo}" | iex',
        "mac": f'curl -fsSL "{base}/instalar.sh?c={codigo}{sufixo}" | sh',
        "linux": f'curl -fsSL "{base}/instalar.sh?c={codigo}{sufixo}" | sh',
    }

