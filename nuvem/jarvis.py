"""Ferramentas que o Jarvis usa no chat, e as sugestões para o parque.

As entradas das ferramentas chegam do modelo e são validadas aqui antes de
qualquer efeito. Tarefas criadas pelo chat nunca rodam sozinhas: nascem com
status "aguardando" e só vão para a fila quando o usuário aprova no painel.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from . import app as nucleo
from . import parque


class EntradaInvalida(ValueError):
    pass


def _texto(entrada: dict[str, Any], campo: str, obrigatorio: bool = True, maximo: int = 20_000) -> str:
    v = entrada.get(campo)
    if v is None and not obrigatorio:
        return ""
    if not isinstance(v, str) or (obrigatorio and not v.strip()):
        raise EntradaInvalida(f"campo '{campo}' precisa ser um texto")
    return v[:maximo]


def executar_ferramenta(uid: int, nome: str, entrada: Any) -> tuple[str, dict[str, Any] | None]:
    """Devolve (texto para o modelo, evento para a tela ou None)."""
    if not isinstance(entrada, dict):
        raise EntradaInvalida("a entrada precisa ser um objeto")
    if nome == "ver_parque":
        return parque.resumo_do_parque(uid), None
    if nome == "criar_tarefa":
        from . import acoes

        tipo = _texto(entrada, "tipo")
        if tipo not in ("acao", "shell", "claude"):
            raise EntradaInvalida("tipo precisa ser 'acao', 'shell' ou 'claude'")
        maquina = _texto(entrada, "maquina", maximo=80)
        titulo = _texto(entrada, "titulo", maximo=120)
        explicacao = _texto(entrada, "explicacao", maximo=500)
        if tipo == "acao":
            alvo = _texto(entrada, "acao", maximo=60)
            if alvo not in acoes.ACOES:
                raise EntradaInvalida("acao desconhecida; use um id da lista de ações prontas de ver_parque")
        else:
            alvo = _texto(entrada, "comando")
        try:
            jid = parque.criar_job(uid, maquina, tipo, alvo, titulo, "jarvis", "aguardando")
        except HTTPException as exc:
            return f"Não deu para criar a tarefa: {exc.detail}", None
        comando = acoes.ACOES[alvo]["descricao"] if tipo == "acao" else alvo
        evento = {"id": jid, "titulo": titulo, "maquina": maquina, "tipo": tipo, "comando": comando,
                  "explicacao": explicacao, "pronta": tipo == "acao"}
        return (f"Tarefa #{jid} preparada em {maquina}. Ela aparece agora para o usuário aprovar; "
                "só roda depois disso."), evento
    if nome == "ver_tarefa":
        jid = entrada.get("id")
        if not isinstance(jid, int):
            raise EntradaInvalida("id precisa ser um número")
        try:
            j = parque.detalhe_job(jid, nucleo.db().execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone())
        except HTTPException:
            return f"Não existe tarefa #{jid}.", None
        resultado = j["resultado"] or {}
        saida = resultado.get("stdout") or resultado.get("resposta") or ""
        return (f"Tarefa #{jid} '{j['titulo']}' em {j['agente'] or j['tags']}: {j['status']}"
                f" ({j['mensagem'] or ''}).\nSaída:\n{(saida or j['log'])[-3000:]}"), None
    if nome == "ler_cerebro":
        alvo = _texto(entrada, "nome", obrigatorio=False, maximo=120).strip()
        if not alvo:
            parque._semear_cerebro(uid)
            nomes = [r["nome"] for r in nucleo.db().execute(
                "SELECT nome FROM notas WHERE usuario_id=? ORDER BY nome", (uid,)).fetchall()]
            return "Notas no cérebro: " + ", ".join(nomes), None
        r = nucleo.db().execute("SELECT conteudo FROM notas WHERE usuario_id=? AND nome=?", (uid, alvo)).fetchone()
        return (r["conteudo"][:20_000] if r else f"Não há nota chamada '{alvo}'."), None
    if nome == "salvar_no_cerebro":
        alvo = _texto(entrada, "nome", maximo=120).strip()
        parque.salvar_nota(uid, alvo, _texto(entrada, "conteudo", maximo=100_000), "jarvis")
        return f"Nota '{alvo}' salva no cérebro.", {"nota": alvo}
    raise EntradaInvalida(f"ferramenta desconhecida: {nome}")


# --------------------------------------------------------------- sugestões ---

SCHEMA_SUGESTOES = {
    "type": "object",
    "properties": {"sugestoes": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "tag": {"type": "string", "enum": ["desempenho", "organizacao", "seguranca", "ia", "sistema"]},
            "titulo": {"type": "string"},
            "descricao": {"type": "string"},
            "motivo": {"type": "string"},
            "maquina": {"type": "string"},
            "pergunta": {"type": "string"},
        },
        "required": ["tag", "titulo", "descricao", "motivo", "maquina", "pergunta"],
        "additionalProperties": False,
    }}},
    "required": ["sugestoes"],
    "additionalProperties": False,
}


def prompt_sugestoes(uid: int) -> str:
    return (
        "Você é o conselheiro do parque de computadores de uma pessoa comum (não técnica).\n"
        "Com base no estado abaixo, sugira de 2 a 4 ações úteis para HOJE. Cada uma precisa de um motivo "
        "concreto tirado do estado (um número, o nome de uma máquina, uma tarefa que falhou, um disco "
        "cheio). Nada genérico.\n"
        "Cada sugestão vira um botão: o campo 'pergunta' é o pedido que será enviado ao Jarvis (o assistente "
        "com acesso às máquinas) quando a pessoa clicar, escrito na voz da pessoa. 'maquina' é o nome da "
        "máquina envolvida, ou vazio. Títulos com até 5 palavras, em português simples.\n"
        "Máquina desligada é normal, não é problema.\n\n"
        f"{parque.resumo_do_parque(uid)}"
    )


def validar_sugestoes(dados: Any) -> list[dict[str, str]]:
    boas = []
    for s in (dados or {}).get("sugestoes", [])[:6]:
        if isinstance(s, dict) and str(s.get("titulo", "")).strip() and str(s.get("pergunta", "")).strip():
            boas.append({k: str(s.get(k, ""))[:300] for k in SCHEMA_SUGESTOES["properties"]["sugestoes"]
                         ["items"]["properties"]})
    return boas


def guardar_sugestoes(uid: int, sugestoes: list[dict[str, str]]) -> None:
    with nucleo.transacao() as con:
        con.execute("DELETE FROM sugestoes WHERE usuario_id=?", (uid,))
        con.execute("INSERT INTO sugestoes VALUES (?, ?, ?)",
                    (uid, json.dumps(sugestoes, ensure_ascii=False), nucleo.agora()))
