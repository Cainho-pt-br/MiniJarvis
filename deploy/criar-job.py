#!/usr/bin/env python3
"""Cria no broker o job descrito num arquivo de agendamento.

É o que os timers do systemd executam. Fica separado do agendador de propósito:
o agendador mexe em /etc/systemd e precisa de sudo; isto aqui só fala HTTP com
o broker e roda como usuário comum.

    python3 criar-job.py agendadas/backup-do-banco.json
"""
import json
import os
import sys
import urllib.error
import urllib.request

BROKER = os.environ.get("BROKER_URL", "http://127.0.0.1:8080").rstrip("/")


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: criar-job.py <arquivo.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        tarefa = json.load(f)

    job = tarefa.get("job")
    if not job:
        print("arquivo sem a chave 'job'", file=sys.stderr)
        return 2

    # O nome da rotina vai no payload: é por ele que o painel sabe quando cada
    # rotina rodou pela última vez e se deu certo (/api/rotinas).
    job = dict(job, payload=dict(job.get("payload") or {}, agendada=tarefa.get("nome", "?")))
    dados = json.dumps(job).encode()
    req = urllib.request.Request(BROKER + "/api/jobs", data=dados, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
    except urllib.error.URLError as e:
        # Sair com erro faz o systemd registrar a falha no journal, que é onde
        # se procura quando uma tarefa agendada "não rodou".
        print("não consegui falar com o broker: %s" % e, file=sys.stderr)
        return 1

    print("job %s criado (%s)" % (resp.get("id"), tarefa.get("nome", "?")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
