#!/usr/bin/env python3
"""Backup e poda do histórico de jobs do broker.

    python3 manutencao-jobs.py contar
    python3 manutencao-jobs.py backup <arquivo-destino>
    python3 manutencao-jobs.py limpar

A poda remove só job FINALIZADO. Pendente e executando ficam: apagar um job em
execução deixaria o agente reportando progresso de algo que o broker não conhece
mais, e o zelador não teria como devolvê-lo.

Sempre rode `backup` antes de `limpar` — o histórico é a única memória durável
do parque, e a lista de agentes em memória não a reconstrói.
"""
import os
import sqlite3
import sys

BANCO = "/opt/orquestrador/broker/orquestrador.db"
FINALIZADOS = ("concluido", "falhou", "cancelado")


def con(caminho=BANCO):
    # timeout: o broker escreve no mesmo banco; em WAL a escrita é serializada
    # e sem espera daria "database is locked" na primeira coincidência.
    c = sqlite3.connect(caminho, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def contar():
    c = con()
    for r in c.execute("SELECT status, COUNT(*) n FROM jobs"
                       " GROUP BY status ORDER BY n DESC"):
        print("  %-12s %d" % (r["status"], r["n"]))
    print("  %-12s %d" % ("TOTAL",
          c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]))


def backup(destino):
    # API de backup do próprio sqlite: sai consistente mesmo com o broker
    # escrevendo no meio, diferente de copiar o arquivo com cp.
    origem, dst = con(), sqlite3.connect(destino)
    origem.backup(dst)
    dst.close()
    origem.close()
    v = sqlite3.connect(destino)          # confere que o backup abre mesmo
    n = v.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    v.close()
    print("backup: %s  (%d bytes, %d jobs)" %
          (destino, os.path.getsize(destino), n))


def limpar():
    c = con()
    cur = c.execute("DELETE FROM jobs WHERE status IN (?,?,?)", FINALIZADOS)
    c.commit()
    restam = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print("removidos %d; restaram %d (pendente/executando preservados)"
          % (cur.rowcount, restam))
    c.execute("VACUUM")                   # devolve o espaço ao cartão
    c.commit()
    c.close()
    print("banco agora com %d bytes" % os.path.getsize(BANCO))


if __name__ == "__main__":
    acao = sys.argv[1] if len(sys.argv) > 1 else "contar"
    if acao == "contar":
        contar()
    elif acao == "backup":
        backup(sys.argv[2])
    elif acao == "limpar":
        limpar()
    else:
        print("uso: contar | backup <arquivo> | limpar")
        sys.exit(2)
