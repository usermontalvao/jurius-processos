"""Plano de publicação: quem pode mudar o status de `processes`."""

import json
from types import SimpleNamespace

from jurius_processos.publicar import planejar


class BancoFalso:
    def __init__(self, linhas):
        self.linhas = linhas

    def processos(self, *_):
        return self.linhas


def _linha(numero, pid, status_crm):
    return {"numero": numero, "analise": json.dumps({"status_crm": status_crm}),
            "vinculo": json.dumps({"crm_process_id": pid, "tipo": "cadastro"})}


def _crm(pid, status, manual=False):
    return SimpleNamespace(id=pid, status=status, status_manual=manual)


def test_desarquiva_o_que_robo_arquivou_e_nunca_o_que_pessoa_arquivou():
    banco = BancoFalso([_linha("1" * 20, "robo", "cumprimento"), _linha("2" * 20, "pessoa", "cumprimento")])
    crm = [_crm("robo", "arquivado"), _crm("pessoa", "arquivado")]
    plano = planejar(banco, crm, arquivados_por_pessoa={"pessoa"})
    assert [s["id"] for s in plano["status"]] == ["robo"]
    assert [s["id"] for s in plano["conferir"]] == ["pessoa"]


def test_sem_lista_de_auditoria_nao_desarquiva_nada():
    banco = BancoFalso([_linha("1" * 20, "robo", "cumprimento")])
    plano = planejar(banco, [_crm("robo", "arquivado")])
    assert plano["status"] == [] and len(plano["conferir"]) == 1


def test_status_manual_e_soberano():
    banco = BancoFalso([_linha("1" * 20, "x", "recurso")])
    plano = planejar(banco, [_crm("x", "sentenca", manual=True)], arquivados_por_pessoa=set())
    assert plano["status"] == [] and len(plano["conferir"]) == 1


def test_troca_de_status_so_avisa_o_cliente_quando_o_fato_e_recente():
    from datetime import date
    from jurius_processos.publicar import status_e_novidade
    hoje = date(2026, 9, 26)
    assert status_e_novidade("2026-09-20", hoje)          # conciliação de ontem → aviso
    assert not status_e_novidade("2026-07-17", hoje)      # corrigir fato de julho → calado
    assert not status_e_novidade(None, hoje)              # sem data do fato → calado
