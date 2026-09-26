"""Ficha do processo: montada no servidor, resumo refeito só quando algo muda."""

import json
from datetime import date
from types import SimpleNamespace

import httpx

from jurius_processos import alimentar, ficha
from jurius_processos.banco import Banco

HOJE = date(2026, 9, 26)


def test_comarca_sai_do_nome_do_orgao():
    assert ficha.comarca("3º JUIZADO ESPECIAL CÍVEL DE CUIABÁ") == "Cuiabá"
    assert ficha.comarca("1ª VARA DO TRABALHO DE VÁRZEA GRANDE") == "Várzea Grande"
    assert ficha.comarca("JUIZADO ESPECIAL CÍVEL DE LUCAS DO RIO VERDE") == "Lucas do Rio Verde"
    assert ficha.comarca("6ª VARA DO TRABALHO") is None
    assert ficha.comarca(None) is None


def test_ficha_mostra_so_a_proxima_audiencia_e_as_partes():
    analise = {"orgao": "6ª VARA DO TRABALHO DE CUIABÁ", "status_crm": "instrucao", "situacao": "ativo",
               "audiencia": {"tipo": "instrução", "data": "2026-11-18"}, "pendencias": []}
    f = ficha.montar(analise, {"partes": {"A": ["LUANA"], "P": ["CASAS X", "OUTRA"]}}, HOJE)
    assert f["polo_ativo"] == "LUANA" and f["polo_passivo"] == "CASAS X, OUTRA"
    assert f["comarca"] == "Cuiabá" and f["proxima_audiencia"]["data"] == "2026-11-18"
    analise["audiencia"] = {"tipo": "conciliação", "data": "2026-08-20"}
    assert ficha.montar(analise, {}, HOJE)["proxima_audiencia"] is None


def _entradas(**kw):
    base = dict(proc={"codigo": "x"}, analise={}, ficha={}, intimacoes=[], movimentos=[], prazos=[], agenda=[],
                acordos=[], notas=None)
    base.update(kw)
    return ficha.entradas(base["proc"], base["analise"], base["ficha"], base["intimacoes"], base["movimentos"],
                          base["prazos"], base["agenda"], base["acordos"], base["notas"])


def test_assinatura_muda_com_qualquer_novidade_e_so_com_ela():
    a = ficha.assinatura(_entradas())
    assert a == ficha.assinatura(_entradas())
    novidades = [
        dict(intimacoes=[{"id": "i1", "data": "2026-09-20", "texto": "t"}]),
        dict(intimacoes=[{"id": "i1", "data": "2026-09-20", "texto": "t", "resumo": "análise chegou"}]),
        dict(movimentos=[{"dataHora": "2026-09-20T10:00:00Z", "nome": "Sentença"}]),
        dict(prazos=[{"title": "Manifestar", "due_date": "2026-10-01", "status": "pendente"}]),
        dict(agenda=[{"quando": "2026-11-18T13:00:00Z", "titulo": "Audiência", "status": "pendente"}]),
        dict(acordos=[{"total_value": 1000, "status": "ativo", "parcelas": [{"status": "pago"}]}]),
        dict(notas="Cliente ligou"),
        dict(ficha={"fase": "instrucao"}),
    ]
    vistas = {a}
    for n in novidades:
        s = ficha.assinatura(_entradas(**n))
        assert s not in vistas, n
        vistas.add(s)


def test_notas_em_texto_solto_ou_lista():
    assert _entradas(notas='"Origem: Assinatura KIT"')["notas"][0]["texto"] == "Origem: Assinatura KIT"
    assert _entradas(notas=[{"text": "a", "created_at": "2026-09-01T10:00"}])["notas"][0]["texto"] == "a"


def test_prompt_leva_agenda_prazo_e_pagamento():
    e = _entradas(agenda=[{"quando": "2026-11-18T13:00", "titulo": "AUDIÊNCIA PRESENCIAL", "status": "pendente"}],
                  prazos=[{"title": "Réplica", "due_date": "2026-10-01", "status": "pendente"}],
                  acordos=[{"total_value": 5000, "status": "ativo", "parcelas": [{"status": "pago"}, {"status": "pendente"}]}])
    p = ficha.prompt(e)
    assert "AUDIÊNCIA PRESENCIAL" in p and "Réplica" in p and "1/2 parcela(s) paga(s)" in p


class Sup:
    def __init__(self, guardadas):
        self.guardadas, self.escritas = guardadas, []

    def __call__(self, req):
        tabela = req.url.path.rsplit("/", 1)[-1]
        if req.method == "GET":
            return httpx.Response(200, json=self.guardadas if tabela == "process_insights" else [])
        self.escritas.append((req.method, tabela, json.loads(req.content or b"null")))
        return httpx.Response(201)


class CRMFalso:
    def para_a_ficha(self):
        return {"p1": {"area": "consumidor", "notas": None, "intimacoes": [], "prazos": []}}

    def agenda(self, procs):
        return {}

    def financeiro(self):
        return {}

    def clientes(self):
        return [SimpleNamespace(id="c1", nome="LUANA")]


def _banco(tmp_path):
    b = Banco(tmp_path / "f.sqlite3")
    n = "1" * 20
    b.garantir_processo(n, "crm")
    b.gravar_analise(n, {"status_crm": "instrucao", "orgao": "VARA DE CUIABÁ", "pendencias": []},
                     {"partes": {"A": ["LUANA"], "P": ["EMPRESA"]}})
    return b, [SimpleNamespace(id="p1", numero=n, codigo="c", client_id="c1")]


def test_resumo_so_e_refeito_quando_a_assinatura_muda(tmp_path, monkeypatch):
    b, procs = _banco(tmp_path)
    cfg = SimpleNamespace(supabase_url="u", supabase_key="k", deepseek_key="d", deepseek_modelo="m")
    chamadas = []
    monkeypatch.setattr(alimentar, "gerar_resumo", lambda cfg, t, c=None: chamadas.append(t) or "Resumo.")

    sup = Sup([])
    monkeypatch.setattr(alimentar, "_cliente", lambda cfg: httpx.Client(base_url="https://x", transport=httpx.MockTransport(sup)))
    r = alimentar.ficha(b, cfg, procs, aplicar=True, hoje=HOJE, crm=CRMFalso())
    assert r["resumos_feitos"] == 1 and len(chamadas) == 1
    ficha_gravada = [e for e in sup.escritas if e[0] == "POST"][0][2][0]
    assert ficha_gravada["polo_ativo"] == "LUANA" and ficha_gravada["fase"] == "instrucao"
    ass = [e for e in sup.escritas if e[0] == "PATCH"][0][2]["resumo_assinatura"]

    # Mesmas entradas: ficha regravada, IA não é chamada de novo.
    sup2 = Sup([{"process_id": "p1", "resumo_assinatura": ass}])
    monkeypatch.setattr(alimentar, "_cliente", lambda cfg: httpx.Client(base_url="https://x", transport=httpx.MockTransport(sup2)))
    r = alimentar.ficha(b, cfg, procs, aplicar=True, hoje=HOJE, crm=CRMFalso())
    assert r["resumos_feitos"] == 0 and len(chamadas) == 1
    assert not any(e[0] == "PATCH" for e in sup2.escritas)


def test_ensaio_da_ficha_nao_escreve_nem_chama_ia(tmp_path, monkeypatch):
    b, procs = _banco(tmp_path)
    cfg = SimpleNamespace(supabase_url="u", supabase_key="k", deepseek_key="d", deepseek_modelo="m")
    monkeypatch.setattr(alimentar, "gerar_resumo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("IA")))
    sup = Sup([])
    monkeypatch.setattr(alimentar, "_cliente", lambda cfg: httpx.Client(base_url="https://x", transport=httpx.MockTransport(sup)))
    r = alimentar.ficha(b, cfg, procs, aplicar=False, hoje=HOJE, crm=CRMFalso())
    assert r["resumos_desatualizados"] == 1 and sup.escritas == []


def test_comarca_nos_outros_formatos_do_acervo():
    assert ficha.comarca("VARA DO TRABALHO DE FEIJÓ/AC") == "Feijó"
    assert ficha.comarca("CAMBÉ - JUIZADO ESPECIAL CÍVEL, CRIMINAL E DA FAZENDA PÚBLICA") == "Cambé"
    assert ficha.comarca("06ª Vara JEF- Cuiabá") == "Cuiabá"
    assert ficha.comarca("NÚCLEO DE PRECATÓRIOS") is None
    assert ficha.comarca("GABINETE DO DESEMBARGADOR NICANOR FAVERO FILHO") is None
    assert ficha.comarca_das_instancias(["GABINETE DA DESEMBARGADORA X", "2ª VARA DO TRABALHO DE CUIABÁ"]) == "Cuiabá"


def test_comarca_no_formato_do_tjmt_no_datajud():
    assert ficha.comarca("Décima Vara Cível - Comarca de Cuiabá - SDCR") == "Cuiabá"
    assert ficha.comarca("Quarta Vara Cível - Comarca de Várzea Grande - SDCR") == "Várzea Grande"
    assert ficha.comarca("Terceiro Juizado Especial Cível de Cuiabá - Comarca da Capital - SDCR") == "Cuiabá"
    assert ficha.comarca("Sexto Juizado Especial Cível de Cuiabá - SDCR") == "Cuiabá"
    assert ficha.comarca("Quinto Juizado Especial Cível - SDCR") is None

def test_turma_recursal_do_sistema_de_juizados_nao_e_cidade():
    assert ficha.comarca("Gabinete do Juiz 4 - 1ª Turma Recursal do Sistema de Juizados Especiais") is None
    assert ficha.comarca("Gabinete do Juiz 1 - 3ª Turma Recursal do Sistema de Juizados Especiais - Comarca de Cuiabá - SDCR") == "Cuiabá"
