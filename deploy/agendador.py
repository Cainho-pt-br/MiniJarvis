#!/usr/bin/env python3
"""Agenda jobs do orquestrador usando timers do systemd.

POR QUE systemd E NÃO UM LAÇO NO BROKER
O systemd já faz tudo que um agendador precisa fazer bem: sobrevive a reboot,
recupera execução perdida enquanto a máquina estava desligada (Persistent),
registra cada disparo no journal e sabe dizer quando é o próximo. Reescrever
isso dentro do broker seria pôr um cron num processo que não pode morrer — e a
regra 7 do projeto proíbe abstração antes de haver carga real.

CADA TAREFA É UM ARQUIVO em agendadas/*.json:

    {
      "nome": "backup-do-banco",          # vira orq-backup-do-banco.timer
      "descricao": "...",                 # aparece no systemctl status
      "quando": "03:20",                  # sintaxe OnCalendar do systemd
      "job": { ... }                      # o corpo POSTado em /api/jobs
    }

O timer não executa a tarefa: ele cria um JOB no broker, e quem executa é o
agente da máquina indicada na tag. Assim tudo que roda sozinho aparece no
painel, com log e histórico, em vez de acontecer escondido no journal.

RODE A PARTIR DO CLONE, NUNCA DE /opt. Os units apontam para o JSON no lugar
de onde este script foi rodado. O clone (/home/pi/orq no Pi) é puxado pelo
agente antes de todo job de modelo, então commit e push bastam para a tarefa
mudar. Uma cópia em /opt não é puxada por ninguém: em 2026-09-09 os units foram
apontados para lá, e a troca da fila de leads para o Gemini, commitada em
2026-09-10, rodou naquele dia como Claude com o prompt velho. O `listar` mostra
de onde cada timer lê, justamente para isso saltar aos olhos.

    sudo python3 deploy/agendador.py aplicar      # da raiz do clone
    python3 deploy/agendador.py listar
    sudo python3 deploy/agendador.py remover <nome>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
AGENDADAS = AQUI / "agendadas"
UNITS = Path("/etc/systemd/system")
PREFIXO = "orq-"
NOME_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def tarefas() -> list[dict]:
    saida = []
    for arq in sorted(AGENDADAS.glob("*.json")):
        t = json.loads(arq.read_text(encoding="utf-8"))
        t["_arquivo"] = arq
        if not NOME_OK.match(t.get("nome", "")):
            raise SystemExit("nome inválido em %s: use minúsculas, dígitos e hífen"
                             % arq.name)
        saida.append(t)
    return saida


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=False)


def aplicar() -> None:
    for t in tarefas():
        nome = t["nome"]
        base = PREFIXO + nome
        servico = UNITS / (base + ".service")
        timer = UNITS / (base + ".timer")

        servico.write_text(
            "[Unit]\n"
            "Description=%s\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            # Roda como pi: só precisa falar HTTP com o broker em 127.0.0.1.
            "User=pi\n"
            "ExecStart=/usr/bin/python3 %s %s\n"
            % (t.get("descricao", nome), AQUI / "criar-job.py", t["_arquivo"]),
            encoding="utf-8")

        # "quando" aceita string ou lista. Cadência quinzenal não cabe num
        # OnCalendar só, e a forma que o systemd oferece é repetir a linha —
        # por exemplo primeira e terceira segunda-feira do mês.
        quando = t["quando"]
        linhas_quando = "".join(
            "OnCalendar=%s\n" % q
            for q in ([quando] if isinstance(quando, str) else quando))

        timer.write_text(
            "[Unit]\n"
            "Description=%s\n\n"
            "[Timer]\n"
            "%s"
            # Persistent: se o Pi estiver desligado na hora, roda assim que
            # voltar em vez de simplesmente pular o dia.
            "Persistent=true\n"
            # Espalha o disparo: sem isso várias tarefas com o mesmo horário
            # acordariam juntas e o Pi levaria um pico à toa.
            "RandomizedDelaySec=120\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
            % (t.get("descricao", nome), linhas_quando),
            encoding="utf-8")

        print("  %-28s %s" % (base, linhas_quando.replace("\n", "  ").strip()))

    systemctl("daemon-reload")
    for t in tarefas():
        systemctl("enable", "--now", PREFIXO + t["nome"] + ".timer")
    print("\naplicado. `python3 agendador.py listar` mostra o próximo disparo.")


def listar() -> None:
    subprocess.run(["systemctl", "list-timers", PREFIXO + "*", "--all",
                    "--no-pager"], check=False)
    # De onde cada timer lê o JSON. Se aparecer /opt, ou qualquer caminho que
    # não seja este clone, a tarefa commitada não é a que vai rodar.
    print("\nde onde cada timer lê a tarefa (esperado: %s):" % AGENDADAS)
    for servico in sorted(UNITS.glob(PREFIXO + "*.service")):
        m = re.search(r"^ExecStart=.*criar-job\.py\s+(\S+)",
                      servico.read_text(encoding="utf-8"), re.M)
        origem = Path(m.group(1)) if m else None
        if origem is None:
            estado = "?? sem criar-job.py no ExecStart"
        elif origem.parent == AGENDADAS:
            estado = "ok"
        else:
            estado = "FORA DO CLONE: " + str(origem)
        print("  %-28s %s" % (servico.stem, estado))


def remover(nome: str) -> None:
    base = PREFIXO + nome
    systemctl("disable", "--now", base + ".timer")
    for suf in (".timer", ".service"):
        alvo = UNITS / (base + suf)
        if alvo.exists():
            alvo.unlink()
            print("removido", alvo.name)
    systemctl("daemon-reload")


if __name__ == "__main__":
    acao = sys.argv[1] if len(sys.argv) > 1 else "listar"
    if acao == "aplicar":
        aplicar()
    elif acao == "listar":
        listar()
    elif acao == "remover" and len(sys.argv) > 2:
        remover(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
