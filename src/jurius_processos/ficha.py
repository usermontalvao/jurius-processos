"""A ficha do processo que o CRM mostra — montada aqui, lida lá (process_insights).

Partes, vara/comarca, próxima audiência, estágio, pendências e o RESUMO do
processo. Antes a tela do CRM fazia isso no clique: pedia à IA as partes e o
resumo a cada abertura, e o resumo nunca ficava guardado.

O resumo é caro (uma chamada de IA), então só é refeito quando muda algo que
ele leva em conta. `entradas()` junta tudo o que conta — intimações e suas
análises, movimentos, prazos em aberto, agenda, pagamentos, notas internas e o
estágio — e `assinatura()` vira a impressão digital disso. Mesma impressão, o
resumo guardado continua valendo; impressão nova, o servidor escreve outro.

Tudo aqui é puro (sem rede) para os testes cobrirem as regras.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

MAX_INTIMACOES = 12
MAX_MOVIMENTOS = 25
MAX_NOTAS = 6


def _limpo(texto: str | None, limite: int) -> str:
    t = re.sub(r"<[^>]+>", " ", texto or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limite else t[:limite].rstrip() + "..."


_NAO_E_CIDADE = re.compile(r"\b(TRABALHO|FAZENDA|FAM[IÍ]LIA|CONSUMIDOR|REGI[AÃ]O|JUSTI[CÇ]A|SUCESS[OÕ]ES|FEDERAL|"
                           r"CRIMINAL|PRECAT[OÓ]RIOS?|EXECU[CÇ][OÕ]ES|FALE?NCIAS?|REGISTROS?|GABINETE|DESEMBARGADOR[A]?|"
                           r"JUIZADOS?|ESPECIA(?:L|IS)|VARAS?|C[IÍ]VEL|TURMAS?|C[AÂ]MARA|N[UÚ]CLEO|SE[CÇ][AÃ]O|CENTRAL|JEF|JUI|ESP|CIV|SDCR|COMARCA|CAPITAL)\b")


def _cidade(bruto: str) -> str | None:
    c = re.sub(r"/[A-Z]{2}$", "", bruto.strip(" -–"))          # "FEIJÓ/AC"
    if not re.fullmatch(r"[A-ZÀ-Ý][A-ZÀ-Ý' -]{2,40}", c) or _NAO_E_CIDADE.search(c):
        return None
    minusculas = {"de", "da", "do", "das", "dos", "e"}
    return " ".join(p.lower() if p.lower() in minusculas and i else p.capitalize()
                    for i, p in enumerate(c.lower().split()))


def comarca(orgao: str | None) -> str | None:
    """Cidade do órgão, nos formatos que o acervo tem (auditoria de 26/09/2026):
    "3º JUIZADO ESPECIAL CÍVEL DE CUIABÁ" → Cuiabá; "VARA DO TRABALHO DE FEIJÓ/AC"
    → Feijó; "CAMBÉ - JUIZADO ESPECIAL CÍVEL" → Cambé; "06ª Vara JEF- Cuiabá" →
    Cuiabá. Gabinete de desembargador e afins não têm cidade: None."""
    t = re.sub(r"\s+", " ", (orgao or "").strip().upper())
    # TJMT no DataJud: "Décima Vara Cível - Comarca de Cuiabá - SDCR".
    t = re.sub(r"\s*[-–]\s*SDCR\b.*$", "", t)
    t = re.sub(r"\s*[-–]\s*COMARCA DA CAPITAL\b", "", t)
    if not t:
        return None
    if m := re.search(r"\bCOMARCA DE ([A-ZÀ-Ý][A-ZÀ-Ý' ]{2,40}?)(?:\s*[-–]|$)", t):
        if cidade := _cidade(m.group(1)):
            return cidade
    candidatos = []
    if " DE " in t:
        candidatos.append(t.rsplit(" DE ", 1)[1])            # a cidade vem depois do ÚLTIMO " DE "
    if re.search(r"\s?[-–]\s", t) or re.search(r"[-–]\s", t):
        partes = [x for x in re.split(r"\s*[-–]\s*", t) if x]
        candidatos += [partes[-1], partes[0]]                  # "... JEF- CUIABÁ" / "CAMBÉ - JUIZADO ..."
    for c in candidatos:
        if cidade := _cidade(c):
            return cidade
    return None


def comarca_das_instancias(orgaos: list[str | None]) -> str | None:
    """A primeira cidade que aparecer: o órgão mais recente pode ser um gabinete
    de 2º grau, e aí a comarca é a da vara de origem."""
    for o in orgaos:
        if c := comarca(o):
            return c
    return None


def montar(analise: dict, vinculo: dict, hoje: date, orgaos: list[str | None] | None = None) -> dict:
    """As colunas de process_insights que não dependem de IA. orgaos: os das
    outras instâncias e do DJEN, para achar a comarca quando o atual é de 2º grau."""
    partes = (vinculo or {}).get("partes") or {}
    aud = analise.get("audiencia")
    if aud and aud.get("data") and aud["data"] < hoje.isoformat():
        aud = None  # a ficha mostra a PRÓXIMA; a que passou está no histórico
    orgao = analise.get("orgao")
    return {
        "polo_ativo": ", ".join(partes.get("A") or []) or None,
        "polo_passivo": ", ".join(partes.get("P") or []) or None,
        "orgao": orgao,
        "comarca": comarca_das_instancias([orgao, *(orgaos or [])]),
        "tribunal": analise.get("tribunal"),
        "classe": analise.get("classe"),
        "fase": analise.get("status_crm"),
        "situacao": analise.get("situacao"),
        "proxima_audiencia": aud,
        "pendencias": analise.get("pendencias") or [],
    }


def _notas(bruto) -> list[dict]:
    """processes.notes vem como texto solto (130 processos em 26/09/2026) ou lista."""
    if isinstance(bruto, str):
        try:
            bruto = json.loads(bruto)
        except ValueError:
            return [{"quando": None, "texto": bruto}] if bruto.strip() else []
    if isinstance(bruto, str):
        return [{"quando": None, "texto": bruto}] if bruto.strip() else []
    out = []
    for n in bruto or []:
        if isinstance(n, dict) and (n.get("text") or n.get("texto")):
            out.append({"quando": n.get("created_at"), "texto": n.get("text") or n.get("texto")})
    return out


def entradas(proc: dict, analise: dict, ficha: dict, intimacoes: list[dict], movimentos: list[dict],
             prazos: list[dict], agenda: list[dict], acordos: list[dict], notas) -> dict:
    """Tudo o que o resumo leva em conta, já cortado e em ordem estável."""
    ints = sorted(intimacoes, key=lambda i: (i.get("data") or "", i.get("id") or ""), reverse=True)[:MAX_INTIMACOES]
    movs = sorted(movimentos, key=lambda m: m.get("dataHora") or "", reverse=True)[:MAX_MOVIMENTOS]
    return {
        "processo": {"numero": proc.get("codigo"), "area": proc.get("area"), "cliente": proc.get("cliente")},
        "ficha": {k: ficha.get(k) for k in ("polo_ativo", "polo_passivo", "orgao", "classe", "fase", "situacao",
                                            "proxima_audiencia")},
        "pendencias": [p.get("descricao") for p in analise.get("pendencias") or []],
        "intimacoes": [{"id": i.get("id"), "data": (i.get("data") or "")[:10], "tipo": i.get("tipo"),
                        "resumo": i.get("resumo"), "prazo_dias": i.get("prazo_dias"),
                        "texto": _limpo(i.get("texto"), 700)} for i in ints],
        "movimentos": [{"data": (m.get("dataHora") or "")[:10], "nome": m.get("nome")} for m in movs],
        "prazos": sorted([{"titulo": p.get("title"), "vence": (p.get("due_date") or "")[:10], "status": p.get("status")}
                          for p in prazos], key=lambda p: (p["vence"], p["titulo"] or "")),
        "agenda": sorted([{"quando": (a.get("quando") or "")[:16], "titulo": a.get("titulo"), "status": a.get("status")}
                          for a in agenda], key=lambda a: a["quando"]),
        "pagamentos": sorted([{"valor": a.get("total_value"), "status": a.get("status"),
                               "parcelas": sorted([(p.get("status"), (p.get("payment_date") or "")[:10], p.get("paid_value"))
                                                   for p in a.get("parcelas") or []], key=str)}
                              for a in acordos], key=str),
        "notas": [{"quando": (n.get("quando") or "")[:16], "texto": _limpo(n.get("texto"), 400)}
                  for n in _notas(notas)][-MAX_NOTAS:],
    }


def assinatura(e: dict) -> str:
    return hashlib.sha256(json.dumps(e, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# O mesmo pedido que a tela fazia (ProcessesModule, "Resumo Inteligente"), para
# a tela continuar desenhando igual: parágrafo corrido + "**Próximo Passo Recomendado**".
PROMPT_SISTEMA = """Você é um advogado sênior brasileiro especialista em análise processual. Leia o histórico com precisão factual e escreva de forma técnica e direta.

REGRAS ABSOLUTAS:
1. Nunca atribua uma ação a uma parte sem que o texto identifique isso expressamente.
2. Nunca invente datas, prazos, decisões, recursos, petições, valores ou providências.
3. Distribuição, conclusão, juntada, remessa, recebimento, publicação/disponibilização no DJE e atos cartorários não significam, por si só, decisão de mérito nem medida da parte.
4. Se o histórico mostrar apenas atos ordinatórios ou fase inicial, diga isso expressamente.
5. Nunca recomende "apresentar petição inicial" se o processo já está distribuído/ajuizado.
6. Se não houver prazo ou decisão material identificável, diga isso claramente.
7. O próximo passo recomendado deve ser conservador e aderente ao histórico real. Se faltarem elementos para uma providência ativa, recomende apenas acompanhar o andamento.
8. Cite a data (dd/mm/aaaa) do evento que embasa cada afirmação dentro da própria frase. Nunca acrescente sufixos rotulados entre parênteses.
9. Não repita a mesma informação.
10. A agenda, os prazos, os pagamentos e as notas são do escritório: use-os (audiência marcada, prazo em aberto, valor recebido), mas não os confunda com atos do tribunal."""


def prompt(e: dict) -> str:
    f, p = e["ficha"], e["processo"]
    aud = f.get("proxima_audiencia") or {}
    linhas = [
        f"PROCESSO: {p.get('numero')} | ÁREA: {p.get('area') or 'não informada'}",
        f"POLO ATIVO: {f.get('polo_ativo') or p.get('cliente') or 'não identificado'}",
        f"POLO PASSIVO: {f.get('polo_passivo') or 'não identificado'}",
        f"ÓRGÃO: {f.get('orgao') or 'não informado'} | CLASSE: {f.get('classe') or 'não informada'}",
        f"ESTÁGIO (análise do andamento): {f.get('fase') or 'indefinido'} | SITUAÇÃO: {f.get('situacao') or '-'}",
    ]
    if aud:
        linhas.append(f"PRÓXIMA AUDIÊNCIA: {aud.get('tipo')} em {aud.get('data') or 'data não informada'}")
    if e["pendencias"]:
        linhas.append("PENDÊNCIAS APONTADAS: " + "; ".join(e["pendencias"]))
    linhas.append("\n=== INTIMAÇÕES (DJEN, mais recente primeiro) ===")
    for i in e["intimacoes"] or []:
        linhas.append(f"[{i['data']}] {i.get('tipo') or 'Intimação'}"
                      + (f" | prazo: {i['prazo_dias']} dia(s)" if i.get("prazo_dias") else "")
                      + (f"\nResumo prévio: {i['resumo']}" if i.get("resumo") else "")
                      + (f"\nTrecho: {i['texto']}" if i.get("texto") else ""))
    if not e["intimacoes"]:
        linhas.append("Sem intimações.")
    linhas.append("\n=== MOVIMENTOS (DataJud/CNJ, mais recente primeiro) ===")
    linhas += [f"[{m['data']}] {m['nome']}" for m in e["movimentos"]] or ["Sem movimentos."]
    if e["prazos"]:
        linhas.append("\n=== PRAZOS EM ABERTO NO ESCRITÓRIO ===")
        linhas += [f"[{x['vence']}] {x['titulo']} ({x['status']})" for x in e["prazos"]]
    if e["agenda"]:
        linhas.append("\n=== AUDIÊNCIAS NA AGENDA DO ESCRITÓRIO ===")
        linhas += [f"[{a['quando']}] {a['titulo']} ({a['status']})" for a in e["agenda"]]
    if e["pagamentos"]:
        linhas.append("\n=== FINANCEIRO (acordos/recebimentos lançados) ===")
        for a in e["pagamentos"]:
            pagas = sum(1 for s, _, _ in a["parcelas"] if s == "pago")
            linhas.append(f"Acordo {a['status']}: valor {a['valor']}, {pagas}/{len(a['parcelas'])} parcela(s) paga(s)")
    if e["notas"]:
        linhas.append("\n=== NOTAS INTERNAS DO ESCRITÓRIO ===")
        linhas += [f"[{n['quando'] or 's/d'}] {n['texto']}" for n in e["notas"]]
    linhas.append("""
TAREFA:
Escreva a análise em DOIS blocos, exatamente neste formato:

Um único parágrafo corrido (3 a 5 frases, sem título, sem bullets): a fase atual e a última movimentação com sua data; as movimentações anteriores com efeito prático; e prazo, audiência, pagamento ou risco que mereça atenção.

**Próximo Passo Recomendado**
• [Uma frase com a providência concreta e conservadora.]

Não use outros títulos. Baseie-se somente nos dados acima.""")
    return "\n".join(linhas)
