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
# Um lote que falha não pode segurar a fila. Medido em 13/11/2026 com o DataJud
# sobrecarregado: 504 do balanceador aos 60 s e 429 (fila de busca cheia). Com
# 5 tentativas de 180 s e espera dobrando até 480 s, UM lote ruim custava até
# 30 min do ciclo. Agora: 3 tentativas, e a passada inteira tem teto.
TENTATIVAS = 3
ESPERA_S = 20
TIMEOUT_S = 100          # a consulta boa leva 18–53 s; o balanceador corta aos 60
FALHAS_SEGUIDAS = 2      # dois lotes seguidos sem resposta: o DataJud está fora, o resto fica para o próximo ciclo
ORCAMENTO_S = 20 * 60    # teto da passada; o que sobra é adiado, não marcado como erro


class RespostaParcial(httpx.HTTPError):
    """HTTP 200 com resultado incompleto: busca que estourou o tempo ou fatia
    (shard) que recusou. O Elasticsearch devolve 200 com MENOS acertos nesses
    casos; lido como "o DataJud não conhece o processo", isso apagava a cópia
    boa de dezenas de processos de uma vez (13/11/2026: 98 viraram 'vazio')."""


class ClienteDataJud:
    def __init__(self, chave: str, cliente: httpx.Client | None = None):
        self.http = cliente or httpx.Client(
            timeout=httpx.Timeout(TIMEOUT_S, connect=20),
            headers={"Authorization": f"APIKey {chave}", "Content-Type": "application/json"},
        )
        self.consultas = 0

    def _buscar(self, indice: str, numeros: list[str]) -> list[dict]:
        # Cada processo pode ter várias instâncias: folga de 4 por número.
        corpo = {"size": len(numeros) * 4, "query": {"terms": {"numeroProcesso": numeros}}}
        espera = ESPERA_S
        ultimo_erro: Exception | None = None
        for tentativa in range(TENTATIVAS):
            try:
                self.consultas += 1
                r = self.http.post(f"{BASE}/{indice}/_search", json=corpo)
                if r.status_code == 404:
                    return []
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                corpo_resp = r.json()
                fatias = corpo_resp.get("_shards") or {}
                if corpo_resp.get("timed_out") or fatias.get("failed"):
                    raise RespostaParcial(f"resposta parcial (timed_out={corpo_resp.get('timed_out')}, _shards={fatias})")
                return [h["_source"] for h in corpo_resp.get("hits", {}).get("hits", [])]
            except (httpx.HTTPError, ValueError) as e:  # ValueError: corpo que não é JSON
                ultimo_erro = e
                log.info("DataJud %s (%d números) tentativa %d: %s", indice, len(numeros), tentativa + 1, e)
                if tentativa + 1 < TENTATIVAS:
                    time.sleep(espera)
                    espera *= 2
        raise RuntimeError(f"DataJud {indice} falhou: {ultimo_erro}")

    def lote(self, numeros: list[str]):
        """Gera (numero, status, instancias, erro) para cada número pedido.

        status: 'ok' | 'vazio' (DataJud não conhece) | 'sem_indice' | 'erro'
                | 'adiado' (nem foi consultado: o DataJud parou de responder ou a
                  passada estourou o teto — fica como estava e volta no próximo ciclo)
        """
        inicio = time.monotonic()
        falhas_seguidas = 0
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
                if falhas_seguidas >= FALHAS_SEGUIDAS or time.monotonic() - inicio > ORCAMENTO_S:
                    for n in pedaco:
                        yield n, "adiado", None, None
                    continue
                try:
                    fontes = self._buscar(indice, pedaco)
                except Exception as e:  # noqa: BLE001 — registra no processo e segue
                    falhas_seguidas += 1
                    for n in pedaco:
                        yield n, "erro", None, str(e)
                    continue
                falhas_seguidas = 0
                por_numero: dict[str, list[dict]] = defaultdict(list)
                for f in fontes:
                    por_numero[f.get("numeroProcesso")].append(f)
                for n in pedaco:
                    inst = por_numero.get(n, [])
                    yield n, ("ok" if inst else "vazio"), inst, None
                log.info("DataJud %s: %d/%d com dados", indice, sum(1 for n in pedaco if por_numero.get(n)), len(pedaco))
