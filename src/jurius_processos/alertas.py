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

from .fases import _norm, _tipo_audiencia, audiencia_no_texto, hora_da_audiencia

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


# Intimação "para ciência" só é prazo nosso com ação concreta. Caso Eduarda
# (0001000-28, 26/09/2026): "tomar ciência" de despacho que manda a CAIXA
# recolher o FGTS; o prazo nosso só vem depois, em outra intimação ("comprovada
# a transferência, intime-se a exequente"). A IA resumiu "prazo de 5 dias para
# manifestação" — manifestação genérica não basta.
_CIENCIA = re.compile(r"ci[êe]ncia", re.IGNORECASE)
# "recolher/recolha" (nós, custas) — não "recolhimento", que é o que o banco faz
# no ofício.
_ACAO_PROPRIA = re.compile(r"contrarraz|contraminuta|c[áa]lculo|recurso|embargos|emend|impugn|\brecolh(?:er|a)\b|informar|junt|"
                           r"comprov|\bpagar\b|agendament|dila[çc][ãa]o|especific|apresent", re.IGNORECASE)


# Menção NEGADA de um ato não é providência: "declarou preclusos eventuais
# embargos", "sem interposição de recurso". Caso Igor (1035374-69, 01/10/2026):
# a palavra "embargos" desse trecho fazia a sentença de extinção virar prazo.
_NEGADO = re.compile(
    r"preclus\w*\s+(?:\w+\s+){0,2}(?:embargos|recursos?|impugna\w*)"
    r"|sem\s+(?:a\s+)?(?:interposi\w+\s+de\s+|oposi\w+\s+de\s+)?(?:recursos?|embargos|impugna\w*)"
    r"|(?:embargos|recursos?)\s+(?:n[ãa]o\s+)?(?:foram\s+|foi\s+)?(?:opostos|interpostos?)\s+no\s+prazo",
    re.IGNORECASE)
# Encerramento FAVORÁVEL: a execução acabou porque o devedor pagou tudo. Não há
# prazo nosso — o levantamento sai por alvará e o processo vai ao arquivo.
_ENCERRAMENTO_PAGO = re.compile(
    r"(?:extint\w*|extin[çc][ãa]o)\b.{0,80}?(?:cumprimento\s+integral|satisfa[çc][ãa]o|pagamento\s+integral|quita[çc][ãa]o)"
    r"|(?:cumprimento\s+integral|satisfa[çc][ãa]o\s+(?:integral\s+)?da\s+obriga[çc][ãa]o|obriga[çc][ãa]o\s+(?:foi\s+)?integralmente\s+cumprida)"
    r".{0,120}?(?:extint\w*|extin[çc][ãa]o|arquiv)",
    re.IGNORECASE | re.DOTALL)
# Intimação sobre DINHEIRO do processo: se já há lançamento no Financeiro, quem
# cuida dela é o Financeiro (pedido do usuário, caso Igor).
_SOBRE_DINHEIRO = re.compile(
    r"alvar[áa]|levantament|dep[óo]sito|\bpagamento|\bpago\b|cumprimento\s+integral|extin[çc][ãa]o|extint|quita[çc]|"
    r"satisfa[çc][ãa]o|valores?\s+(?:depositad|bloquead)|transfer[êe]ncia\s+de\s+valores|rpv|precat[óo]rio",
    re.IGNORECASE)


def pede_providencia(resumo: str | None) -> bool:
    t = _NEGADO.sub(" ", resumo or "")
    if _ENCERRAMENTO_PAGO.search(t):
        return False
    if _AUDIENCIA.search(t) and not _ACAO_FORTE.search(t):
        return False
    if _CIENCIA.search(t) and not _ACAO_PROPRIA.search(t):
        return False
    return bool(_ACAO.search(t)) and not _SEM_ACAO.search(t)


def _cortar(texto: str, limite: int) -> str:
    """Corta em palavra inteira, com reticências — "…declarou preclusos" no
    meio da frase era o título do alerta."""
    t = " ".join((texto or "").split())
    if len(t) <= limite:
        return t
    corte = t[:limite].rsplit(" ", 1)[0].rstrip(",.;:—-")
    return f"{corte}…"


# O NOME do prazo, como o escritório cadastra ("CONTRARRAZÕES", "MANIFESTAÇÃO").
# O título do prazo pré-preenchido era o resumo inteiro da IA. A ordem importa:
# o mais específico primeiro ("manifestar sobre a contestação" é réplica).
_NOMES = [
    ("impugnacao", "Impugnação à contestação"),
    ("contrarrazoes", "Contrarrazões"),
    ("embargos", "Embargos"),
    ("calculos", "Cálculos"),
    ("emenda", "Emenda à inicial"),
    ("pericia", "Perícia"),
    ("endereco", "Informar endereço"),
    ("pagamento", "Pagamento / custas"),
]


def titulo_do_prazo(resumo: str | None) -> str | None:
    t = _NEGADO.sub(" ", resumo or "")
    tipos = _tipos(t)
    for chave, nome in _NOMES:
        if chave in tipos:
            return nome
    if re.search(r"\brecurso\b|apela|agravo", t, re.IGNORECASE) and re.search(r"interpor|recorrer|prazo\s+(?:para|de)\s+recurso", t, re.IGNORECASE):
        return "Recurso"
    if "manifestacao" in tipos or re.search(r"manifest", t, re.IGNORECASE):
        return "Manifestação"
    return None


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _prioridade(urgencia: str | None) -> str:
    return {"critica": "urgente", "alta": "alta", "media": "media", "baixa": "baixa"}.get(urgencia or "", "media")


def detectar(proc: dict, intimacoes: list[dict], prazos: list[dict], agenda: list[dict],
             audiencia: dict | None, agora: datetime, financeiro: list[dict] | None = None) -> list[dict]:
    """proc: {id, client_id, codigo, cliente}. intimacoes: {id, chegou_em, data,
    vencimento, prazo_dias, resumo, urgencia}. prazos: {due_date, status,
    created_at}. agenda: audiências {quando, status}. audiencia: a da análise.
    financeiro: lançamentos (agreements) do processo {status, created_at}."""
    tem_lancamento = any((f.get("status") or "") != "cancelado" for f in (financeiro or []))
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
        if tem_lancamento and _SOBRE_DINHEIRO.search(i.get("resumo") or ""):
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
        nome_do_prazo = titulo_do_prazo(resumo)
        titulo = f"Prazo: {nome_do_prazo}" if nome_do_prazo else f"Prazo: {_cortar(resumo, 90)}"
        out.append({
            "chave": f"prazo:{proc['id']}:{venc.isoformat()}", "tipo": "prazo", "intimation_id": i["id"], **_ids(base),
            "titulo": titulo, "data": venc.isoformat(), "hora": None,
            "descricao": f"Intimação de {_br(_dia(i.get('data')) or venc)} com prazo de "
                         f"{i.get('prazo_dias') or '?'} dia(s), vence em {_br(venc)}. Nenhum prazo cadastrado no processo.",
            "dados": {**base, "title": (nome_do_prazo or _cortar(resumo, 120)).upper(), "due_date": venc.isoformat(),
                      "priority": _prioridade(i.get("urgencia")),
                      "description": f"Intimação de {_br(_dia(i.get('data')) or venc)}: {resumo}"},
        })

    # Audiência: a de CADA intimação que designa uma (não só a mais recente da
    # análise) é conferida na AGENDA — é compromisso, não prazo (pedido do
    # usuário, caso Carlos 0000676-46). Mais a da análise (DataJud/DJEN).
    # A data que vale é a da intimação MAIS RECENTE: a que redesigna, antecipa ou
    # adia tira a validade das anteriores. Caso Joanil (0000607-14, 01/10/2026):
    # instrução de 03/11 (intimação de 31/08) antecipada para 30/09 (de 08/09);
    # enquanto 30/09 estava por vir, a agenda escondia o alerta, e no dia
    # seguinte o 03/11 voltou como "não está na agenda". A redesignação vale
    # mesmo chegada há menos de 24 h e mesmo com a nova data já passada.
    todas: list[dict] = []
    for i in intimacoes:
        achada = audiencia_no_texto(i.get("texto"))
        if achada and achada[1]:
            dia = achada[1].date().isoformat()
            chegou = _dt(i.get("chegou_em"))
            todas.append({"tipo": achada[0], "data": dia, "hora": hora_da_audiencia(i.get("texto"), dia),
                          "designada_em": (i.get("data") or "")[:10] or None, "fonte": "djen",
                          "_ordem": ((i.get("data") or "")[:10], str(i.get("chegou_em") or "")),
                          "_madura": chegou is not None and agora - chegou >= ESPERA})
    candidatas = [a for a in todas if a["_madura"]]
    if audiencia and audiencia.get("data") and audiencia.get("fonte") != "agenda":
        candidatas.append({**audiencia, "_ordem": ((audiencia.get("designada_em") or "")[:10], "")})
    ja_avisadas: set[str] = set()
    for aud in candidatas:
        if _redesignada(aud, todas):
            continue
        _alerta_de_audiencia(proc, base, {k: v for k, v in aud.items() if not k.startswith("_")},
                             agenda, hoje, ja_avisadas, out)
    return out


def _redesignada(aud: dict, todas: list[dict]) -> bool:
    """Uma intimação posterior marca audiência do mesmo tipo em outra data."""
    tipo = _tipo_audiencia(_norm(aud.get("tipo")))
    for outra in todas:
        if outra["_ordem"] <= aud["_ordem"] or outra["data"] == aud["data"][:10]:
            continue
        tipo_outra = _tipo_audiencia(_norm(outra.get("tipo")))
        if tipo_outra == tipo or "audiência" in (tipo_outra, tipo):
            return True
    return False


def _alerta_de_audiencia(proc: dict, base: dict, audiencia: dict, agenda: list[dict], hoje: date,
                         ja_avisadas: set[str], out: list[dict]) -> None:
    if audiencia["data"][:10] in ja_avisadas:
        return
    quando = date.fromisoformat(audiencia["data"][:10])
    designada = _dia(audiencia.get("designada_em")) if audiencia.get("designada_em") else None
    ja_passou_o_prazo = designada is not None and (hoje - designada).days >= 1
    tipo_aud = _tipo_audiencia(_norm(audiencia.get("tipo")))
    na_agenda = False
    for e in agenda:
        d = _dia_local(e.get("quando"))
        if not d or "cancel" in (e.get("status") or ""):
            continue
        if abs((d - quando).days) <= FOLGA_AUDIENCIA_DIAS:
            na_agenda = True
            break
        # Redesignada: a agenda já tem a PRÓXIMA audiência desse tipo em outra
        # data (auditoria de 26/09/2026: Carlos Daniel 29/09→27/10, Paulo
        # Fabiano 01/10→29/10, Joanil 03/11 antecipada para 30/09). Título
        # genérico ("Audiência Online — X") vale para qualquer tipo.
        tipo_ag = _tipo_audiencia(_norm(e.get("titulo")))
        if d >= hoje and (tipo_ag == tipo_aud or "audiência" in (tipo_ag, tipo_aud)):
            na_agenda = True
            break
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
        ja_avisadas.add(quando.isoformat())


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


# "Manifestar sobre a contestação" É a réplica/impugnação — não uma manifestação
# qualquer. Caso Manoel (1034205-29, 30/09/2026): "IMPUGNAÇÃO" criada no PJe em
# 28/09 e cumprida em 29/09; a intimação do DJEN chegou em 29/09 pedindo
# "manifestar sobre a contestação" e o alerta dizia "nenhum prazo cadastrado".
_REPLICA = re.compile(r"(?:manifest|fal)\w*\s+(?:\w+\s+){0,4}(?:sobre|acerca|quanto)\s+(?:[aà]s?\s+|d[aeo]s?\s+)?contesta", re.IGNORECASE)


def _tipos(texto: str | None) -> set[str]:
    t = (texto or "").lower()
    tipos = {k for k, rx in _TIPOS.items() if re.search(rx, t)}
    if _REPLICA.search(t):
        tipos.add("impugnacao")
    return tipos


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
