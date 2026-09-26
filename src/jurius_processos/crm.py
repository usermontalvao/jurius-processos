"""Leitura do CRM (Supabase REST). Nesta etapa o cérebro só LÊ do CRM."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import cnj
from .config import cabecalhos_supabase
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
    codigo: str | None = None  # process_code como está no CRM (com máscara)


class CRM:
    def __init__(self, url: str, chave: str):
        if not url or not chave:
            raise RuntimeError("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios")
        self.http = httpx.Client(
            base_url=f"{url}/rest/v1",
            timeout=60,
            headers=cabecalhos_supabase(chave),
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

    def agenda(self, procs: list[ProcessoCRM]) -> dict[str, list[dict]]:
        """Audiências da agenda do CRM por processo: {process_id: [{quando, titulo, status}]}.

        Em 26/09/2026 só 56 das 137 audiências tinham processo; 74 só o cliente.
        Sem processo, vale o cliente que tem UM processo não arquivado (27 delas);
        com dois ou mais, não há como saber de qual é e a audiência fica de fora.
        """
        ativos: dict[str, list[str]] = {}
        for p in procs:
            if p.client_id and (p.status or "") != "arquivado":
                ativos.setdefault(p.client_id, []).append(p.id)
        out: dict[str, list[dict]] = {}
        for e in self._tudo("calendar_events", "process_id,client_id,title,start_at,status,created_at",
                            {"event_type": "eq.hearing"}):
            pid = e.get("process_id")
            if not pid:
                ids = ativos.get(e.get("client_id") or "", [])
                pid = ids[0] if len(ids) == 1 else None
            if pid:
                out.setdefault(pid, []).append({"quando": e.get("start_at"), "titulo": e.get("title") or "",
                                                "status": e.get("status"), "criado_em": e.get("created_at")})
        return out

    def prazos(self, procs: list[ProcessoCRM]) -> dict[str, list[dict]]:
        """Prazos por processo, para o estágio (réplica cumprida → aguardando
        sentença). Prazo só com o cliente vale para o único processo ativo dele."""
        ativos: dict[str, list[str]] = {}
        for p in procs:
            if p.client_id and (p.status or "") != "arquivado":
                ativos.setdefault(p.client_id, []).append(p.id)
        out: dict[str, list[dict]] = {}
        for d in self._tudo("deadlines", "process_id,client_id,title,due_date,status",
                            {"deleted_at": "is.null", "status": "neq.cancelado"}):
            pid = d.get("process_id")
            if not pid:
                ids = ativos.get(d.get("client_id") or "", [])
                pid = ids[0] if len(ids) == 1 else None
            if pid:
                out.setdefault(pid, []).append(d)
        return out

    def para_a_ficha(self) -> dict[str, dict]:
        """O que a ficha/resumo precisa do CRM, por processo, numa varredura só:
        {process_id: {area, notas, intimacoes, prazos}}. Uma consulta por tabela
        (e não por processo): o ciclo lê os ~200 processos de uma vez."""
        out: dict[str, dict] = {}
        for p in self._tudo("processes", "id,practice_area,notes"):
            out[p["id"]] = {"area": p.get("practice_area"), "notas": p.get("notes"), "intimacoes": [], "prazos": []}
        for i in self._tudo("djen_comunicacoes",
                            "id,process_id,data_disponibilizacao,tipo_documento,texto,created_at,"
                            "intimation_ai_analysis(summary,deadline_days,deadline_due_date,urgency)",
                            {"process_id": "not.is.null"}):
            if i["process_id"] in out:
                a = i.get("intimation_ai_analysis") or {}
                a = a[0] if isinstance(a, list) and a else (a if isinstance(a, dict) else {})
                out[i["process_id"]]["intimacoes"].append({
                    "id": i["id"], "data": i.get("data_disponibilizacao"), "tipo": i.get("tipo_documento"),
                    "texto": i.get("texto"), "resumo": a.get("summary"), "prazo_dias": a.get("deadline_days"),
                    "vencimento": a.get("deadline_due_date"), "urgencia": a.get("urgency"),
                    "chegou_em": i.get("created_at")})
        # Prazo sem processo, só com o cliente: 323 assim em 26/09/2026. Vai para
        # a chave "cliente:<id>" — o alerta de cadastro conta com ele.
        for d in self._tudo("deadlines", "process_id,client_id,title,description,due_date,status,created_at,intimation_id",
                            {"deleted_at": "is.null", "status": "neq.cancelado"}):
            if d.get("process_id") in out:
                out[d["process_id"]]["prazos"].append(d)
            elif not d.get("process_id") and d.get("client_id"):
                out.setdefault(f"cliente:{d['client_id']}", {"prazos": []})["prazos"].append(d)
        return out

    def processos(self) -> list[ProcessoCRM]:
        return [
            ProcessoCRM(p["id"], cnj.limpar(p.get("process_code")), p["client_id"], p.get("status"), bool(p.get("status_manual")),
                        p.get("process_code"))
            for p in self._tudo("processes", "id,process_code,client_id,status,status_manual")
        ]
