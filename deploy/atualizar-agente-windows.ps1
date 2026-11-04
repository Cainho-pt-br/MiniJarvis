# Atualiza o agente de uma máquina Windows para a versão que o broker serve.
#
# Feito para rodar COMO UM JOB do próprio orquestrador, já que máquinas Windows
# não aceitam Tailscale SSH e não há como empurrar arquivo para elas:
#   powershell -NoProfile -Command "& ([scriptblock]::Create((irm <broker>/atualizar-agente-windows.ps1)))"
#
# A troca só acontece depois de o arquivo novo compilar, e o reinício é
# agendado em processo separado — senão o agente mataria a si mesmo no meio do
# job e o broker o reenfileiraria em looping.
#
# Quem reinicia o agente é o supervisor.py (ver instalar-agente-windows.ps1):
# basta derrubar o agente.py que ele sobe outro, já com o arquivo novo, em 15s.
# Não existe mais agente.vbs nem pasta em %LOCALAPPDATA% — o caminho real é
# %USERPROFILE%\Orquestrador (ver a armadilha do MSIX no CLAUDE.md).

param([string]$BrokerUrl = "https://raspberrypi.taild7492b.ts.net")

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$destino = "$env:USERPROFILE\Orquestrador"
if (-not (Test-Path "$destino\agente.py") -and (Test-Path "$env:LOCALAPPDATA\Orquestrador\agente.py")) {
  # instalação antiga, nunca migrada: atualiza onde o agente de fato mora
  $destino = "$env:LOCALAPPDATA\Orquestrador"
  Write-Host "!! agente ainda em $destino; reinstale pelo instalar-agente-windows.ps1 quando puder"
}
$atual   = "$destino\agente.py"
$novo    = "$destino\agente-novo.py"
$backup  = "$destino\agente-anterior.py"
if (-not (Test-Path $atual)) { throw "agente não encontrado em $atual; rode o instalador" }

Write-Host "== baixando do broker"
Invoke-WebRequest -Uri "$BrokerUrl/agente.py" -OutFile $novo -UseBasicParsing
$tam = (Get-Item $novo).Length
Write-Host "   $tam bytes"
if ($tam -lt 5000) { throw "arquivo suspeito de tão pequeno ($tam bytes); abortando" }

Write-Host "== verificando antes de trocar"
$py = (Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
       Where-Object { $_.CommandLine -like "*Orquestrador*" } |
       Select-Object -First 1).CommandLine
# o executável do agente em execução, para compilar com o mesmo Python
$exe = if ($py -match '^"?([^"]+python)w?\.exe"?') { $matches[1] + ".exe" } else { "python" }
& $exe -m py_compile $novo
if ($LASTEXITCODE -ne 0) { throw "o agente novo não compila; nada foi trocado" }

# Arquivo igual NÃO significa nada a fazer: o processo em execução pode ser
# anterior à troca, com o código velho carregado em memória. Só o reinício
# resolve isso, então ele acontece sempre.
if ((Get-FileHash $atual).Hash -eq (Get-FileHash $novo).Hash) {
  Write-Host "== arquivo já estava atualizado; reiniciando mesmo assim"
  Remove-Item $novo -Force
} else {
  Write-Host "== trocando (backup em agente-anterior.py)"
  Copy-Item $atual $backup -Force
  Move-Item $novo $atual -Force
}

Write-Host "== agendando reinício em 12s, fora deste processo"
# Derruba só o agente.py: o supervisor.py fica de pé e sobe outro em 15s. Se
# não houver supervisor (instalação antiga ou ele morreu), dispara a tarefa
# agendada, que é o único autostart do conjunto.
$cmd = @'
Start-Sleep -Seconds 12
Get-CimInstance Win32_Process -Filter "Name='python.exe' or Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -like '*Orquestrador*agente.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
$sup = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*Orquestrador*supervisor.py*' }
if (-not $sup) { Start-ScheduledTask -TaskName 'Orquestrador agente' -ErrorAction SilentlyContinue }
'@
# Em arquivo, não em -Command: bloco multilinha passado como argumento único de
# -Command é mal interpretado, e o resultado era o kill nunca rodar.
$helper = "$destino\reiniciar.ps1"
Set-Content -Path $helper -Value $cmd -Encoding UTF8
Start-Process powershell -WindowStyle Hidden `
  -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $helper
Write-Host "OK: agente trocado; o supervisor o sobe de novo em instantes"
