#!/usr/bin/env python3
"""Cancela no broker um job em execução ou na fila.

Uso:
    python3 deploy/cancelar-job.py <id>
    python3 deploy/cancelar-job.py --ultimo   # cancela o mais recente
"""
import json
import os
import sys
import urllib.error
import urllib.request

BROKER = os.environ.get("BROKER_URL", "http://127.0.0.1:8080").rstrip("/")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("uso: cancelar-job.py <job_id> | --ultimo", file=sys.stderr)
        return 2

    arg = sys.argv[1]
    if arg in ("--ultimo", "-u"):
        url = BROKER + "/api/jobs/cancelar-ativo"
    else:
        try:
            job_id = int(arg.lstrip("#"))
            url = f"{BROKER}/api/jobs/{job_id}/cancelar"
        except ValueError:
            print(f"id inválido: {arg}", file=sys.stderr)
            return 2

    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", errors="replace")
        print(f"erro do broker ({e.code}): {detalhe}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"não consegui falar com o broker: {e}", file=sys.stderr)
        return 1

    if resp.get("ok"):
        jid = resp.get("id") or arg
        print(f"job {jid} cancelado (estava {resp.get('estado_anterior', '?')})")
        return 0
    else:
        print(f"aviso: {resp.get('mensagem', 'não cancelado')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
