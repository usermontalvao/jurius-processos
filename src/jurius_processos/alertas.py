"""Alertas de cadastro: o que o servidor achou numa intimação e o escritório não cadastrou.

Pedido do usuário (26/09/2026): a intimação chega; se em 24 h o prazo ou a
audiência dela não estiver no CRM, o administrador vê um triângulo ao lado do
sino. "Cadastrar" abre o modal já preenchido; "Ignorar" some para todos.

Tudo aqui é puro — `detectar` recebe o que o CRM tem e devolve os alertas que
valem AGORA. Quem grava, resolve e reabre é `alimentar.alertas`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

ESPERA = timedelta(hours=24)       # tempo para o escritório cadastrar antes do alerta
FOLGA_PRAZO_DIAS = 5               # prazo cadastrado com vencimento perto conta como o mesmo
FOLGA_AUDIENCIA_DIAS = 1
CUIABA = timedelta(hours=-4)


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _dia(v) -> date | None:
    """Data de calendário (vencimento, disponibilização): gravada como meia-noite
    UTC, vale o dia escrito — converter para Cuiabá a jogaria para a véspera."""
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _dia_local(v) -> date | None:
    """Horário de verdade (início do compromisso na agenda): o dia em Cuiabá."""
    d = _dt(v)
    return (d + CUIABA).date() if d else None


# Prazo que pede providência nossa. Ensaio de 26/09/2026 contra produção: 28
# alertas, metade ruído — "comparecer à audiência" (tem alerta próprio),
# "aguardar audiência", "sem prazo específico", "ação proposta", pauta com
# vencimento em 2031. Lido no resumo da IA da intimação.
_ACAO = re.compile(r"manifest|contrarraz|contraminuta|apresent|informar|junt|\bpagamento\b|\bpagar\b|c[áa]lculo|emend|"
                   r"comprov|recolh|impugn|especific|agendament|provid[êe]nci|dila[çc][ãa]o|recurso|embargos|"
                   r"per[íi]cia|prazo de \d+", re.IGNORECASE)
_SEM_ACAO = re.compile(
    r"sem prazo|aguardar|apenas (?:para )?ci[êe]ncia"
    # É prazo da OUTRA parte ou do perito (auditoria de 26/09/2026: Giancarlo, Gabriel).
    r"|pagamento (?:volunt[áa]rio )?d[oe] d[ée]bito|parte executada|\bdevedor|por perito|intima\w* o perito",
    re.IGNORECASE)
# Intimação de audiência: o alerta é o da AUDIÊNCIA (outra regra). "Apresentação
# de testemunhas" é na própria audiência (Carlos, 0000676-46).
_AUDIENCIA = re.compile(r"audi[êe]ncia", re.IGNORECASE)
_ACAO_FORTE = re.compile(r"manifest|contrarraz|contraminuta|c[áa]lculo|recurso|embargos|emend|impugn|recolh|informar|"
                         r"junt|\bpagar\b|agendament|dila[çc][ãa]o", re.IGNORECASE)
HORIZONTE_DIAS = 120


def pede_providencia(resumo: str | None) -> bool:
    t = resumo or ""
    if _AUDIENCIA.search(t) and not _ACAO_FORTE.search(t):
        return False
    return bool(_ACAO.search(t)) and not _SEM_ACAO.search(t)


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _prioridade(urgencia: str | None) -> str:
    return {"critica": "urgente", "alta": "alta", "media": "media", "baixa": "baixa"}.get(urgencia or "", "media")


def detectar(proc: dict, intimacoes: list[dict], prazos: list[dict], agenda: list[dict],
             audiencia: dict | None, agora: datetime) -> list[dict]:
    """proc: {id, client_id, codigo, cliente}. intimacoes: {id, chegou_em, data,
    vencimento, prazo_dias, resumo, urgencia}. prazos: {due_date, status,
    created_at}. agenda: audiências {quando, status}. audiencia: a da análise."""
    hoje = (agora + CUIABA).date()
    base = {"process_id": proc["id"], "client_id": proc.get("client_id"),
            "process_code": proc.get("codigo"), "client_name": proc.get("cliente")}
    prazos_validos = [p for p in prazos if p.get("status") != "cancelado"]
    out = []

    vistos: set[str] = set()
    for i in sorted(intimacoes, key=lambda x: str(x.get("chegou_em") or "")):
        venc, chegou = _dia(i.get("vencimento")), _dt(i.get("chegou_em"))
        if not venc or venc < hoje or (venc - hoje).days > HORIZONTE_DIAS or not chegou or agora - chegou < ESPERA:
            continue
        if not pede_providencia(i.get("resumo")):
            continue
        # A mesma intimação publicada duas vezes (ou duas do mesmo ato): um alerta
        # por processo e vencimento.
        if venc.isoformat() in vistos:
            continue
        vistos.add(venc.isoformat())
        coberto = any(_cobre(p, i, venc, chegou) for p in prazos_validos)
        if coberto:
            continue
        resumo = (i.get("resumo") or "Intimação").strip()
        titulo = f"Prazo: {resumo[:90]}"
        out.append({
            "chave": f"prazo:{proc['id']}:{venc.isoformat()}", "tipo": "prazo", "intimation_id": i["id"], **_ids(base),
            "titulo": titulo, "data": venc.isoformat(), "hora": None,
            "descricao": f"Intimação de {_br(_dia(i.get('data')) or venc)} com prazo de "
                         f"{i.get('prazo_dias') or '?'} dia(s), vence em {_br(venc)}. Nenhum prazo cadastrado no processo.",
            "dados": {**base, "title": resumo[:120], "due_date": venc.isoformat(), "priority": _prioridade(i.get("urgencia")),
                      "description": f"Intimação de {_br(_dia(i.get('data')) or venc)}: {resumo}"},
        })

    if audiencia and audiencia.get("data") and audiencia.get("fonte") != "agenda":
        quando = date.fromisoformat(audiencia["data"][:10])
        designada = _dia(audiencia.get("designada_em")) if audiencia.get("designada_em") else None
        ja_passou_o_prazo = designada is not None and (hoje - designada).days >= 1
        na_agenda = any((d := _dia_local(e.get("quando"))) and abs((d - quando).days) <= FOLGA_AUDIENCIA_DIAS
                        and "cancel" not in (e.get("status") or "") for e in agenda)
        if quando >= hoje and ja_passou_o_prazo and not na_agenda:
            tipo = audiencia.get("tipo") or "audiência"
            rotulo = "Audiência" if tipo == "audiência" else f"Audiência de {tipo}"
            hora = audiencia.get("hora")
            out.append({
                "chave": f"audiencia:{proc['id']}:{quando.isoformat()}", "tipo": "audiencia", "intimation_id": None,
                **_ids(base), "titulo": f"{rotulo} em {_br(quando)}{' às ' + hora if hora else ''}",
                "data": quando.isoformat(), "hora": hora,
                "descricao": f"{rotulo} designada ({_br(designada) if designada else 'intimação'}) e não está na agenda.",
                "dados": {**base, "title": f"{rotulo.upper()} - {(proc.get('cliente') or '').upper()}".strip(" -"),
                          "date": quando.isoformat(), "time": hora or "", "type": "hearing",
                          "description": f"{rotulo} designada no processo {proc.get('codigo') or ''}."},
            })
    return out


# Tipo de providência, para reconhecer o prazo cadastrado ANTES da intimação
# chegar pelo DJEN (o escritório vê no PJe primeiro). Caso real 26/09/2026:
# 0000114-55.2026.5.23.0003 tinha "CONTRARRAZÕES" criado em 31/08, vencido e
# cumprido em 10/09; a intimação chegou pelo DJEN em 03/09 e a IA contou 15
# dias (eram 8) — o alerta dizia "nenhum prazo cadastrado".
_TIPOS = {
    "contrarrazoes": r"contrarraz|contra-raz|contraminuta",
    # Intimação da sentença é prazo de recurso (Weverton, 0000184-09: "RECURSO
    # ORDINARIO" cadastrado no dia em que a sentença chegou).
    "recurso": r"recurso|apela|agravo|\bro\b|senten[çc]a",
    "embargos": r"embargo",
    "manifestacao": r"manifest|peti[çc][ãa]o",
    # Impugnação/réplica é ato próprio: não responde a qualquer intimação
    # (Edilza, 1017719-66: "IMPUGNAÇÃO" não é o agendamento da perícia).
    "impugnacao": r"r[ée]plica|impugn",
    "calculos": r"c[áa]lculo|liquida",
    "pericia": r"per[íi]cia|laudo|quesito",
    "pagamento": r"\bpagamento\b|\bpagar\b|dep[óo]sito|custas|preparo",
    "emenda": r"emend",
    "endereco": r"endere[çc]o",
}
JANELA_ANTES_DIAS = 20


def _tipos(texto: str | None) -> set[str]:
    t = (texto or "").lower()
    return {k for k, rx in _TIPOS.items() if re.search(rx, t)}


def _cobre(p: dict, i: dict, venc: date, chegou: datetime) -> bool:
    """Este prazo cadastrado é o da intimação?"""
    if p.get("intimation_id") and p["intimation_id"] == i.get("id"):
        return True
    d = _dia(p.get("due_date"))
    if d and abs((d - venc).days) <= FOLGA_PRAZO_DIAS:
        return True
    criado = _dt(p.get("created_at"))
    if not criado or criado < chegou - timedelta(days=JANELA_ANTES_DIAS):
        return False
    tipos_prazo = _tipos(f"{p.get('title') or ''} {p.get('description') or ''}")
    tipos_intimacao = _tipos(i.get("resumo"))
    # Mesmo tipo de providência, cadastrado perto da intimação (antes ou depois).
    if tipos_prazo & tipos_intimacao:
        return True
    # Prazo genérico ("MANIFESTAÇÃO", "PETIÇÃO", sem tipo) cadastrado quando a
    # intimação chegou é a resposta a ela (Paulo Fabiano, 0000882-66: "informar
    # endereço" chegou em 18/09 e virou "MANIFESTAÇÃO" no mesmo dia).
    generico = not tipos_prazo or tipos_prazo <= {"manifestacao"}
    return generico and criado >= chegou - timedelta(days=1)


def _ids(base: dict) -> dict:
    return {"process_id": base["process_id"], "client_id": base.get("client_id")}
