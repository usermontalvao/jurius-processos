"""Painel da raiz: só números agregados, e as contagens batem com o acervo."""

import json

from jurius_processos.banco import Banco
from jurius_processos.painel import HTML, dados


def test_painel_agrega_sem_expor_nome_de_parte(tmp_path):
    b = Banco(tmp_path / "t.sqlite3")
    for numero, fase, tipo, pid in [("1" * 20, "conhecimento", "sem_cliente", None),
                                    ("2" * 20, "recursal", "cadastro", "p1")]:
        b.garantir_processo(numero, "djen")
        b.gravar_analise(numero, {"fase": fase, "situacao": "ativo"},
                         {"tipo": tipo, "crm_process_id": pid, "parte_principal": "FULANO SECRETO"})
    d = dados(b, ocupado=False)
    assert d["processos"] == 2 and d["fora_do_crm"] == 1
    assert d["fases"] == {"conhecimento": 1, "recursal": 1}
    assert "FULANO SECRETO" not in json.dumps(d)
    assert "painel.json" in HTML


def test_falha_antiga_some_depois_de_um_ciclo_ok(tmp_path):
    b = Banco(tmp_path / "f.sqlite3")
    i = b.abrir_execucao("descobrir")
    b.fechar_execucao(i, False, {"erro": "interrompida"})
    assert dados(b, False)["ultimo_erro"] == "interrompida"
    j = b.abrir_execucao("analisar")
    b.fechar_execucao(j, True, {})
    assert dados(b, False)["ultimo_erro"] is None
