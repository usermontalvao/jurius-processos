"""As etapas do cérebro: descobrir → enriquecer → analisar (fase + vínculo).

Cada etapa é idempotente e pode ser rodada sozinha. Quem dispara é o
agendador (agendador.py) ou a linha de comando (__main__.py).
"""

from __future__ import annotations

import json
import logging
from datetime import date

from . import cnj, fases, financeiro as fin, vinculo
from .banco import Banco
from .config import Config
from .crm import CRM
from .datajud import ClienteDataJud
from .djen import Advogado, ClienteDJEN, meses

log = logging.getLogger(__name__)


# ── 1. DESCOBERTA ──────────────────────────────────────────────────────────
def descobrir(banco: Banco, advogados: list[Advogado], inicio: str, fim: date | None = None,
              djen: ClienteDJEN | None = None) -> dict:
    """Varre o DJEN mês a mês e registra toda intimação e todo processo do advogado."""
    djen = djen or ClienteDJEN()
    fim = fim or date.today()
    exec_id = banco.abrir_execucao("descobrir")
    resumo = {"inicio": inicio, "fim": fim.isoformat(), "intimacoes_novas": 0, "processos_novos": 0,
              "por_motivo": {}, "invalidos": 0, "meses": 0}
    try:
        for adv in advogados:
            for a, b in meses(inicio, fim):
                achados = djen.do_advogado(adv, a, b)
                resumo["meses"] += 1
                for item, motivo in achados.values():
                    numero = cnj.limpar(item.get("numero_processo"))
                    if not numero:
                        resumo["invalidos"] += 1
                        continue
                    if banco.gravar_comunicacao(item, numero, motivo):
                        resumo["intimacoes_novas"] += 1
                        resumo["por_motivo"][motivo] = resumo["por_motivo"].get(motivo, 0) + 1
                    if banco.garantir_processo(numero, "djen"):
                        resumo["processos_novos"] += 1
                log.info("%s %s..%s: %d intimações", adv.nome, a, b, len(achados))
        banco.fechar_execucao(exec_id, True, resumo)
    except Exception as e:
        resumo["erro"] = str(e)
        banco.fechar_execucao(exec_id, False, resumo)
        raise
    resumo["pedidos_djen"] = djen.pedidos
    return resumo


def descobrir_por_processo(banco: Banco, numeros: list[str], inicio: str, fim: date | None = None,
                           djen: ClienteDJEN | None = None) -> dict:
    """Intimações de cada processo do CRM pelo NÚMERO, não só as endereçadas ao advogado.

    A busca pela OAB/nome só traz o que tem o advogado como destinatário. A
    intimação do mesmo processo dirigida à outra parte, ao INSS ou ao próprio
    cliente ficava de fora — o laboratório de 26/09/2026 achou 57 no Supabase
    que o serviço não tinha. Para a linha do tempo, todas contam.
    """
    djen = djen or ClienteDJEN()
    fim = fim or date.today()
    exec_id = banco.abrir_execucao("descobrir_por_processo")
    resumo = {"inicio": inicio, "processos": 0, "intimacoes_novas": 0}
    try:
        for numero in numeros:
            for item in djen.do_processo(numero, inicio, fim.isoformat()):
                if banco.gravar_comunicacao(item, numero, "processo"):
                    resumo["intimacoes_novas"] += 1
            resumo["processos"] += 1
        banco.fechar_execucao(exec_id, True, resumo)
    except Exception as e:
        resumo["erro"] = str(e)
        banco.fechar_execucao(exec_id, False, resumo)
        raise
    resumo["pedidos_djen"] = djen.pedidos
    return resumo


def incluir_do_crm(banco: Banco, crm_processos) -> int:
    """Processos cadastrados no CRM que o DJEN nunca citou também entram no acervo."""
    n = 0
    for p in crm_processos:
        if p.numero and banco.garantir_processo(p.numero, "crm"):
            n += 1
    return n


# ── 2. ENRIQUECIMENTO ──────────────────────────────────────────────────────
def enriquecer(banco: Banco, cfg: Config, somente_pendentes: bool = True, idade_horas: int = 20,
               limite: int | None = None) -> dict:
    """Consulta o DataJud em lotes de até 50 números por tribunal (ver datajud.py)."""
    if somente_pendentes:
        # Nunca consultado, deu erro, está velho, ou chegou intimação depois da última consulta.
        linhas = banco.processos(
            "datajud_consultado_em is null or datajud_status='erro' "
            "or datajud_consultado_em < datetime('now', ?) "
            "or exists (select 1 from comunicacoes c where c.numero = processos.numero "
            "           and c.visto_em > processos.datajud_consultado_em)", (f"-{idade_horas} hours",))
    else:
        linhas = banco.processos()
    numeros = [r["numero"] for r in linhas][:limite]
    exec_id = banco.abrir_execucao("enriquecer")
    cliente = ClienteDataJud(cfg.datajud_key)
    resumo = {"consultados": 0, "ok": 0, "vazio": 0, "erro": 0, "sem_indice": 0}
    for numero, status, inst, erro in cliente.lote(numeros):
        banco.gravar_datajud(numero, status, inst, erro)
        resumo["consultados"] += 1
        resumo[status] += 1
    resumo["consultas_datajud"] = cliente.consultas
    banco.fechar_execucao(exec_id, resumo["erro"] == 0, resumo)
    return resumo


# ── 3. ANÁLISE: fase + vínculo ─────────────────────────────────────────────
def analisar(banco: Banco, clientes, crm_processos, hoje: date | None = None,
             somente: list[str] | None = None, financeiro: dict[str, list[dict]] | None = None) -> dict:
    hoje = hoje or date.today()
    indice = vinculo.IndiceClientes(clientes)
    por_numero = {p.numero: p for p in crm_processos if p.numero}
    exec_id = banco.abrir_execucao("analisar")
    resumo = {"processos": 0, "fases": {}, "vinculos": {}}
    linhas = banco.processos()
    if somente is not None:
        linhas = [r for r in linhas if r["numero"] in set(somente)]
    for linha in linhas:
        numero = linha["numero"]
        comunicacoes = [dict(c) for c in banco.comunicacoes(numero)]
        instancias = json.loads(linha["datajud"]) if linha["datajud"] else []
        a = fases.analisar(numero, instancias, comunicacoes, hoje)
        v = vinculo.vincular(numero, comunicacoes, indice, por_numero.get(numero))
        if financeiro is not None:
            # Alvará × Financeiro só para processo do CRM: o de fora não tem lançamento.
            pid = v.get("crm_process_id")
            acordos = None
            if pid:
                acordos = financeiro.get(pid, []) + (financeiro.get(f"cliente:{v['client_id']}", []) if v.get("client_id") else [])
            textos = [c.get("texto") or "" for c in comunicacoes if "alvar" in (c.get("texto") or "").lower()]
            a = fin.cruzar(a, acordos, textos, hoje)

        antes_a = json.loads(linha["analise"]) if linha["analise"] else None
        antes_v = json.loads(linha["vinculo"]) if linha["vinculo"] else None
        if antes_a and antes_a.get("fase") != a["fase"]:
            banco.evento(numero, "fase_mudou", {"de": antes_a.get("fase"), "para": a["fase"]})
        if v.get("client_id") and (not antes_v or antes_v.get("client_id") != v["client_id"]):
            banco.evento(numero, "vinculado", {"client_id": v["client_id"], "como": v["tipo"]})
        banco.gravar_analise(numero, a, v)

        resumo["processos"] += 1
        resumo["fases"][a["fase"]] = resumo["fases"].get(a["fase"], 0) + 1
        resumo["vinculos"][v["tipo"]] = resumo["vinculos"].get(v["tipo"], 0) + 1
    banco.fechar_execucao(exec_id, True, resumo)
    return resumo


def carregar_crm(cfg: Config):
    crm = CRM(cfg.supabase_url, cfg.supabase_key)
    return crm, crm.advogados(), crm.clientes(), crm.processos()
