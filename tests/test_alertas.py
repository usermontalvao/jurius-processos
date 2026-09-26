"""Alerta de cadastro: prazo/audiência achado pelo servidor e não cadastrado em 24 h."""

from datetime import datetime, timezone

from jurius_processos.alertas import detectar

AGORA = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
PROC = {"id": "p1", "client_id": "c1", "codigo": "1051311-22.2026.8.11.0001", "cliente": "HIAGO"}


def intim(chegou="2026-09-24T10:00:00+00:00", venc="2026-10-08T00:00:00+00:00"):
    return {"id": "i1", "chegou_em": chegou, "data": "2026-09-24", "vencimento": venc, "prazo_dias": 10,
            "resumo": "Intimação para manifestar sobre a contestação", "urgencia": "media"}


def test_prazo_sem_cadastro_depois_de_24h_vira_alerta_ja_preenchido():
    [a] = detectar(PROC, [intim()], [], [], None, AGORA)
    assert a["tipo"] == "prazo" and a["chave"] == "prazo:p1:2026-10-08" and a["data"] == "2026-10-08"
    assert a["dados"]["due_date"] == "2026-10-08" and a["dados"]["process_id"] == "p1"
    assert a["dados"]["title"] == "Intimação para manifestar sobre a contestação" and a["dados"]["client_name"] == "HIAGO"


def test_antes_de_24h_nao_avisa():
    assert detectar(PROC, [intim(chegou="2026-09-26T01:00:00+00:00")], [], [], None, AGORA) == []


def test_prazo_cadastrado_perto_do_vencimento_ou_depois_da_intimacao_cobre():
    assert detectar(PROC, [intim()], [{"due_date": "2026-10-06T00:00:00+00:00", "status": "pendente",
                                       "created_at": "2026-01-01T00:00:00+00:00"}], [], None, AGORA) == []
    assert detectar(PROC, [intim()], [{"due_date": "2026-12-01T00:00:00+00:00", "status": "cumprido",
                                       "created_at": "2026-09-25T00:00:00+00:00"}], [], None, AGORA) == []
    # Cancelado não cobre.
    assert len(detectar(PROC, [intim()], [{"due_date": "2026-10-08T00:00:00+00:00", "status": "cancelado",
                                           "created_at": "2026-09-25T00:00:00+00:00"}], [], None, AGORA)) == 1


def test_prazo_ja_vencido_nao_vira_alerta():
    assert detectar(PROC, [intim(venc="2026-09-20T00:00:00+00:00")], [], [], None, AGORA) == []


AUD = {"tipo": "conciliação", "data": "2026-11-05", "hora": "09:00", "designada_em": "2026-09-21", "fonte": "djen"}


def test_audiencia_do_djen_fora_da_agenda_vira_alerta_com_data_e_hora():
    [a] = detectar(PROC, [], [], [], AUD, AGORA)
    assert a["tipo"] == "audiencia" and a["chave"] == "audiencia:p1:2026-11-05"
    assert a["dados"]["date"] == "2026-11-05" and a["dados"]["time"] == "09:00" and a["dados"]["type"] == "hearing"
    assert a["titulo"] == "Audiência de conciliação em 05/11/2026 às 09:00"


def test_audiencia_ja_na_agenda_ou_vinda_da_agenda_nao_avisa():
    # 09h em Cuiabá = 13h UTC.
    assert detectar(PROC, [], [], [{"quando": "2026-11-05T13:00:00+00:00", "status": "pendente"}], AUD, AGORA) == []
    assert detectar(PROC, [], [], [], {**AUD, "fonte": "agenda"}, AGORA) == []


def test_audiencia_designada_hoje_espera_um_dia():
    assert detectar(PROC, [], [], [], {**AUD, "designada_em": "2026-09-26"}, AGORA) == []


def test_ciclo_de_vida_novo_resolvido_e_ignorado_nunca_volta(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace
    import httpx
    from jurius_processos import alimentar
    from jurius_processos.banco import Banco

    b = Banco(tmp_path / "a.sqlite3")
    n = "1" * 20
    b.garantir_processo(n, "crm")
    b.gravar_analise(n, {"situacao": "ativo", "audiencia": None}, {})
    procs = [SimpleNamespace(id="p1", numero=n, codigo="c", client_id="c1")]

    class CRMF:
        def __init__(self, intims):
            self.intims = intims
        def para_a_ficha(self):
            return {"p1": {"intimacoes": self.intims, "prazos": []}}
        def agenda(self, ps):
            return {}
        def clientes(self):
            return [SimpleNamespace(id="c1", nome="HIAGO")]

    def rodar(intims, guardados):
        escritas = []
        def h(req):
            if req.method == "GET":
                return httpx.Response(200, json=guardados)
            escritas.append((req.method, dict(req.url.params), _json.loads(req.content)))
            return httpx.Response(201)
        monkeypatch.setattr(alimentar, "_cliente", lambda cfg: httpx.Client(base_url="https://x", transport=httpx.MockTransport(h)))
        r = alimentar.alertas(b, SimpleNamespace(supabase_url="u", supabase_key="k"), procs, True, agora=AGORA, crm=CRMF(intims))
        return r, escritas

    r, e = rodar([intim()], [])
    assert r["novos"] == 1 and e[0][0] == "POST" and e[0][2][0]["situacao"] == "aberto"
    # Cadastraram o prazo: o alerta aberto é resolvido sozinho.
    r, e = rodar([], [{"id": "a1", "chave": "prazo:p1:2026-10-08", "situacao": "aberto", "process_id": "p1"}])
    assert r["resolvidos"] == 1 and e[0][2]["situacao"] == "resolvido"
    # Ignorado: continua faltando, mas não volta.
    r, e = rodar([intim()], [{"id": "a1", "chave": "prazo:p1:2026-10-08", "situacao": "ignorado", "process_id": "p1"}])
    assert r["novos"] == 0 and r["atualizados"] == 0 and e == []


def test_so_prazo_que_pede_providencia_e_um_alerta_por_vencimento():
    base = intim()
    ruido = ["Intimação para comparecer à Audiência de Conciliação Virtual", "Exordial recebida, aguardar audiência de conciliação.",
             "Intimação para ciência da sentença proferida nos autos, sem prazo específico", "Ação de reparação por danos morais proposta"]
    for r in ruido:
        assert detectar(PROC, [{**base, "resumo": r}], [], [], None, AGORA) == [], r
    gemeas = [{**base, "id": "i1", "resumo": "Apresentação de contrarrazões"}, {**base, "id": "i2", "resumo": "Apresentar contrarrazões"}]
    assert len(detectar(PROC, gemeas, [], [], None, AGORA)) == 1
    assert detectar(PROC, [{**base, "resumo": "Manifestar", "vencimento": "2031-10-11T00:00:00+00:00"}], [], [], None, AGORA) == []


def test_nome_da_empresa_nao_e_providencia():
    r = "Ação de reparação por danos morais proposta contra NU Pagamentos S.A. devido a bloqueio"
    assert detectar(PROC, [{**intim(), "resumo": r}], [], [], None, AGORA) == []
    assert len(detectar(PROC, [{**intim(), "resumo": "Intimação para pagamento das custas processuais em 5 dias"}], [], [], None, AGORA)) == 1


def test_caso_flavio_prazo_cadastrado_antes_do_djen_cobre():
    # 0000114-55.2026.5.23.0003: CONTRARRAZÕES criado 31/08, venc. 10/09, cumprido;
    # intimação chegou pelo DJEN em 03/09 e a IA contou 15 dias (venc. 28/09).
    i = {"id": "e759", "chegou_em": "2026-09-03T04:00:06+00:00", "data": "2026-09-03",
         "vencimento": "2026-09-28T00:00:00+00:00", "prazo_dias": 15,
         "resumo": "Intimação para apresentação de contrarrazões em processo trabalhista.", "urgencia": "media"}
    prazo = {"title": "CONTRARRAZÕES", "due_date": "2026-09-10T00:00:00+00:00", "status": "cumprido",
             "created_at": "2026-08-31T15:52:40+00:00"}
    agora = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    assert detectar(PROC, [i], [prazo], [], None, agora) == []
    # Prazo de OUTRO tipo, antigo, não cobre.
    outro = {"title": "RECURSO ORDINARIO", "due_date": "2026-08-28T00:00:00+00:00", "status": "cumprido",
             "created_at": "2026-08-19T19:26:43+00:00"}
    assert len(detectar(PROC, [i], [outro], [], None, agora)) == 1


def test_prazo_ligado_a_intimacao_cobre():
    p = {"title": "X", "due_date": "2026-12-01T00:00:00+00:00", "status": "pendente",
         "created_at": "2026-01-01T00:00:00+00:00", "intimation_id": "i1"}
    assert detectar(PROC, [intim()], [p], [], None, AGORA) == []


def _i(resumo, chegou="2026-09-18T04:00:00+00:00", venc="2026-10-13T00:00:00+00:00"):
    return {"id": "x", "chegou_em": chegou, "data": chegou[:10], "vencimento": venc, "prazo_dias": 15,
            "resumo": resumo, "urgencia": "media"}


def test_auditoria_dos_alertas_de_26_09():
    agora = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    # Giancarlo: prazo do devedor, não nosso.
    assert detectar(PROC, [_i("Intimação para pagamento de débito em cumprimento de sentença no prazo de 15 dias")], [], [], None, agora) == []
    # Gabriel: cálculos pelo perito.
    assert detectar(PROC, [_i("Despacho que determina a elaboração de cálculos de liquidação por perito contábil")], [], [], None, agora) == []
    # Carlos: é a audiência (com testemunhas), não prazo.
    assert detectar(PROC, [_i("Despacho que designa audiência de instrução presencial, com apresentação de testemunhas")], [], [], None, agora) == []
    # Weverton: sentença de 03/09, RECURSO ORDINARIO cadastrado no mesmo dia.
    sent = _i("Intimação para manifestação no prazo legal sobre sentença parcialmente procedente",
              chegou="2026-09-03T04:00:00+00:00", venc="2026-09-28T00:00:00+00:00")
    ro = {"title": "RECURSO ORDINARIO", "due_date": "2026-09-15T00:00:00+00:00", "status": "cumprido",
          "created_at": "2026-09-03T19:00:00+00:00"}
    assert detectar(PROC, [sent], [ro], [], None, agora) == []
    # Paulo Fabiano: "informar endereço" chegou 18/09 → "MANIFESTAÇÃO" criada no mesmo dia.
    man = {"title": "MANIFESTAÇÃO", "due_date": "2026-09-25T00:00:00+00:00", "status": "cumprido",
           "created_at": "2026-09-18T15:00:00+00:00"}
    assert detectar(PROC, [_i("Intimação para informar endereço do réu em 15 dias.")], [man], [], None, agora) == []
    # Eduarda: real — manifestação em 5 dias e a última MANIFESTAÇÃO é de julho.
    julho = {"title": "MANIFESTAÇÃO", "due_date": "2026-07-13T00:00:00+00:00", "status": "cumprido",
             "created_at": "2026-07-06T15:00:00+00:00"}
    eduarda = _i("Despacho que determina expedição de ofício à CEF, com prazo de 5 dias para manifestação.",
                 chegou="2026-09-25T04:00:00+00:00", venc="2026-10-05T00:00:00+00:00")
    assert len(detectar(PROC, [eduarda], [julho], [], None, datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc))) == 1


def test_impugnacao_nao_responde_a_intimacao_de_pericia():
    i = _i("Intimação do polo ativo para agendamento de perícia médica, com prazo de 30 dias.",
           chegou="2026-09-16T04:00:00+00:00")
    imp = {"title": "IMPUGNAÇÃO", "due_date": "2026-09-29T00:00:00+00:00", "status": "pendente",
           "created_at": "2026-09-16T15:00:00+00:00"}
    assert len(detectar(PROC, [i], [imp], [], None, datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc))) == 1
