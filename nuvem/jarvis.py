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
                f" ({j['mensagem'] or ''}).{(chr(10) + 'Explicação: ' + j['ajuda']) if j.get('ajuda') else ''}\n"
                f"Saída:\n{(saida or j['log'])[-3000:]}"), None
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


def prompt_sugestoes(uid: int, idioma: str | None = None) -> str:
    from .i18n import idioma_atual, normalizar_idioma

    lang = normalizar_idioma(idioma) or idioma_atual()
    regra_idioma = {
        "en": "Titles up to 5 words, and all fields ('titulo', 'descricao', 'motivo', 'pergunta') in simple English.",
        "es": "Títulos de hasta 5 palabras, y todos los campos ('titulo', 'descricao', 'motivo', 'pergunta') en español simple.",
    }.get(lang, "Títulos com até 5 palavras, em português simples.")
    return (
        "Você é o conselheiro do parque de computadores de uma pessoa comum (não técnica).\n"
        "Com base no estado abaixo, sugira de 2 a 4 ações úteis para HOJE. Cada uma precisa de um motivo "
        "concreto tirado do estado (um número, o nome de uma máquina, uma tarefa que falhou, um disco "
        "cheio). Nada genérico.\n"
        "Cada sugestão vira um botão: o campo 'pergunta' é o pedido que será enviado ao Jarvis (o assistente "
        "com acesso às máquinas) quando a pessoa clicar, escrito na voz da pessoa. 'maquina' é o nome da "
        f"máquina envolvida, ou vazio. {regra_idioma}\n"
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


def atualizar_sugestoes(u: Any, cobrar: bool) -> tuple[list[dict[str, str]], int]:
    """Pede as sugestões à IA e guarda. Com `cobrar=False` (planos com sugestões
    automáticas) não mexe nos créditos. Estoura HTTPException se o teto diário de
    gasto foi atingido ou a IA falhou. Devolve (sugestões, saldo)."""
    from . import operacao
    from .i18n import idioma_atual, normalizar_idioma, t
    from .ia import modelo_sugestoes_do_plano

    operacao.conferir_teto()
    try:
        u_idioma = u["idioma"] if "idioma" in u.keys() else None
    except Exception:
        u_idioma = None
    idioma = normalizar_idioma(u_idioma) or idioma_atual()
    try:
        dados, final = nucleo.ia().gerar_json(prompt_sugestoes(u["id"], idioma), SCHEMA_SUGESTOES,
                                              modelo_sugestoes_do_plano(u["plano"]))
    except Exception as exc:  # noqa: BLE001
        nucleo.log.exception("Falha ao gerar sugestões: %s", type(exc).__name__)
        operacao.incidente("ia")
        raise HTTPException(503, t("erro_sugestoes", idioma))
    sugestoes = validar_sugestoes(dados)
    guardar_sugestoes(u["id"], sugestoes)
    saldo = nucleo._cobrar(u["id"], nucleo._custo(final), "sugestões do parque") if cobrar else u["creditos"]
    return sugestoes, saldo


def sugestoes_automaticas(limite: int = 20) -> int:
    """Chamado pelo cron: renova as sugestões dos planos com `sugestoes_auto`
    que têm máquina conectada, sem cobrar créditos. No máximo `limite` pessoas
    por rodada, para caber no tempo da função."""
    from datetime import datetime, timedelta, timezone

    planos_auto = [p["id"] for p in nucleo.PLANOS if p.get("sugestoes_auto")]
    if not planos_auto:
        return 0
    marcadores = ",".join("?" * len(planos_auto))
    velha = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(timespec="seconds")
    rows = nucleo.db().execute(
        f"SELECT u.* FROM usuarios u LEFT JOIN sugestoes s ON s.usuario_id = u.id "
        f"WHERE u.plano IN ({marcadores}) AND u.email_verificado = 1 AND (s.gerado_em IS NULL OR s.gerado_em < ?) "
        f"AND EXISTS (SELECT 1 FROM agentes a WHERE a.usuario_id = u.id) ORDER BY u.id LIMIT ?",
        (*planos_auto, velha, limite)).fetchall()
    feitas = 0
    for u in rows:
        try:
            atualizar_sugestoes(u, cobrar=False)
            feitas += 1
        except HTTPException as exc:
            if exc.status_code == 503 and ("limite" in str(exc.detail).lower() or "limit" in str(exc.detail).lower()):
                break  # teto diário: para por hoje
    return feitas


def guardar_sugestoes(uid: int, sugestoes: list[dict[str, str]]) -> None:
    with nucleo.transacao() as con:
        con.execute("DELETE FROM sugestoes WHERE usuario_id=?", (uid,))
        con.execute("INSERT INTO sugestoes VALUES (?, ?, ?)",
                    (uid, json.dumps(sugestoes, ensure_ascii=False), nucleo.agora()))

