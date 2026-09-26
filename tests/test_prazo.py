"""Os mesmos casos do src/utils/intimationDeadline.test.ts do CRM."""

from jurius_processos.prazo import contar_prazo_da_intimacao, eh_dia_util, proximo_dia_util

FERIADOS = {"2026-09-07", "2026-10-12", "2026-11-02", "2026-11-15"}


def test_publicacao_no_dia_util_seguinte():
    assert proximo_dia_util("2026-08-17") == "2026-08-18"
    assert proximo_dia_util("2026-08-21") == "2026-08-24"
    assert proximo_dia_util("2026-09-04", FERIADOS) == "2026-09-08"


def test_caso_de_referencia_15_dias():
    assert contar_prazo_da_intimacao("2026-08-17T00:00:00+00", 15, FERIADOS) == {
        "publicacao": "2026-08-18", "inicio": "2026-08-19", "vencimento": "2026-09-09"}


def test_prazo_de_um_dia_vence_no_inicio():
    c = contar_prazo_da_intimacao("2026-08-17", 1)
    assert c["vencimento"] == c["inicio"] == "2026-08-19"


def test_feriado_adia_um_dia_util():
    assert contar_prazo_da_intimacao("2026-08-31", 10)["vencimento"] == "2026-09-15"
    assert contar_prazo_da_intimacao("2026-08-31", 10, FERIADOS)["vencimento"] == "2026-09-16"


def test_entradas_invalidas():
    assert contar_prazo_da_intimacao(None, 15) is None
    assert contar_prazo_da_intimacao("2026-08-17", None) is None
    assert contar_prazo_da_intimacao("2026-08-17", 0) is None
    assert contar_prazo_da_intimacao("data ruim", 15) is None


def test_vencimento_sempre_em_dia_util():
    from datetime import date, timedelta
    d = date(2026, 8, 1)
    for _ in range(60):
        for dias in (1, 5, 10, 15, 30):
            c = contar_prazo_da_intimacao(d.isoformat(), dias, FERIADOS)
            assert eh_dia_util(c["vencimento"], FERIADOS)
        d += timedelta(days=1)
