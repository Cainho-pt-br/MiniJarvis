from contextlib import contextmanager
from types import SimpleNamespace

from nuvem import ia


class _ClienteFalso:
    def __init__(self):
        self.pedidos = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    @contextmanager
    def stream(self, **kwargs):
        self.pedidos.append(kwargs)
        uso = SimpleNamespace(input_tokens=100, output_tokens=50,
                              cache_read_input_tokens=1000, cache_creation_input_tokens=0)
        msg = SimpleNamespace(model=kwargs["model"], content=[], usage=uso, stop_reason="end_turn")
        yield SimpleNamespace(text_stream=iter(["Olá"]), get_final_message=lambda: msg)


def _motor():
    motor = ia.ClaudeIA.__new__(ia.ClaudeIA)
    motor.cliente = _ClienteFalso()
    return motor


def test_modelo_por_plano():
    assert ia.modelo_do_plano("gratis") == "claude-haiku-5-5"
    assert ia.modelo_do_plano("basico") == "claude-sonnet-5-5"


def test_fallback_so_onde_e_suportado():
    motor = _motor()
    final = ia.Resposta()
    assert "".join(motor.responder([{"role": "user", "content": "oi"}], final, "claude-sonnet-5-5")) == "Olá"
    assert motor.cliente.pedidos[-1]["fallbacks"] == "default"
    list(motor.responder([{"role": "user", "content": "oi"}], ia.Resposta(), "claude-haiku-5-5"))
    assert "fallbacks" not in motor.cliente.pedidos[-1]
    assert "betas" not in motor.cliente.pedidos[-1]
    # leitura de cache conta 0,1x: 100 + 0,1 * 1000
    assert final.tokens_entrada == 200


def test_custo_nunca_abaixo_do_preco():
    assert ia.custo_usd("modelo-desconhecido", 1_000_000, 0) == max(p[0] for p in ia.PRECOS.values())
