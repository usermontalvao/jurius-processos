"""Alvará do processo × recebimento lançado no Financeiro do CRM. Função pura.

O processo diz QUANDO o dinheiro saiu (alvará/RPV expedido — DataJud ou DJEN);
o Financeiro diz se o escritório RECEBEU (parcela paga, com data e valor).
Cruzar os dois responde "tem dinheiro parado?" sem ninguém abrir processo:

  - alvará expedido e nenhum recebimento lançado depois → valor a levantar;
  - alvará expedido e o processo sem nenhum acordo no Financeiro → cadastrar;
  - recebimento lançado → a pendência cai e vira registro de conferência;
  - valor do alvará (quando a intimação traz) diferente do recebido → conferir.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Recebimento lançado até alguns dias ANTES da expedição ainda conta: a data do
# alvará no DataJud/DJEN costuma chegar depois do dia em que o valor saiu.
TOLERANCIA_ANTES = timedelta(days=7)
PRAZO_ALTA = 15  # dias sem recebimento lançado para virar pendência alta
DIVERGENCIA = 0.05  # 5% de diferença entre alvará e recebido

_VALOR_ALVARA = re.compile(
    r"alvar[áa][^.]{0,160}?r\$\s*(?P<v>\d{1,3}(?:\.\d{3})*,\d{2})|r\$\s*(?P<w>\d{1,3}(?:\.\d{3})*,\d{2})[^.]{0,80}?alvar[áa]",
    re.I)


def valor_em_reais(txt: str) -> float:
    return float(txt.replace(".", "").replace(",", "."))


def valor_do_alvara(textos: list[str]) -> float | None:
    """Maior valor citado junto de "alvará" nas intimações (None se nenhuma cita)."""
    achados = []
    for t in textos:
        for m in _VALOR_ALVARA.finditer(t or ""):
            achados.append(valor_em_reais(m.group("v") or m.group("w")))
    return max(achados) if achados else None


def _brl(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dmy(d: str) -> str:
    a, m, dia = d[:10].split("-")
    return f"{dia}/{m}/{a}"


def cruzar(analise: dict, acordos: list[dict] | None, textos_alvara: list[str], hoje: date) -> dict:
    """Reescreve a pendência de alvará de `analise` à luz do Financeiro.

    `acordos`: acordos do processo no CRM, cada um com `parcelas` (status,
    payment_date, paid_value, value). None = processo fora do CRM (não há
    Financeiro para conferir: fica a pendência genérica do motor).
    """
    alvaras = [a for a in analise.get("alvaras") or [] if a]
    if not alvaras or acordos is None:
        return analise
    ultimo = max(alvaras)
    d_alvara = date.fromisoformat(ultimo)
    if (hoje - d_alvara).days > 365:
        return analise
    arquivado_em = analise.get("arquivado_em")
    if arquivado_em and ultimo <= arquivado_em:
        return analise

    pend = [p for p in analise.get("pendencias") or [] if p.get("tipo") != "valor_a_levantar"]
    recebidos = [
        p for a in acordos for p in a.get("parcelas") or []
        if p.get("status") == "pago" and p.get("payment_date")
        and date.fromisoformat(p["payment_date"][:10]) >= d_alvara - TOLERANCIA_ANTES
    ]
    dias = (hoje - d_alvara).days

    if recebidos:
        total = sum(float(p.get("paid_value") or p.get("value") or 0) for p in recebidos)
        quando = max(p["payment_date"][:10] for p in recebidos)
        pend.append({"tipo": "alvara_recebido", "severidade": "info",
                     "descricao": f"Alvará de {_dmy(ultimo)} conferido: {_brl(total)} lançado no Financeiro em {_dmy(quando)}",
                     "desde": quando})
        esperado = valor_do_alvara(textos_alvara)
        if esperado and total and abs(total - esperado) / esperado > DIVERGENCIA:
            pend.append({"tipo": "alvara_valor_divergente", "severidade": "media",
                         "descricao": f"Alvará de {_brl(esperado)} e recebido {_brl(total)} no Financeiro: conferir",
                         "desde": quando})
    elif not acordos:
        pend.append({"tipo": "alvara_sem_financeiro", "severidade": "alta",
                     "descricao": f"Alvará expedido em {_dmy(ultimo)} e o processo não tem lançamento no Financeiro",
                     "desde": ultimo})
    else:
        pend.append({"tipo": "valor_a_levantar", "severidade": "alta" if dias >= PRAZO_ALTA else "media",
                     "descricao": f"Alvará expedido em {_dmy(ultimo)} ({dias} dias) e nenhum recebimento lançado no Financeiro",
                     "desde": ultimo})

    sev = {p["severidade"] for p in pend}
    if analise.get("saude") != "sem_dados":
        analise["saude"] = "critico" if "critica" in sev else ("atencao" if sev & {"alta", "media"} else "ok")
    analise["pendencias"] = pend
    return analise
