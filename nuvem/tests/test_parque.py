import json
from datetime import datetime, timedelta, timezone

from nuvem import app as modulo
from nuvem import parque

from .test_app import cadastrar, eventos


def parear(c, hostname="pc-sala"):
    codigo = c.post("/api/parque/parear").json()["codigo"]
    r = c.post("/api/agente/parear", json={"codigo": codigo, "hostname": hostname})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def batida(c, ag, **extra):
    corpo = {"hostname": "x", "tags": "", "versao_agente": "0.3.0", "cpu_pct": 12.5, "ram_pct": 40,
             "info": {"so": "Linux", "cpu": "Ryzen", "ram_total_mb": 16384, "tem_claude": True}}
    corpo.update(extra)
    return c.post("/api/agentes/heartbeat", json=corpo, headers=ag)


def test_pareamento_e_maquina_no_parque(cliente):
    cadastrar(cliente)
    r = cliente.post("/api/parque/parear").json()
    assert len(r["codigo"]) == 6 and "agente.py --parear " + r["codigo"] in r["comandos"]["linux"]
    ag = {"Authorization": "Bearer " + cliente.post(
        "/api/agente/parear", json={"codigo": r["codigo"], "hostname": "pc-sala"}).json()["token"]}
    # código só vale uma vez
    assert cliente.post("/api/agente/parear", json={"codigo": r["codigo"], "hostname": "outro"}).status_code == 400
    assert batida(cliente, ag).json()["heartbeat_s"] == parque.HEARTBEAT_S
    p = cliente.get("/api/parque").json()
    assert p["resumo"]["online"] == 1
    m = p["maquinas"][0]
    assert m["hostname"] == "pc-sala" and m["info"]["cpu"] == "Ryzen" and m["metricas"]["cpu_pct"] == 12.5


def test_sem_token_ou_token_errado(cliente):
    assert cliente.get("/api/trabalho?espera=0").status_code == 401
    assert cliente.get("/api/trabalho?espera=0", headers={"Authorization": "Bearer x"}).status_code == 401


def test_plano_gratis_conecta_uma_maquina(cliente):
    cadastrar(cliente)
    parear(cliente)
    assert cliente.post("/api/parque/parear").status_code == 402
    u = cliente.get("/api/eu").json()
    modulo.db().execute("UPDATE usuarios SET plano='pro' WHERE id=?", (u["id"],))
    parear(cliente, "pc-quarto")
    assert cliente.get("/api/parque").json()["resumo"]["maquinas"] == 2


def test_ciclo_do_job(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "tipo": "shell", "texto": "echo oi"}).json()["id"]
    job = cliente.get("/api/trabalho?agente=pc-sala&tags=&espera=0", headers=ag).json()
    assert job["id"] == jid and job["payload"] == {"comando": "echo oi"} and job["tentativa"] == 1
    assert cliente.get("/api/trabalho?espera=0", headers=ag).status_code == 204  # já foi pego
    r = cliente.post(f"/api/trabalho/{jid}/progresso", json={"log": "oi\n", "mensagem": "rodando"}, headers=ag)
    assert r.json()["cancelar"] is False
    cliente.post(f"/api/trabalho/{jid}/concluir",
                 json={"resultado": {"returncode": 0, "stdout": "oi\n", "saida_completa": "x" * 10}}, headers=ag)
    d = cliente.get(f"/api/jobs/{jid}").json()
    assert d["status"] == "concluido" and d["log"] == "oi\n" and d["resultado"] == {"returncode": 0, "stdout": "oi\n"}
    assert cliente.get("/api/parque").json()["resumo"]["concluidos_24h"] == 1


def test_falha_tenta_de_novo_e_depois_desiste(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "texto": "false"}).json()["id"]
    for esperado in ("pendente", "falhou"):
        assert cliente.get("/api/trabalho?espera=0", headers=ag).json()["id"] == jid
        cliente.post(f"/api/trabalho/{jid}/falhar", json={"erro": "saiu com código 1"}, headers=ag)
        assert cliente.get(f"/api/jobs/{jid}").json()["status"] == esperado


def test_cancelar_job_em_execucao(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "texto": "sleep 100"}).json()["id"]
    cliente.get("/api/trabalho?espera=0", headers=ag)
    cliente.post(f"/api/jobs/{jid}/cancelar")
    assert cliente.post(f"/api/trabalho/{jid}/progresso", json={}, headers=ag).json()["cancelar"] is True
    assert batida(cliente, ag, job_atual=jid).json()["cancelar"] is True
    cliente.post(f"/api/trabalho/{jid}/falhar", json={"erro": "cancelado"}, headers=ag)
    assert cliente.get(f"/api/jobs/{jid}").json()["status"] == "cancelado"


def test_job_orfao_volta_para_a_fila(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "texto": "echo"}).json()["id"]
    cliente.get("/api/trabalho?espera=0", headers=ag)
    velho = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    modulo.db().execute("UPDATE jobs SET visto_em=? WHERE id=?", (velho, jid))
    assert cliente.get("/api/jobs?status=pendente").json()[0]["id"] == jid


def test_contas_isoladas(cliente):
    cadastrar(cliente)
    ag_ana = parear(cliente)
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "texto": "segredo"}).json()["id"]
    cliente.post("/api/sair")
    cliente.cookies.clear()
    cadastrar(cliente, "beto@exemplo.com")
    ag_beto = parear(cliente, "pc-beto")
    assert cliente.get("/api/parque").json()["resumo"]["maquinas"] == 1
    assert cliente.get(f"/api/jobs/{jid}").status_code == 404
    assert cliente.post("/api/jobs", json={"maquina": "pc-sala", "texto": "x"}).status_code == 404
    # o agente do Beto não pega trabalho da Ana nem mexe nele
    assert cliente.get("/api/trabalho?espera=0", headers=ag_beto).status_code == 204
    assert cliente.post(f"/api/trabalho/{jid}/concluir", json={}, headers=ag_beto).status_code == 404
    assert cliente.get("/api/trabalho?espera=0", headers=ag_ana).json()["id"] == jid


def test_remover_maquina_revoga_token(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    mid = cliente.get("/api/parque").json()["maquinas"][0]["id"]
    assert cliente.delete(f"/api/maquinas/{mid}").status_code == 200
    assert batida(cliente, ag).status_code == 401


def test_jarvis_prepara_tarefa_que_espera_aprovacao(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    ev = eventos(cliente.post("/api/mensagens", json={"texto": "execute `ls ~/Downloads` em pc-sala"}).text)
    tarefa = next(d for e, d in ev if e == "tarefa")
    assert tarefa["comando"] == "ls ~/Downloads" and tarefa["maquina"] == "pc-sala"
    assert "".join(d for e, d in ev if e == "texto").endswith("só roda depois disso.")
    # não roda antes de aprovar
    assert cliente.get("/api/trabalho?espera=0", headers=ag).status_code == 204
    cliente.post(f"/api/jobs/{tarefa['id']}/aprovar")
    assert cliente.get("/api/trabalho?espera=0", headers=ag).json()["id"] == tarefa["id"]
    # as idas e vindas com a ferramenta ficam escondidas da conversa visível
    cid = ev[0][1]["id"]
    papeis = [m["papel"] for m in cliente.get(f"/api/conversas/{cid}").json()["mensagens"]]
    assert papeis == ["user", "assistant", "assistant"]
    # e a conversa continua aceitando mensagens (histórico com tool_use/tool_result válido)
    ev2 = eventos(cliente.post("/api/mensagens", json={"texto": "valeu", "conversa_id": cid}).text)
    assert ev2[-1][0] == "fim"


def test_ferramentas_validam_entrada():
    from nuvem.jarvis import EntradaInvalida, executar_ferramenta
    import pytest

    for nome, entrada in (("criar_tarefa", {"maquina": "x", "tipo": "rm", "comando": "a", "titulo": "t"}),
                          ("criar_tarefa", {"maquina": "x"}), ("ver_tarefa", {"id": "1"}), ("nada", {}),
                          ("salvar_no_cerebro", "texto")):
        with pytest.raises(EntradaInvalida):
            executar_ferramenta(1, nome, entrada)


def test_cerebro(cliente):
    cadastrar(cliente)
    c = cliente.get("/api/cerebro").json()
    assert {n["nome"] for n in c["notas"]} >= {"Jarvis", "Parque"}
    assert {"de": "Jarvis", "para": "Parque", "existe": True} in c["arestas"]
    cliente.put("/api/cerebro/Viagem", json={"conteudo": "Ir para [[Lisboa]] em maio. Ver [[Jarvis]]."})
    c = cliente.get("/api/cerebro").json()
    assert {"de": "Viagem", "para": "Lisboa", "existe": False} in c["arestas"]
    assert cliente.get("/api/cerebro/Viagem").json()["conteudo"].startswith("Ir para")
    ag = parear(cliente)
    r = cliente.post("/api/agente/cerebro", json={"notas": [{"nome": "Projeto X", "conteudo": "notas"}]}, headers=ag)
    assert r.json()["gravadas"] == 1
    assert cliente.get("/api/cerebro/Projeto X").json()["origem"] == "pc-sala"


def test_rotinas_e_cron(cliente, monkeypatch):
    u = cadastrar(cliente)
    ag = parear(cliente)
    assert cliente.post("/api/rotinas", json={"nome": "B", "maquina": "pc-sala", "texto": "x",
                                              "frequencia": "diaria"}).status_code == 402
    modulo.db().execute("UPDATE usuarios SET plano='pro' WHERE id=?", (u["id"],))
    rid = cliente.post("/api/rotinas", json={"nome": "Backup", "maquina": "pc-sala", "texto": "tar czf b.tgz docs",
                                             "frequencia": "diaria"}).json()["id"]
    jid = cliente.post(f"/api/rotinas/{rid}/rodar").json()["id"]
    assert cliente.get("/api/rotinas").json()[0]["ultimo"]["id"] == jid
    assert cliente.get("/api/trabalho?espera=0", headers=ag).json()["id"] == jid
    assert cliente.get("/api/cron/rotinas").status_code == 401
    monkeypatch.setenv("CRON_SECRET", "s3")
    cab = {"Authorization": "Bearer s3"}
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["disparadas"] == 0  # já rodou hoje
    modulo.db().execute("UPDATE rotinas SET ultima_execucao='2020-01-01T00:00:00+00:00'")
    assert cliente.get("/api/cron/rotinas", headers=cab).json()["disparadas"] == 1


def test_sugestoes(cliente):
    cadastrar(cliente)
    assert cliente.get("/api/sugestoes").json()["sugestoes"] == []
    r = cliente.post("/api/sugestoes/gerar").json()
    assert r["sugestoes"][0]["pergunta"] and r["creditos"] < modulo.CREDITOS_INICIAIS
    assert cliente.get("/api/sugestoes").json()["sugestoes"] == r["sugestoes"]
    assert cliente.post("/api/sugestoes/gerar").status_code == 429


def test_interesse_e_agente_para_baixar(cliente):
    cadastrar(cliente)
    assert cliente.post("/api/planos/pro/interesse").status_code == 200
    assert cliente.post("/api/planos/gratis/interesse").status_code == 404
    r = cliente.get("/agente.py")
    assert r.status_code == 200 and "def main()" in r.text
    assert json.loads(json.dumps(cliente.get("/api/planos").json()))[2]["destaque"] is True
