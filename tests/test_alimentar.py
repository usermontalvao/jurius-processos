"""O cérebro alimentando o CRM: sem duplicar, sem histórico no portal, sem aviso em dobro."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from jurius_processos import alimentar
from jurius_processos.banco import Banco


class Supabase:
    """PostgREST falso: responde GETs de tabelas e guarda o que foi escrito."""

    def __init__(self, tabelas):
        self.tabelas = tabelas
        self.escritas = []

    def __call__(self, req: httpx.Request):
        tabela = req.url.path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(200, json=self.tabelas.get(tabela, []))
        corpo = json.loads(req.content or b"null")
        self.escritas.append((req.method, tabela, dict(req.url.params), corpo))
        if "return=representation" in req.headers.get("prefer", ""):
            return httpx.Response(201, json=corpo if isinstance(corpo, list) else [corpo])
        return httpx.Response(201)


@pytest.fixture()
def cfg():
    return SimpleNamespace(supabase_url="https://x.supabase.co", supabase_key="eyJ", deepseek_key="k",
                           deepseek_modelo="deepseek-flash")


def ligar(monkeypatch, sup):
    monkeypatch.setattr(alimentar, "_cliente",
                        lambda cfg: httpx.Client(base_url="https://x/rest/v1", transport=httpx.MockTransport(sup)))


def proc(pid, numero, codigo, client_id="c1"):
    return SimpleNamespace(id=pid, numero=numero, codigo=codigo, client_id=client_id)


def test_intimacao_nova_entra_uma_vez_e_a_antiga_nao(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "1" * 20
    b.garantir_processo(n, "djen")
    for i, (data, h) in enumerate([("2026-09-20", "novo"), ("2026-09-21", "ja-existe"), ("2026-01-10", "velho")]):
        b.gravar_comunicacao({"id": i, "hash": h, "data_disponibilizacao": data, "texto": "t",
                              "siglaTribunal": "TJMT", "destinatarioadvogados": [], "destinatarios": []}, n, "oab")
    sup = Supabase({"djen_comunicacoes": [{"hash": "ja-existe"}]})
    ligar(monkeypatch, sup)
    r = alimentar.intimacoes(b, cfg, [proc("p1", n, "1111111-11.1111.1.11.1111")], aplicar=True, hoje=date(2026, 9, 26))
    assert r["novas"] == 1
    posts = [e for e in sup.escritas if e[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["on_conflict"] == "hash"
    linha = posts[0][3][0]
    assert linha["hash"] == "novo" and linha["process_id"] == "p1" and linha["client_id"] == "c1"
    # marca de sincronização no processo, nunca o status
    patch = [e for e in sup.escritas if e[0] == "PATCH"][0][3]
    assert "status" not in patch and patch["djen_has_data"] is True


def test_ensaio_nao_escreve(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    sup = Supabase({})
    ligar(monkeypatch, sup)
    alimentar.intimacoes(b, cfg, [], aplicar=False, hoje=date(2026, 9, 26))
    alimentar.datajud(b, cfg, [], aplicar=False)
    assert sup.escritas == []


def _datajud(b, n):
    inst = [{"tribunal": "TJMT", "grau": "JE", "dataHoraUltimaAtualizacao": "2026-09-20T00:00:00Z",
             "movimentos": [{"codigo": 219, "nome": "Procedência", "dataHora": "2024-03-01T10:00:00Z"},
                            {"codigo": 848, "nome": "Trânsito em julgado", "dataHora": "2026-09-10T10:00:00Z"},
                            {"codigo": 85, "nome": "Petição", "dataHora": "2026-09-20T10:00:00Z"}]}]
    b.garantir_processo(n, "crm")
    b.gravar_datajud(n, "ok", inst)


def test_movimento_antigo_nunca_vai_para_o_portal(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "2" * 20
    _datajud(b, n)
    cod = "2222222-22.2222.2.22.2222"
    # Supabase já tem movimentos até 15/09: só a petição de 20/09 é nova.
    sup = Supabase({"datajud_movimentos": [{"process_code": cod, "data_hora": "2026-09-15T00:00:00+00:00"}]})
    ligar(monkeypatch, sup)
    r = alimentar.datajud(b, cfg, [proc("p2", n, cod)], aplicar=True)
    assert r["movimentos_novos"] == 1
    novos = [e for e in sup.escritas if e[1] == "datajud_movimentos"][0][3]
    assert [m["nome"] for m in novos] == ["Petição"]
    cache = [e for e in sup.escritas if e[1] == "processes"][0][3]
    assert len(cache["datajud_cache"]["processo"]["movimentos"]) == 3 and cache["datajud_synced_at"]


def test_processo_sem_historico_no_supabase_so_leva_os_ultimos_30_dias(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "3" * 20
    _datajud(b, n)
    sup = Supabase({"datajud_movimentos": []})
    ligar(monkeypatch, sup)
    r = alimentar.datajud(b, cfg, [proc("p3", n, "x")], aplicar=True,
                          agora=datetime(2026, 9, 26, tzinfo=timezone.utc))
    assert r["movimentos_novos"] == 2  # trânsito (10/09) e petição (20/09); a sentença de 2024 não


def test_categoria_e_estagio_iguais_ao_datajud_sync():
    assert alimentar.categorizar(848, "Trânsito em julgado") == "sentenca"
    assert alimentar.categorizar(246, "Definitivo") == "arquivamento"
    assert alimentar.estagio("outro", "Expedição de alvará") == "cumprimento"
    assert alimentar.estagio("audiencia", "Audiência de conciliação") == "conciliacao"


def test_ia_sem_chave_nao_faz_nada(cfg):
    cfg.deepseek_key = ""
    assert alimentar.ia(cfg, aplicar=True) == {"pulado": "sem DEEPSEEK_API_KEY"}


def test_ia_nao_avisa_em_dobro_quando_a_rotina_antiga_chegou_antes(cfg, monkeypatch):
    it = {"id": "i1", "texto": "prazo de 5 dias", "numero_processo": "1", "numero_processo_mascara": "1",
          "sigla_tribunal": "TJMT", "data_disponibilizacao": "2026-09-21", "process_id": None}

    class Sup(Supabase):
        def __call__(self, req):
            if req.method == "POST" and req.url.path.endswith("intimation_ai_analysis"):
                self.escritas.append(("POST", "intimation_ai_analysis", {}, None))
                return httpx.Response(201, json=[])  # conflito ignorado: já analisada
            return super().__call__(req)

    sup = Sup({"djen_comunicacoes": [it], "intimation_ai_analysis": [], "holidays": [],
               "profiles": [{"user_id": "u1"}]})
    ligar(monkeypatch, sup)
    monkeypatch.setattr(alimentar, "analisar_com_ia",
                        lambda cfg, t, c=None: {"urgency": "alta", "deadline": {"days": 5}, "summary": "s"})
    r = alimentar.ia(cfg, aplicar=True)
    assert r["analisadas"] == 0 and r["avisos"] == 0
    assert not any(e[1] == "user_notifications" for e in sup.escritas)


def test_ia_grava_prazo_em_dias_uteis_e_avisa_a_equipe(cfg, monkeypatch):
    it = {"id": "i2", "texto": "t", "numero_processo": "1", "numero_processo_mascara": "1-1",
          "sigla_tribunal": "TJMT", "data_disponibilizacao": "2026-08-17", "process_id": None}
    sup = Supabase({"djen_comunicacoes": [it], "intimation_ai_analysis": [], "holidays": [{"date": "2026-09-07"}],
                    "profiles": [{"user_id": "u1"}, {"user_id": "u2"}], "user_notifications": [], "djen_destinatarios": []})
    ligar(monkeypatch, sup)
    monkeypatch.setattr(alimentar, "analisar_com_ia",
                        lambda cfg, t, c=None: {"urgency": "media", "deadline": {"days": 15}, "summary": "Manifestar"})
    r = alimentar.ia(cfg, aplicar=True)
    assert r == {"pendentes": 1, "analisadas": 1, "avisos": 2, "aplicado": True}
    analise = [e for e in sup.escritas if e[1] == "intimation_ai_analysis"][0][3]
    assert analise["deadline_due_date"].startswith("2026-09-09")  # mesmo caso de referência do CRM
