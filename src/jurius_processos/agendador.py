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

from . import etapas, publicar
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
                "ultimo_ok_em": None}
JANELA = range(6, 23)


def carga_completa(cfg: Config, banco: Banco) -> dict:
    """Servidor novo, banco vazio: todo o histórico antes do primeiro publicar.

    Publicar com o acervo pela metade mudaria status no CRM a partir de meia
    informação. Leva ~30 min (o DataJud responde em 18–53 s por lote).
    """
    crm, advs, clientes, procs = etapas.carregar_crm(cfg)
    exec_id = banco.abrir_execucao("carga_completa")
    res = {
        "descobrir": etapas.descobrir(banco, advs, cfg.djen_inicio),
        "por_processo": etapas.descobrir_por_processo(banco, [p.numero for p in procs if p.numero], cfg.djen_inicio),
        "do_crm": etapas.incluir_do_crm(banco, procs),
        "enriquecer": etapas.enriquecer(banco, cfg, somente_pendentes=False),
    }
    banco.fechar_execucao(exec_id, True, {k: v for k, v in res.items() if k != "enriquecer"})
    return res


def ciclo(cfg: Config, banco: Banco) -> dict:
    if not banco.carga_feita():
        log.info("banco sem carga completa: rodando o histórico desde %s antes do 1º ciclo", cfg.djen_inicio)
        carga_completa(cfg, banco)
    crm, advs, clientes, procs = etapas.carregar_crm(cfg)
    res = {
        "descobrir": etapas.descobrir(banco, advs, (date.today() - timedelta(days=10)).isoformat()),
        "por_processo": etapas.descobrir_por_processo(
            banco, [p.numero for p in procs if p.numero], (date.today() - timedelta(days=10)).isoformat()),
        "do_crm": etapas.incluir_do_crm(banco, procs),
        "enriquecer": etapas.enriquecer(banco, cfg),
        "analisar": etapas.analisar(banco, clientes, procs, financeiro=crm.financeiro()),
    }
    res["publicar"] = publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key, aplicar=cfg.publicar,
                                        cadastrar_auto=cfg.cadastrar_auto, atualizar_status=cfg.atualizar_status)
    return res


def _laco(cfg: Config, banco: Banco, trava: threading.Lock):
    proximo = datetime.now(FUSO)
    ESTADO["iniciado_em"] = proximo.isoformat(timespec="seconds")
    while True:
        ESTADO["proximo"] = proximo.isoformat(timespec="seconds")
        agora = datetime.now(FUSO)
        # A carga inicial roda a qualquer hora; o ciclo normal, só na janela.
        if agora >= proximo and (agora.hour in JANELA or not banco.carga_feita()):
            with trava:
                try:
                    log.info("ciclo: %s", ciclo(cfg, banco))
                    proximo = agora + timedelta(hours=INTERVALO_H)
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
