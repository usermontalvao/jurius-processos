"""Análise de intimação: o que a IA lê, o que ela devolve e o que a regra corrige.

Até 01/10/2026 a IA lia a intimação SOZINHA — sem saber em que juízo o
processo corre, de que lado está o nosso cliente, nem o que veio antes. Dois
casos mostraram o custo:

  • Pedro (1038323-66, 6º JEC de Cuiabá): sentença que julgou TUDO a favor do
    cliente virou "procedência parcial" e "Manifestar sobre a sentença, 15
    dias". No Juizado não existe apelação de 15 dias: contra a sentença cabe
    recurso inominado em 10 (Lei 9.099, art. 42) e embargos em 5 (art. 49).
  • Vicente (1059802-92, Vara da Fazenda): a decisão marcou perícia em
    23/11/2026 às 14h, e o CRM só sugeria prazo — a perícia, que é o que vai
    para a Agenda, não aparecia como compromisso.

Agora a IA recebe o contexto inteiro (tribunal, órgão, rito, classe, cliente e
polos, resumo do processo, intimações anteriores e a tabela de prazos do rito)
e devolve, além do prazo, o COMPROMISSO (audiência, perícia, julgamento) e as
OPÇÕES de prazo cabíveis. Depois, regra pura corrige o que a IA ainda erra:
dias do recurso pelo rito, "manifestar sobre a sentença" genérico, compromisso
com data que não está no texto.

Módulo puro (sem rede): testável sem DeepSeek nem Supabase.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

VERSAO = 3  # intimation_ai_analysis.analise_versao — as de versão menor são refeitas


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def _norm(s: str | None) -> str:
    return " ".join(_sem_acento(s or "").lower().split())


# ── 1. RITO ─────────────────────────────────────────────────────────────────
RITOS = {
    "juizado_civel": "Juizado Especial Cível (Lei 9.099/95)",
    "juizado_fazenda": "Juizado Especial da Fazenda Pública (Lei 12.153/09 + Lei 9.099/95)",
    "jef": "Juizado Especial Federal (Lei 10.259/01 + Lei 9.099/95)",
    "turma_recursal": "Turma Recursal dos Juizados",
    "trabalho": "Justiça do Trabalho, 1º grau (CLT)",
    "trabalho_2g": "Justiça do Trabalho, 2º grau — TRT (CLT)",
    "civel": "Justiça comum, 1º grau (CPC)",
    "tribunal": "Tribunal, 2º grau (CPC)",
    "superior": "Tribunal superior (STJ/STF/TST)",
    "criminal": "Processo penal (CPP)",
}


def rito(orgao: str | None, tribunal: str | None, classe: str | None = None) -> str:
    """O rito pelo nome do órgão, pela sigla do tribunal e pela classe."""
    o, t, c = _norm(orgao), _norm(tribunal), _norm(classe)
    if t in {"stj", "stf", "tst"}:
        return "superior"
    if "turma recursal" in o or "recurso inominado" in c:
        return "turma_recursal"
    trabalhista = t.startswith("trt") or "trabalho" in o or "trabalhista" in c or "cejusc-jt" in o
    if trabalhista:
        if re.search(r"\bturma\b|gab|desembarg|tribunal pleno|secao especializada", o) or c.startswith("recurso ordinario"):
            return "trabalho_2g"
        return "trabalho"
    if "criminal" in o or "penal" in c or "persecucao penal" in c or "juri" in o.split():
        return "criminal"
    if "juizado" in o or "juizado especial" in c:
        if "fazenda" in o or "fazenda" in c:
            return "juizado_fazenda"
        if t.startswith("trf") or "federal" in o or "jef" in o.split():
            return "jef"
        return "juizado_civel"
    if re.search(r"\bcamara\b|\bgab\b|gabinete|desembarg|tribunal pleno|orgao especial|\bturma\b|secao civel", o):
        return "tribunal"
    return "civel"


def eh_juizado(r: str) -> bool:
    return r in {"juizado_civel", "juizado_fazenda", "jef"}


# ── 2. TABELA DE PRAZOS POR RITO ────────────────────────────────────────────
# (palavra-chave da providência normalizada, dias úteis, fundamento). É o que
# vai no prompt E o que a salvaguarda usa para corrigir os dias.
TABELA: dict[str, list[tuple[str, str, int, str]]] = {
    # (chave, rótulo, dias, fundamento)
    "civel": [
        ("apelacao", "Interpor apelação", 15, "CPC, art. 1.003, §5º"),
        ("embargos de declaracao", "Opor embargos de declaração", 5, "CPC, art. 1.023"),
        ("contrarrazoes", "Apresentar contrarrazões", 15, "CPC, art. 1.010, §1º"),
        ("agravo de instrumento", "Interpor agravo de instrumento", 15, "CPC, art. 1.003, §5º"),
        ("replica", "Apresentar réplica", 15, "CPC, arts. 350 e 351"),
        ("quesitos", "Indicar assistente técnico e quesitos", 15, "CPC, art. 465, §1º"),
        ("laudo", "Manifestar sobre o laudo pericial", 15, "CPC, art. 477, §1º"),
        ("impugnacao ao cumprimento", "Impugnar o cumprimento de sentença", 15, "CPC, art. 525"),
    ],
    "juizado_civel": [
        ("recurso inominado", "Interpor recurso inominado", 10, "Lei 9.099/95, art. 42"),
        ("embargos de declaracao", "Opor embargos de declaração", 5, "Lei 9.099/95, art. 49"),
        ("contrarrazoes", "Apresentar contrarrazões ao recurso inominado", 10, "Lei 9.099/95, art. 42, §2º"),
    ],
    "turma_recursal": [
        ("embargos de declaracao", "Opor embargos de declaração", 5, "Lei 9.099/95, art. 49"),
        ("recurso extraordinario", "Interpor recurso extraordinário", 15, "CPC, art. 1.003, §5º"),
        ("agravo interno", "Interpor agravo interno", 15, "CPC, art. 1.021"),
        ("contrarrazoes", "Apresentar contrarrazões", 15, "CPC, art. 1.030"),
    ],
    "tribunal": [
        ("embargos de declaracao", "Opor embargos de declaração", 5, "CPC, art. 1.023"),
        ("recurso especial", "Interpor recurso especial", 15, "CPC, art. 1.003, §5º"),
        ("recurso extraordinario", "Interpor recurso extraordinário", 15, "CPC, art. 1.003, §5º"),
        ("agravo interno", "Interpor agravo interno", 15, "CPC, art. 1.021"),
        ("contrarrazoes", "Apresentar contrarrazões", 15, "CPC, art. 1.030"),
    ],
    "trabalho": [
        ("recurso ordinario", "Interpor recurso ordinário", 8, "CLT, art. 895"),
        ("embargos de declaracao", "Opor embargos de declaração", 5, "CLT, art. 897-A"),
        ("contrarrazoes", "Apresentar contrarrazões ao recurso ordinário", 8, "CLT, art. 900"),
        ("agravo de peticao", "Interpor agravo de petição", 8, "CLT, art. 897, a"),
        ("embargos a execucao", "Opor embargos à execução", 5, "CLT, art. 884"),
        ("impugnacao a sentenca de liquidacao", "Impugnar a sentença de liquidação", 5, "CLT, art. 884, §3º"),
    ],
    "trabalho_2g": [
        ("embargos de declaracao", "Opor embargos de declaração", 5, "CLT, art. 897-A"),
        ("recurso de revista", "Interpor recurso de revista", 8, "CLT, art. 896"),
        ("contrarrazoes", "Apresentar contrarrazões", 8, "CLT, art. 900"),
    ],
}
TABELA["juizado_fazenda"] = [
    ("recurso inominado", "Interpor recurso inominado", 10, "Lei 9.099/95, art. 42 (Lei 12.153/09, art. 27)"),
    ("embargos de declaracao", "Opor embargos de declaração", 5, "Lei 9.099/95, art. 49"),
    ("contrarrazoes", "Apresentar contrarrazões ao recurso inominado", 10, "Lei 9.099/95, art. 42, §2º"),
]
TABELA["jef"] = [
    ("recurso inominado", "Interpor recurso inominado", 10, "Lei 9.099/95, art. 42 (Lei 10.259/01, art. 1º)"),
    ("embargos de declaracao", "Opor embargos de declaração", 5, "Lei 9.099/95, art. 49"),
    ("contrarrazoes", "Apresentar contrarrazões ao recurso inominado", 10, "Lei 9.099/95, art. 42, §2º"),
]

# Recurso que NÃO existe naquele rito → o que existe no lugar.
TROCAS = {
    "juizado_civel": {"apelacao": "recurso inominado", "recurso ordinario": "recurso inominado"},
    "juizado_fazenda": {"apelacao": "recurso inominado", "recurso ordinario": "recurso inominado"},
    "jef": {"apelacao": "recurso inominado", "recurso ordinario": "recurso inominado"},
    "trabalho": {"apelacao": "recurso ordinario", "recurso inominado": "recurso ordinario"},
    "civel": {"recurso inominado": "apelacao", "recurso ordinario": "apelacao"},
}

# Contra a sentença, o recurso do rito.
RECURSO_DA_SENTENCA = {"civel": "apelacao", "juizado_civel": "recurso inominado", "juizado_fazenda": "recurso inominado",
                       "jef": "recurso inominado", "trabalho": "recurso ordinario"}
RECURSO_DO_ACORDAO = {"tribunal": "recurso especial", "turma_recursal": "recurso extraordinario", "trabalho_2g": "recurso de revista"}


def regras_do_rito(r: str) -> str:
    """O trecho do prompt com os prazos do rito, em dias úteis."""
    linhas = [f"- {rot}: {d} dias úteis ({fund})" for _, rot, d, fund in TABELA.get(r, [])]
    extra = {
        "juizado_civel": "No Juizado NÃO existe apelação nem agravo de instrumento contra sentença: contra sentença cabe recurso inominado (10 dias) ou embargos de declaração (5 dias, se houver omissão, contradição, obscuridade ou erro material).",
        "juizado_fazenda": "No Juizado da Fazenda NÃO existe apelação: contra sentença cabe recurso inominado (10 dias) ou embargos (5 dias). Não há prazo em dobro para a Fazenda (Lei 12.153, art. 7º).",
        "jef": "No JEF NÃO existe apelação: contra sentença cabe recurso inominado (10 dias) ou embargos (5 dias). O INSS não tem prazo em dobro no JEF (Lei 10.259, art. 9º).",
        "turma_recursal": "Contra acórdão da Turma Recursal: embargos (5 dias); recurso extraordinário (15 dias) só com questão constitucional; no JEF, pedido de uniformização (15 dias).",
        "civel": "Prazo em dobro é da Fazenda Pública/INSS (CPC, art. 183), NÃO do nosso cliente particular. Sem prazo fixado pelo juiz, o prazo é de 5 dias (CPC, art. 218, §3º).",
        "tribunal": "Contra acórdão: embargos (5 dias); recurso especial/extraordinário (15 dias).",
        "trabalho": "Prazos da CLT em dias úteis (art. 775). Sem prazo fixado pelo juiz, use o que o despacho disser; manifestação genérica costuma ser 5 dias.",
        "trabalho_2g": "Contra acórdão do TRT: embargos (5 dias) ou recurso de revista (8 dias).",
        "criminal": "Processo penal: prazos CONTÍNUOS (CPP, art. 798). Apelação 5 dias (CPP, art. 593); embargos de declaração 2 dias (CPP, art. 619); RESE 5 dias.",
    }.get(r)
    return "\n".join(linhas + ([extra] if extra else []))


# ── 3. O PROMPT ─────────────────────────────────────────────────────────────
PROMPT_SISTEMA = """Você é o advogado sênior de um escritório que atua em previdenciário, trabalhista e cível em Mato Grosso. Lê a intimação com TODO o contexto do processo e diz, sem rodeio, o que o escritório tem de fazer. Retorne APENAS um JSON válido:
{
  "summary": "1-2 frases: o que o juiz decidiu/determinou e o que isso significa para o NOSSO cliente",
  "tipo_ato": "sentenca" | "acordao" | "decisao" | "despacho" | "ato_ordinatorio" | "pauta_julgamento" | "designacao_audiencia" | "designacao_pericia" | "outro",
  "resultado": "favoravel" | "desfavoravel" | "parcial" | "neutro",
  "tutela": "concedida" | "concedida_em_parte" | "negada" | "revogada" | "mantida" | "postergada" | "nao_ha",
  "urgency": "baixa" | "media" | "alta" | "critica",
  "deadline": { "days": dias úteis ou null, "action": "o que o advogado precisa fazer", "fundamento": "artigo de lei ou 'fixado pelo juiz'" } ou null,
  "alternativas": [ { "action": "...", "days": n, "fundamento": "..." } ],
  "compromisso": null ou {
    "tipo": "audiencia" | "pericia" | "julgamento",
    "subtipo": "conciliação" | "inicial" | "una" | "instrução" | "instrução e julgamento" | "encerramento de instrução" | "médica" | "social" | "técnica" | "sessão virtual" | "sessão presencial" | "sessão por videoconferência" | outro curto,
    "data": "AAAA-MM-DD", "hora": "HH:MM" ou null, "data_fim": "AAAA-MM-DD" ou null,
    "modalidade": "presencial" | "online" | "hibrida" | null,
    "local": "endereço/sala, como escrito" ou null, "link": "URL de acesso" ou null,
    "perito": "nome do perito/empresa" ou null,
    "observacoes": "o que o cliente/advogado precisa saber: levar documentos, testemunhas, sustentação oral até 48h antes..." ou null
  }
}

REGRAS
1. "resultado" é do ponto de vista do NOSSO cliente (o contexto diz quem é e em que polo está; quando diz o POLO do cliente, isso é certo e vence qualquer dedução sua). O escritório advoga para pessoas físicas (consumidor, trabalhador, segurado) contra bancos, empresas e o INSS: nunca conclua que o cliente é a empresa/banco/INSS porque o nome não bateu exatamente. Sentença que julga improcedente o pedido do autor é FAVORÁVEL a quem é réu. Não chame de "parcial" o que é vitória total.
2. Use o RITO do contexto. Recurso e prazo têm de ser os do rito (tabela abaixo). Nunca sugira apelação em Juizado, nem recurso inominado na Justiça comum, nem 15 dias para recurso inominado.
3. Sentença/acórdão:
   - desfavorável ou parcial → deadline = o recurso do rito; alternativas = embargos de declaração.
   - favorável → deadline = {"action": "Acompanhar trânsito em julgado", "days": prazo recursal do rito}; alternativas = embargos de declaração SÓ se você identificar omissão, contradição, obscuridade ou erro material (diga qual no fundamento).
   - Nunca use "Manifestar sobre a sentença" — diga o recurso ou o acompanhamento.
4. Ordem com prazo do juiz ("no prazo de X dias") vale X dias. Quesitos/assistente técnico (perícia nomeada) = 15 dias (CPC, 465, §1º) se o juiz não fixar outro.
5. COMPROMISSO: audiência, perícia ou sessão de julgamento COM DATA marcada no texto. Copie data e hora exatamente do texto (formato ISO). Se a sessão virtual tem início e fim, data = início e data_fim = fim. Audiência/perícia cancelada, já realizada ou "a ser designada" → compromisso null. Redesignada → a NOVA data. Modalidade: link/Zoom/Teams/videoconferência/telepresencial/virtual → "online"; sala física/endereço/presencial → "presencial"; mista/híbrida → "hibrida".
6. Pauta de julgamento NÃO abre prazo por si: deadline null (a sustentação oral vai em observacoes). Designação de audiência/perícia normalmente não tem prazo próprio, salvo ordem expressa (quesitos, rol de testemunhas, emenda...).
7. "action" vira o TÍTULO do prazo no CRM: CURTO (até 50 caracteres), verbo no infinitivo + objeto, sem número de processo, sem nome de parte. Ex.: "Interpor recurso inominado", "Opor embargos de declaração", "Indicar assistente técnico e quesitos", "Apresentar contrarrazões ao recurso ordinário".
8. Leia o texto INTEIRO: o que vale é o dispositivo (meio e fim). Citação de doutrina não é ordem.
9. Urgência pelo prazo: critica <= 2 dias; alta <= 5; media <= 15; baixa > 15 ou sem prazo. Audiência/perícia em menos de 10 dias = alta.
10. Use o histórico só para entender o processo; a análise é da intimação NOVA.
11. "tutela" é o que ESTA decisão fez com o pedido de tutela/liminar — pelo verbo do JUIZ ("defiro", "indefiro", "postergo a apreciação", "revogo", "mantenho"). Doutrina e jurisprudência citadas não são decisão. "Concedo a justiça gratuita" NÃO é tutela. Análise adiada para depois da perícia/contestação = "postergada" (nunca "negada" nem "concedida"). Sem pedido de tutela decidido aqui = "nao_ha". No summary, diga exatamente isso (ex.: "adiou a análise da tutela"), nunca "concedeu a tutela" se ela foi adiada."""


_EMPRESA = re.compile(r"\b(s\.?\s?a\.?|ltda|eireli|me|epp|banco|bank|instituto nacional|inss|uniao|estado de|municipio|"
                      r"fazenda|caixa economica|telefonica|seguradora|seguros|financeira|credito|pagamentos|cooperativa|"
                      r"companhia|cia\.?|associacao|fundacao|condominio|empresa|servicos|comercio|industria)\b")
_PARTICULAS = {"de", "da", "do", "das", "dos", "e"}


def _nomes(polo: str | None) -> list[str]:
    return [n.strip() for n in re.split(r",|;| e outros?", polo or "") if n.strip()]


def _parecido(a: str, b: str) -> float:
    ta = {t for t in _norm(a).split() if t not in _PARTICULAS}
    tb = {t for t in _norm(b).split() if t not in _PARTICULAS}
    if not ta or not tb:
        return 0.0
    comum = ta & tb
    primeiro = _norm(a).split()[:1] == _norm(b).split()[:1]
    return len(comum) / min(len(ta), len(tb)) + (0.5 if primeiro else 0.0)


def polo_do_cliente(cliente: str | None, polo_ativo: str | None, polo_passivo: str | None) -> str | None:
    """'ativo' | 'passivo' | None. Nome parecido decide; empate ou nome que não
    bate: o escritório advoga para PESSOA FÍSICA (consumidor, trabalhador,
    segurado) contra banco, empresa e INSS — a pessoa física é o cliente.
    Caso Jessica (1028248-65): no CRM "Jessica Pereira da Silva", no processo
    "Jessica da Silva Gonçalves" × NU PAGAMENTOS; sem isto a IA concluiu que o
    cliente era o banco e chamou a derrota de vitória."""
    ativos, passivos = _nomes(polo_ativo), _nomes(polo_passivo)
    if not ativos and not passivos:
        return None
    if cliente:
        na = max((_parecido(cliente, n) for n in ativos), default=0.0)
        np_ = max((_parecido(cliente, n) for n in passivos), default=0.0)
        if max(na, np_) >= 1.0 and abs(na - np_) >= 0.5:
            return "ativo" if na > np_ else "passivo"
    pf_a = any(not _EMPRESA.search(_norm(n)) for n in ativos)
    pf_p = any(not _EMPRESA.search(_norm(n)) for n in passivos)
    if pf_a != pf_p:
        return "ativo" if pf_a else "passivo"
    return None


def _polos_do_texto(texto: str) -> dict:
    """"AUTOR: FULANO REU: BANCO X" do cabeçalho do PJe, quando a ficha não tem polos."""
    t = " ".join((texto or "")[:1500].split())
    a = re.search(r"\b(?:AUTORA?|REQUERENTE|RECLAMANTE|EXEQUENTE|RECORRENTE|APELANTE|IMPETRANTE)\(?S?\)?:\s*(.+?)\s+(?:Advogad|REU|RÉU|REQUERID|RECLAMAD|EXECUTAD|RECORRID|APELAD|IMPETRAD)", t)
    p = re.search(r"\b(?:REU|RÉU|REQUERIDO|REQUERIDA|RECLAMADA?O?|EXECUTADO|EXECUTADA|RECORRIDO|RECORRIDA|APELADO|APELADA|IMPETRADO)\(?S?\)?:\s*(.+?)(?:\s+Advogad|\s+Vistos|\s+[A-Z][a-z]+\s|\.\s|$)", t)
    return {"polo_ativo": a.group(1).strip() if a else None, "polo_passivo": p.group(1).strip()[:120] if p else None}


def contexto(it: dict, cliente: str | None, polos: dict | None, resumo_processo: str | None,
             historico: list[dict]) -> str:
    """O bloco de contexto que vai antes do texto da intimação."""
    r = rito(it.get("nome_orgao"), it.get("sigla_tribunal"), it.get("nome_classe"))
    linhas = [
        "CONTEXTO DO PROCESSO",
        f"Processo: {it.get('numero_processo_mascara') or it.get('numero_processo') or '—'}",
        f"Tribunal: {it.get('sigla_tribunal') or '—'} · Órgão: {it.get('nome_orgao') or '—'}",
        f"Classe: {it.get('nome_classe') or '—'} · Documento: {it.get('tipo_documento') or it.get('tipo_comunicacao') or '—'}",
        f"Rito: {RITOS[r]}",
        f"Disponibilizada em: {str(it.get('data_disponibilizacao') or '')[:10]}",
        f"NOSSO CLIENTE: {cliente or 'não vinculado (deduza pelo texto e pelo advogado intimado)'}",
    ]
    if not polos or not (polos.get("polo_ativo") or polos.get("polo_passivo")):
        polos = _polos_do_texto(it.get("texto") or "")
    if polos.get("polo_ativo") or polos.get("polo_passivo"):
        linhas.append(f"Polo ativo: {polos.get('polo_ativo') or '—'} · Polo passivo: {polos.get('polo_passivo') or '—'}")
    lado = polo_do_cliente(cliente, polos.get("polo_ativo"), polos.get("polo_passivo"))
    if lado:
        linhas.append(f"O NOSSO CLIENTE ESTÁ NO POLO {lado.upper()} ({'autor/reclamante/exequente/recorrente' if lado == 'ativo' else 'réu/reclamado/executado/recorrido'}) — "
                      "o resultado é do ponto de vista dele.")
    if resumo_processo:
        linhas += ["", "RESUMO DO PROCESSO", resumo_processo.strip()[:1500]]
    if historico:
        linhas += ["", "INTIMAÇÕES ANTERIORES (mais recente primeiro)"]
        for h in historico[:8]:
            linhas.append(f"- {str(h.get('data') or '')[:10]} · {h.get('tipo') or 'Intimação'}: {(h.get('resumo') or '').strip()[:300]}")
    linhas += ["", f"PRAZOS DO RITO ({RITOS[r]})", regras_do_rito(r) or "—"]
    return "\n".join(linhas)


# ── 4. SALVAGUARDAS ─────────────────────────────────────────────────────────
TUTELAS = {"concedida", "concedida_em_parte", "negada", "revogada", "mantida", "postergada", "nao_ha"}

# O verbo do JUIZ, em primeira pessoa: é o dispositivo. A doutrina citada no
# meio da decisão fala na terceira ("a decisão que concede tutela...") e não
# decide nada. Ordem importa: "não defiro" antes de "defiro", "em parte" antes
# do inteiro.
_TUTELA_NO_TEXTO = [
    ("postergada", r"\b(postergo|difiro|reservo-me|relego|deixo para (apreciar|analisar)|apreciarei|analisarei|"
                   r"sera (apreciad|analisad)[oa] (apos|oportunamente|por ocasiao|depois))"),
    ("negada", r"\b(nao (defiro|concedo|vislumbro|verifico)|indefiro|nego|denego|rejeito)\b"),
    ("revogada", r"\b(revogo|casso|torno sem efeito)\b"),
    # "concedo a segurança, confirmando a liminar": o "concedo" é da segurança.
    ("mantida", r"\b(mantenho|ratific(o|ar|ando)|confirm(o|ar|ando))\b"),
    ("concedida_em_parte", r"\b(defiro|concedo)\b[^.;]{0,40}\b(parcialmente|em parte)\b"),
    ("concedida", r"\b(defiro|concedo|antecipo)\b"),
]
_REMEDIO = re.compile(r"\b(tutela|liminar|antecipacao dos efeitos)")


def tutela_no_texto(texto: str) -> str | None:
    """O que o juiz decidiu sobre a tutela, pelo verbo dele na MESMA oração da
    tutela. Havendo mais de uma, vale a última (o dispositivo fica no fim).
    Caso Vicente (1059802-92): "postergo a apreciação do pedido de tutela" e,
    na frase seguinte, "CONCEDO" a justiça gratuita — virou "tutela concedida"."""
    achado = None
    for frase in re.split(r"[.;!?]+", _norm(texto)):
        if not _REMEDIO.search(frase):
            continue
        oracoes = re.split(r",|\bmas\b|\bporem\b", frase)
        for i, oracao in enumerate(oracoes):
            if not _REMEDIO.search(oracao):
                continue
            # A própria oração primeiro; sem verbo nela ("Não defiro, por ora,
            # a liminar"), até duas orações antes.
            for trecho in (oracao, " ".join(oracoes[max(0, i - 1):i + 1]), " ".join(oracoes[max(0, i - 2):i + 1])):
                tipo = next((t for t, verbo in _TUTELA_NO_TEXTO if re.search(verbo, trecho)), None)
                if tipo:
                    achado = tipo
                    break
    return achado


def conferir_tutela(da_ia, texto: str) -> str | None:
    """A palavra do juiz vence a da IA; sem verbo do juiz na oração da tutela,
    vale a IA (participio, "Tutela deferida.", a regra não lê)."""
    ia = da_ia if da_ia in TUTELAS else None
    lido = tutela_no_texto(texto)
    if lido is None:
        return ia
    if ia in {"concedida", "concedida_em_parte"} and lido in {"concedida", "concedida_em_parte"}:
        return ia
    return lido


_MESES = {m: i for i, m in enumerate(["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
                                      "setembro", "outubro", "novembro", "dezembro"], 1)}


def _datas_no_texto(texto: str) -> set[str]:
    """Toda data escrita no texto, em ISO: 23/11/2026, 23-11-2026, 23 de novembro de 2026."""
    t = _norm(texto)
    out = set()
    for d, m, a in re.findall(r"\b(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})\b", t):
        out.add(f"{a}-{int(m):02d}-{int(d):02d}")
    for d, m, a in re.findall(r"\b(\d{1,2})\s*(?:º|o)?\s*de\s+(" + "|".join(_MESES) + r")\s+de\s+(\d{4})", t):
        out.add(f"{a}-{_MESES[m]:02d}-{int(d):02d}")
    return out


def _horas_no_texto(texto: str) -> set[str]:
    t = _norm(texto)
    out = set()
    for h, m in re.findall(r"\b(\d{1,2})\s*(?::|h)\s*(\d{2})?", t):
        if int(h) <= 23 and (not m or int(m) <= 59):
            out.add(f"{int(h):02d}:{int(m or 0):02d}")
    return out


def _iso(v) -> str | None:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(v or "").strip())
    if not m:
        return None
    try:
        date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    return m[0]


def conferir_compromisso(c, texto: str, disponibilizacao: str | None) -> dict | None:
    """O compromisso da IA só passa se a data (e a hora, quando dita) está no
    texto e não é anterior à publicação — data inventada na agenda é pior que
    nenhuma (a tela ainda tem a leitura por regra como plano B)."""
    if not isinstance(c, dict):
        return None
    tipo = str(c.get("tipo") or "").lower()
    if tipo not in {"audiencia", "pericia", "julgamento"}:
        return None
    data = _iso(c.get("data"))
    if not data or data not in _datas_no_texto(texto):
        return None
    base = str(disponibilizacao or "")[:10]
    if base and data < base:
        return None
    hora = c.get("hora") if re.match(r"^\d{2}:\d{2}$", str(c.get("hora") or "")) else None
    if hora and hora not in _horas_no_texto(texto):
        hora = None
    fim = _iso(c.get("data_fim"))
    if fim and (fim < data or fim not in _datas_no_texto(texto)):
        fim = None
    modalidade = c.get("modalidade") if c.get("modalidade") in {"presencial", "online", "hibrida"} else None
    limpo = lambda k, n: (str(c.get(k)).strip()[:n] or None) if c.get(k) else None  # noqa: E731
    link = limpo("link", 500)
    if link and not link.startswith("http"):
        link = None
    return {"tipo": tipo, "subtipo": limpo("subtipo", 60), "data": data, "hora": hora, "data_fim": fim,
            "modalidade": modalidade, "local": limpo("local", 300), "link": link,
            "perito": limpo("perito", 120), "observacoes": limpo("observacoes", 600)}


# Prazo LEGAL: recurso e embargos têm o prazo da lei, que o juiz não muda.
# Os demais (réplica, quesitos, laudo, impugnação) o juiz pode fixar outro —
# aí vale o que ele escreveu.
_PRAZO_LEGAL = {"apelacao", "recurso inominado", "recurso ordinario", "recurso de revista", "recurso especial",
                "recurso extraordinario", "agravo de instrumento", "agravo de peticao", "agravo interno",
                "embargos de declaracao", "contrarrazoes", "embargos a execucao"}


def _chave_da_acao(acao: str, r: str) -> str | None:
    """Qual linha da tabela a providência é ("Opor embargos..." → embargos de declaracao)."""
    a = _norm(acao)
    if "embargos de declara" in a or "embargos declarat" in a:
        return "embargos de declaracao"
    for chave in ("recurso inominado", "recurso ordinario", "recurso de revista", "recurso especial",
                  "recurso extraordinario", "agravo de instrumento", "agravo de peticao", "agravo interno",
                  "contrarrazoes", "replica", "quesitos", "embargos a execucao", "impugnacao ao cumprimento"):
        if chave in a:
            return chave
    if re.search(r"\bapela", a):
        return "apelacao"
    if "impugna" in a and "contesta" in a:
        return "replica"
    if "laudo" in a:
        return "laudo"
    return None


def _linha(r: str, chave: str | None):
    for k, rot, d, fund in TABELA.get(r, []):
        if k == chave:
            return rot, d, fund
    return None


def corrigir_providencia(p: dict | None, r: str) -> dict | None:
    """Nome do recurso e dias pelo rito. O que não está na tabela passa como veio."""
    if not isinstance(p, dict):
        return None
    acao = " ".join(str(p.get("action") or "").split()).rstrip(".;:")
    dias = p.get("days") if isinstance(p.get("days"), (int, float)) and not isinstance(p.get("days"), bool) and p.get("days") > 0 else None
    fund = str(p.get("fundamento") or "").strip() or None
    chave = _chave_da_acao(acao, r)
    if chave in TROCAS.get(r, {}):
        chave = TROCAS[r][chave]
        acao = _linha(r, chave)[0] if _linha(r, chave) else acao
    linha = _linha(r, chave)
    if linha:
        _, d, f = linha
        fixado_pelo_juiz = bool(fund and re.search(r"juiz|fixad|despacho|decisao", _norm(fund)))
        if chave in _PRAZO_LEGAL or not dias or not fixado_pelo_juiz:
            dias, fund = d, f
    return {"action": acao, "days": int(dias) if dias else None, "fundamento": fund}


def salvaguardar(a: dict, it: dict, texto: str) -> dict:
    """A análise da IA depois das regras. Devolve o mesmo formato, corrigido."""
    r = rito(it.get("nome_orgao"), it.get("sigla_tribunal"), it.get("nome_classe"))
    a = dict(a or {})
    doc = _norm(it.get("tipo_documento"))
    ato = str(a.get("tipo_ato") or "").lower()
    if "sentenca" in doc:
        ato = "sentenca"
    elif "acordao" in doc:
        ato = "acordao"
    elif "pauta" in doc:
        ato = "pauta_julgamento"
    a["tipo_ato"] = ato or None
    resultado = a.get("resultado") if a.get("resultado") in {"favoravel", "desfavoravel", "parcial", "neutro"} else None
    a["resultado"] = resultado

    principal = corrigir_providencia(a.get("deadline"), r)
    alternativas = [x for x in (corrigir_providencia(p, r) for p in (a.get("alternativas") or [])[:4]) if x and x["action"]]

    # Sentença/acórdão: o genérico "Manifestar sobre a sentença" vira o que cabe.
    recurso = RECURSO_DA_SENTENCA.get(r) if ato == "sentenca" else RECURSO_DO_ACORDAO.get(r) if ato == "acordao" else None
    if recurso and _linha(r, recurso):
        rot, d, f = _linha(r, recurso)
        generico = not principal or not principal["action"] or re.search(r"manifestar|ciencia|tomar conhecimento|analisar", _norm(principal["action"]))
        if resultado == "favoravel":
            if generico or _chave_da_acao(principal["action"], r) == recurso:
                principal = {"action": "Acompanhar trânsito em julgado", "days": d,
                             "fundamento": f"prazo recursal da parte contrária — {f}"}
        elif generico:
            principal = {"action": rot, "days": d, "fundamento": f}
        emb = _linha(r, "embargos de declaracao")
        if emb and resultado != "favoravel" and not any(_chave_da_acao(x["action"], r) == "embargos de declaracao" for x in alternativas + [principal]):
            alternativas.append({"action": emb[0], "days": emb[1], "fundamento": f"{emb[2]} — se houver omissão, contradição, obscuridade ou erro material"})
        if resultado != "favoravel" and _chave_da_acao(principal["action"], r) != recurso and not any(_chave_da_acao(x["action"], r) == recurso for x in alternativas):
            alternativas.insert(0, {"action": rot, "days": d, "fundamento": f})

    if ato == "pauta_julgamento" and principal and re.search(r"julgamento|pauta|sessao|sustenta", _norm(principal["action"])):
        principal = None  # pauta não abre prazo; a sessão é compromisso

    vistos, unicas = ({_norm(principal["action"])} if principal else set()), []
    for x in alternativas:
        k = _norm(x["action"])
        if k and k not in vistos:
            vistos.add(k)
            unicas.append(x)

    a["deadline"] = principal if principal and (principal.get("action") or principal.get("days")) else None
    a["alternativas"] = unicas[:3]
    a["compromisso"] = conferir_compromisso(a.get("compromisso"), texto, it.get("data_disponibilizacao"))
    a["tutela"] = conferir_tutela(a.get("tutela"), texto)
    a["rito"] = r
    return a
