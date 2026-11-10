import pytest

from nuvem.falhas import explicar_falha

from .test_app import cadastrar
from .test_parque import parear


@pytest.mark.parametrize("erro,trecho", [
    ("claude não encontrado no PATH deste agente", "Claude Code instalado"),
    ("comando saiu com código 1: Permission denied", "negou o acesso"),
    ("OSError: [Errno 28] No space left on device", "disco está cheio"),
    ("bash: foo: command not found", "não existe neste computador"),
    ("'foo' não é reconhecido como um comando interno", "não existe neste computador"),
    ("Temporary failure in name resolution", "internet"),
    ("a máquina parou de responder", "desligou"),
    ("comando saiu com código 2: algo", "terminou com erro"),
    ("mensagem inédita que ninguém previu", "não disse o motivo"),
])
def test_traduz_erros_comuns(erro, trecho):
    assert trecho in explicar_falha("falhou", erro, "")


def test_so_explica_o_que_falhou():
    assert explicar_falha("concluido", "ok", "") is None
    assert explicar_falha("executando", "", "") is None


def test_tarefa_que_falhou_traz_ajuda_no_detalhe(cliente):
    cadastrar(cliente)
    ag = parear(cliente)
    jid = cliente.post("/api/jobs", json={"maquina": "pc-sala", "texto": "cat /raiz/segredo"}).json()["id"]
    for _ in range(2):  # 2 tentativas
        cliente.get("/api/trabalho?espera=0", headers=ag)
        cliente.post(f"/api/trabalho/{jid}/falhar", json={"erro": "comando saiu com código 1: Permission denied"},
                     headers=ag)
    d = cliente.get(f"/api/jobs/{jid}").json()
    assert d["status"] == "falhou" and "negou o acesso" in d["ajuda"]
    assert cliente.get("/api/jobs/9999").status_code == 404
