"""Motor de fase: andamentos → fase, situação, pendências. Função pura, sem rede.

Decide pelo CÓDIGO nacional do movimento (Tabela Processual Unificada do CNJ),
não por palavra solta. O motor antigo procurava "sentença" no nome e errava
em "Remessa" com complemento "por julgamento definitivo do recurso", por
exemplo. Os códigos abaixo foram tirados dos 9.892 andamentos que já estavam
no CRM em 24/09/2026, e o nome só entra como plano B quando o código é nulo.

Quando o DataJud não conhece o processo (segredo de justiça, atraso, tribunal
fora do ar), a fase sai da CLASSE que o DJEN informa na última intimação.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

# ── tabelas de códigos (TPU) ────────────────────────────────────────────────
DISTRIBUICAO = {26, 36}
AUDIENCIA = {970, 12740, 12749, 12624, 12747, 12750, 12751, 12752}
AUDIENCIA_INSTRUCAO = {12749, 12750}
PERICIA = {12306, 14904}
# Julgamento que encerra a instância: em 1º grau é sentença, em 2º é acórdão.
JULGAMENTO = {
    219, 220, 221, 442, 454, 458, 459, 460, 461, 462, 463, 464, 465, 466, 467,
    468, 469, 470, 471, 472, 473, 11376, 12187, 12259, 12325, 12649,
}
JULGAMENTO_FAVORAVEL = {219, 221, 442, 466, 12187, 12649}
RECURSO_JULGADO = {230, 235, 237, 238, 239, 240, 241, 242, 12253, 944}
RECURSO_PROVIDO = {237, 238}
TRANSITO = {848}
CUMPRIMENTO = {11384, 11385}
EVOLUCAO_CLASSE = {14739}
HOMOLOGACAO_ACORDO_EXECUCAO = {14099, 277}
EXTINCAO_EXECUCAO = {196}
ARQUIVAMENTO = {246}
ARQUIVAMENTO_PROVISORIO = {245}
BAIXA = {22}
DESARQUIVAMENTO = {893, 849}
SUSPENSAO = {25, 265, 272, 898, 11975, 12098, 12099, 12100}
FIM_SUSPENSAO = {12066}
ALVARA = {12548}
# Movimentos de rotina: não provam que o processo "andou" de verdade.
ROTINA = {85, 92, 1061, 1051, 51, 581, 11010, 11383, 60, 132, 123, 14736, 14737, 12265, 12282,
          12287, 12266, 12288, 15101, 12215, 12315, 981, 982}
# Decisões que só existem depois de a ação estar em curso (fase de conhecimento).
DECISOES_INICIAIS = {12164, 785, 792, 332, 339, 787, 334, 12185, 15085, 12261}

# Classes (código TPU da classe) que já são fase de cumprimento/execução.
CLASSES_CUMPRIMENTO = {156, 157, 12078, 229, 15215, 15160}
CLASSES_EXECUCAO = {159, 1116, 12154, 1111, 994}
GRAUS_RECURSAIS = {"G2", "TR", "SUP", "TRU", "TNU"}

# Frases de dispositivo lidas no texto do DJEN (texto já em minúsculas).
_EXTINTA_EXECUCAO = re.compile(
    r"extint[ao]\s+(a|à)\s+(presente\s+)?execu|extingo\s+a\s+(presente\s+)?execu"
    # Extinção pelo pagamento dita de outro jeito (caso 1028965-77, 16/09/2026):
    # "satisfeita a execução... art. 924, II... DECLARO EXTINTO O PROCESSO".
    # "Declaro extinto o processo" sozinho NÃO entra: é também o art. 485.
    r"|satisfeita\s+a\s+(obriga|execu)|\b924\s*,?\s*(inciso\s+)?ii\b|extin[çc][ãa]o\s+do\s+(processo|feito)\s+pelo\s+pagamento")
# Início do cumprimento de sentença lido no DJEN: a intimação do art. 523 chega
# semanas antes da evolução de classe no DataJud.
_INICIO_CUMPRIMENTO = re.compile(
    r"pagamento\s+volunt[áa]rio|\bart(igo|\.)?\s*523\b|impugna[çc][ãa]o\s+ao\s+cumprimento\s+de\s+senten"
    # Liquidação é o começo do cumprimento (Gabriel 0000284-09: "elaboração de
    # cálculos de liquidação" com o DataJud parado em junho).
    r"|c[áa]lculos\s+de\s+liquida|liquida[çc][ãa]o\s+de\s+senten|fase\s+de\s+liquida")
_CIENCIA_SENTENCA = re.compile(r"ci[êe]ncia\s+d[ao]\s+senten[çc]a")
_ALVARA_TEXTO = re.compile(r"expedi[çc][ãa]o\s+d[oe]\s+alvar[áa]|alvar[áa]\s+eletr[ôo]nico|requisi[çc][ãa]o\s+de\s+pequeno\s+valor"
                           r"|expe[çc]o\s+(o\s+)?(competente\s+)?alvar[áa]|alvar[áa]\s+finalizado")
_HOMOLOGO_ACORDO = re.compile(r"homologo\s+o\s+acordo|homologo,?\s+por\s+senten")
_JULGO = re.compile(r"julgo\s+(parcialmente\s+)?(im)?procedente")
# Formatos reais: "designo audiência de instrução ... para o dia 26/07/2023" (TJMT),
# "DADOS DA AUDIÊNCIA: ... Data: 27/10/2026" (TJMT), "pauta de audiências
# INICIAIS do dia 29/10/2026" e "a realizar-se no dia 05/11/2026" (TRT23).
_AUDIENCIA_COM_DATA = re.compile(
    r"audi[êe]ncias?[^.]{0,200}?(?:data:\s*|dia\s+(?:de\s+)?|para\s+(?:o\s+dia\s+)?)(?P<data>\d{1,2}/\d{1,2}/\d{4})")
_AUDIENCIA_SEM_DATA = re.compile(
    r"aguarde-se\s+(?:a\s+)?audi[êe]ncia\s+de\s+(?:concilia|instru)\w*\s+(?:j[áa]\s+)?designada")


_HORA_DEPOIS_DA_DATA = r"{data}[^.;]{{0,60}}?(?:[àa]s|,|hor[áa]rio:?)\s*(\d{{1,2}})\s*(?:h|:|horas?)\s*(\d{{2}})?"


def hora_da_audiencia(texto: str | None, data: str | None) -> str | None:
    """"05/11/2026, às 09h" → "09:00"; "20/08/2026, às 08:25 horas" → "08:25".
    data em ISO (2026-11-05); a hora só vale se vier logo depois dessa data."""
    if not texto or not data:
        return None
    t = re.sub(r"\s+", " ", texto.lower())
    a, m, d = data[:10].split("-")
    for fmt in (f"{d}/{m}/{a}", f"{int(d)}/{int(m)}/{a}"):
        if r := re.search(_HORA_DEPOIS_DA_DATA.format(data=re.escape(fmt)), t):
            h, mi = int(r.group(1)), int(r.group(2) or 0)
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
    return None


def audiencia_no_texto(texto: str) -> tuple[str, datetime | None] | None:
    """(tipo, data) da audiência mais adiante citada no texto, ou None."""
    t = re.sub(r"\s+", " ", (texto or "").lower())
    melhor = None
    for m in _AUDIENCIA_COM_DATA.finditer(t):
        trecho = m.group(0)
        if "realizada em" in trecho or "ocorrida" in trecho:
            continue
        try:
            data = datetime.strptime(m.group("data"), "%d/%m/%Y")
        except ValueError:
            continue
        if not melhor or data > melhor[1]:
            melhor = (_tipo_audiencia(trecho), data)
    if melhor:
        return melhor
    m = _AUDIENCIA_SEM_DATA.search(t)
    return (_tipo_audiencia(m.group(0)), None) if m else None


def _tipo_audiencia(trecho: str) -> str:
    if "instru" in trecho or re.search(r"\buna\b", trecho):
        return "instrução"
    if "concilia" in trecho or "inicia" in trecho:  # "inicial" no TRT é a de conciliação
        return "conciliação"
    return "audiência"

ORDEM = ["desconhecida", "distribuicao", "conhecimento", "instrucao", "sentenciado",
         "recursal", "transitado", "cumprimento_sentenca", "execucao"]

# Vocabulário que a coluna processes.status do CRM aceita hoje.
PARA_STATUS_CRM = {
    "distribuicao": "distribuido",
    "conhecimento": "andamento",
    "instrucao": "instrucao",
    "sentenciado": "sentenca",
    "recursal": "recurso",
    "transitado": "sentenca",
    # "aguardando_sentenca" não vem de fase: é refinamento de conhecimento/instrução (ver analisar).
    "cumprimento_sentenca": "cumprimento",
    "execucao": "cumprimento",
    "desconhecida": None,
}


def _norm(s: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


def _data(v) -> datetime | None:
    if not v:
        return None
    v = str(v)
    try:
        if re.fullmatch(r"\d{14}", v):
            return datetime.strptime(v, "%Y%m%d%H%M%S")
        if re.fullmatch(r"\d{8}", v):
            return datetime.strptime(v, "%Y%m%d")
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _dia(d: datetime | None) -> str | None:
    return d.date().isoformat() if d and d != datetime.min else None


def _complemento(mov: dict, descricao: str) -> tuple[str | None, int | None]:
    for c in mov.get("complementosTabelados") or []:
        if c.get("descricao") == descricao:
            return c.get("nome"), c.get("valor")
    return None, None


def _movimentos(instancias: list[dict]) -> list[dict]:
    """Andamentos de todas as instâncias numa linha do tempo única."""
    out = []
    for inst in instancias:
        grau = inst.get("grau")
        for m in inst.get("movimentos") or []:
            quando = _data(m.get("dataHora"))
            if not quando:
                continue
            out.append({"quando": quando, "codigo": m.get("codigo"), "nome": m.get("nome") or "",
                        "grau": grau, "bruto": m})
    out.sort(key=lambda m: (m["quando"], m["codigo"] or 0))
    return out


def fase_por_classe(classe_codigo: int | None, classe_nome: str | None) -> str | None:
    n = _norm(classe_nome)
    if classe_codigo in CLASSES_EXECUCAO or ("execucao" in n and "sentenca" not in n and "embargos" not in n):
        return "execucao"
    if classe_codigo in CLASSES_CUMPRIMENTO or "cumprimento" in n or "liquidacao" in n:
        return "cumprimento_sentenca"
    if any(k in n for k in ("recurso", "apelacao", "agravo", "remessa necessaria")):
        return "recursal"
    return None


def _agenda_audiencias(agenda: list[dict] | None) -> list[tuple[datetime, str, datetime | None]]:
    """(quando, título normalizado, lançada_em) das audiências da agenda do CRM, em ordem; sem as canceladas."""
    out = []
    for e in agenda or []:
        q = _data(e.get("quando"))
        if q and "cancel" not in _norm(e.get("status")):
            out.append((q, _norm(e.get("titulo")), _data(e.get("criado_em"))))
    return sorted(out, key=lambda x: x[0])


def analisar(numero: str, instancias: list[dict], comunicacoes: list[dict], hoje: date,
             agenda: list[dict] | None = None, prazos: list[dict] | None = None) -> dict:
    """agenda: audiências da agenda do CRM ({quando, titulo, status}). Depois da
    audiência inicial, o DataJud e o DJEN do TRT costumam calar por meses — a
    instrução marcada na audiência só existe na agenda do escritório."""
    movs = _movimentos(instancias)
    marcos: list[dict] = []
    alcancou: dict[str, datetime] = {}  # fase → quando chegou nela pela 1ª vez

    def chegou(fase: str, quando: datetime):
        if fase not in alcancou or quando < alcancou[fase]:
            alcancou[fase] = quando

    ultimo_arquivo = ultimo_desarquivo = arquivo_provisorio = None
    # Último ato que só acontece em processo VIVO: julgamento, recurso novo
    # distribuído, pauta, audiência, decisão. Depois de um arquivamento, ele
    # reabre o processo mesmo sem o código de desarquivamento — o laboratório
    # de 26/09/2026 achou 37 arquivados com recurso distribuído e julgado
    # depois. Petição e ato ordinatório NÃO contam: chegam em processo
    # arquivado o tempo todo (pedido de alvará, de certidão).
    ultimo_ato_vivo = None
    ultima_suspensao = ultimo_fim_suspensao = None
    transito = cumprimento_inicio = extincao_execucao = ultimo_substantivo = None
    conclusao_julgamento = None
    julgamentos_favoraveis: list[datetime] = []
    alvaras: list[datetime] = []
    audiencias: list[dict] = []

    if movs:
        chegou("distribuicao", movs[0]["quando"])

    for m in movs:
        c, q, grau, nome_n = m["codigo"], m["quando"], m["grau"], _norm(m["nome"])
        recursal = grau in GRAUS_RECURSAIS

        if (c in JULGAMENTO or c in RECURSO_JULGADO or c in AUDIENCIA or c in DECISOES_INICIAIS
                or (c in DISTRIBUICAO and recursal) or "inclusao em pauta" in nome_n
                or "pauta virtual" in nome_n or "julgamento de merito" in nome_n):
            ultimo_ato_vivo = q

        if c in DISTRIBUICAO:
            chegou("distribuicao", q)
        elif c in AUDIENCIA or nome_n.startswith("audiencia"):
            situacao_aud, _ = _complemento(m["bruto"], "situacao_da_audiencia")
            tipo, _ = _complemento(m["bruto"], "tipo_de_audiencia")
            tipo = tipo or m["nome"]
            audiencias.append({"quando": q, "tipo": tipo, "situacao": situacao_aud})
            if c in AUDIENCIA_INSTRUCAO or "instrucao" in _norm(tipo):
                chegou("instrucao", q)
            else:
                chegou("conhecimento", q)
        elif c in PERICIA:
            chegou("instrucao", q)
        elif c in JULGAMENTO and not recursal:
            chegou("sentenciado", q)
            marcos.append({"quando": q, "marco": "sentenca", "nome": m["nome"], "grau": grau})
            if c in JULGAMENTO_FAVORAVEL:
                julgamentos_favoraveis.append(q)
        elif c in RECURSO_JULGADO or (c in JULGAMENTO and recursal):
            chegou("recursal", q)
            marcos.append({"quando": q, "marco": "acordao", "nome": m["nome"], "grau": grau})
            if c in RECURSO_PROVIDO or c in JULGAMENTO_FAVORAVEL:
                julgamentos_favoraveis.append(q)
        elif c in TRANSITO:
            transito = q
            chegou("transitado", q)
            marcos.append({"quando": q, "marco": "transito", "nome": m["nome"], "grau": grau})
        elif c in CUMPRIMENTO or c in HOMOLOGACAO_ACORDO_EXECUCAO:
            cumprimento_inicio = cumprimento_inicio or q
            chegou("cumprimento_sentenca", q)
            marcos.append({"quando": q, "marco": "cumprimento", "nome": m["nome"], "grau": grau})
        elif c in EVOLUCAO_CLASSE:
            _, nova = _complemento(m["bruto"], "classe_nova")
            f = fase_por_classe(nova, None)
            if f in ("cumprimento_sentenca", "execucao"):
                cumprimento_inicio = cumprimento_inicio or q
                chegou(f, q)
                marcos.append({"quando": q, "marco": "cumprimento", "nome": "Evolução para cumprimento", "grau": grau})
        elif c in EXTINCAO_EXECUCAO:
            extincao_execucao = q
            marcos.append({"quando": q, "marco": "extincao_execucao", "nome": m["nome"], "grau": grau})
        elif c in ARQUIVAMENTO or (c in BAIXA and not recursal):
            ultimo_arquivo = q
            marcos.append({"quando": q, "marco": "arquivamento", "nome": m["nome"], "grau": grau})
        elif c in ARQUIVAMENTO_PROVISORIO:
            arquivo_provisorio = q
        elif c in DESARQUIVAMENTO:
            ultimo_desarquivo = q
            marcos.append({"quando": q, "marco": "desarquivamento", "nome": m["nome"], "grau": grau})
        elif c in FIM_SUSPENSAO:
            ultimo_fim_suspensao = q
        elif c in SUSPENSAO or "sobrestamento" in nome_n or (
                "suspens" in nome_n and "levantamento" not in nome_n and "efeito" not in nome_n):
            ultima_suspensao = q
        elif c == 123:
            motivo, _ = _complemento(m["bruto"], "motivo_da_remessa")
            if motivo and "grau de recurso" in motivo:
                chegou("recursal", q)
        elif c == 60:
            doc, _ = _complemento(m["bruto"], "tipo_de_documento")
            if _norm(doc) == "alvara":
                alvaras.append(q)
        elif c in DECISOES_INICIAIS and not recursal:
            chegou("conhecimento", q)

        # "Conclusão [para julgamento]" no 1º grau: autos com o juiz para sentença.
        if c == 51 and not recursal and any("julgamento" in _norm(x.get("nome")) or "sentenca" in _norm(x.get("nome"))
                                            for x in (m["bruto"].get("complementosTabelados") or [])):
            conclusao_julgamento = q
        if c in ALVARA or "requisicao de pequeno valor" in nome_n or "precatorio" in nome_n:
            alvaras.append(q)
        if c not in ROTINA:
            ultimo_substantivo = q

    # ── intimações mais novas que o DataJud ─────────────────────────────────
    # O DJEN chega antes: em 24/09/2026 um processo tinha "declaro extinta a
    # execução" publicado em 19/08 e o DataJud ainda sem o movimento 196.
    # Só frases de dispositivo, nunca "arquivem-se" (toda sentença diz isso).
    djen_audiencia = None
    corte_djen = movs[-1]["quando"] if movs else datetime.min
    for com in comunicacoes:
        d = _data(com.get("data"))
        if not d or d <= corte_djen:
            continue
        t = (com.get("texto") or "").lower()
        if _EXTINTA_EXECUCAO.search(t):
            extincao_execucao = d
            cumprimento_inicio = cumprimento_inicio or d
            chegou("cumprimento_sentenca", d)
            marcos.append({"quando": d, "marco": "extincao_execucao", "nome": "Execução extinta (DJEN)", "grau": None})
        elif _INICIO_CUMPRIMENTO.search(t):
            cumprimento_inicio = cumprimento_inicio or d
            chegou("cumprimento_sentenca", d)
            marcos.append({"quando": d, "marco": "cumprimento", "nome": "Cumprimento de sentença (DJEN)", "grau": None})
        elif _HOMOLOGO_ACORDO.search(t):
            chegou("sentenciado", d)
            julgamentos_favoraveis.append(d)
            marcos.append({"quando": d, "marco": "sentenca", "nome": "Acordo homologado (DJEN)", "grau": None})
        elif m := _JULGO.search(t):
            chegou("sentenciado", d)
            if "improcedente" not in m.group(0):
                julgamentos_favoraveis.append(d)
            marcos.append({"quando": d, "marco": "sentenca", "nome": f"{m.group(0).capitalize()} (DJEN)", "grau": None})
        elif _CIENCIA_SENTENCA.search(t):
            chegou("sentenciado", d)
            marcos.append({"quando": d, "marco": "sentenca", "nome": "Sentença publicada (DJEN)", "grau": None})
        if _ALVARA_TEXTO.search(t):
            alvaras.append(d)
        if aud := audiencia_no_texto(com.get("texto")):
            tipo, quando_aud = aud
            djen_audiencia = {"tipo": tipo, "situacao": "designada", "designada_em": _dia(d), "data": _dia(quando_aud),
                              "hora": hora_da_audiencia(com.get("texto"), _dia(quando_aud)), "fonte": "djen"}
            chegou("instrucao" if tipo == "instrução" else "conhecimento", d)

    # ── agenda do CRM ───────────────────────────────────────────────────────
    # Audiência de tipo não dito no título ("AUDIÊNCIA PRESENCIAL") marcada
    # DEPOIS de uma conciliação/inicial que já aconteceu é a de instrução
    # (caso 0000691-24.2026.5.23.0006, 26/09/2026).
    hoje_dt = datetime.combine(hoje, datetime.min.time())
    conciliacoes_passadas = [a["quando"] for a in audiencias
                             if "concilia" in _norm(a["tipo"]) and _norm(a["situacao"]) == "realizada"]
    if djen_audiencia and djen_audiencia["data"] and djen_audiencia["tipo"] == "conciliação" \
            and djen_audiencia["data"] < hoje.isoformat():
        conciliacoes_passadas.append(datetime.fromisoformat(djen_audiencia["data"]))
    audiencias_datadas_passadas = [datetime.fromisoformat(djen_audiencia["data"])] \
        if djen_audiencia and djen_audiencia["data"] and djen_audiencia["data"] < hoje.isoformat() else []
    agenda_futura = None
    instrucoes_passadas = [a["quando"] for a in audiencias
                           if "instru" in _norm(a["tipo"]) and _norm(a["situacao"]) == "realizada"]
    for q, titulo, lancada in _agenda_audiencias(agenda):
        tipo = _tipo_audiencia(titulo)
        # Dia seguinte em diante: no mesmo dia é a própria conciliação lançada
        # na agenda com outro nome ("Audiência Online — ...").
        if tipo == "audiência" and any(c.date() < q.date() for c in conciliacoes_passadas):
            tipo = "instrução"
        if q < hoje_dt:
            audiencias_datadas_passadas.append(q)
            if tipo == "conciliação":
                conciliacoes_passadas.append(q)
            elif tipo == "instrução":
                instrucoes_passadas.append(q)
        if tipo == "instrução":
            chegou("instrucao", min(q, hoje_dt))
        elif tipo == "conciliação":
            chegou("conhecimento", min(q, hoje_dt))
        if q >= hoje_dt and agenda_futura is None:
            # start_at é UTC; a audiência é na hora de Cuiabá (UTC-4, sem horário de verão).
            local = q - timedelta(hours=4)
            agenda_futura = {"tipo": tipo, "situacao": "designada", "designada_em": _dia(lancada), "data": _dia(local),
                             "hora": local.strftime("%H:%M"), "fonte": "agenda"}

    # ── classe atual (DataJud da instância mais recente, senão DJEN) ─────────
    inst_recente = max(instancias, key=lambda i: i.get("dataHoraUltimaAtualizacao") or "", default=None)
    classe_nome = ((inst_recente or {}).get("classe") or {}).get("nome")
    djen_recente = comunicacoes[-1] if comunicacoes else None
    if not classe_nome and djen_recente:
        classe_nome = djen_recente.get("classe")
    for inst in instancias:
        cl = inst.get("classe") or {}
        f = fase_por_classe(cl.get("codigo"), cl.get("nome"))
        quando = _data(inst.get("dataAjuizamento")) or datetime.min
        if f in ("cumprimento_sentenca", "execucao"):
            cumprimento_inicio = cumprimento_inicio or quando
            chegou(f, quando)
        elif f == "recursal":
            chegou("recursal", quando)

    fonte = "datajud" if movs else ("djen" if comunicacoes else "nenhuma")
    if not movs and djen_recente:
        quando = _data(djen_recente.get("data")) or datetime.min
        chegou(fase_por_classe(None, djen_recente.get("classe")) or "conhecimento", quando)

    # Fase = a mais adiantada alcançada. Recurso já decidido e transitado
    # conta como "transitado", não como recurso ainda em curso.
    fase = max(alcancou, key=ORDEM.index) if alcancou else "desconhecida"
    if fase == "recursal" and transito and transito >= alcancou["recursal"]:
        fase = "transitado"

    # ── situação ──────────────────────────────────────────────────────────
    situacao, arquivado_em = "ativo", None
    reaberto = max(filter(None, [ultimo_desarquivo,
                                 ultimo_ato_vivo if ultimo_arquivo and ultimo_ato_vivo and ultimo_ato_vivo > ultimo_arquivo else None]),
                   default=None)
    # Extinção da execução NÃO arquiva: é o momento em que o dinheiro sai
    # (alvará/RPV). Arquivar ali tirava o processo da lista justamente com
    # valor a levantar. Só o arquivamento do próprio tribunal arquiva.
    execucao_extinta = bool(extincao_execucao and (not reaberto or extincao_execucao > reaberto)
                            and fase in ("cumprimento_sentenca", "execucao"))
    if ultimo_arquivo and (not reaberto or ultimo_arquivo > reaberto):
        situacao, arquivado_em = "arquivado", ultimo_arquivo
    elif arquivo_provisorio and (not reaberto or arquivo_provisorio > reaberto):
        situacao = "suspenso"
        ultima_suspensao = max(filter(None, [ultima_suspensao, arquivo_provisorio]))
    elif ultima_suspensao and (not ultimo_fim_suspensao or ultima_suspensao > ultimo_fim_suspensao):
        situacao = "suspenso"

    # ── últimos sinais de vida ────────────────────────────────────────────
    ultimo_mov = movs[-1] if movs else None
    ultima_com = _data(djen_recente.get("data")) if djen_recente else None
    ultima_atividade = max(filter(None, [ultimo_mov["quando"] if ultimo_mov else None, ultima_com]), default=None)

    # ── audiência pendente ────────────────────────────────────────────────
    audiencia = None
    if audiencias and situacao == "ativo":
        a = audiencias[-1]
        if _norm(a["situacao"]) in ("designada", "redesignada"):
            audiencia = {"tipo": a["tipo"], "situacao": a["situacao"], "designada_em": _dia(a["quando"]), "data": None}
    # A do DJEN é mais nova que o DataJud e ainda traz a DATA da audiência.
    # Sem data ("aguarde-se a audiência já designada") só vale se a intimação é recente.
    if djen_audiencia and situacao == "ativo":
        if djen_audiencia["data"]:
            if djen_audiencia["data"] >= hoje.isoformat():
                audiencia = djen_audiencia
        elif (hoje - date.fromisoformat(djen_audiencia["designada_em"])).days <= 120:
            audiencia = djen_audiencia
    # "Designada" no DataJud não traz a data: se uma audiência datada (DJEN ou
    # agenda) marcada depois dessa designação já passou, ela aconteceu.
    if audiencia and not audiencia.get("data") and audiencia.get("designada_em") \
            and any(_dia(q) >= audiencia["designada_em"] for q in audiencias_datadas_passadas):
        if "concilia" in _norm(audiencia["tipo"]):
            conciliacoes_passadas.append(max(audiencias_datadas_passadas))
        audiencia = None
    # A agenda do escritório é a fonte mais fiel da DATA da próxima audiência;
    # o tipo, quando o título não diz ("Audiência Online — FULANO"), vem do
    # tribunal (DataJud/DJEN) — senão a conciliação virava "audiência" e o
    # estágio caía para andamento.
    if agenda_futura and situacao == "ativo" and (
            not audiencia or not audiencia.get("data") or agenda_futura["data"] <= audiencia["data"]):
        if agenda_futura["tipo"] == "audiência" and audiencia and _norm(audiencia["tipo"]) != "audiencia" \
                and (not audiencia.get("data") or audiencia["data"] == agenda_futura["data"]):
            agenda_futura = {**agenda_futura, "tipo": audiencia["tipo"]}
        audiencia = agenda_futura

    # ── pendências ────────────────────────────────────────────────────────
    pend: list[dict] = []

    def pendencia(tipo, severidade, descricao, desde=None):
        pend.append({"tipo": tipo, "severidade": severidade, "descricao": descricao,
                     "desde": _dia(desde) if isinstance(desde, datetime) else desde})

    agora = datetime.combine(hoje, datetime.min.time())
    if situacao == "ativo" and ultima_atividade:
        dias = (agora - ultima_atividade).days
        if dias >= 240:
            pendencia("parado_demais", "alta", f"Sem andamento há {dias} dias", ultima_atividade)
        elif dias >= 120:
            pendencia("parado_demais", "media", f"Sem andamento há {dias} dias", ultima_atividade)
    if transito and not cumprimento_inicio and situacao == "ativo" \
            and any(j <= transito for j in julgamentos_favoraveis) and (agora - transito).days >= 30:
        pendencia("execucao_pendente", "alta",
                  f"Transitou em julgado há {(agora - transito).days} dias e o cumprimento não começou", transito)
    alvaras_recentes = [a for a in alvaras if (agora - a).days <= 365 and (not arquivado_em or a > arquivado_em)]
    if alvaras_recentes:
        pendencia("valor_a_levantar", "alta", "Alvará/RPV expedido: conferir o levantamento", max(alvaras_recentes))
    if audiencia:
        quando_txt = f" para {audiencia['data']}" if audiencia.get("data") else ""
        pendencia("audiencia_designada", "media", f"Audiência ({audiencia['tipo']}) designada{quando_txt}",
                  audiencia["designada_em"])
    if situacao == "suspenso" and ultima_suspensao and (agora - ultima_suspensao).days >= 180:
        pendencia("suspensao_esquecida", "media",
                  f"Suspenso há {(agora - ultima_suspensao).days} dias", ultima_suspensao)
    if fonte != "datajud":
        pendencia("sem_linha_do_tempo", "baixa", "O DataJud não tem os andamentos deste processo")
    if situacao == "arquivado" and arquivado_em:
        depois = [d for d in (_data(c.get("data")) for c in comunicacoes) if d and d > arquivado_em]
        if depois:
            pendencia("intimacao_apos_arquivamento", "media",
                      f"{len(depois)} intimação(ões) depois do arquivamento: conferir se o processo voltou a andar",
                      max(depois))
    if execucao_extinta and situacao == "ativo":
        pendencia("execucao_extinta", "baixa",
                  "Execução extinta: falta o levantamento do valor e o arquivamento pelo tribunal", extincao_execucao)

    sev = {p["severidade"] for p in pend}
    if fonte == "nenhuma":
        saude = "sem_dados"
    elif "critica" in sev:
        saude = "critico"
    elif sev & {"alta", "media"}:
        saude = "atencao"
    else:
        saude = "ok"

    status_crm = "arquivado" if situacao == "arquivado" else PARA_STATUS_CRM.get(fase)
    if status_crm == "andamento" and audiencia and "concilia" in _norm(audiencia["tipo"]):
        status_crm = "conciliacao"
    elif status_crm == "andamento" and conciliacoes_passadas:
        # Conciliação feita e sem acordo: é a fase da defesa. "andamento" era
        # desenhado em cima de Instrução na barra de estágios da Linha do Tempo.
        status_crm = "contestacao"

    # Aguardando sentença (pedido do usuário, 26/09/2026 — caso Juliana
    # 1049137-40: conciliação 18/09, réplica cumprida 24/09). Sem audiência
    # futura e: concluso para julgamento sem ato do juiz depois; ou réplica/
    # impugnação cumprida depois da conciliação; ou instrução já realizada.
    aguardando_desde = None
    if status_crm in ("distribuido", "andamento", "contestacao", "instrucao") and situacao == "ativo" and not audiencia:
        sinais = []
        if conclusao_julgamento and (not ultimo_ato_vivo or conclusao_julgamento >= ultimo_ato_vivo):
            sinais.append(conclusao_julgamento)
        if conciliacoes_passadas:
            depois = max(conciliacoes_passadas)
            for p in prazos or []:
                venc = _data(p.get("due_date"))
                if venc and venc >= depois and p.get("status") == "cumprido" \
                        and re.search(r"impugn|replica", _norm(p.get("title"))):
                    sinais.append(venc)
        if instrucoes_passadas:
            sinais.append(max(instrucoes_passadas))
        if sinais:
            status_crm, aguardando_desde = "aguardando_sentenca", max(sinais)

    # Quando aconteceu o fato que dá o status_crm. O CRM avisa o cliente no
    # portal a cada troca de status; a troca que só CORRIGE o estágio (fato
    # antigo) vai calada — ver publicar.py.
    if situacao == "arquivado":
        status_desde = arquivado_em
    elif status_crm == "aguardando_sentenca":
        status_desde = aguardando_desde
    elif status_crm == "contestacao":
        status_desde = max(conciliacoes_passadas)
    elif status_crm in ("conciliacao", "instrucao") and audiencia and audiencia.get("designada_em"):
        status_desde = datetime.fromisoformat(audiencia["designada_em"])
    else:
        status_desde = alcancou.get(fase)

    ajuizamentos = [d for d in (_data(i.get("dataAjuizamento")) for i in instancias) if d]
    return {
        "fase": fase,
        "fase_desde": _dia(alcancou.get(fase)),
        "situacao": situacao,
        "arquivado_em": _dia(arquivado_em),
        "status_crm": status_crm,
        "status_desde": _dia(status_desde),
        "saude": saude,
        "fonte": fonte,
        "classe": classe_nome,
        "orgao": ((inst_recente or {}).get("orgaoJulgador") or {}).get("nome") or (djen_recente or {}).get("orgao"),
        "tribunal": (inst_recente or {}).get("tribunal") or (djen_recente or {}).get("tribunal"),
        "graus": sorted({i.get("grau") for i in instancias if i.get("grau")}),
        "ajuizado_em": _dia(min(ajuizamentos)) if ajuizamentos else None,
        "ultimo_movimento": {"data": _dia(ultimo_mov["quando"]), "nome": ultimo_mov["nome"],
                             "grau": ultimo_mov["grau"]} if ultimo_mov else None,
        "ultima_intimacao": djen_recente.get("data") if djen_recente else None,
        "ultima_atividade": _dia(ultima_atividade),
        "ultimo_substantivo": _dia(ultimo_substantivo),
        # Tipo sempre no mesmo vocabulário: o DataJud manda "de Conciliação" e a
        # ficha escrevia "Audiência de de Conciliação".
        "audiencia": {**audiencia, "tipo": _tipo_audiencia(_norm(audiencia["tipo"]))} if audiencia else None,
        "pendencias": pend,
        "marcos": [{**m, "quando": _dia(m["quando"])} for m in marcos],
        "total_movimentos": len(movs),
        "total_intimacoes": len(comunicacoes),
        "alvaras": sorted({_dia(a) for a in alvaras}),
        "execucao_extinta_em": _dia(extincao_execucao) if execucao_extinta else None,
    }
