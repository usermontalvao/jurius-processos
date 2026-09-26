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
JANELA = range(6, 23)


def ciclo(cfg: Config, banco: Banco) -> dict:
    _, advs, clientes, procs = etapas.carregar_crm(cfg)
    res = {
        "descobrir": etapas.descobrir(banco, advs, (date.today() - timedelta(days=10)).isoformat()),
        "do_crm": etapas.incluir_do_crm(banco, procs),
        "enriquecer": etapas.enriquecer(banco, cfg),
        "analisar": etapas.analisar(banco, clientes, procs),
    }
    res["publicar"] = publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key, aplicar=cfg.publicar,
                                        cadastrar_auto=cfg.cadastrar_auto, atualizar_status=cfg.atualizar_status)
    return res


def _laco(cfg: Config, banco: Banco, trava: threading.Lock):
    ultimo = None
    while True:
        agora = datetime.now(FUSO)
        devido = ultimo is None or agora - ultimo >= timedelta(hours=INTERVALO_H)
        if devido and agora.hour in JANELA:
            with trava:
                try:
                    log.info("ciclo: %s", ciclo(cfg, banco))
                except Exception:  # noqa: BLE001 — o laço não pode morrer
                    log.exception("ciclo falhou")
            ultimo = agora
        time.sleep(60)


def iniciar(cfg: Config, banco: Banco, trava: threading.Lock):
    threading.Thread(target=_laco, args=(cfg, banco, trava), daemon=True, name="agendador").start()
