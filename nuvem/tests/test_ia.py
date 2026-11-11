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
    assert ia.modelo_do_plano("basico") == "claude-haiku-5-5"
    assert ia.modelo_do_plano("pro") == "claude-sonnet-5-5"
    assert ia.modelo_do_plano("turbo") == "claude-opus-5-5"
    for p in ("gratis", "basico", "pro", "turbo"):
        assert ia.modelo_sugestoes_do_plano(p) == "claude-haiku-5-5"


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


def test_fallback_de_modelo_em_erro_da_api_cobra_modelo_que_respondeu(cliente):
    import pytest
    from nuvem import app as nucleo

    class _ClienteComFalha(_ClienteFalso):
        def __init__(self, falhar_modelos):
            super().__init__()
            self.falhar_modelos = set(falhar_modelos)

        @contextmanager
        def stream(self, **kwargs):
            if kwargs["model"] in self.falhar_modelos:
                self.pedidos.append(kwargs)
                raise RuntimeError("529 overloaded_error")
            with super().stream(**kwargs) as s:
                yield s

    # Pro (Sonnet) falha -> tenta Haiku uma vez, cobra preço do Haiku e registra incidente
    motor = ia.ClaudeIA.__new__(ia.ClaudeIA)
    motor.cliente = _ClienteComFalha(["claude-sonnet-5-5"])
    final = ia.Resposta()
    texto = "".join(motor.responder([{"role": "user", "content": "oi"}], final, "claude-sonnet-5-5"))
    assert texto == "Olá"
    assert [p["model"] for p in motor.cliente.pedidos] == ["claude-sonnet-5-5", "claude-haiku-5-5"]
    assert final.modelo == "claude-haiku-5-5"
    assert ia.custo_usd(final.modelo, 1_000_000, 0) == ia.PRECOS["claude-haiku-5-5"][0]
    ev = nucleo.db().execute("SELECT COUNT(*) AS n FROM eventos WHERE nome='falha:ia_fallback'").fetchone()
    assert ev["n"] == 1

    # Turbo (Opus) falha -> tenta Sonnet uma vez (e se Sonnet também falhar, não desce para Haiku)
    motor.cliente = _ClienteComFalha(["claude-opus-5-5", "claude-sonnet-5-5"])
    with pytest.raises(RuntimeError):
        list(motor.responder([{"role": "user", "content": "oi"}], ia.Resposta(), "claude-opus-5-5"))
    assert [p["model"] for p in motor.cliente.pedidos] == ["claude-opus-5-5", "claude-sonnet-5-5"]

    # Básico / Grátis (Haiku) falha -> sem fallback
    motor.cliente = _ClienteComFalha(["claude-haiku-5-5"])
    with pytest.raises(RuntimeError):
        list(motor.responder([{"role": "user", "content": "oi"}], ia.Resposta(), "claude-haiku-5-5"))
    assert [p["model"] for p in motor.cliente.pedidos] == ["claude-haiku-5-5"]


def test_custo_nunca_abaixo_do_preco():
    assert ia.custo_usd("modelo-desconhecido", 1_000_000, 0) == max(p[0] for p in ia.PRECOS.values())


def test_comparar_modelos_dry_run():
    from nuvem import comparar_modelos

    assert len(comparar_modelos.PEDIDOS) == 20
    assert comparar_modelos.main(["--dry-run"]) == 0
