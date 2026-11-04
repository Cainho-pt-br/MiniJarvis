# Instala o agente do orquestrador numa maquina Windows do tailnet.
# Nao precisa de administrador: instala o Python no escopo do usuario e usa
# tarefa agendada em vez de servico do Windows.
#
# Uso recomendado (PowerShell normal, na maquina que vai virar agente):
#
#   & ([scriptblock]::Create((irm https://raspberrypi.taild7492b.ts.net/instalar-agente-windows.ps1))) `
#       -Tags "cpu-leve" -Nome "celeron"
#
# Essa forma executa em memoria e nao esbarra na ExecutionPolicy. Baixar o .ps1
# e chamar .\instalar-agente-windows.ps1 falha com PSSecurityException na maioria
# das maquinas Windows, porque o padrao (Restricted/RemoteSigned) recusa script
# de arquivo vindo da internet.
#
# RODE NUM POWERSHELL DE VERDADE (menu Iniciar), nao no terminal embutido de um
# app empacotado. Veja a nota sobre redirecionamento MSIX em "0/4" abaixo.
#
# Alternativa, se preferir o arquivo em disco:
#   powershell -ExecutionPolicy Bypass -File .\inst.ps1 -Tags "cpu-leve" -Nome "celeron"

param(
  [string]$BrokerUrl = "https://raspberrypi.taild7492b.ts.net",
  [string]$Tags      = "cpu-leve",
  [string]$Nome      = $env:COMPUTERNAME.ToLower(),
  # Pasta onde os jobs tipo "claude" rodam. É o que faz o Claude Code carregar
  # o CLAUDE.md do projeto e já chegar com o contexto do parque.
  [string]$ClaudeCwd = "",
  # Modelo padrão para os jobs `claude` desta máquina (vazio = padrão da CLI).
  [string]$Modelo = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# O destino NAO fica mais em %LOCALAPPDATA%. Apps empacotados (MSIX) tem
# AppData redirecionado para o armazenamento privado do pacote, e uma
# instalacao ali e invisivel para o resto do Windows. %USERPROFILE% nao sofre
# esse redirecionamento. Veja "0/4".
$destino = "$env:USERPROFILE\Orquestrador"
$antigo  = "$env:LOCALAPPDATA\Orquestrador"
New-Item -ItemType Directory -Force -Path $destino | Out-Null

Write-Host "== 0/4 checando redirecionamento de arquivos"
# Se este script rodar dentro de um app empacotado (MSIX/AppContainer), o
# Windows redireciona escritas em AppData e em HKCU para
# %LOCALAPPDATA%\Packages\<pacote>\LocalCache\... Os arquivos existem para quem
# escreveu e NAO EXISTEM para o Agendador de Tarefas nem para o logon: o
# autostart aponta para um caminho fantasma e o agente nunca sobe no boot.
# Isso aconteceu no `boss` e custou duas reinicializacoes para ser achado, com
# a tarefa devolvendo LastTaskResult=2 (ERROR_FILE_NOT_FOUND) sem explicacao.
$empacotado = $false
try {
  $sonda = Join-Path $env:LOCALAPPDATA "orq-sonda-redirecionamento"
  New-Item -ItemType Directory -Force -Path $sonda | Out-Null
  $alvo = (Get-Item $sonda -Force).Target
  Remove-Item $sonda -Recurse -Force -ErrorAction SilentlyContinue
  if ($alvo -and ($alvo -like "*\Packages\*")) {
    $empacotado = $true
    Write-Host "   AVISO: este PowerShell roda dentro de um app empacotado."
    Write-Host "   AppData e HKCU estao redirecionados para: $alvo"
    Write-Host "   O agente vai para $destino (caminho real) e o autostart fica"
    Write-Host "   so na tarefa agendada. A chave Run seria gravada no registro"
    Write-Host "   virtual do pacote e nao valeria nada no logon."
  } else {
    Write-Host "   ok, sem redirecionamento"
  }
} catch {
  Write-Host "   nao consegui checar ($($_.Exception.Message)); seguindo"
}

Write-Host "== 1/4 Python"

function Achar-Pythonw {
  # 1) no PATH -- mas o atalho da Microsoft Store tambem se chama python.exe e
  #    nao executa nada, entao o teste de verdade e mandar imprimir a versao.
  $c = Get-Command python -ErrorAction SilentlyContinue
  if ($c -and $c.Source -notlike "*WindowsApps*") {
    try { & $c.Source --version 2>$null | Out-Null
          if ($LASTEXITCODE -eq 0) {
            $w = Join-Path (Split-Path $c.Source) "pythonw.exe"
            if (Test-Path $w) { return $w }
          } } catch {}
  }
  # 2) instalacoes reais fora do PATH (caso comum: o atalho da Store sombreia)
  foreach ($raiz in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python*", "C:\Python*")) {
    foreach ($d in (Get-ChildItem $raiz -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
      $w = Join-Path $d.FullName "pythonw.exe"
      if (Test-Path $w) { return $w }
    }
  }
  return $null
}

$pythonw = Achar-Pythonw
if (-not $pythonw) {
  Write-Host "   instalando Python pelo winget (escopo do usuario)..."
  winget install --id Python.Python.3.12 --scope user --silent `
    --accept-package-agreements --accept-source-agreements
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "Machine")
  $pythonw = Achar-Pythonw
}
if (-not $pythonw) {
  throw "Python nao encontrado nem instalado. Instale de python.org e rode de novo."
}
Write-Host "   usando: $pythonw"

Write-Host "== 2/4 baixando o agente do broker"
Invoke-WebRequest -Uri "$BrokerUrl/agente.py" -OutFile "$destino\agente.py" -UseBasicParsing
Write-Host "   $((Get-Item "$destino\agente.py").Length) bytes"

Write-Host "== 3/4 supervisor"
# Supervisor em Python puro, sob pythonw (que nao abre console).
#
# A versao anterior era wscript -> VBScript -> PowerShell -> Python. Essa
# corrente falhou em logons reais sem deixar erro, o Windows 11 esta
# descontinuando o VBScript, e cada elo a mais e uma chance de falhar em
# silencio. Aqui sobra um elo so.
$configJson = @{
  BROKER_URL   = $BrokerUrl
  TAGS         = $Tags
  AGENTE_NOME  = $Nome
  CLAUDE_CWD   = $ClaudeCwd
  MODELO_CLAUDE = $Modelo
} | ConvertTo-Json
[System.IO.File]::WriteAllText("$destino\config.json", $configJson, [System.Text.UTF8Encoding]::new($false))

$supervisorPy = @'
"""Supervisor do agente. Roda sob pythonw: sem console, sem VBScript.

Mantem exatamente um agente de pe, reiniciando se ele cair, e registra a
propria partida — sem esse registro nao da para distinguir "o agente morreu"
de "o autostart nunca disparou".
"""
import json, os, socket, subprocess, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG  = BASE / "supervisor.log"

def registra(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def trava_unica(nome):
    """Impede dois supervisores. A tarefa agendada tenta subir de 5 em 5
    minutos como auto-cura; sem esta trava, cada tentativa criaria mais um
    supervisor, e eles ficariam brigando pelo agente indefinidamente."""
    porta = 49500 + (sum(nome.encode()) * 7919) % 300
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", porta))
        s.listen(1)
    except OSError:
        raise SystemExit(0)      # ja tem supervisor de pe: sai quieto
    return s

def main():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    _trava = trava_unica(str(cfg.get("AGENTE_NOME", "agente")))
    env = dict(os.environ)
    for k, v in cfg.items():
        if v:
            env[k] = str(v)
    registra("supervisor iniciou (pid %d) para %s" % (os.getpid(), cfg.get("AGENTE_NOME")))

    python = sys.executable.replace("pythonw.exe", "python.exe")
    while True:
        inicio = time.time()
        with open(BASE / "agente.log", "w", encoding="utf-8") as saida, \
             open(BASE / "agente.err", "w", encoding="utf-8") as erro:
            p = subprocess.Popen([python, str(BASE / "agente.py")], env=env,
                                 stdout=saida, stderr=erro,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            p.wait()
        registra("agente encerrou apos %ds (codigo %s); reiniciando"
                 % (int(time.time() - inicio), p.returncode))
        time.sleep(15)

if __name__ == "__main__":
    main()
'@
[System.IO.File]::WriteAllText("$destino\supervisor.py", $supervisorPy, [System.Text.UTF8Encoding]::new($false))
Write-Host "   supervisor.py + config.json gravados em $destino"

Write-Host "== 4/4 autostart"
# Tarefa agendada: o mecanismo que o Windows oferece para isto, e o unico que
# tem AUTO-CURA. Dois gatilhos: no logon e a cada 5 minutos. Se o logon falhar,
# em ate 5 minutos o agente sobe assim mesmo. A trava de instancia unica do
# supervisor faz as repeticoes serem inofensivas.
#
# A ORDEM IMPORTA: o autostart novo e criado e CONFERIDO antes de remover o
# antigo. Numa atualizacao remota (o instalador roda como job na propria
# maquina), apagar o .vbs da pasta Inicializar antes de ter a tarefa de pe
# deixaria a maquina sem nenhum autostart -- e sem ninguem por perto.
$tarefa = "Orquestrador agente"
$tarefaOk = $false
try {
  $acao = New-ScheduledTaskAction -Execute $pythonw `
            -Argument "`"$destino\supervisor.py`"" -WorkingDirectory $destino
  $g1 = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $g2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
          -RepetitionInterval (New-TimeSpan -Minutes 5)
  $cfgT = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName $tarefa -Action $acao -Trigger $g1,$g2 `
    -Settings $cfgT -Description "Mantem o agente do orquestrador de pe" `
    -Force -ErrorAction Stop | Out-Null
  # Conferir, nao confiar: registrar sem erro e uma coisa, existir e outra.
  $tarefaOk = [bool](Get-ScheduledTask -TaskName $tarefa -ErrorAction SilentlyContinue)
  Write-Host "   tarefa agendada '$tarefa' (logon + a cada 5 min): $tarefaOk"
} catch {
  Write-Host "   AVISO: nao consegui criar a tarefa agendada ($($_.Exception.Message))."
  Write-Host "   Se ja existir uma tarefa com esse nome criada como administrador,"
  Write-Host "   remova-a num PowerShell elevado e rode este instalador de novo:"
  Write-Host "     Unregister-ScheduledTask -TaskName '$tarefa' -Confirm:`$false"
}

# Chave Run: util quando o script roda num PowerShell de verdade. Dentro de um
# app empacotado ela iria para o registro virtual do pacote, invisivel no
# logon, entao nesse caso nem tentamos -- melhor nao ter do que ter mentindo.
$runOk = $false
if ($empacotado) {
  Write-Host "   chave Run: pulada (registro virtualizado)"
} else {
  New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name "OrquestradorAgente" -PropertyType String -Force `
    -Value ("`"$pythonw`" `"$destino\supervisor.py`"") | Out-Null
  $runOk = $true
  Write-Host "   chave Run gravada"
}

# So agora a corrente antiga sai de cena. Ela (.vbs -> supervisor.ps1)
# convivendo com o supervisor.py fazia os dois brigarem pela trava de
# instancia unica: o agente reiniciava de 15 em 15 segundos e nunca subia.
if ($tarefaOk -or $runOk) {
  foreach ($lixo in @("agente.vbs", "supervisor.ps1", "reiniciar.ps1")) {
    Remove-Item (Join-Path $destino $lixo) -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $antigo  $lixo) -Force -ErrorAction SilentlyContinue
  }
  $startup = [Environment]::GetFolderPath("Startup")
  Remove-Item "$startup\Orquestrador - agente.vbs" -Force -ErrorAction SilentlyContinue
  Write-Host "   lancador antigo removido"
} else {
  Write-Host "   AVISO: nenhum autostart novo confirmado; o lancador antigo foi"
  Write-Host "   MANTIDO de proposito, para a maquina nao ficar sem subir no boot."
}

# Um agente já rodando não cede lugar sozinho: o novo encontra a trava de
# instância única e sai, mantendo a configuração antiga. Por isso o início
# passa por um script separado que primeiro derruba o que estiver rodando —
# separado porque, numa reinstalação disparada como job, o agente que morre é
# quem está executando este próprio script.
$reinicio = @"
# 30s: numa reinstalação disparada como job, quem morre aqui é o agente que
# está executando este script. A espera dá tempo do job terminar e ser
# reportado — senão ele fica órfão em 'executando'.
Start-Sleep -Seconds 30
# Filtro pelo caminho do script, nunca pela palavra 'Orquestrador' solta: ela
# casa tambem com o proprio PowerShell que roda este arquivo, e o script
# acabava se matando (ja aconteceu aqui).
`$meu = `$PID
`$pai = (Get-CimInstance Win32_Process -Filter "ProcessId=`$meu").ParentProcessId
for (`$i = 0; `$i -lt 5; `$i++) {
  `$vivos = @(Get-CimInstance Win32_Process |
              Where-Object { `$_.ProcessId -ne `$meu -and `$_.ProcessId -ne `$pai -and
                             `$_.Name -match '^pythonw?\.exe`$' -and
                             (`$_.CommandLine -match 'agente\.py' -or
                              `$_.CommandLine -match 'supervisor\.py') })
  if (`$vivos.Count -eq 0) { break }
  `$vivos | ForEach-Object { Stop-Process -Id `$_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 3
}
try { Start-ScheduledTask -TaskName "$tarefa" -ErrorAction Stop }
catch { Start-Process "$pythonw" -ArgumentList '"$destino\supervisor.py"' -WindowStyle Hidden }
"@
$scriptReinicio = "$destino\iniciar-agora.ps1"
Set-Content -Path $scriptReinicio -Value $reinicio -Encoding UTF8
Start-Process powershell -WindowStyle Hidden `
  -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $scriptReinicio

Write-Host ""
Write-Host "Pronto. O agente sobe em ate ~40s (ou no proximo logon)."
Write-Host "Conferir depois com:"
Write-Host "  (Invoke-RestMethod '$BrokerUrl/api/agentes') | Where-Object hostname -eq '$Nome'"
