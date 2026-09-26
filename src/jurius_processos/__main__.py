"""Linha de comando.

    python -m jurius_processos descobrir [--desde 2023-01-01]
    python -m jurius_processos enriquecer [--todos] [--limite N]
    python -m jurius_processos analisar
    python -m jurius_processos relatorio [--saida relatorio.md]
    python -m jurius_processos publicar [--aplicar]   # sem --aplicar é ensaio
    python -m jurius_processos ciclo          # descobrir → enriquecer → analisar (→ publicar)
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta

from . import etapas, publicar, relatorio
from .banco import Banco
from .config import carregar


def main():
    p = argparse.ArgumentParser(prog="jurius_processos")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("descobrir")
    d.add_argument("--desde")
    e = sub.add_parser("enriquecer")
    e.add_argument("--todos", action="store_true")
    e.add_argument("--limite", type=int)
    sub.add_parser("analisar")
    r = sub.add_parser("relatorio")
    r.add_argument("--saida", default="dados/relatorio.md")
    sub.add_parser("ciclo")
    pb = sub.add_parser("publicar")
    pb.add_argument("--aplicar", action="store_true")
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = carregar()
    banco = Banco(cfg.banco)

    if a.cmd == "descobrir":
        _, advs, _, procs = etapas.carregar_crm(cfg)
        print("advogados:", [(x.nome, x.oab, x.uf) for x in advs])
        res = etapas.descobrir(banco, advs, a.desde or cfg.djen_inicio)
        res["do_crm"] = etapas.incluir_do_crm(banco, procs)
    elif a.cmd == "enriquecer":
        res = etapas.enriquecer(banco, cfg, somente_pendentes=not a.todos, limite=a.limite)
    elif a.cmd == "analisar":
        _, _, clientes, procs = etapas.carregar_crm(cfg)
        res = etapas.analisar(banco, clientes, procs)
    elif a.cmd == "relatorio":
        _, _, clientes, procs = etapas.carregar_crm(cfg)
        res = {"arquivo": relatorio.gerar(banco, clientes, procs, a.saida)}
    elif a.cmd == "publicar":
        _, _, _, procs = etapas.carregar_crm(cfg)
        res = publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key,
                                aplicar=a.aplicar or cfg.publicar,
                                cadastrar_auto=cfg.cadastrar_auto, atualizar_status=cfg.atualizar_status)
    else:  # ciclo: só a janela recente no DJEN; a carga completa é o `descobrir`
        _, advs, clientes, procs = etapas.carregar_crm(cfg)
        res = {
            "descobrir": etapas.descobrir(banco, advs, (date.today() - timedelta(days=10)).isoformat()),
            "do_crm": etapas.incluir_do_crm(banco, procs),
            "enriquecer": etapas.enriquecer(banco, cfg),
            "analisar": etapas.analisar(banco, clientes, procs),
        }
        res["publicar"] = publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key,
                                            aplicar=cfg.publicar, cadastrar_auto=cfg.cadastrar_auto,
                                            atualizar_status=cfg.atualizar_status)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
