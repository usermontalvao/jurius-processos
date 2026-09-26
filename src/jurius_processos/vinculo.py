"""Vínculo: processo ↔ cadastro do CRM, processo ↔ cliente, processo ↔ processos.

Regra (a mesma da vinculação automática do Nextcloud): age sozinho SÓ quando
não há como errar. Do contrário, sugere e alguém confirma com um clique.

  cadastro     o número já está em `processes` do CRM → cliente de lá. Certo.
  nome_exato   nome da parte == nome de UM cliente cadastrado. Certo.
  sugestao     nome parecido, pré-cadastro, ou mais de um cliente possível.
  sem_cliente  nenhuma parte bate com cliente: aparece com o nome da parte.
  divergente   o CRM diz um cliente e as partes do processo dizem outro.

Os nomes das partes vêm do DJEN (`destinatarios`, com o polo). O DataJud não
tem parte nenhuma.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict

from . import cnj

_PARTICULAS = {"DA", "DE", "DO", "DAS", "DOS", "E", "D"}
# Nome de ente/empresa: nunca é "cliente pessoa" provável quando o outro polo tem gente.
_ENTE = re.compile(
    r"\b(LTDA|S\.?A\.?|S/A|EIRELI|ME|EPP|BANCO|INSTITUTO|UNIAO|ESTADO|MUNICIPIO|FAZENDA|INSS|"
    r"SEGURADORA|COMPANHIA|CIA|ASSOCIACAO|COOPERATIVA|FUNDACAO|CAIXA|TELECOM|TELECON|ENERGIA|"
    r"FEDERAL|MINISTERIO|PREFEITURA|SERVICOS|COMERCIO|INDUSTRIA|CONSORCIO|FUNDO)\b")


def normalizar(nome: str | None) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", nome or "") if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Za-z ]", " ", s).upper()
    return re.sub(r"\s+", " ", s).strip()


def tokens(nome: str) -> list[str]:
    return [t for t in normalizar(nome).split() if t not in _PARTICULAS]


def e_ente(nome: str) -> bool:
    return bool(_ENTE.search(normalizar(nome)))


class IndiceClientes:
    def __init__(self, clientes):
        self.clientes = {c.id: c for c in clientes}
        self.por_nome: dict[str, list] = defaultdict(list)
        self.por_ponta: dict[tuple[str, str], list] = defaultdict(list)
        for c in clientes:
            self.por_nome[normalizar(c.nome)].append(c)
            t = tokens(c.nome)
            if len(t) >= 2:
                self.por_ponta[(t[0], t[-1])].append(c)

    def procurar(self, nome: str) -> tuple[str, list[dict]]:
        """('certo', [cliente]) | ('sugestao', [...]) | ('nada', [])."""
        exatos = self.por_nome.get(normalizar(nome), [])
        definitivos = [c for c in exatos if not c.pre_cadastro]
        # "JOSE DA SILVA" é idêntico e ainda assim arriscado: nome curto só sugere.
        curto = len(tokens(nome)) < 3
        if len(definitivos) == 1 and not curto:
            return "certo", [_sug(definitivos[0], 1.0, "nome idêntico")]
        if exatos:
            motivo = ("nome idêntico a mais de um cliente" if len(definitivos) > 1
                      else "nome idêntico, mas curto demais para vincular sozinho" if definitivos
                      else "nome idêntico a um pré-cadastro")
            return "sugestao", [_sug(c, 0.95, motivo) for c in exatos]

        t = tokens(nome)
        if len(t) < 2:
            return "nada", []
        achados = []
        for c in self.por_ponta.get((t[0], t[-1]), []):
            tc = tokens(c.nome)
            a, b = set(t), set(tc)
            if a <= b or b <= a:
                achados.append(_sug(c, 0.9, "mesmo primeiro e último nome; um nome abreviado ou faltando"))
            elif len(a & b) / len(a | b) >= 0.6:
                achados.append(_sug(c, 0.75, "nomes parecidos"))
        return ("sugestao", achados) if achados else ("nada", [])


def _sug(c, score: float, motivo: str) -> dict:
    return {"client_id": c.id, "nome": c.nome, "score": score, "motivo": motivo, "pre_cadastro": c.pre_cadastro}


def partes(comunicacoes: list[dict], advogados_nomes: set[str] | None = None) -> dict[str, list[str]]:
    """Partes por polo, na ordem em que aparecem, sem o próprio advogado."""
    advs = {normalizar(n) for n in (advogados_nomes or set())}
    for c in comunicacoes:
        for a in json.loads(c.get("advogados") or "[]"):
            advs.add(normalizar(a.get("nome")))
    polos: dict[str, list[str]] = {"A": [], "P": []}
    vistos = set()
    for c in comunicacoes:
        for d in json.loads(c.get("destinatarios") or "[]"):
            nome, polo = (d.get("nome") or "").strip(), d.get("polo")
            chave = normalizar(nome)
            if not nome or chave in advs or chave in vistos or polo not in polos:
                continue
            vistos.add(chave)
            polos[polo].append(nome)
    return polos


def relacionados(numero: str, comunicacoes: list[dict]) -> list[str]:
    """Outros processos citados no texto das intimações (origem, apenso, recurso)."""
    achados = set()
    for c in comunicacoes:
        achados |= cnj.mencoes(c.get("texto"))
    achados.discard(numero)
    return sorted(achados)


def vincular(numero: str, comunicacoes: list[dict], indice: IndiceClientes, crm_processo=None) -> dict:
    ps = partes(comunicacoes)
    rel = relacionados(numero, comunicacoes)

    # Cliente de cada parte, polo ativo primeiro (é onde o escritório costuma estar).
    certos: dict[str, dict] = {}
    sugestoes: dict[str, dict] = {}
    polo_de: dict[str, str] = {}
    for polo in ("A", "P"):
        for nome in ps[polo]:
            veredito, lista = indice.procurar(nome)
            for s in lista:
                s = {**s, "parte": nome, "polo": polo}
                polo_de.setdefault(s["client_id"], polo)
                if veredito == "certo":
                    certos.setdefault(s["client_id"], s)
                else:
                    sugestoes.setdefault(s["client_id"], s)

    principal = next((n for n in ps["A"] if not e_ente(n)), None) or (ps["A"] or ps["P"] or [None])[0]
    base = {
        "partes": ps,
        "parte_principal": principal,
        "relacionados": rel,
        "crm_process_id": crm_processo.id if crm_processo else None,
    }

    if crm_processo:
        cid = crm_processo.client_id
        c = indice.clientes.get(cid)
        outros = [s for k, s in certos.items() if k != cid]
        # Divergência só quando a parte certa é OUTRO cliente e o do CRM não aparece nas partes.
        if outros and cid not in certos and cid not in sugestoes:
            return {**base, "tipo": "divergente", "client_id": cid, "client_nome": c.nome if c else None,
                    "polo_cliente": None, "sugestoes": outros,
                    "motivo": "O CRM vincula um cliente que não aparece nas partes; as partes batem com outro"}
        return {**base, "tipo": "cadastro", "client_id": cid, "client_nome": c.nome if c else None,
                "polo_cliente": polo_de.get(cid), "sugestoes": [], "motivo": "Processo já cadastrado no CRM"}

    if len(certos) == 1:
        s = next(iter(certos.values()))
        return {**base, "tipo": "nome_exato", "client_id": s["client_id"], "client_nome": s["nome"],
                "polo_cliente": s["polo"], "sugestoes": list(sugestoes.values()),
                "motivo": f"Parte \"{s['parte']}\" é cliente cadastrado"}
    if len(certos) > 1:
        # Dois clientes no mesmo processo (ex.: litisconsórcio). Não escolho: sugiro os dois.
        return {**base, "tipo": "sugestao", "client_id": None, "client_nome": None, "polo_cliente": None,
                "sugestoes": list(certos.values()) + list(sugestoes.values()),
                "motivo": "Mais de um cliente cadastrado entre as partes"}
    if sugestoes:
        return {**base, "tipo": "sugestao", "client_id": None, "client_nome": None, "polo_cliente": None,
                "sugestoes": sorted(sugestoes.values(), key=lambda s: -s["score"]),
                "motivo": "Nome parecido com cliente cadastrado"}
    return {**base, "tipo": "sem_cliente", "client_id": None, "client_nome": None, "polo_cliente": None,
            "sugestoes": [], "motivo": "Nenhuma parte é cliente cadastrado"}


def processos_do_nome(nome: str, partes_por_processo: dict[str, dict[str, list[str]]]) -> list[tuple[str, str]]:
    """Para o cadastro de cliente: processos do acervo em que este nome é parte.

    Devolve [(numero, polo)] só com nome idêntico — é o caso que vincula sozinho
    e avisa "este cliente já tinha processo; vinculei".
    """
    alvo = normalizar(nome)
    out = []
    for numero, ps in partes_por_processo.items():
        for polo in ("A", "P"):
            if any(normalizar(n) == alvo for n in ps.get(polo, [])):
                out.append((numero, polo))
                break
    return out
