"""Cliente do DJEN (comunicaapi.pje.jus.br) — a fonte da DESCOBERTA.

Três armadilhas medidas em 24/09/2026, com a OAB MT 30021:

1. A OAB é guardada como "30021/O" na maioria dos tribunais. A busca por
   `numeroOab=30021` devolveu 4 intimações em março/2026; `30021/O`, 219.
2. A busca por nome trouxe 232 no mesmo mês, todas do advogado. Nenhuma das
   três buscas sozinha cobre tudo: o sistema faz as três e junta pelo id.
3. Cada página traz no máximo 100 itens, e o limite é de 20 pedidos por janela
   (cabeçalho X-RateLimit-Remaining). Esgotou, espera.
"""

from __future__ import annotations

import calendar
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Iterator

import httpx

log = logging.getLogger(__name__)

BASE = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"


@dataclass(frozen=True)
class Advogado:
    nome: str
    oab: str | None  # só dígitos, sem zeros à esquerda; None = busca só pelo nome
    uf: str | None

    @staticmethod
    def so_nome(nome: str) -> "Advogado | None":
        """Advogado sem OAB cadastrada: a descoberta corre só pelo nome completo."""
        nome = (nome or "").strip()
        return Advogado(nome=nome, oab=None, uf=None) if nome else None

    @staticmethod
    def de_texto(nome: str, oab_texto: str) -> "Advogado | None":
        """Lê o formato do cadastro do CRM: "OAB-MT 30.021", "30021/MT", "MT 30021"."""
        uf = re.search(r"\b([A-Z]{2})\b", (oab_texto or "").upper().replace("OAB", ""))
        num = re.search(r"(\d[\d.]*)", oab_texto or "")
        if not uf or not num:
            return None
        return Advogado(nome=nome.strip(), oab=num.group(1).replace(".", "").lstrip("0"), uf=uf.group(1))


def _sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _nome_chave(s: str) -> str:
    return re.sub(r"\s+", " ", _sem_acento(s or "").upper()).strip()


def pertence(item: dict, adv: Advogado) -> str | None:
    """Por que esta intimação é do advogado: 'oab', 'nome' — ou None (homônimo/ruído)."""
    for d in (item.get("destinatarioadvogados") or []) if adv.oab else []:
        a = d.get("advogado") or {}
        num = re.sub(r"\D", "", a.get("numero_oab") or "").lstrip("0")
        if num == adv.oab and (a.get("uf_oab") or "").upper() == adv.uf:
            return "oab"
    for d in item.get("destinatarioadvogados") or []:
        if _nome_chave((d.get("advogado") or {}).get("nome", "")) == _nome_chave(adv.nome):
            return "nome"
    return None


def meses(inicio: str, fim: date) -> Iterator[tuple[str, str]]:
    a = date.fromisoformat(inicio)
    y, m = a.year, a.month
    while (y, m) <= (fim.year, fim.month):
        ultimo = min(date(y, m, calendar.monthrange(y, m)[1]), fim)
        yield date(y, m, 1).isoformat(), ultimo.isoformat()
        m += 1
        if m == 13:
            y, m = y + 1, 1


class ClienteDJEN:
    def __init__(self, cliente: httpx.Client | None = None):
        self.http = cliente or httpx.Client(timeout=60, headers={"User-Agent": "jurius-processos/1.0"})
        self.pedidos = 0

    def _get(self, params: dict) -> dict:
        espera = 15
        for tentativa in range(6):
            try:
                r = self.http.get(BASE, params=params)
            except httpx.HTTPError as e:
                log.warning("DJEN falhou (%s), tentativa %d", e, tentativa + 1)
                time.sleep(espera)
                espera *= 2
                continue
            self.pedidos += 1
            if r.status_code == 429:
                log.info("DJEN 429 — aguardando %ss", espera)
                time.sleep(espera)
                espera *= 2
                continue
            r.raise_for_status()
            restante = r.headers.get("X-RateLimit-Remaining")
            if restante is not None and int(restante) <= 2:
                time.sleep(20)
            return r.json()
        raise RuntimeError(f"DJEN não respondeu após 6 tentativas: {params}")

    def buscar(self, **filtros) -> Iterator[dict]:
        """Todas as páginas de uma busca."""
        pagina = 1
        while True:
            d = self._get({**filtros, "itensPorPagina": 100, "pagina": pagina})
            itens = d.get("items") or []
            yield from itens
            if len(itens) < 100 or pagina * 100 >= int(d.get("count") or 0):
                return
            pagina += 1

    def do_advogado(self, adv: Advogado, inicio: str, fim: str) -> dict[int, tuple[dict, str]]:
        """Intimações do advogado no período: {id: (item, motivo)}. Já sem homônimos."""
        achados: dict[int, tuple[dict, str]] = {}
        buscas = [{"nomeAdvogado": adv.nome}]
        if adv.oab:
            buscas = [
                {"numeroOab": adv.oab, "ufOab": adv.uf},
                {"numeroOab": f"{adv.oab}/O", "ufOab": adv.uf},
                *buscas,
            ]
        for filtro in buscas:
            for item in self.buscar(**filtro, dataDisponibilizacaoInicio=inicio, dataDisponibilizacaoFim=fim):
                if item["id"] in achados:
                    continue
                motivo = pertence(item, adv)
                if motivo:
                    achados[item["id"]] = (item, motivo)
        return achados

    def do_processo(self, numero: str, inicio: str, fim: str) -> list[dict]:
        return list(self.buscar(numeroProcesso=numero, dataDisponibilizacaoInicio=inicio, dataDisponibilizacaoFim=fim))
