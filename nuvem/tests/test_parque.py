import json
from datetime import datetime, timedelta, timezone

from nuvem import app as modulo
from nuvem import parque

from .test_app import cadastrar, eventos


def parear(c, hostname="pc-sala", livre=True, so="Linux"):
    codigo = c.post("/api/parque/parear").json()["codigo"]
    r = c.post("/api/agente/parear", json={"codigo": codigo, "hostname": hostname})
    assert r.status_code == 200, r.text
    ag = {"Authorization": "Bearer " + r.json()["token"]}
    c.post("/api/agentes/heartbeat", json={"info": {"so": so}}, headers=ag)
    if livre:
        mid = next(m["id"] for m in c.get("/api/parque").json()["maquinas"] if m["hostname"] == hostname)
        assert c.post(f"/api/maquinas/{mid}/modo", json={"modo": "livre", "senha": "senha-forte-1", "aceito": True}).status_code == 200
    return ag


def batida(c, ag, **extra):
    corpo = {"hostname": "x", "tags": "", "versao_agente": "0.3.0", "cpu_pct": 12.5, "ram_pct": 40,
             "info": {"so": "Linux", "cpu": "Ryzen", "ram_total_mb": 16384, "tem_claude": True}}
    corpo.update(extra)
    return c.post("/api/agentes/heartbeat", json=corpo, headers=ag)


def test_pareamento_e_maquina_no_parque(cliente):
    cadastrar(cliente)
    r = cliente.post("/api/parque/parear").json()
    assert len(r["codigo"]) == 6 and f'instalar.sh?c={r["codigo"]}" | sh' in r["comandos"]["linux"]
    assert r["comandos"]["windows"].startswith("irm ") and r["comandos"]["windows"].endswith("| iex")
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
    texto = "".join(d for e, d in ev if e == "texto")
    assert texto.startswith("Vou preparar isso.\n\nPronto!") and texto.endswith("só roda depois disso.")
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


def test_sugestoes(cliente, monkeypatch):
    from nuvem.ia import FakeIA

    modelos_usados = []

    class EspiaSugestoes(FakeIA):
        def gerar_json(self, prompt, schema, modelo):
            modelos_usados.append(modelo)
            return super().gerar_json(prompt, schema, modelo)

    monkeypatch.setattr(modulo, "_ia", EspiaSugestoes())
    u = cadastrar(cliente)
    assert cliente.get("/api/sugestoes").json()["sugestoes"] == []
    r = cliente.post("/api/sugestoes/gerar").json()
    assert r["sugestoes"][0]["pergunta"] and r["creditos"] < modulo.CREDITOS_INICIAIS
    assert cliente.get("/api/sugestoes").json()["sugestoes"] == r["sugestoes"]
    assert cliente.post("/api/sugestoes/gerar").status_code == 429
    # Mesmo no Pro, sugestões usam Haiku 5.5
    modulo.db().execute("UPDATE usuarios SET plano='pro' WHERE id=?", (u["id"],))
    modulo.db().execute("DELETE FROM sugestoes")
    assert cliente.post("/api/sugestoes/gerar").status_code == 200
    assert modelos_usados == ["claude-haiku-5-5", "claude-haiku-5-5"]


def test_interesse_e_agente_para_baixar(cliente):
    cadastrar(cliente)
    assert cliente.post("/api/planos/pro/interesse").status_code == 200
    assert cliente.post("/api/planos/gratis/interesse").status_code == 404
    r = cliente.get("/agente.py")
    assert r.status_code == 200 and "def main()" in r.text
    planos = json.loads(json.dumps(cliente.get("/api/planos").json()))
    assert planos[2]["destaque"] is True
    for p in planos:
        for campo in ("modelo", "modelo_sugestoes", "maquinas", "creditos", "rotinas_diarias",
                      "sugestoes_auto", "max_tokens", "mensagens_estimadas"):
            assert campo in p


def test_sem_linha_em_branco_quando_a_primeira_rodada_so_usa_ferramenta(cliente, monkeypatch):
    from nuvem.ia import FakeIA

    class SoFerramenta(FakeIA):
        def responder(self, historico, final, modelo, ferramentas=None, max_tokens=None):
            if isinstance(historico[-1]["content"], str):
                final.modelo, final.stop_reason = modelo, "tool_use"
                final.conteudo = [{"type": "tool_use", "id": "t1", "name": "ver_parque", "input": {}}]
                final.chamadas = final.conteudo
                return iter(())
            return super().responder(historico, final, modelo, ferramentas)

    monkeypatch.setattr(modulo, "_ia", SoFerramenta())
    cadastrar(cliente)
    ev = eventos(cliente.post("/api/mensagens", json={"texto": "como está o parque?"}).text)
    assert "".join(d for e, d in ev if e == "texto").startswith("Pronto! O usuário ainda não conectou")


def test_modo_seguro_so_roda_acoes_prontas(cliente):
    cadastrar(cliente)
    ag = parear(cliente, livre=False)
    m = cliente.get("/api/parque").json()["maquinas"][0]
    assert m["modo"] == "seguro"
    r = cliente.post("/api/jobs", json={"maquina": "pc-sala", "tipo": "shell", "texto": "rm -rf ~"})
    assert r.status_code == 403 and "modo seguro" in r.json()["detail"]
    assert cliente.post("/api/jobs", json={"maquina": "pc-sala", "tipo": "acao", "texto": "nao-existe"}).status_code == 400
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "tipo": "acao", "texto": "espaco_disco"}).json()["id"]
    job = cliente.get("/api/trabalho?espera=0", headers=ag).json()
    assert job["tipo"] == "shell" and job["payload"]["acao"] == "espaco_disco" and "df -h" in job["payload"]["comando"]
    assert cliente.get(f"/api/jobs/{jid}").json()["titulo"] == "Ver espaço em disco"
    # repetir continua sendo a ação pronta
    assert cliente.post(f"/api/jobs/{jid}/repetir").status_code == 200
    # rotina livre recusada; rotina de ação aceita
    assert cliente.post("/api/rotinas", json={"nome": "x", "maquina": "pc-sala", "texto": "ls"}).status_code == 403
    assert cliente.post("/api/rotinas", json={"nome": "Limpeza", "maquina": "pc-sala", "tipo": "acao",
                                              "texto": "limpar_temporarios"}).status_code == 200
    assert cliente.get("/api/rotinas").json()[0]["texto"] == "Limpar arquivos temporários"


def test_acao_no_windows_usa_powershell_codificado(cliente):
    import base64

    cadastrar(cliente)
    ag = parear(cliente, livre=False, so="Windows 11 Pro")
    cliente.post("/api/jobs", json={"maquina": "pc-sala", "tipo": "acao", "texto": "info_sistema"})
    cmd = cliente.get("/api/trabalho?espera=0", headers=ag).json()["payload"]["comando"]
    assert cmd.startswith("powershell -NoProfile")
    assert "Win32_OperatingSystem" in base64.b64decode(cmd.split()[-1]).decode("utf-16-le")


def test_modo_livre_exige_senha(cliente):
    cadastrar(cliente)
    parear(cliente, livre=False)
    mid = cliente.get("/api/parque").json()["maquinas"][0]["id"]
    assert cliente.post(f"/api/maquinas/{mid}/modo", json={"modo": "livre", "senha": "errada"}).status_code == 403
    assert cliente.post(f"/api/maquinas/{mid}/modo", json={"modo": "livre", "senha": "senha-forte-1", "aceito": True}).status_code == 200
    assert cliente.post(f"/api/maquinas/{mid}/modo", json={"modo": "seguro"}).status_code == 200  # voltar não pede senha


def test_instaladores_trazem_endereco_e_codigo(cliente):
    sh = cliente.get("/instalar.sh?c=abc234").text
    assert 'CODIGO="ABC234"' in sh and 'JARVIS="http://testserver"' in sh and "--so-parear" in sh
    assert 'CODIGO=""' in cliente.get("/instalar.sh?c=;rm -rf /").text  # código estranho não entra no script
    ps = cliente.get("/instalar.ps1?c=ABC234").text
    assert "$Codigo = 'ABC234'" in ps and "Register-ScheduledTask" in ps and ps.rstrip().endswith("Instalar-Jarvis")
    assert "minijarvis" in cliente.get("/desinstalar.sh").text
    assert "Unregister-ScheduledTask" in cliente.get("/desinstalar.ps1").text


def test_jarvis_usa_acao_pronta_no_modo_seguro(cliente):
    cadastrar(cliente)
    ag = parear(cliente, livre=False)
    ev = eventos(cliente.post("/api/mensagens", json={"texto": "execute `:limpar_temporarios` em pc-sala"}).text)
    t = next(d for e, d in ev if e == "tarefa")
    assert t["pronta"] is True and "temporários" in t["comando"] and t["explicacao"]
    cliente.post(f"/api/jobs/{t['id']}/aprovar")
    assert cliente.get("/api/trabalho?espera=0", headers=ag).json()["payload"]["acao"] == "limpar_temporarios"
    # comando livre no modo seguro: a ferramenta devolve o motivo ao modelo, sem criar nada
    ev = eventos(cliente.post("/api/mensagens", json={"texto": "execute `rm -rf ~` em pc-sala"}).text)
    assert not any(e == "tarefa" for e, _ in ev)
    assert "modo seguro" in "".join(d for e, d in ev if e == "texto")
