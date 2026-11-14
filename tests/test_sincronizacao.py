"""13/11/2026: a sincronização que parecia parada e o status que ia e voltava.

Três garantias:
  - consulta ao DataJud que falha, volta parcial ou volta vazia nunca apaga a
    cópia boa, e um lote ruim não segura a fila;
  - `djen_last_sync` anda em toda consulta que deu certo, com ou sem intimação;
  - o publicar não escreve em `processes` (status é do CRM).
"""

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from jurius_processos import alimentar, datajud, etapas, publicar
from jurius_processos.banco import Banco

TRF1 = ["1000105482026401360" + str(i) for i in range(3)]  # J.TR = 4.01
INST = [{"grau": "G1", "numeroProcesso": TRF1[0], "movimentos": [{"codigo": 26, "nome": "Distribuição",
                                                                 "dataHora": "2026-01-10T10:00:00Z"}]}]


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(datajud.time, "sleep", lambda s: None)


def _datajud_falso(respostas):
    """respostas: lista consumida a cada POST (a última se repete)."""
    chamadas = []

    def responder(req: httpx.Request):
        chamadas.append(json.loads(req.content))
        r = respostas[min(len(chamadas) - 1, len(respostas) - 1)]
        return r(req) if callable(r) else r
    c = datajud.ClienteDataJud("k", httpx.Client(transport=httpx.MockTransport(responder)))
    return c, chamadas


def _ok(numeros):
    return httpx.Response(200, json={"timed_out": False, "_shards": {"total": 5, "successful": 5, "failed": 0},
                                     "hits": {"hits": [{"_source": {**INST[0], "numeroProcesso": n}} for n in numeros]}})


def test_resposta_parcial_do_datajud_e_erro_e_nao_vazio():
    parcial = httpx.Response(200, json={"timed_out": False, "_shards": {"total": 5, "successful": 3, "failed": 2},
                                        "hits": {"hits": []}})
    c, chamadas = _datajud_falso([parcial])
    assert [s for _, s, _, _ in c.lote(TRF1)] == ["erro"] * 3
    assert len(chamadas) == datajud.TENTATIVAS

    estourou = httpx.Response(200, json={"timed_out": True, "_shards": {"failed": 0}, "hits": {"hits": []}})
    c, _ = _datajud_falso([estourou, _ok(TRF1)])
    assert [s for _, s, _, _ in c.lote(TRF1)] == ["ok"] * 3  # a 2ª tentativa resolve


def test_lote_falho_nao_trava_a_fila(monkeypatch):
    monkeypatch.setattr(datajud, "LOTE", 1)
    # 1º lote: 504 nas 3 tentativas. 2º lote responde. A fila anda.
    c, _ = _datajud_falso([httpx.Response(504)] * datajud.TENTATIVAS + [_ok([TRF1[1]]), _ok([TRF1[2]])])
    assert [s for _, s, _, _ in c.lote(TRF1)] == ["erro", "ok", "ok"]

    # DataJud fora do ar: depois de 2 lotes seguidos sem resposta o resto é
    # ADIADO (nem consultado, nem marcado como erro) em vez de gastar o ciclo.
    c, chamadas = _datajud_falso([httpx.Response(429)])
    assert [s for _, s, _, _ in c.lote(TRF1)] == ["erro", "erro", "adiado"]
    assert len(chamadas) == datajud.FALHAS_SEGUIDAS * datajud.TENTATIVAS


def test_falha_ou_vazio_nunca_apaga_a_copia_boa(tmp_path):
    b = Banco(tmp_path / "b.sqlite3")
    n = TRF1[0]
    b.garantir_processo(n, "crm")
    b.gravar_datajud(n, "ok", INST)
    antes = b.processo(n)["datajud_consultado_em"]

    b.gravar_datajud(n, "erro", None, "HTTP 504")
    r = b.processo(n)
    assert r["datajud_status"] == "ok" and json.loads(r["datajud"]) == INST
    assert "504" in r["datajud_erro"] and r["datajud_consultado_em"] == antes  # segue pendente

    b.gravar_datajud(n, "vazio", [], None)
    r = b.processo(n)
    assert r["datajud_status"] == "ok" and json.loads(r["datajud"]) == INST and r["datajud_erro"]

    b.gravar_datajud(n, "adiado", None, None)
    assert b.processo(n)["datajud_status"] == "ok"

    b.gravar_datajud(n, "ok", INST)  # voltou a responder: a anotação some
    assert b.processo(n)["datajud_erro"] is None

    # Quem nunca teve andamento continua podendo ser 'vazio' e 'erro'.
    m = TRF1[1]
    b.garantir_processo(m, "crm")
    b.gravar_datajud(m, "vazio", [], None)
    assert b.processo(m)["datajud_status"] == "vazio"
    b.gravar_datajud(m, "erro", None, "x")
    assert b.processo(m)["datajud_status"] == "erro"


def test_enriquecer_poe_os_ativos_na_frente_e_conta_o_adiado(tmp_path, monkeypatch):
    monkeypatch.setattr(datajud, "LOTE", 1)
    b = Banco(tmp_path / "b.sqlite3")
    for n in TRF1:
        b.garantir_processo(n, "crm")
    c, chamadas = _datajud_falso([lambda req: _ok(json.loads(req.content)["query"]["terms"]["numeroProcesso"])])
    r = etapas.enriquecer(b, SimpleNamespace(datajud_key="k"), primeiro={TRF1[2]}, cliente=c)
    assert chamadas[0]["query"]["terms"]["numeroProcesso"] == [TRF1[2]]
    assert r["ok"] == 3 and r["adiado"] == 0

    b2 = Banco(tmp_path / "b2.sqlite3")
    for n in TRF1:
        b2.garantir_processo(n, "crm")
    c, _ = _datajud_falso([httpx.Response(504)])
    r = etapas.enriquecer(b2, SimpleNamespace(datajud_key="k"), cliente=c)
    assert (r["erro"], r["adiado"], r["consultados"]) == (2, 1, 2)
    assert b2.processo(TRF1[2])["datajud_consultado_em"] is None  # adiado: intocado, volta no próximo ciclo


class Supabase:
    def __init__(self, tabelas=None):
        self.tabelas, self.escritas = tabelas or {}, []

    def __call__(self, req: httpx.Request):
        tabela = req.url.path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(200, json=self.tabelas.get(tabela, []))
        self.escritas.append((req.method, tabela, dict(req.url.params), json.loads(req.content or b"null")))
        return httpx.Response(201)


CFG = SimpleNamespace(supabase_url="https://x.supabase.co", supabase_key="eyJ")


def _ligar(monkeypatch, sup):
    monkeypatch.setattr(alimentar, "_cliente",
                        lambda cfg: httpx.Client(base_url="https://x/rest/v1", transport=httpx.MockTransport(sup)))


def test_consulta_sem_intimacao_nova_tambem_marca_a_sincronizacao(tmp_path, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    procs = [SimpleNamespace(id=f"p{i}", numero=n, codigo=f"c{i}", client_id="c") for i, n in enumerate(TRF1)]
    sup = Supabase()
    _ligar(monkeypatch, sup)
    r = alimentar.intimacoes(b, CFG, procs, aplicar=True, hoje=date(2026, 11, 13), consultados=set(TRF1[:2]))
    assert r["novas"] == 0 and r["processos_conferidos"] == 2
    [(metodo, _, params, corpo)] = [e for e in sup.escritas if e[1] == "processes"]
    assert metodo == "PATCH" and params == {"id": "in.(p0,p1)"}  # o não consultado (p2) fica como estava
    # Só as colunas da marca: nada de status, updated_at, cliente ou djen_has_data.
    assert sorted(corpo) == ["djen_last_sync", "djen_synced"] and corpo["djen_synced"] is True


def test_copia_anterior_nao_anda_o_carimbo_do_datajud(tmp_path, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = TRF1[0]
    b.garantir_processo(n, "crm")
    b.gravar_datajud(n, "ok", INST)
    b.gravar_datajud(n, "erro", None, "HTTP 504")
    monkeypatch.setattr(alimentar, "_COPIA_GRAVADA", {})
    sup = Supabase({"datajud_movimentos": []})
    _ligar(monkeypatch, sup)
    r = alimentar.datajud(b, CFG, [SimpleNamespace(id="p0", numero=n, codigo="c0", client_id="c")], aplicar=True)
    assert r["copia_anterior_mantida"] == 1 and r["caches"] == 0
    assert [e for e in sup.escritas if e[1] == "processes"] == []


def test_publicar_nunca_escreve_em_processes(tmp_path, monkeypatch):
    """Mesmo com o interruptor antigo ligado e a análise discordando do CRM."""
    b = Banco(tmp_path / "b.sqlite3")
    n = TRF1[0]
    b.garantir_processo(n, "crm")
    b.gravar_analise(n, {"status_crm": "cumprimento", "status_desde": "2026-11-10"},
                     {"crm_process_id": "p0", "tipo": "cadastro"})
    sup = Supabase()
    real = httpx.Client
    monkeypatch.setattr(publicar.httpx, "Client",
                        lambda **kw: real(base_url=kw["base_url"], transport=httpx.MockTransport(sup)))
    crm = [SimpleNamespace(id="p0", status="arquivado", status_manual=False)]
    r = publicar.publicar(b, crm, "https://x.supabase.co", "eyJ", aplicar=True, atualizar_status=True,
                          arquivados_por_pessoa=set())
    assert r["status"] == 0 and r["status_sugerido_diferente_do_crm"] == 1
    assert {e[1] for e in sup.escritas} == {"acervo_processos"}
    [linha] = sup.escritas[0][3]
    assert linha["status_crm"] == "cumprimento"  # a sugestão continua saindo, no acervo
