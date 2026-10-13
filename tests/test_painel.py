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


def test_estado_da_ia_sem_expor_a_chave():
    import httpx
    from types import SimpleNamespace
    from jurius_processos.painel import estado_ia as est

    def resp(codigo, corpo=None):
        return lambda *a, **k: httpx.Response(codigo, json=corpo)

    com = SimpleNamespace(supabase_url="https://x.supabase.co", supabase_key="eyJsegredo")
    sem = SimpleNamespace(supabase_url="https://x.supabase.co", supabase_key="")
    assert est(sem)["estado"] == "ausente"
    ok = resp(200, {"texto": "ok", "provider": "anthropic", "model": "claude-haiku-5-5"})
    assert est(com, ok)["estado"] == "ok"
    assert "claude-haiku-5-5" in est(com, ok)["texto"]
    assert est(com, resp(200, {"texto": ""}))["estado"] == "nao_verificada"
    assert est(com, resp(401))["estado"] == "invalida"
    assert est(com, resp(502))["estado"] == "nao_verificada"
    assert est(com, resp(503))["estado"] == "nao_verificada"

    def cai(*a, **k):
        raise httpx.ConnectError("x")
    r = est(com, cai)
    assert r["estado"] == "nao_verificada" and "eyJsegredo" not in json.dumps(r)


def test_painel_consulta_a_ia_no_maximo_a_cada_10_min(tmp_path, monkeypatch):
    from jurius_processos import painel
    chamadas = []
    monkeypatch.setattr(painel, "estado_ia", lambda c: chamadas.append(c) or {"estado": "ok", "texto": "ok"})
    monkeypatch.setitem(painel._CHAVE_IA, "estado", None)
    b = Banco(tmp_path / "c.sqlite3")
    for _ in range(3):
        assert dados(b, False)["chave_ia"]["estado"] == "ok"
    assert len(chamadas) == 1
