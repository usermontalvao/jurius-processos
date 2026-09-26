"""O que muda ao subir no servidor: túnel, geobloqueio e carga inicial."""

import importlib
import json

import httpx
import pytest


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("JURIUS_BANCO", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("JURIUS_TOKEN_API", "segredo")
    import jurius_processos.api as m
    m = importlib.reload(m)
    chamadas = []
    monkeypatch.setattr(m.agendador, "ciclo", lambda cfg, banco: chamadas.append(1))
    from fastapi.testclient import TestClient
    return TestClient(m.app), chamadas  # sem `with`: o agendador não sobe


def test_pedido_pelo_tunel_sem_token_nao_roda_ciclo(api):
    cli, _ = api
    r = cli.post("/ciclo", headers={"CF-Connecting-IP": "200.1.2.3"})
    assert r.status_code == 403


def test_pedido_pelo_tunel_com_token_roda(api):
    cli, _ = api
    r = cli.post("/ciclo", headers={"CF-Connecting-IP": "200.1.2.3", "Authorization": "Bearer segredo"})
    assert r.status_code == 200


def test_painel_abre_sem_token(api):
    cli, _ = api
    assert cli.get("/").status_code == 200
    d = cli.get("/painel.json").json()
    assert d["carga_feita"] is False and d["processos"] == 0


def test_djen_403_e_geobloqueio_nao_lista_vazia():
    from jurius_processos.djen import ClienteDJEN, ErroGeobloqueio
    transporte = httpx.MockTransport(lambda req: httpx.Response(403, json={}))
    djen = ClienteDJEN(httpx.Client(transport=transporte))
    with pytest.raises(ErroGeobloqueio):
        list(djen.buscar(nomeAdvogado="X"))


def test_carga_feita(tmp_path):
    from jurius_processos.banco import Banco
    b = Banco(tmp_path / "c.sqlite3")
    assert not b.carga_feita()
    i = b.abrir_execucao("descobrir")
    b.fechar_execucao(i, True, {"inicio": "2026-09-16"})  # ciclo curto não conta
    assert not b.carga_feita()
    i = b.abrir_execucao("carga_completa")
    b.fechar_execucao(i, True, {})
    assert b.carga_feita()


def test_chave_com_aspas_e_espaco_e_limpa(monkeypatch):
    from jurius_processos.config import carregar
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", '  "eyJabc.def.ghi"\n')
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co/ ")
    c = carregar()
    assert c.supabase_key == "eyJabc.def.ghi" and c.supabase_url == "https://x.supabase.co"


def test_chave_nova_sb_secret_vai_so_no_apikey():
    from jurius_processos.config import cabecalhos_supabase
    assert cabecalhos_supabase("sb_secret_abc") == {"apikey": "sb_secret_abc"}
    assert cabecalhos_supabase("eyJx")["Authorization"] == "Bearer eyJx"


def test_crm_pode_chamar_o_servico_pelo_navegador(api):
    cli, _ = api
    r = cli.options("/processos/1/atualizar", headers={
        "Origin": "https://jurius.com.br", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization"})
    assert r.headers.get("access-control-allow-origin") == "https://jurius.com.br"
    r = cli.options("/processos/1/atualizar", headers={
        "Origin": "https://site-qualquer.com", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") is None


def test_atualizar_grava_so_aquele_processo_com_a_lista_inteira_do_crm(api, monkeypatch):
    import jurius_processos.api as m
    from types import SimpleNamespace
    cli, _ = api
    n = "10141411620268110001"  # CNJ com dígito verificador válido
    procs = [SimpleNamespace(id="p1", numero=n), SimpleNamespace(id="p2", numero="2" * 20)]
    monkeypatch.setattr(m, "ClienteDJEN", lambda: SimpleNamespace(do_processo=lambda *a: []))
    monkeypatch.setattr(m, "ClienteDataJud", lambda k: SimpleNamespace(lote=lambda ns: []))
    monkeypatch.setattr(m.etapas, "carregar_crm", lambda cfg: (SimpleNamespace(financeiro=dict, agenda=lambda ps: {}, prazos=lambda ps: {}), None, [], procs))
    monkeypatch.setattr(m.etapas, "analisar", lambda *a, **k: None)
    monkeypatch.setattr(m.publicar, "publicar", lambda *a, **k: None)
    visto = {}
    monkeypatch.setattr(m.agendador, "alimentar_crm",
                        lambda cfg, banco, ps, aplicar, somente=None: visto.update(ps=ps, somente=somente) or {"ok": 1})
    r = cli.post(f"/processos/{n}/atualizar", headers={"Authorization": "Bearer segredo"})
    assert r.status_code == 200 and r.json()["alimentar"] == {"ok": 1}
    assert visto["ps"] == procs and visto["somente"] == [n]  # vínculo vem da lista inteira


def test_atualizar_durante_o_ciclo_entra_na_fila_em_vez_de_recusar(api, monkeypatch):
    import jurius_processos.api as m
    cli, _ = api
    from types import SimpleNamespace
    monkeypatch.setattr(m, "_trava", SimpleNamespace(acquire=lambda timeout=-1: False, locked=lambda: True))
    iniciadas = []
    monkeypatch.setattr(m.threading, "Thread", lambda target, args, **k: SimpleNamespace(start=lambda: iniciadas.append(args)))
    m._na_fila.clear()
    for _ in range(2):  # dois cliques: um pedido só na fila
        r = cli.post("/processos/10141411620268110001/atualizar", headers={"Authorization": "Bearer segredo"})
        assert r.status_code == 202 and r.json()["na_fila"] is True
    assert iniciadas == [("10141411620268110001",)]
