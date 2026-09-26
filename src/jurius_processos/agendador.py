"""Agendador interno: roda o ciclo a cada 2 horas, das 06h às 22h (horário de Cuiabá).

Fica dentro do mesmo processo da API para não depender de cron do sistema.
Cada ciclo:
  - DJEN dos últimos 10 dias (cobre fim de semana e feriado);
  - DataJud do que recebeu intimação nova ou está há mais de 20 h sem consulta;
  - análise de todo o acervo (barata: é CPU local);
  - publicação no Supabase (se JURIUS_PUBLICAR=1).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import alimentar, etapas, publicar
from .banco import Banco
from .config import Config

log = logging.getLogger(__name__)
FUSO = ZoneInfo("America/Cuiaba")
INTERVALO_H = 2
# Ciclo que falhou tenta de novo bem antes das 2 h: no 1º boot do servidor a
# carga morreu no meio e ficaria parada até o próximo intervalo.
REPETIR_APOS_FALHA_MIN = 10

# O que o painel mostra sobre o agendador. Falha ANTES de abrir uma execução
# (ex.: o Supabase recusou a leitura dos clientes) não deixava rastro nenhum na
# tabela de execuções — só no log do contêiner, que ninguém vê do navegador.
ESTADO: dict = {"iniciado_em": None, "proximo": None, "ultima_falha": None, "ultima_falha_em": None,
                "ultimo_ok_em": None, "etapa": None, "etapa_desde": None, "batida": None}


def _etapa(nome: str | None):
    """O que o serviço está fazendo agora — o indicador do painel lê daqui."""
    ESTADO["etapa"] = nome
    ESTADO["etapa_desde"] = datetime.now(FUSO).isoformat(timespec="seconds") if nome else None
JANELA = range(6, 23)


def carga_completa(cfg: Config, banco: Banco) -> dict:
    """Servidor novo, banco vazio: todo o histórico antes do primeiro publicar.

    Publicar com o acervo pela metade mudaria status no CRM a partir de meia
    informação. Leva ~30 min (o DataJud responde em 18–53 s por lote).
    """
    crm, advs, clientes, procs = etapas.carregar_crm(cfg)
    exec_id = banco.abrir_execucao("carga_completa")
    res = {}
    _etapa("Primeira carga: intimações pela OAB e pelo nome (DJEN, desde 2023)")
    res["descobrir"] = etapas.descobrir(banco, advs, cfg.djen_inicio)
    _etapa("Primeira carga: intimações pelo número de cada processo do CRM (DJEN)")
    res["por_processo"] = etapas.descobrir_por_processo(banco, [p.numero for p in procs if p.numero], cfg.djen_inicio)
    res["do_crm"] = etapas.incluir_do_crm(banco, procs)
    _etapa("Primeira carga: andamentos de todos os processos (DataJud)")
    res["enriquecer"] = etapas.enriquecer(banco, cfg, somente_pendentes=False)
    banco.fechar_execucao(exec_id, True, {k: v for k, v in res.items() if k != "enriquecer"})
    return res


def ciclo(cfg: Config, banco: Banco) -> dict:
    _etapa("Lendo clientes e processos do CRM")
    if not banco.carga_feita():
        log.info("banco sem carga completa: rodando o histórico desde %s antes do 1º ciclo", cfg.djen_inicio)
        carga_completa(cfg, banco)
    _etapa("Lendo clientes e processos do CRM")
    crm, advs, clientes, procs = etapas.carregar_crm(cfg)
    _etapa("Buscando intimações novas e andamentos, analisando e publicando")
    res = {
        "descobrir": etapas.descobrir(banco, advs, (date.today() - timedelta(days=10)).isoformat()),
        "por_processo": etapas.descobrir_por_processo(
            banco, [p.numero for p in procs if p.numero], (date.today() - timedelta(days=10)).isoformat()),
        "do_crm": etapas.incluir_do_crm(banco, procs),
        "enriquecer": etapas.enriquecer(banco, cfg),
        "analisar": etapas.analisar(banco, clientes, procs, financeiro=crm.financeiro(), agenda=crm.agenda(procs)),
    }
    res["publicar"] = publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key, aplicar=cfg.publicar,
                                        cadastrar_auto=cfg.cadastrar_auto, atualizar_status=cfg.atualizar_status)
    res["alimentar"] = alimentar_crm(cfg, banco, procs, aplicar=cfg.publicar)
    return res


def alimentar_crm(cfg: Config, banco: Banco, procs, aplicar: bool, somente: list[str] | None = None) -> dict:
    """As entregas que substituem as rotinas do Supabase, cada uma no seu
    interruptor e na sua execução: uma que falha não impede as outras.

    somente: números (sem máscara) — o "Atualizar" de um processo. procs é
    sempre a lista INTEIRA do CRM (é dela que sai o vínculo das intimações)."""
    alvo = set(somente) if somente is not None else None
    ids = None if alvo is None else [p.id for p in procs if p.numero in alvo]
    entregas = []
    if cfg.alimentar_intimacoes:
        entregas.append(("alimentar_intimacoes", lambda: alimentar.intimacoes(banco, cfg, procs, aplicar, somente=alvo)))
        if alvo is None:  # auto-cura é do ciclo, não do clique
            entregas.append(("revincular_orfas", lambda: alimentar.revincular_orfas(cfg, procs, aplicar)))
    if cfg.alimentar_datajud:
        entregas.append(("alimentar_datajud", lambda: alimentar.datajud(banco, cfg, procs, aplicar, somente=alvo)))
    if cfg.alimentar_ia:
        entregas.append(("alimentar_ia", lambda: alimentar.ia(cfg, aplicar, process_ids=ids)))
    # Depois da IA das intimações: o resumo usa as análises que ela acabou de gravar.
    if getattr(cfg, "alimentar_ficha", False):
        entregas.append(("alimentar_ficha", lambda: alimentar.ficha(banco, cfg, procs, aplicar, somente=alvo)))
    res = {}
    for nome, fazer in entregas:
        _etapa(f"Alimentando o CRM: {nome.replace('alimentar_', '').replace('_', ' ')}")
        exec_id = banco.abrir_execucao(nome)
        try:
            res[nome] = fazer()
            banco.fechar_execucao(exec_id, True, res[nome])
        except Exception as e:  # noqa: BLE001
            log.exception("%s falhou", nome)
            res[nome] = {"erro": f"{type(e).__name__}: {e}"[:300]}
            banco.fechar_execucao(exec_id, False, res[nome])
    return res


def _laco(cfg: Config, banco: Banco, trava: threading.Lock):
    proximo = datetime.now(FUSO)
    ESTADO["iniciado_em"] = proximo.isoformat(timespec="seconds")
    while True:
        ESTADO["proximo"] = proximo.isoformat(timespec="seconds")
        ESTADO["batida"] = datetime.now(FUSO).isoformat(timespec="seconds")
        agora = datetime.now(FUSO)
        # A carga inicial roda a qualquer hora; o ciclo normal, só na janela.
        if agora >= proximo and (agora.hour in JANELA or not banco.carga_feita()):
            with trava:
                try:
                    try:
                        log.info("ciclo: %s", ciclo(cfg, banco))
                    finally:
                        _etapa(None)
                    proximo = agora + timedelta(hours=INTERVALO_H)
                    ESTADO["ultima_falha"] = None
                    ESTADO["ultimo_ok_em"] = datetime.now(FUSO).isoformat(timespec="seconds")
                except Exception as e:  # noqa: BLE001 — o laço não pode morrer
                    log.exception("ciclo falhou; nova tentativa em %s min", REPETIR_APOS_FALHA_MIN)
                    proximo = agora + timedelta(minutes=REPETIR_APOS_FALHA_MIN)
                    ESTADO["ultima_falha"] = f"{type(e).__name__}: {e}"[:500]
                    ESTADO["ultima_falha_em"] = datetime.now(FUSO).isoformat(timespec="seconds")
        time.sleep(30)


def iniciar(cfg: Config, banco: Banco, trava: threading.Lock):
    if n := banco.fechar_interrompidas():
        log.warning("%d execução(ões) interrompida(s) numa subida anterior foram fechadas como falha", n)
    threading.Thread(target=_laco, args=(cfg, banco, trava), daemon=True, name="agendador").start()
