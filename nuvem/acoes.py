"""Ações prontas: o que uma máquina no modo seguro pode fazer.

Para quem não é técnico, deixar o Jarvis (ou alguém com a senha da conta)
rodar qualquer comando no computador é perigoso demais. No modo seguro, que
é o padrão de toda máquina nova, só rodam estas ações: cada uma tem o comando
escrito aqui, revisado, para Windows e para Mac/Linux. Nenhuma apaga arquivos
pessoais; a limpeza só mexe na pasta de temporários.
"""
from __future__ import annotations

import base64
from typing import Any

_PASTAS = {
    "Imagens": "jpg jpeg png gif webp heic bmp svg",
    "Documentos": "pdf doc docx xls xlsx ppt pptx txt csv odt ods rtf",
    "Videos": "mp4 mov avi mkv webm",
    "Musicas": "mp3 wav m4a flac ogg",
    "Compactados": "zip rar 7z tar gz",
    "Instaladores": "exe msi dmg pkg deb appimage",
}

_PS_MAPA = "; ".join(f"{p}=" + ",".join(f"'{e}'" for e in exts.split()) for p, exts in _PASTAS.items())
_SH_CASE = " ".join(f"{'|'.join(exts.split())}) p={p};;" for p, exts in _PASTAS.items())

ACOES: dict[str, dict[str, Any]] = {
    "espaco_disco": {
        "titulo": "Ver espaço em disco", "icone": "💽",
        "descricao": "Mostra quanto espaço livre cada disco tem.",
        "windows": "Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Used -ne $null } | ForEach-Object { "
                   "'{0}: {1:N1} GB livres de {2:N1} GB' -f $_.Name, ($_.Free/1GB), (($_.Used+$_.Free)/1GB) }",
        "unix": "df -h -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null || df -h",
    },
    "info_sistema": {
        "titulo": "Informações do computador", "icone": "🖥️",
        "descricao": "Mostra o sistema, o processador, a memória e há quanto tempo está ligado.",
        "windows": "$o=Get-CimInstance Win32_OperatingSystem; $c=Get-CimInstance Win32_Processor | Select-Object -First 1; "
                   "'Sistema: ' + $o.Caption; 'Processador: ' + $c.Name; "
                   "'Memoria: {0:N1} GB livres de {1:N1} GB' -f ($o.FreePhysicalMemory/1MB), ($o.TotalVisibleMemorySize/1MB); "
                   "'Ligado desde: ' + $o.LastBootUpTime",
        "unix": "uname -sr; (sw_vers 2>/dev/null || grep -E '^(NAME|VERSION)=' /etc/os-release 2>/dev/null); "
                "uptime; (free -h 2>/dev/null || vm_stat 2>/dev/null | head -5)",
    },
    "processos_pesados": {
        "titulo": "O que está pesando", "icone": "📈",
        "descricao": "Lista os 10 programas que mais usam memória agora. Não fecha nada.",
        "windows": "Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 10 | ForEach-Object { "
                   "'{0,-32} {1,8:N0} MB' -f $_.ProcessName, ($_.WorkingSet64/1MB) }",
        "unix": "echo '%MEM %CPU PROGRAMA'; ps -eo pmem=,pcpu=,comm= | sort -nr | head -10",
    },
    "maiores_downloads": {
        "titulo": "Maiores arquivos em Downloads", "icone": "📦",
        "descricao": "Mostra os 15 maiores arquivos da pasta Downloads, para você decidir o que apagar. Não apaga nada.",
        "windows": "$d=Join-Path $env:USERPROFILE 'Downloads'; Get-ChildItem $d -File -Recurse -ErrorAction SilentlyContinue | "
                   "Sort-Object Length -Descending | Select-Object -First 15 | ForEach-Object { "
                   "'{0,10:N1} MB  {1}' -f ($_.Length/1MB), $_.FullName.Substring($d.Length+1) }",
        "unix": "cd \"$HOME/Downloads\" 2>/dev/null || { echo 'Pasta Downloads não encontrada'; exit 0; }; "
                "find . -type f -exec du -k {} + 2>/dev/null | sort -nr | head -15 | "
                "awk '{t=$1; $1=\"\"; printf \"%8.1f MB %s\\n\", t/1024, $0}'",
    },
    "limpar_temporarios": {
        "titulo": "Limpar arquivos temporários", "icone": "🧹",
        "descricao": "Apaga arquivos temporários com mais de 7 dias. Seus documentos, fotos e downloads não são tocados.",
        "windows": "$t=$env:TEMP; $a=(Get-ChildItem $t -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; "
                   "Get-ChildItem $t -Force -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | "
                   "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue; "
                   "$b=(Get-ChildItem $t -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; "
                   "'Liberado: {0:N1} MB' -f (($a-$b)/1MB)",
        "unix": "T=\"${TMPDIR:-/tmp}\"; A=$(du -sk \"$T\" 2>/dev/null | cut -f1); "
                "find \"$T\" -mindepth 1 -maxdepth 1 -user \"$(id -u)\" -mtime +7 -exec rm -rf {} + 2>/dev/null; "
                "B=$(du -sk \"$T\" 2>/dev/null | cut -f1); echo \"Liberado: $(( (${A:-0}-${B:-0})/1024 )) MB\"",
    },
    "organizar_downloads": {
        "titulo": "Organizar Downloads", "icone": "🗂️",
        "descricao": "Separa os arquivos soltos da pasta Downloads em pastas (Imagens, Documentos, Vídeos…). "
                     "Nada é apagado: só muda de pasta.",
        "windows": "$d=Join-Path $env:USERPROFILE 'Downloads'; $m=@{" + _PS_MAPA + "}; $n=0; "
                   "Get-ChildItem $d -File | ForEach-Object { $e=$_.Extension.TrimStart('.').ToLower(); "
                   "$p=($m.GetEnumerator() | Where-Object { $_.Value -contains $e } | Select-Object -First 1).Key; "
                   "if(-not $p){$p='Outros'}; $dst=Join-Path $d $p; New-Item -ItemType Directory -Force $dst | Out-Null; "
                   "if(-not (Test-Path (Join-Path $dst $_.Name))){ Move-Item $_.FullName $dst; $n++ } }; "
                   "'Arquivos organizados: ' + $n",
        "unix": "cd \"$HOME/Downloads\" 2>/dev/null || { echo 'Pasta Downloads não encontrada'; exit 0; }; n=0; "
                "for f in *; do [ -f \"$f\" ] || continue; e=$(printf '%s' \"${f##*.}\" | tr 'A-Z' 'a-z'); "
                f"case \"$e\" in {_SH_CASE} *) p=Outros;; esac; mkdir -p \"$p\"; "
                "if [ ! -e \"$p/$f\" ]; then mv \"$f\" \"$p/\" && n=$((n+1)); fi; done; "
                "echo \"Arquivos organizados: $n\"",
    },
    "testar_internet": {
        "titulo": "Testar a internet", "icone": "📶",
        "descricao": "Mede se a internet está respondendo e com que rapidez.",
        "windows": "ping -n 4 1.1.1.1; if ($LASTEXITCODE -ne 0) { 'Sem resposta do ping: a internet caiu ou a rede bloqueia este teste.' }",
        "unix": "ping -c 4 1.1.1.1 || echo 'Sem resposta do ping: a internet caiu ou a rede bloqueia este teste.'",
    },
}


def eh_windows(info: dict[str, Any] | None) -> bool | None:
    so = str((info or {}).get("so") or "").lower()
    if not so:
        return None
    return "windows" in so


def comando(acao: str, info: dict[str, Any] | None) -> str:
    """O comando da ação para o sistema da máquina (KeyError se a ação não existe)."""
    a = ACOES[acao]
    win = eh_windows(info)
    if win is None:
        raise ValueError("a máquina ainda não informou o sistema operacional; espere ela conectar")
    if win:
        # -EncodedCommand evita qualquer problema de aspas no cmd.exe, que é
        # quem o agente usa para jobs shell no Windows.
        codificado = base64.b64encode(a["windows"].encode("utf-16-le")).decode()
        return f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {codificado}"
    return a["unix"]


def catalogo() -> list[dict[str, str]]:
    return [{"id": k, "titulo": v["titulo"], "descricao": v["descricao"], "icone": v["icone"]}
            for k, v in ACOES.items()]
