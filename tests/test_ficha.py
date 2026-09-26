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


def _resposta(conteudo, fim):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": conteudo}, "finish_reason": fim}]})))


def test_resumo_cortado_ou_vazio_nunca_e_gravado():
    import pytest
    cfg = SimpleNamespace(deepseek_key="k", deepseek_modelo="m")
    assert alimentar.gerar_resumo(cfg, "x", _resposta("Texto inteiro.", "stop")) == "Texto inteiro."
    for conteudo, fim in [("A fase atual é de cumprimento, com a execução", "length"), ("", "stop"), (None, "stop")]:
        with pytest.raises(alimentar.ResumoIncompleto):
            alimentar.gerar_resumo(cfg, "x", _resposta(conteudo, fim))


def test_mudar_a_versao_do_pedido_refaz_os_resumos(monkeypatch):
    a = ficha.assinatura(_entradas())
    monkeypatch.setattr(ficha, "VERSAO_RESUMO", ficha.VERSAO_RESUMO + 1)
    assert ficha.assinatura(_entradas()) != a


def test_partes_lidas_no_texto_quando_o_djen_so_traz_o_cliente():
    # Textos reais (26/09/2026).
    hiago = ["PROCESSO n. 1051311-22.2026.8.11.0001 Valor da causa: R$ 10.000,00 POLO ATIVO: Nome: HIAGO DE OLIVEIRA LIMA "
             "Endereço: RUA VITÓRIA-RÉGIA, 13 POLO PASSIVO: Nome: NU PAGAMENTOS S.A. - INSTITUICAO DE PAGAMENTO Endereço: X",
             "Cuida-se de AÇÃO DE OBRIGAÇÃO DE FAZER, ajuizada por HIAGO DE OLIVEIRA LIMA em face de NU PAGAMENTOS S/A, pleiteando"]
    p = ficha.partes_do_texto(hiago)
    assert p["A"] == ["HIAGO DE OLIVEIRA LIMA"]
    assert p["P"] == ["NU PAGAMENTOS S.A. - INSTITUICAO DE PAGAMENTO"]
    luana = ["ATOrd 0000691-24.2026.5.23.0006 RECLAMANTE: LUANA ALENCAR CASTRO RECLAMADO: CASAS CAMINHO REDENTOR 1. DESIGNO audiência"]
    assert ficha.partes_do_texto(luana) == {"A": ["LUANA ALENCAR CASTRO"], "P": ["CASAS CAMINHO REDENTOR"]}
    ramona = ["Processo: 1028965-77. AUTOR: RAMONA APARECIDA RODRIGUES MARTINEZ REU: MERCADO PAGO INSTITUICAO DE PAGAMENTO LTDA Vistos, etc"]
    assert ficha.partes_do_texto(ramona)["P"] == ["MERCADO PAGO INSTITUICAO DE PAGAMENTO LTDA"]


def test_ficha_completa_so_o_polo_que_falta():
    analise = {"pendencias": []}
    f = ficha.montar(analise, {"partes": {"A": ["HIAGO"], "P": []}}, HOJE, [],
                     ["REU: EMPRESA QUALQUER LTDA Vistos", "AUTOR: OUTRO NOME Vistos"])
    assert f["polo_ativo"] == "HIAGO" and f["polo_passivo"] == "EMPRESA QUALQUER LTDA"


def test_limpeza_das_partes():
    assert ficha.limpar_partes(["INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS ATO ORDINATÓRIO",
                                "INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS SENTENÇA TIPO"]) == [
        "INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS"]
    assert ficha.limpar_partes(["N. E. S. D. Q.", "USUáRIO DO SISTEMA 2", "N. E. S. D. Q. REPRESENTANTES",
                                "PEDRO RODRIGUES MONTALVAO NETO - MT30021-A"]) == ["N. E. S. D. Q."]
    # O advogado pode ser parte de verdade (1038323-66: "POLO PASSIVO: REU: PEDRO ...").
    assert ficha.limpar_partes(["PEDRO RODRIGUES MONTALVAO NETO"]) == ["PEDRO RODRIGUES MONTALVAO NETO"]


def test_resumo_sabe_o_que_ja_aconteceu():
    # Juliana (26/09/2026): o resumo mandava "comparecer à audiência de 18/09".
    e = ficha.entradas({"codigo": "x"}, {}, {"fase": "aguardando_sentenca"}, [], [], [],
                       [{"quando": "2026-09-18T22:00:00+00:00", "titulo": "Audiência Online — JULIANA", "status": "pendente"},
                        {"quando": "2026-11-18T14:00:00+00:00", "titulo": "AUDIÊNCIA X", "status": "pendente"}],
                       [], None, HOJE)
    p = ficha.prompt(e, HOJE)
    assert "DATA DE HOJE: 26/09/2026" in p
    assert "[2026-09-18 18:00] Audiência Online — JULIANA (JÁ OCORREU)" in p
    assert "[2026-11-18 10:00] AUDIÊNCIA X (marcado)" in p
    assert "aguardando sentença" in p
    # A data sozinha não muda a assinatura (senão refaria tudo todo dia) ...
    assert ficha.assinatura(e) == ficha.assinatura(ficha.entradas({"codigo": "x"}, {}, {"fase": "aguardando_sentenca"}, [], [], [],
        [{"quando": "2026-09-18T22:00:00+00:00", "titulo": "Audiência Online — JULIANA", "status": "pendente"},
         {"quando": "2026-11-18T14:00:00+00:00", "titulo": "AUDIÊNCIA X", "status": "pendente"}], [], None, date(2026, 9, 27)))
    # ... mas a audiência passar muda.
    depois = ficha.entradas({"codigo": "x"}, {}, {"fase": "aguardando_sentenca"}, [], [], [],
        [{"quando": "2026-11-18T14:00:00+00:00", "titulo": "AUDIÊNCIA X", "status": "pendente"}], [], None, date(2026, 11, 19))
    assert depois["agenda"][0]["ja_ocorreu"] is True
