from jurius_processos import cnj
from jurius_processos.djen import Advogado, meses, pertence
from datetime import date


def test_limpar_aceita_so_numero_com_digito_certo():
    assert cnj.limpar("1003202-56.2026.4.01.3600") == "10032025620264013600"
    assert cnj.limpar("1003202-57.2026.4.01.3600") is None  # DV errado
    assert cnj.limpar("123") is None
    assert cnj.limpar(None) is None


def test_indice_datajud_por_segmento():
    assert cnj.indice_datajud("10032025620264013600") == "api_publica_trf1"
    assert cnj.indice_datajud("10471812020258110002") == "api_publica_tjmt"
    assert cnj.indice_datajud("00011211920255230003") == "api_publica_trt23"


def test_mencoes_pega_processo_de_origem_e_ignora_numero_invalido():
    texto = ("AGRAVO DE INSTRUMENTO (202) 1031041-60.2024.4.01.0000 Processo de origem: "
             "1019760-74.2024.4.01.3600 telefone 1234567-89.2024.4.01.3600")
    achados = cnj.mencoes(texto)
    assert "10310416020244010000" in achados
    assert "10197607420244013600" in achados
    assert len(achados) == 2


def test_oab_do_cadastro_do_crm():
    a = Advogado.de_texto("PEDRO RODRIGUES MONTALVAO NETO", "OAB-MT 30.021")
    assert (a.oab, a.uf) == ("30021", "MT")
    assert Advogado.de_texto("X", "30021/MT").oab == "30021"
    assert Advogado.de_texto("X", "sem numero") is None


def _item(oab, uf="MT", nome="PEDRO RODRIGUES MONTALVAO NETO"):
    return {"destinatarioadvogados": [{"advogado": {"nome": nome, "numero_oab": oab, "uf_oab": uf}}]}


def test_pertence_reconhece_oab_com_sufixo_O():
    adv = Advogado("PEDRO RODRIGUES MONTALVAO NETO", "30021", "MT")
    assert pertence(_item("30021/O"), adv) == "oab"
    assert pertence(_item("30021"), adv) == "oab"
    assert pertence(_item("030021"), adv) == "oab"


def test_pertence_descarta_homonimo_de_outro_estado_so_pelo_nome_quando_oab_difere():
    adv = Advogado("PEDRO RODRIGUES MONTALVAO NETO", "30021", "MT")
    assert pertence(_item("99999", "SP", "FULANO DE TAL"), adv) is None
    # Mesmo nome, OAB diferente: aceita pelo nome (o DJEN às vezes grava a OAB errada)
    assert pertence(_item("99999", "SP"), adv) == "nome"


def test_meses_corta_no_fim():
    m = list(meses("2026-08-01", date(2026, 9, 24)))
    assert m == [("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-24")]


def test_advogado_so_nome_busca_e_reconhece_pelo_nome_completo():
    adv = Advogado.so_nome("  Maria da Silva Souza ")
    assert (adv.nome, adv.oab, adv.uf) == ("Maria da Silva Souza", None, None)
    # Sem OAB, o nome (sem acento, sem caixa) é o único critério
    assert pertence(_item("12345", "SP", "MARIA DA SILVA SOUZA"), adv) == "nome"
    assert pertence(_item("12345", "SP", "MARIA DA SILVA"), adv) is None
    assert Advogado.so_nome("   ") is None


def test_do_advogado_sem_oab_so_consulta_por_nome():
    from jurius_processos.djen import ClienteDJEN

    class Falso(ClienteDJEN):
        def __init__(self):
            self.filtros = []

        def buscar(self, **filtros):
            self.filtros.append(filtros)
            return iter([])

    f = Falso()
    f.do_advogado(Advogado.so_nome("MARIA DA SILVA SOUZA"), "2026-09-01", "2026-09-30")
    assert [x.get("nomeAdvogado") for x in f.filtros] == ["MARIA DA SILVA SOUZA"]
    assert not any("numeroOab" in x for x in f.filtros)

    f = Falso()
    f.do_advogado(Advogado("PEDRO RODRIGUES MONTALVAO NETO", "30021", "MT"), "2026-09-01", "2026-09-30")
    assert [x.get("numeroOab") for x in f.filtros] == ["30021", "30021/O", None]
    assert f.filtros[-1]["nomeAdvogado"] == "PEDRO RODRIGUES MONTALVAO NETO"
