"""Análise de intimação: rito, contexto e salvaguardas — com os casos reais que motivaram cada regra."""

from jurius_processos import analise

PEDRO = {  # 1038323-66.2026.8.11.0001 — o cliente é o RÉU e ganhou tudo
    "id": "p1", "numero_processo_mascara": "1038323-66.2026.8.11.0001", "sigla_tribunal": "TJMT",
    "nome_orgao": "6º JUIZADO ESPECIAL CÍVEL DE CUIABÁ", "nome_classe": "PROCEDIMENTO DO JUIZADO ESPECIAL CíVEL",
    "tipo_documento": "Sentença", "data_disponibilizacao": "2026-10-01 00:00:00+00",
}
VICENTE = {  # 1059802-92.2026.8.11.0041 — perícia marcada na decisão
    "id": "v1", "sigla_tribunal": "TJMT", "nome_orgao": "1ª VARA ESP. DA FAZENDA PÚBLICA DE CUIABÁ",
    "nome_classe": "PROCEDIMENTO COMUM CÍVEL", "tipo_documento": "Intimação", "data_disponibilizacao": "2026-09-30",
}
TEXTO_VICENTE = ("Nomeio a empresa PERICIAS MED, devidamente cadastrada, situada na Avenida Miguel Sutil, CEP 78070-300, "
                 "para realizar a perícia médica conforme a agenda disponibilizada perante este juízo, no dia 23/11/2026, às "
                 "14h00min. Intimem-se as partes para, no prazo de 15 dias, indicarem assistente técnico e quesitos.")


def test_rito_pelo_orgao():
    assert analise.rito("6º JUIZADO ESPECIAL CÍVEL DE CUIABÁ", "TJMT") == "juizado_civel"
    assert analise.rito("JUIZADO ESPECIAL DA FAZENDA PÚBLICA DE VÁRZEA GRANDE", "TJMT") == "juizado_fazenda"
    assert analise.rito("1º Juizado Especial Federal de Cuiabá", "TRF1") == "jef"
    assert analise.rito("Primeira Turma Recursal", "TJMT") == "turma_recursal"
    assert analise.rito("9ª VARA DO TRABALHO DE CUIABÁ", "TRT23") == "trabalho"
    assert analise.rito("Gabinete da Desembargadora Eliney Veloso", "TRT23") == "trabalho_2g"
    assert analise.rito("1ª VARA ESP. DA FAZENDA PÚBLICA DE CUIABÁ", "TJMT") == "civel"
    assert analise.rito("Gab. 03 - DESEMBARGADOR FEDERAL MARCELO ALBERNAZ", "TRF1") == "tribunal"
    assert analise.rito("6ª VARA CRIMINAL DE CUIABÁ", "TJMT") == "criminal"
    assert analise.rito("7ª VARA CÍVEL DE CUIABÁ", "TJMT") == "civel"


def test_prompt_do_juizado_diz_que_nao_ha_apelacao():
    ctx = analise.contexto(PEDRO, "PEDRO RODRIGUES MONTALVAO NETO", {"polo_passivo": "PEDRO RODRIGUES MONTALVAO NETO"},
                           "Ação de cobrança de honorários.", [{"data": "2026-09-01", "tipo": "Despacho", "resumo": "Réplica"}])
    assert "Juizado Especial Cível" in ctx and "recurso inominado: 10 dias úteis" in ctx.lower()
    assert "NÃO existe apelação" in ctx
    assert "NOSSO CLIENTE: PEDRO" in ctx and "Polo passivo: PEDRO" in ctx
    assert "INTIMAÇÕES ANTERIORES" in ctx and "Réplica" in ctx


def test_sentenca_favoravel_no_juizado_nao_vira_manifestar_15_dias():
    """O caso Pedro: a IA antiga disse 'Manifestar sobre a sentença', 15 dias."""
    a = analise.salvaguardar({"resultado": "favoravel", "deadline": {"action": "Manifestar sobre a sentença", "days": 15}},
                             PEDRO, "JULGO IMPROCEDENTES os pedidos")
    assert a["deadline"]["action"] == "Acompanhar trânsito em julgado" and a["deadline"]["days"] == 10
    assert "9.099" in a["deadline"]["fundamento"]
    assert a["tipo_ato"] == "sentenca" and a["rito"] == "juizado_civel"


def test_sentenca_desfavoravel_no_juizado_e_recurso_inominado_com_embargos_de_alternativa():
    a = analise.salvaguardar({"resultado": "desfavoravel", "deadline": {"action": "Interpor apelação", "days": 15}},
                             PEDRO, "JULGO PROCEDENTE")
    assert a["deadline"] == {"action": "Interpor recurso inominado", "days": 10, "fundamento": "Lei 9.099/95, art. 42"}
    assert [(x["action"], x["days"]) for x in a["alternativas"]] == [("Opor embargos de declaração", 5)]


def test_sentenca_generica_sem_resultado_vira_o_recurso_do_rito():
    a = analise.salvaguardar({"deadline": {"action": "Manifestar sobre a sentença", "days": 15}},
                             {**PEDRO, "nome_orgao": "7ª VARA CÍVEL DE CUIABÁ", "nome_classe": None}, "")
    assert (a["deadline"]["action"], a["deadline"]["days"]) == ("Interpor apelação", 15)


def test_embargos_tem_5_dias_em_todo_rito_civel_e_trabalhista():
    for orgao, trib in [("6º JUIZADO ESPECIAL CÍVEL", "TJMT"), ("7ª VARA CÍVEL", "TJMT"), ("9ª VARA DO TRABALHO", "TRT23")]:
        p = analise.corrigir_providencia({"action": "Opor embargos de declaração", "days": 10}, analise.rito(orgao, trib))
        assert p["days"] == 5, orgao


def test_trabalhista_recurso_ordinario_8_dias_e_apelacao_vira_ro():
    p = analise.corrigir_providencia({"action": "Interpor apelação", "days": 15}, "trabalho")
    assert p == {"action": "Interpor recurso ordinário", "days": 8, "fundamento": "CLT, art. 895"}


def test_prazo_fixado_pelo_juiz_vale_para_replica_mas_nao_para_recurso():
    assert analise.corrigir_providencia({"action": "Apresentar réplica", "days": 10, "fundamento": "fixado pelo juiz"}, "civel")["days"] == 10
    assert analise.corrigir_providencia({"action": "Interpor recurso inominado", "days": 15, "fundamento": "fixado pelo juiz"}, "juizado_civel")["days"] == 10


def test_pericia_do_vicente_vira_compromisso_e_mantem_os_quesitos():
    a = analise.salvaguardar({
        "tipo_ato": "designacao_pericia", "resultado": "neutro",
        "deadline": {"action": "Indicar assistente técnico e quesitos", "days": 15, "fundamento": "CPC, art. 465, §1º"},
        "compromisso": {"tipo": "pericia", "subtipo": "médica", "data": "2026-11-23", "hora": "14:00",
                        "modalidade": "presencial", "local": "Avenida Miguel Sutil, CEP 78070-300", "perito": "PERICIAS MED",
                        "observacoes": "Levar documento com foto, exames e laudos."},
    }, VICENTE, TEXTO_VICENTE)
    c = a["compromisso"]
    assert (c["tipo"], c["data"], c["hora"], c["modalidade"], c["perito"]) == ("pericia", "2026-11-23", "14:00", "presencial", "PERICIAS MED")
    assert a["deadline"]["days"] == 15


def test_compromisso_com_data_que_nao_esta_no_texto_e_descartado():
    a = analise.salvaguardar({"compromisso": {"tipo": "audiencia", "data": "2026-11-24", "hora": "14:00"}}, VICENTE, TEXTO_VICENTE)
    assert a["compromisso"] is None


def test_compromisso_anterior_a_publicacao_e_descartado():
    texto = "audiência realizada em 10/09/2026, às 09:00"
    a = analise.salvaguardar({"compromisso": {"tipo": "audiencia", "data": "2026-09-10", "hora": "09:00"}}, VICENTE, texto)
    assert a["compromisso"] is None


def test_hora_que_nao_esta_no_texto_cai_mas_a_data_fica():
    a = analise.salvaguardar({"compromisso": {"tipo": "pericia", "data": "2026-11-23", "hora": "15:30"}}, VICENTE, TEXTO_VICENTE)
    assert a["compromisso"]["data"] == "2026-11-23" and a["compromisso"]["hora"] is None


def test_pauta_virtual_da_turma_recursal_e_julgamento_sem_prazo():
    it = {"sigla_tribunal": "TJMT", "nome_orgao": "Primeira Turma Recursal", "tipo_documento": "Intimação de pauta",
          "data_disponibilizacao": "2026-08-28"}
    texto = ("INTIMAÇÃO DE PAUTA DE JULGAMENTO JULGAMENTO DESIGNADO PARA A SESSÃO Ordinária, QUE SERÁ REALIZADA entre "
             "10 de Setembro de 2026 a 14 de Setembro de 2026, ÀS 08:00 HORAS, NO PLENÁRIO VIRTUAL")
    a = analise.salvaguardar({"deadline": {"action": "Acompanhar julgamento", "days": 5},
                              "compromisso": {"tipo": "julgamento", "subtipo": "sessão virtual", "data": "2026-09-10",
                                              "data_fim": "2026-09-14", "hora": "08:00", "modalidade": "online"}}, it, texto)
    assert a["deadline"] is None and a["tipo_ato"] == "pauta_julgamento"
    assert (a["compromisso"]["data"], a["compromisso"]["data_fim"], a["compromisso"]["hora"]) == ("2026-09-10", "2026-09-14", "08:00")


def test_link_que_nao_e_url_some():
    a = analise.salvaguardar({"compromisso": {"tipo": "pericia", "data": "2026-11-23", "link": "sala 3"}}, VICENTE, TEXTO_VICENTE)
    assert a["compromisso"]["link"] is None


def test_alternativas_sem_repetir_o_principal():
    a = analise.salvaguardar({"resultado": "desfavoravel", "deadline": {"action": "Interpor recurso inominado", "days": 10},
                              "alternativas": [{"action": "Interpor recurso inominado", "days": 10},
                                               {"action": "Opor embargos de declaração", "days": 5}]}, PEDRO, "")
    assert [x["action"] for x in a["alternativas"]] == ["Opor embargos de declaração"]


def test_polo_do_cliente_pelo_nome_e_pela_pessoa_fisica():
    p = analise.polo_do_cliente
    assert p("PEDRO RODRIGUES MONTALVAO NETO", "RAIANE MARQUES DE JESUS", "PEDRO RODRIGUES MONTALVAO NETO") == "passivo"
    # Jessica: nome não bate, mas a pessoa física é o cliente, não o banco
    assert p("JESSICA PEREIRA DA SILVA", "JESSICA DA SILVA GONCALVES", "NU PAGAMENTOS S.A.") == "ativo"
    assert p("MARIA SOUZA", "INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS", "MARIA DE SOUZA") == "passivo"
    assert p(None, "CARLOS ROBERTO", "TRANSPORTES LTDA") == "ativo"
    assert p("JOAO", "JOAO", "JOSE") == "ativo"
    assert p(None, None, None) is None


def test_contexto_le_os_polos_do_cabecalho_quando_a_ficha_nao_tem():
    it = {**PEDRO, "texto": "SENTENÇA Processo: 1038323-66.2026.8.11.0001. AUTOR: RAIANE MARQUES DE JESUS REU: PEDRO RODRIGUES MONTALVAO NETO Vistos. Trata-se"}
    ctx = analise.contexto(it, "PEDRO RODRIGUES MONTALVAO NETO", None, None, [])
    assert "POLO PASSIVO" in ctx and "RAIANE" in ctx


def test_tutela_vicente_postergada_mesmo_com_concedo_da_gratuidade_e_doutrina():
    texto = ("Sobre a concessão da tutela de urgência, ensina a doutrina: A decisão que concede tutela provisória é baseada "
             "em cognição sumária. Portanto, postergo a apreciação do pedido de tutela de urgência para após a produção da "
             "prova pericial. RECEBO a inicial. CONCEDO os benefícios da justiça gratuita.")
    assert analise.tutela_no_texto(texto) == "postergada"
    assert analise.conferir_tutela("concedida", texto) == "postergada"


def test_tutela_pelo_verbo_do_juiz():
    t = analise.tutela_no_texto
    assert t("Ausentes os requisitos, INDEFIRO a tutela de urgência.") == "negada"
    assert t("Não defiro, por ora, a liminar pleiteada.") == "negada"
    assert t("Presentes os requisitos, DEFIRO a tutela de urgência para determinar o restabelecimento.") == "concedida"
    assert t("DEFIRO PARCIALMENTE a tutela de urgência para suspender os atos expropriatórios.") == "concedida_em_parte"
    assert t("Revogo a liminar anteriormente concedida.") == "revogada"
    assert t("A parte requereu tutela de urgência. Cite-se.") is None
    # várias menções: vale a última (dispositivo)
    assert t("A tutela foi indeferida pelo juízo de origem. Reconsidero e DEFIRO a tutela de urgência.") == "concedida"


def test_conferir_tutela_sem_verbo_no_texto_vale_a_ia():
    assert analise.conferir_tutela("concedida", "Tutela deferida.") == "concedida"
    assert analise.conferir_tutela("inventada", "Cite-se.") is None
    assert analise.conferir_tutela("concedida_em_parte", "DEFIRO a tutela.") == "concedida_em_parte"


def test_seguranca_concedida_confirmando_a_liminar_e_liminar_mantida():
    assert analise.tutela_no_texto("Ante o exposto, CONCEDO A SEGURANÇA, confirmando a liminar, e extingo o processo.") == "mantida"
    assert analise.tutela_no_texto("Concedo a segurança para ratificar a decisão liminar.") == "mantida"
