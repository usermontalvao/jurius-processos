"""Cliente da API pública do DataJud (CNJ) — a fonte do ENRIQUECIMENTO.

Medido em 24/09/2026: cada consulta leva de 18 a 53 s, de qualquer origem,
e o DataJud devolve 429 com mais de 2–3 consultas simultâneas. A Edge Function
antiga consultava UM processo por vez e desistia em 12 s: ~70% de "timeout".

A saída é consultar em LOTE: uma busca `terms` com até 50 números do mesmo
tribunal leva o mesmo tempo que uma busca de um número só (40 processos em
35 s no teste). 475 processos viram ~15 consultas em vez de 475.

Um mesmo número volta com VÁRIAS instâncias (1º grau + 2º grau, Juizado +
Turma Recursal). A versão antiga pedia `size: 1` e perdia metade do processo.

O DataJud não traz nome de parte (LGPD) nem processo em segredo de justiça, e
atrasa dias em relação ao tribunal. Serve para andamento e fase — nunca para
achar o cliente.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

import httpx

from . import cnj

log = logging.getLogger(__name__)

BASE = "https://api-publica.datajud.cnj.jus.br"
LOTE = 50


class ClienteDataJud:
    def __init__(self, chave: str, cliente: httpx.Client | None = None):
        self.http = cliente or httpx.Client(
            timeout=httpx.Timeout(180, connect=20),
            headers={"Authorization": f"APIKey {chave}", "Content-Type": "application/json"},
        )
        self.consultas = 0

    def _buscar(self, indice: str, numeros: list[str]) -> list[dict]:
        # Cada processo pode ter várias instâncias: folga de 4 por número.
        corpo = {"size": len(numeros) * 4, "query": {"terms": {"numeroProcesso": numeros}}}
        espera = 30
        ultimo_erro: Exception | None = None
        for tentativa in range(5):
            try:
                self.consultas += 1
                r = self.http.post(f"{BASE}/{indice}/_search", json=corpo)
                if r.status_code == 404:
                    return []
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return [h["_source"] for h in r.json().get("hits", {}).get("hits", [])]
            except httpx.HTTPError as e:
                ultimo_erro = e
                log.info("DataJud %s (%d números) tentativa %d: %s", indice, len(numeros), tentativa + 1, e)
                time.sleep(espera)
                espera *= 2
        raise RuntimeError(f"DataJud {indice} falhou: {ultimo_erro}")

    def lote(self, numeros: list[str]):
        """Gera (numero, status, instancias, erro) para cada número pedido.

        status: 'ok' | 'vazio' (DataJud não conhece) | 'sem_indice' | 'erro'
        """
        por_indice: dict[str, list[str]] = defaultdict(list)
        for n in numeros:
            indice = cnj.indice_datajud(n)
            if indice:
                por_indice[indice].append(n)
            else:
                yield n, "sem_indice", None, "tribunal sem índice no DataJud"

        for indice, lista in sorted(por_indice.items(), key=lambda kv: -len(kv[1])):
            for i in range(0, len(lista), LOTE):
                pedaco = lista[i:i + LOTE]
                try:
                    fontes = self._buscar(indice, pedaco)
                except Exception as e:  # noqa: BLE001 — registra no processo e segue
                    for n in pedaco:
                        yield n, "erro", None, str(e)
                    continue
                por_numero: dict[str, list[dict]] = defaultdict(list)
                for f in fontes:
                    por_numero[f.get("numeroProcesso")].append(f)
                for n in pedaco:
                    inst = por_numero.get(n, [])
                    yield n, ("ok" if inst else "vazio"), inst, None
                log.info("DataJud %s: %d/%d com dados", indice, sum(1 for n in pedaco if por_numero.get(n)), len(pedaco))
