"""Leitura do CRM (Supabase REST). Nesta etapa o cérebro só LÊ do CRM."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import cnj
from .djen import Advogado


@dataclass(frozen=True)
class Cliente:
    id: str
    nome: str
    pre_cadastro: bool
    cpf_cnpj: str | None


@dataclass(frozen=True)
class ProcessoCRM:
    id: str
    numero: str | None
    client_id: str
    status: str | None
    status_manual: bool


class CRM:
    def __init__(self, url: str, chave: str):
        if not url or not chave:
            raise RuntimeError("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios")
        self.http = httpx.Client(
            base_url=f"{url}/rest/v1",
            timeout=60,
            headers={"apikey": chave, "Authorization": f"Bearer {chave}"},
        )

    def _tudo(self, tabela: str, select: str, filtros: dict | None = None) -> list[dict]:
        linhas, inicio, passo = [], 0, 1000
        while True:
            r = self.http.get(
                f"/{tabela}",
                params={"select": select, **(filtros or {})},
                headers={"Range": f"{inicio}-{inicio + passo - 1}"},
            )
            r.raise_for_status()
            lote = r.json()
            linhas += lote
            if len(lote) < passo:
                return linhas
            inicio += passo

    def advogados(self) -> list[Advogado]:
        """Quem a descoberta procura no DJEN.

        - perfil com OAB (`profiles.oab`): busca pela OAB E pelo nome completo;
        - perfil só com nome de advogado (`lawyer_full_name`), sem OAB: pelo nome;
        - a lista "advogados monitorados" das configurações de Intimações
          (`system_settings.djen_config.lawyers_to_monitor`): pelo nome.
        O mesmo nome (sem acento, sem caixa) entra uma vez só; a versão com OAB vence.
        """
        from .djen import _nome_chave
        por_nome: dict[str, Advogado] = {}

        def pôr(a: Advogado | None):
            if not a or not a.nome:
                return
            chave = _nome_chave(a.nome)
            atual = por_nome.get(chave)
            if atual is None or (a.oab and not atual.oab):
                por_nome[chave] = a

        for p in self._tudo("profiles", "lawyer_full_name,name,oab"):
            nome = p.get("lawyer_full_name") or ""
            oab = p.get("oab") or ""
            if oab:
                pôr(Advogado.de_texto(nome or p.get("name") or "", oab))
            if nome:
                pôr(Advogado.so_nome(nome))
        cfg = self._tudo("system_settings", "value", {"key": "eq.djen_config"})
        for nome in ((cfg[0].get("value") or {}).get("lawyers_to_monitor") or []) if cfg else []:
            pôr(Advogado.so_nome(nome))
        return list(por_nome.values())

    def clientes(self) -> list[Cliente]:
        # Clientes fundidos em outro (merged_into_client_id) não recebem vínculo.
        return [
            Cliente(c["id"], c["full_name"], bool(c.get("is_pre_cadastro")), c.get("cpf_cnpj"))
            for c in self._tudo("clients", "id,full_name,is_pre_cadastro,cpf_cnpj", {"merged_into_client_id": "is.null"})
            if c.get("full_name")
        ]

    def arquivados_por_pessoa(self) -> set[str]:
        """Processos cujo ÚLTIMO arquivamento foi feito por uma pessoa.

        O `audit_log` registra quem mudou o status: `user_id` nulo é robô (cron,
        Edge Function, gatilho). Arquivamento de pessoa é decisão do escritório
        e o cérebro nunca desfaz; o de robô ele corrige. Em 26/09/2026 os 69
        arquivados do CRM eram todos de robô, e o cron de palavras-chave
        arquivava e o do DataJud desarquivava o mesmo processo quase todo dia.
        """
        ultimo: dict[str, tuple[str, str | None]] = {}
        for a in self._tudo("audit_log", "entity_id,user_id,created_at",
                            {"entity_type": "eq.processes", "new_value->>status": "eq.arquivado",
                             "order": "created_at.asc"}):
            ultimo[str(a["entity_id"])] = (a["created_at"], a.get("user_id"))
        return {pid for pid, (_, uid) in ultimo.items() if uid}

    def financeiro(self) -> dict[str, list[dict]]:
        """Acordos do Financeiro por processo do CRM, cada um com suas parcelas.

        Acordo sem processo ligado entra pelo cliente (chave `cliente:<id>`):
        o alvará de um processo pode ter sido lançado no acordo "solto" do cliente.
        """
        acordos = self._tudo("agreements", "id,client_id,process_id,total_value,status")
        parcelas: dict[str, list[dict]] = {}
        for p in self._tudo("installments", "agreement_id,status,payment_date,paid_value,value"):
            parcelas.setdefault(p["agreement_id"], []).append(p)
        out: dict[str, list[dict]] = {}
        for a in acordos:
            a["parcelas"] = parcelas.get(a["id"], [])
            chave = a["process_id"] or f"cliente:{a['client_id']}"
            out.setdefault(chave, []).append(a)
        return out

    def processos(self) -> list[ProcessoCRM]:
        return [
            ProcessoCRM(p["id"], cnj.limpar(p.get("process_code")), p["client_id"], p.get("status"), bool(p.get("status_manual")))
            for p in self._tudo("processes", "id,process_code,client_id,status,status_manual")
        ]
