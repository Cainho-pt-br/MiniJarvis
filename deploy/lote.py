#!/usr/bin/env python3
"""Dispara vários jobs de uma vez e acompanha todos juntos.

POR QUE EXISTE
Criar job com `curl` e ficar consultando um por um desperdiça o parque: enquanto
você espera a celeron, o boss e o Pi estão parados. Isto cria tudo de uma vez e
mostra o quadro inteiro atualizando — que é a única forma de ver de verdade se
o roteamento por tag está distribuindo o trabalho.

    python3 deploy/lote.py tarefas.json          # cria, acompanha e imprime log
    python3 deploy/lote.py tarefas.json --so-criar
    python3 deploy/lote.py --acompanhar 520 521 522

O arquivo é uma lista:

    [ {"nome": "editais", "tags": "celeron", "comando": "curl -s ..."},
      {"nome": "render",  "tags": "boss",    "comando": "ffmpeg ..."} ]

Aceita também "tipo" (default "shell") e "max_tentativas" (default 1). Job para
máquina desligada fica `pendente` e isso NÃO é falha — é a regra 1 do projeto.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BROKER = os.environ.get("BROKER_URL",
                        "https://raspberrypi.taild7492b.ts.net").rstrip("/")
INTERVALO = 8
PARADOS = ("concluido", "falhou", "cancelado")


def pedir(caminho: str, corpo: dict | None = None) -> dict:
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(BROKER + caminho, data=dados,
                                 method="POST" if dados else "GET")
    if dados:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def criar(tarefas: list) -> list:
    criados = []
    for t in tarefas:
        corpo = {"tipo": t.get("tipo", "shell"), "tags": t["tags"],
                 "max_tentativas": t.get("max_tentativas", 1),
                 # Prioridade serve para furar fila: um teste de dois minutos
                 # não deve esperar um download de 14 GB que já está na frente.
                 # MENOR roda antes — o índice do broker é (status, prioridade,
                 # id), e o padrão dele é 100.
                 "prioridade": t.get("prioridade", 100),
                 "payload": {"comando": t["comando"]}}
        r = pedir("/api/jobs", corpo)
        criados.append({"id": r["id"], "nome": t.get("nome", "?"),
                        "tags": t["tags"]})
        print("  job %-5s %-14s %s" % (r["id"], t["tags"], t.get("nome", "")))
    return criados


def acompanhar(criados: list, limite_min: int = 40) -> dict:
    """Mostra o quadro até todos pararem. Devolve {id: job}."""
    fim = time.time() + limite_min * 60
    estado: dict = {}
    ultimo = ""
    while time.time() < fim:
        linha = []
        for c in criados:
            try:
                j = pedir("/api/jobs/%d" % c["id"])
            except urllib.error.URLError:
                continue          # broker no Wi-Fi fraco do Pi; tenta de novo
            estado[c["id"]] = j
            linha.append("%s:%s=%s" % (c["tags"], c["id"], j["status"][:4]))
        agora = "  ".join(linha)
        if agora != ultimo:       # só imprime quando algo muda
            print("  " + agora)
            ultimo = agora
        if estado and all(j["status"] in PARADOS for j in estado.values()):
            break
        time.sleep(INTERVALO)
    return estado


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivo", nargs="?")
    ap.add_argument("--so-criar", action="store_true")
    ap.add_argument("--acompanhar", nargs="*", type=int)
    ap.add_argument("--minutos", type=int, default=40)
    args = ap.parse_args()

    if args.acompanhar:
        criados = [{"id": i, "nome": "", "tags": "?"} for i in args.acompanhar]
    elif args.arquivo:
        with open(args.arquivo, encoding="utf-8") as f:
            criados = criar(json.load(f))
        if args.so_criar:
            return 0
    else:
        print(__doc__)
        return 2

    print("\nacompanhando (só imprime quando muda):")
    estado = acompanhar(criados, args.minutos)

    for c in criados:
        j = estado.get(c["id"])
        if not j:
            continue
        print("\n" + "=" * 78)
        print("job %s  %s  %s  [%s]" % (c["id"], c["tags"], c["nome"], j["status"]))
        print("=" * 78)
        print(j.get("log") or "(sem log)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
