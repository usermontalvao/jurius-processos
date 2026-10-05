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
        self.escritas.append((req.method, tabela, dict(req.url.params), corpo, req.headers.get("x-jurius-sem-aviso")))
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
    posts = [e for e in sup.escritas if e[0] == "POST" and e[1] == "djen_comunicacoes"]
    assert len(posts) == 1 and posts[0][2]["on_conflict"] == "hash"
    # O card "Sincronização DJEN" continua vivo com o cron 5 desligado.
    [hist] = [e[3] for e in sup.escritas if e[1] == "djen_sync_history"]
    assert hist["source"] == "jurius-processos" and hist["items_saved"] == 1 and hist["success"] is True
    assert hist["id"]  # a coluna não tinha default: o id vai daqui
    ordem = [e[1] for e in sup.escritas if e[0] == "POST"]
    assert ordem.index("djen_comunicacoes") < ordem.index("djen_sync_history")
    linha = posts[0][3][0]
    assert linha["hash"] == "novo" and linha["process_id"] == "p1" and linha["client_id"] == "c1"
    # marca de sincronização no processo, nunca o status
    patch = [e for e in sup.escritas if e[0] == "PATCH"][0][3]
    assert "status" not in patch and patch["djen_has_data"] is True


def test_historico_recusado_nao_impede_as_intimacoes(tmp_path, cfg, monkeypatch):
    """26–28/09/2026: djen_sync_history recusava o POST (id sem default) e,
    como ele vinha antes, nenhuma intimação entrava no CRM."""
    b = Banco(tmp_path / "b.sqlite3")
    n = "1" * 20
    b.garantir_processo(n, "djen")
    b.gravar_comunicacao({"id": 1, "hash": "novo", "data_disponibilizacao": "2026-09-25", "texto": "t",
                          "siglaTribunal": "TJMT", "destinatarioadvogados": [], "destinatarios": []}, n, "oab")
    sup = Supabase({})

    def recusa_historico(req):
        if req.url.path.endswith("/djen_sync_history"):
            return httpx.Response(400, json={"code": "23502"})
        return sup(req)

    monkeypatch.setattr(alimentar, "_cliente",
                        lambda cfg: httpx.Client(base_url="https://x/rest/v1", transport=httpx.MockTransport(recusa_historico)))
    r = alimentar.intimacoes(b, cfg, [proc("p1", n, "1111111-11.1111.1.11.1111")], aplicar=True, hoje=date(2026, 9, 28))
    assert r["novas"] == 1
    assert [e[3][0]["hash"] for e in sup.escritas if e[1] == "djen_comunicacoes"] == ["novo"]


def _deepseek(conteudo, fim="stop", pedidos=None):
    def responder(req):
        if pedidos is not None:
            pedidos.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": conteudo}, "finish_reason": fim}]})
    return httpx.Client(transport=httpx.MockTransport(responder))


def test_toda_chamada_a_deepseek_desliga_o_pensamento(cfg):
    """28/09/2026: sem isso a deepseek-flash gastava os 500 tokens pensando e
    devolvia vazio — 3 de 7 intimações ficaram sem resumo, sem erro nenhum."""
    pedidos = []
    alimentar.analisar_com_ia(cfg, "texto", _deepseek('{"urgency": "baixa", "summary": "ok"}', pedidos=pedidos))
    alimentar.gerar_resumo(cfg, "texto", _deepseek("Resumo inteiro.", pedidos=pedidos))
    assert [p["thinking"] for p in pedidos] == [{"type": "disabled"}] * 2


def test_resposta_vazia_da_ia_vira_falha_contada(cfg, monkeypatch):
    with pytest.raises(alimentar.RespostaVazia):
        alimentar.analisar_com_ia(cfg, "texto", _deepseek("", "length"))
    sup = Supabase({"djen_comunicacoes": [{"id": "i1", "texto": "t"}], "intimation_ai_analysis": [],
                    "holidays": [], "profiles": []})
    ligar(monkeypatch, sup)
    monkeypatch.setattr(alimentar, "analisar_com_ia",
                        lambda *a, **k: (_ for _ in ()).throw(alimentar.RespostaVazia("finish_reason=length, 0 caracteres")))
    r = alimentar.ia(cfg, aplicar=True)
    assert r["pendentes"] == 1 and r["analisadas"] == 0 and r["falhas"] == 1
    assert "length" in r["ultima_falha"]


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


def _posts_mov(sup):
    return [(e[3], e[4]) for e in sup.escritas if e[1] == "datajud_movimentos"]


def test_movimento_antigo_nunca_vai_para_o_portal(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "2" * 20
    _datajud(b, n)
    cod = "2222222-22.2222.2.22.2222"
    # Supabase já tem o trânsito de 10/09 e parou em 15/09: a petição de 20/09
    # é nova (com aviso); a procedência de 2024 é lacuna (entra calada).
    sup = Supabase({"datajud_movimentos": [
        {"process_code": cod, "codigo": 848, "data_hora": "2026-09-10T10:00:00+00:00"},
        {"process_code": cod, "codigo": 1, "data_hora": "2026-09-15T00:00:00+00:00"}]})
    ligar(monkeypatch, sup)
    r = alimentar.datajud(b, cfg, [proc("p2", n, cod)], aplicar=True)
    assert r["movimentos_novos"] == 1 and r["lacunas_sem_aviso"] == 1
    (calado, marca_calado), (novo, marca_novo) = _posts_mov(sup)
    assert [m["nome"] for m in calado] == ["Procedência"] and marca_calado == "1"
    assert [m["nome"] for m in novo] == ["Petição"] and marca_novo is None
    cache = [e for e in sup.escritas if e[1] == "processes"][0][3]
    assert len(cache["datajud_cache"]["processo"]["movimentos"]) == 3 and cache["datajud_synced_at"]


def test_primeira_carga_do_processo_entra_inteira_e_calada(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "3" * 20
    _datajud(b, n)
    sup = Supabase({"datajud_movimentos": []})
    ligar(monkeypatch, sup)
    r = alimentar.datajud(b, cfg, [proc("p3", n, "x")], aplicar=True)
    assert r["movimentos_novos"] == 0 and r["lacunas_sem_aviso"] == 3
    [(linhas, marca)] = _posts_mov(sup)
    assert len(linhas) == 3 and marca == "1"


def test_nada_faltando_nao_grava_movimento(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "4" * 20
    _datajud(b, n)
    # Datas em formatos diferentes dos do DataJud: tem de reconhecer como iguais.
    sup = Supabase({"datajud_movimentos": [
        {"process_code": "c", "codigo": 219, "data_hora": "2024-03-01T10:00:00+00:00"},
        {"process_code": "c", "codigo": 848, "data_hora": "2026-09-10T10:00:00.000Z"},
        {"process_code": "c", "codigo": 85, "data_hora": "2026-09-20T10:00:00"}]})
    ligar(monkeypatch, sup)
    r = alimentar.datajud(b, cfg, [proc("p4", n, "c")], aplicar=True)
    assert r["movimentos_novos"] == 0 and r["lacunas_sem_aviso"] == 0
    assert all(not linhas for linhas, _ in _posts_mov(sup))


def test_copia_igual_so_anda_o_carimbo(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    n = "5" * 20
    _datajud(b, n)
    monkeypatch.setattr(alimentar, "_COPIA_GRAVADA", {})
    procs = [proc("p5", n, "c5")]

    sup = Supabase({"datajud_movimentos": []})
    ligar(monkeypatch, sup)
    alimentar.datajud(b, cfg, procs, aplicar=True)
    [(_, _, params, corpo, _)] = [e for e in sup.escritas if e[1] == "processes"]
    assert params == {"id": "eq.p5"} and "datajud_cache" in corpo

    # Ciclo seguinte, DataJud igual: nada de reenviar o jsonb, só o carimbo.
    sup = Supabase({"datajud_movimentos": []})
    ligar(monkeypatch, sup)
    r = alimentar.datajud(b, cfg, procs, aplicar=True)
    [(_, _, params, corpo, _)] = [e for e in sup.escritas if e[1] == "processes"]
    assert params == {"id": "in.(p5)"} and list(corpo) == ["datajud_synced_at"]
    assert r["caches_iguais"] == 1


def test_atualizar_um_processo_nao_toca_os_outros(tmp_path, cfg, monkeypatch):
    b = Banco(tmp_path / "b.sqlite3")
    um, outro = "5" * 20, "6" * 20
    for i, (numero, h) in enumerate(((um, "h-um"), (outro, "h-outro"))):
        b.garantir_processo(numero, "djen")
        b.gravar_comunicacao({"id": i, "hash": h, "data_disponibilizacao": "2026-09-20", "texto": "t",
                              "destinatarioadvogados": [], "destinatarios": []}, numero, "oab")
    _datajud(b, outro)
    sup = Supabase({"djen_comunicacoes": [], "datajud_movimentos": []})
    ligar(monkeypatch, sup)
    procs = [proc("p5", um, "c5"), proc("p6", outro, "c6")]
    r = alimentar.intimacoes(b, cfg, procs, aplicar=True, hoje=date(2026, 9, 26), somente={um})
    assert r["novas"] == 1
    [linha] = [e for e in sup.escritas if e[1] == "djen_comunicacoes"][0][3]
    assert linha["hash"] == "h-um" and linha["process_id"] == "p5"
    assert alimentar.datajud(b, cfg, procs, aplicar=True, somente={um})["caches"] == 0


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
                        lambda cfg, t, c=None, contexto=None: {"urgency": "alta", "deadline": {"days": 5}, "summary": "s"})
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
                        lambda cfg, t, c=None, contexto=None: {"urgency": "media", "deadline": {"days": 15}, "summary": "Manifestar"})
    r = alimentar.ia(cfg, aplicar=True)
    assert r == {"pendentes": 1, "analisadas": 1, "avisos": 2, "aplicado": True}
    analise = [e for e in sup.escritas if e[1] == "intimation_ai_analysis"][0][3]
    assert analise["deadline_due_date"].startswith("2026-09-09")  # mesmo caso de referência do CRM


def test_titulo_da_providencia_vem_limpo_ou_nao_vem():
    t = alimentar.titulo_da_providencia
    assert t({"deadline": {"days": 15, "action": "  apresentar contrarrazões ao recurso ordinário. "}}) == "Apresentar contrarrazões ao recurso ordinário"
    assert t({"deadline": {"days": 5, "action": "Prazo"}}) is None
    assert t({"deadline": {"days": 5}}) is None
    assert t({"deadline": None}) is None
    assert t(None) is None
    longo = t({"deadline": {"action": "Manifestar " + "sobre os cálculos " * 10}})
    assert longo and len(longo) <= 60 and not longo.endswith(" ")


def test_ia_grava_o_titulo_do_prazo(cfg, monkeypatch):
    it = {"id": "i3", "texto": "t", "numero_processo": "1", "numero_processo_mascara": "1-1",
          "sigla_tribunal": "TRT23", "data_disponibilizacao": "2026-08-17", "process_id": None}
    sup = Supabase({"djen_comunicacoes": [it], "intimation_ai_analysis": [], "holidays": [],
                    "profiles": [], "user_notifications": [], "djen_destinatarios": []})
    ligar(monkeypatch, sup)
    monkeypatch.setattr(alimentar, "analisar_com_ia", lambda cfg, t, c=None, contexto=None: {
        "urgency": "media", "deadline": {"days": 15, "action": "manifestar sobre os cálculos de liquidação"}, "summary": "s"})
    alimentar.ia(cfg, aplicar=True)
    analise = [e for e in sup.escritas if e[1] == "intimation_ai_analysis"][0][3]
    assert analise["deadline_description"] == "Manifestar sobre os cálculos de liquidação"


def test_rodizio_ativos_sempre_arquivados_uma_vez_por_dia():
    from jurius_processos import etapas
    ativos = [f"{i:020d}" for i in range(30)]
    arquivados = [f"{i:020d}" for i in range(100, 340)]  # 240 arquivados
    todos = ativos + arquivados
    arq = set(arquivados)
    vistos = {}
    for rodada in range(etapas.FATIAS_ARQUIVADOS):  # um dia de ciclos de 2 h
        da_vez = etapas.rodizio(todos, arq, rodada)
        assert set(ativos) <= set(da_vez), "ativo é consultado todo ciclo"
        assert len(da_vez) < len(todos) * 0.3, "o ciclo não pede mais os 240 arquivados"
        for n in da_vez:
            vistos[n] = vistos.get(n, 0) + 1
    assert all(vistos.get(n) == 1 for n in arquivados), "cada arquivado exatamente 1× por dia"
    assert etapas.rodizio(todos, arq, 5) == etapas.rodizio(todos, arq, 5 + etapas.FATIAS_ARQUIVADOS), "estável entre dias e reinícios"


def test_trecho_para_ia_mantem_o_fim_do_texto_longo():
    texto = "A" * 5000 + "ORDEM-NO-MEIO" + "B" * 20000 + "INTIME-SE"
    curto = alimentar.trecho_para_ia(texto)
    assert len(curto) <= alimentar.LIMITE_TEXTO_IA + 10
    assert curto.endswith("INTIME-SE")
    assert alimentar.trecho_para_ia("texto pequeno") == "texto pequeno"
    meio = "x" * 9000
    assert alimentar.trecho_para_ia(meio) == meio  # antes do corte de 3000 perdia 6000


def test_reanalise_refaz_a_antiga_com_compromisso_e_nao_avisa_de_novo(cfg, monkeypatch):
    it = {"id": "v1", "texto": "perícia no dia 23/11/2026, às 14h00min", "numero_processo": "10598029220268110041",
          "numero_processo_mascara": "1059802-92", "sigla_tribunal": "TJMT", "nome_orgao": "1ª VARA ESP. DA FAZENDA PÚBLICA",
          "data_disponibilizacao": "2026-09-30", "process_id": None, "client_id": None}
    sup = Supabase({"djen_comunicacoes": [it], "intimation_ai_analysis": [{"intimation_id": "v1"}], "holidays": []})
    ligar(monkeypatch, sup)
    monkeypatch.setattr(alimentar, "analisar_com_ia", lambda cfg, t, c=None, contexto=None: {
        "urgency": "media", "summary": "Perícia", "deadline": {"days": 15, "action": "Indicar assistente técnico e quesitos"},
        "compromisso": {"tipo": "pericia", "data": "2026-11-23", "hora": "14:00", "modalidade": "presencial"}})
    r = alimentar.reanalisar(cfg, aplicar=True, hoje=date(2026, 10, 1))
    assert r["refeitas"] == 1
    patch = [e for e in sup.escritas if e[0] == "PATCH" and e[1] == "intimation_ai_analysis"][0]
    assert patch[2] == {"intimation_id": "eq.v1"}
    assert patch[3]["compromisso"]["data"] == "2026-11-23" and patch[3]["analise_versao"] == alimentar.analise_mod.VERSAO
    assert "created_at" not in patch[3]
    assert not any(e[1] == "user_notifications" for e in sup.escritas)
