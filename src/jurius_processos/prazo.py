"""Contagem do prazo de uma intimação — cópia fiel de `_shared/intimation-deadline.ts`.

Mesma regra do CRM (dias ÚTEIS pelo CPC, pulando fim de semana e feriado):
disponibilização → publicação no 1º dia útil seguinte → contagem começa no dia
útil seguinte à publicação → vencimento é o (dias-1)º dia útil depois do início.
Os testes deste arquivo são os casos do `src/utils/intimationDeadline.test.ts`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta


def dia_de_calendario(valor) -> str | None:
    if not valor:
        return None
    if isinstance(valor, (date, datetime)):
        return (valor.date() if isinstance(valor, datetime) else valor).isoformat()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(valor).strip())
    return m.group(1) if m else None


def eh_dia_util(dia: str, feriados=None) -> bool:
    d = date.fromisoformat(dia)
    return d.weekday() < 5 and not (feriados and dia in feriados)


def proximo_dia_util(dia: str, feriados=None) -> str:
    d = date.fromisoformat(dia) + timedelta(days=1)
    while not eh_dia_util(d.isoformat(), feriados):
        d += timedelta(days=1)
    return d.isoformat()


def somar_dias_uteis(dia: str, quantidade: int, feriados=None) -> str:
    for _ in range(max(0, int(quantidade))):
        dia = proximo_dia_util(dia, feriados)
    return dia


def contar_prazo_da_intimacao(disponibilizacao, dias, feriados=None) -> dict | None:
    base = dia_de_calendario(disponibilizacao)
    if not base or not dias or dias <= 0:
        return None
    publicacao = proximo_dia_util(base, feriados)
    inicio = proximo_dia_util(publicacao, feriados)
    vencimento = somar_dias_uteis(inicio, int(dias) - 1, feriados)
    return {"publicacao": publicacao, "inicio": inicio, "vencimento": vencimento}
