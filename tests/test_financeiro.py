"""Alvará do processo × recebimento lançado no Financeiro."""

from datetime import date

from jurius_processos.financeiro import cruzar, valor_do_alvara

HOJE = date(2026, 9, 26)


def _analise(alvaras, **extra):
    return {"alvaras": alvaras, "saude": "atencao", "arquivado_em": None,
            "pendencias": [{"tipo": "valor_a_levantar", "severidade": "alta", "descricao": "genérica"}], **extra}


def _acordo(*parcelas):
    return {"id": "a1", "parcelas": list(parcelas)}


def _tipos(a):
    return {p["tipo"]: p for p in a["pendencias"]}


def test_recebimento_lancado_depois_do_alvara_da_baixa():
    a = cruzar(_analise(["2026-09-01"]), [_acordo({"status": "pago", "payment_date": "2026-09-10", "paid_value": 3500})], [], HOJE)
    t = _tipos(a)
    assert "valor_a_levantar" not in t and "alvara_recebido" in t
    assert "3.500,00" in t["alvara_recebido"]["descricao"] and a["saude"] == "ok"


def test_recebimento_poucos_dias_antes_do_alvara_ainda_conta():
    a = cruzar(_analise(["2026-09-10"]), [_acordo({"status": "pago", "payment_date": "2026-09-05", "paid_value": 100})], [], HOJE)
    assert "alvara_recebido" in _tipos(a)


def test_pagamento_antigo_nao_quita_alvara_novo():
    a = cruzar(_analise(["2026-09-01"]), [_acordo({"status": "pago", "payment_date": "2026-03-01", "paid_value": 100},
                                                  {"status": "pendente", "payment_date": None, "value": 500})], [], HOJE)
    t = _tipos(a)
    assert t["valor_a_levantar"]["severidade"] == "alta"  # 25 dias sem lançamento
    assert "nenhum recebimento" in t["valor_a_levantar"]["descricao"]


def test_alvara_recente_sem_recebimento_e_media():
    a = cruzar(_analise(["2026-09-20"]), [_acordo()], [], HOJE)
    assert _tipos(a)["valor_a_levantar"]["severidade"] == "media"


def test_processo_sem_acordo_no_financeiro():
    a = cruzar(_analise(["2026-09-01"]), [], [], HOJE)
    assert _tipos(a)["alvara_sem_financeiro"]["severidade"] == "alta"


def test_processo_fora_do_crm_fica_como_esta():
    a = cruzar(_analise(["2026-09-01"]), None, [], HOJE)
    assert _tipos(a)["valor_a_levantar"]["descricao"] == "genérica"


def test_valor_divergente_entre_alvara_e_recebido():
    textos = ["Expeça-se alvará eletrônico em favor da parte autora no valor de R$ 10.000,00."]
    a = cruzar(_analise(["2026-09-01"]), [_acordo({"status": "pago", "payment_date": "2026-09-10", "paid_value": 7000})], textos, HOJE)
    assert "alvara_valor_divergente" in _tipos(a)
    b = cruzar(_analise(["2026-09-01"]), [_acordo({"status": "pago", "payment_date": "2026-09-10", "paid_value": 10000})], textos, HOJE)
    assert "alvara_valor_divergente" not in _tipos(b)


def test_valor_do_alvara_no_texto():
    assert valor_do_alvara(["R$ 1.234,56 a ser levantado por alvará"]) == 1234.56
    assert valor_do_alvara(["intimação da expedição do alvará eletrônico"]) is None


def test_sem_alvara_nao_mexe():
    a = {"alvaras": [], "pendencias": [{"tipo": "parado_demais"}]}
    assert cruzar(dict(a), [_acordo()], [], HOJE)["pendencias"] == a["pendencias"]
