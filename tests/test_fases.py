from datetime import date

from jurius_processos.fases import analisar

HOJE = date(2026, 9, 24)


def mov(codigo, quando, nome="x", **compl):
    m = {"codigo": codigo, "dataHora": f"{quando}T12:00:00.000Z", "nome": nome}
    if compl:
        m["complementosTabelados"] = [{"descricao": k, "nome": v[0], "valor": v[1]} for k, v in compl.items()]
    return m


def inst(grau, movimentos, classe=(436, "Procedimento do Juizado Especial Cível"), ajuizamento="20240101000000",
         atualizado="2026-09-01T00:00:00Z"):
    return {"grau": grau, "classe": {"codigo": classe[0], "nome": classe[1]}, "movimentos": movimentos,
            "dataAjuizamento": ajuizamento, "dataHoraUltimaAtualizacao": atualizado, "tribunal": "TJMT",
            "orgaoJulgador": {"nome": "6º JEC"}}


def test_distribuido_e_audiencia_de_conciliacao_designada():
    a = analisar("n", [inst("JE", [
        mov(26, "2026-08-01", "Distribuição"),
        mov(12740, "2026-08-02", "de Conciliação", situacao_da_audiencia=("designada", 9)),
    ])], [], HOJE)
    assert a["fase"] == "conhecimento"
    assert a["status_crm"] == "conciliacao"
    assert a["audiencia"]["situacao"] == "designada"
    assert any(p["tipo"] == "audiencia_designada" for p in a["pendencias"])


def test_audiencia_realizada_nao_fica_pendente():
    a = analisar("n", [inst("JE", [
        mov(26, "2026-08-01"),
        mov(12740, "2026-08-02", "de Conciliação", situacao_da_audiencia=("designada", 9)),
        mov(12740, "2026-09-02", "de Conciliação", situacao_da_audiencia=("realizada", 13)),
    ])], [], HOJE)
    assert a["audiencia"] is None


def test_sentenca_procedente_transitada_sem_cumprimento_vira_pendencia():
    a = analisar("n", [inst("JE", [
        mov(26, "2025-01-01"),
        mov(219, "2025-05-01", "Procedência"),
        mov(848, "2025-06-01", "Trânsito em julgado"),
        mov(85, "2026-09-01", "Petição"),
    ])], [], HOJE)
    assert a["fase"] == "transitado"
    assert [p["tipo"] for p in a["pendencias"]] == ["execucao_pendente"]


def test_improcedencia_transitada_nao_cobra_execucao():
    a = analisar("n", [inst("JE", [
        mov(26, "2025-01-01"), mov(220, "2025-05-01", "Improcedência"), mov(848, "2025-06-01"),
        mov(85, "2026-09-01"),
    ])], [], HOJE)
    assert not any(p["tipo"] == "execucao_pendente" for p in a["pendencias"])


def test_recurso_em_segundo_grau():
    a = analisar("n", [
        inst("G1", [mov(26, "2025-01-01"), mov(220, "2025-05-01"),
                    mov(123, "2025-06-01", "Remessa", motivo_da_remessa=("em grau de recurso", 38))]),
        inst("G2", [mov(26, "2025-06-02"), mov(51, "2026-09-01")], classe=(198, "Apelação Cível")),
    ], [], HOJE)
    assert a["fase"] == "recursal"
    assert a["status_crm"] == "recurso"
    assert a["graus"] == ["G1", "G2"]


def test_cumprimento_por_evolucao_de_classe():
    a = analisar("n", [inst("G1", [
        mov(26, "2024-01-01"), mov(219, "2024-05-01"), mov(848, "2024-06-01"),
        mov(14739, "2024-07-01", "Evolução da Classe Processual", classe_nova=("Cumprimento", 156)),
        mov(85, "2026-09-01"),
    ])], [], HOJE)
    assert a["fase"] == "cumprimento_sentenca"
    assert a["status_crm"] == "cumprimento"
    assert not any(p["tipo"] == "execucao_pendente" for p in a["pendencias"])


def test_arquivado_definitivo_e_desarquivamento():
    base = [mov(26, "2024-01-01"), mov(220, "2024-05-01"), mov(848, "2024-06-01"), mov(246, "2024-07-01", "Definitivo")]
    assert analisar("n", [inst("G1", base)], [], HOJE)["situacao"] == "arquivado"
    reaberto = analisar("n", [inst("G1", base + [mov(893, "2026-08-01", "Desarquivamento")])], [], HOJE)
    assert reaberto["situacao"] == "ativo"


def test_baixa_na_instancia_recursal_nao_arquiva_o_processo():
    a = analisar("n", [
        inst("G1", [mov(26, "2025-01-01"), mov(221, "2025-05-01")]),
        inst("TR", [mov(26, "2025-06-01"), mov(239, "2025-09-01"), mov(22, "2025-10-01", "Baixa Definitiva")]),
    ], [], HOJE)
    assert a["situacao"] == "ativo"


def test_extincao_da_execucao_nao_arquiva_so_o_tribunal_arquiva():
    # Extinção é quando o dinheiro sai: arquivar ali escondia valor a levantar.
    a = analisar("n", [inst("G1", [mov(26, "2024-01-01"), mov(11385, "2024-06-01"), mov(196, "2025-01-01")])], [], HOJE)
    assert a["situacao"] == "ativo"
    assert a["execucao_extinta_em"] == "2025-01-01"
    assert any(p["tipo"] == "execucao_extinta" for p in a["pendencias"])
    # O arquivamento do tribunal (246) arquiva de fato.
    b = analisar("n", [inst("G1", [mov(26, "2024-01-01"), mov(11385, "2024-06-01"), mov(196, "2025-01-01"),
                                   mov(246, "2025-02-01", "Arquivamento definitivo")])], [], HOJE)
    assert b["situacao"] == "arquivado" and b["arquivado_em"] == "2025-02-01"


def test_parado_demais():
    a = analisar("n", [inst("G1", [mov(26, "2025-01-01"), mov(785, "2025-02-01")])], [], HOJE)
    assert any(p["tipo"] == "parado_demais" and p["severidade"] == "alta" for p in a["pendencias"])


def test_alvara_recente():
    a = analisar("n", [inst("G1", [mov(26, "2025-01-01"),
                                   mov(60, "2026-09-01", "Expedição de documento", tipo_de_documento=("Alvará", 73))])],
                 [], HOJE)
    assert any(p["tipo"] == "valor_a_levantar" for p in a["pendencias"])


def test_sem_datajud_usa_classe_do_djen():
    coms = [{"data": "2026-09-01", "classe": "CUMPRIMENTO DE SENTENÇA", "tribunal": "TJMT", "orgao": "1ª Vara"}]
    a = analisar("n", [], coms, HOJE)
    assert a["fonte"] == "djen"
    assert a["fase"] == "cumprimento_sentenca"
    assert any(p["tipo"] == "sem_linha_do_tempo" for p in a["pendencias"])


def test_djen_mais_novo_que_datajud_extingue_a_execucao():
    # Caso real (24/09/2026): DataJud parado em "Decurso de Prazo", DJEN já com a extinção.
    datajud = [inst("G1", [mov(26, "2025-06-01"), mov(466, "2025-08-01"), mov(1051, "2026-08-01")])]
    coms = [{"data": "2026-08-19", "classe": "ATOrd", "texto": "1.Diante do cumprimento do acordo, "
             "declaro extinta a execução dos créditos trabalhistas"}]
    a = analisar("n", datajud, coms, HOJE)
    assert a["situacao"] == "ativo"
    assert a["execucao_extinta_em"] == "2026-08-19"
    assert a["status_crm"] != "arquivado"


def test_djen_antigo_nao_sobrepoe_datajud():
    datajud = [inst("G1", [mov(26, "2025-06-01"), mov(893, "2026-09-01", "Desarquivamento")])]
    coms = [{"data": "2026-01-10", "texto": "declaro extinta a execução"}]
    assert analisar("n", datajud, coms, HOJE)["situacao"] == "ativo"


def test_audiencia_designada_no_djen_traz_a_data():
    coms = [{"data": "2026-09-10", "classe": "ATSum",
             "texto": "Designo audiência de instrução e julgamento para o dia 15/10/2026, às 9h."}]
    a = analisar("n", [], coms, HOJE)
    assert a["audiencia"]["data"] == "2026-10-15"
    assert a["fase"] == "instrucao"
    assert any("2026-10-15" in p["descricao"] for p in a["pendencias"])


def test_audiencia_ja_passada_nao_fica_pendente():
    coms = [{"data": "2026-01-10", "texto": "designo audiência de conciliação para o dia 01/02/2026"}]
    assert analisar("n", [], coms, HOJE)["audiencia"] is None


def test_formatos_reais_de_audiencia():
    from jurius_processos.fases import audiencia_no_texto
    tjmt = "DADOS DA AUDIÊNCIA: AUDIÊNCIA DE CONCILIAÇÃO VIRTUAL designada Tipo: Conciliação juizado Data: 27/10/2026 Hora: 13:40"
    trt_a = "INCLUO o presente processo em pauta de AUDIÊNCIA INICIAL TELEPRESENCIAL a realizar-se no dia 05/11/2026 às 09h"
    trt_b = "o incluo pauta de audiências INICIAIS do dia de 29/10/2026 às 11h05min"
    passada = "a audiência realizada em 10/05/2026 restou infrutífera."
    assert audiencia_no_texto(tjmt)[0] == "conciliação" and audiencia_no_texto(tjmt)[1].day == 27
    assert audiencia_no_texto(trt_a)[1].month == 11
    assert audiencia_no_texto(trt_b)[0] == "conciliação"
    assert audiencia_no_texto(passada) is None
    assert audiencia_no_texto("AGUARDE-SE a audiência de conciliação designada no feito.") == ("conciliação", None)


def test_extinta_a_presente_execucao_e_alvara_no_texto():
    datajud = [inst("JE", [mov(26, "2026-06-01"), mov(221, "2026-08-07"), mov(14739, "2026-09-10")])]
    coms = [{"data": "2026-09-21", "texto": "julgo extinta a presente execução. ... expedição de alvará eletrônico"}]
    a = analisar("n", datajud, coms, HOJE)
    # Caso real 10353746920268110001: extinta por pagamento, alvará a levantar.
    assert a["situacao"] == "ativo" and a["status_crm"] == "cumprimento"
    assert "2026-09-21" in a["alvaras"]
    tipos = {p["tipo"] for p in a["pendencias"]}
    assert {"execucao_extinta", "valor_a_levantar"} <= tipos


def test_ciencia_da_sentenca_no_djen():
    datajud = [inst("G1", [mov(26, "2026-02-16"), mov(12740, "2026-03-18", situacao_da_audiencia=("realizada", 13))])]
    coms = [{"data": "2026-09-24", "texto": "Fica V. Sa. intimado para tomar ciência da Sentença ID 041c32d"}]
    assert analisar("n", datajud, coms, HOJE)["fase"] == "sentenciado"


def test_alvara_so_no_djen_vira_pendencia():
    coms = [{"data": "2026-09-17", "classe": "CUMPRIMENTO DE SENTENÇA",
             "texto": "Intimação das partes acerca da expedição do alvará eletrônico de pagamento."}]
    a = analisar("n", [], coms, HOJE)
    assert any(p["tipo"] == "valor_a_levantar" for p in a["pendencias"])


def test_recurso_distribuido_e_julgado_depois_do_arquivo_reabre():
    # Caso real 10127312020268110001: arquivado no Juizado e recurso andando na Turma.
    datajud = [inst("JE", [mov(26, "2026-03-01"), mov(221, "2026-06-01"), mov(246, "2026-07-22", "Arquivamento definitivo")]),
               inst("TR", [mov(26, "2026-08-01", "Distribuição"), mov(12000, "2026-08-20", "Pedido de inclusão em pauta virtual")])]
    a = analisar("n", datajud, [], HOJE)
    assert a["situacao"] == "ativo"


def test_peticao_depois_do_arquivo_nao_reabre():
    datajud = [inst("JE", [mov(26, "2026-03-01"), mov(246, "2026-07-22", "Arquivamento definitivo"),
                           mov(85, "2026-08-10", "Petição"), mov(11383, "2026-08-11", "Ato ordinatório")])]
    assert analisar("n", datajud, [], HOJE)["situacao"] == "arquivado"


def test_intimacao_depois_do_arquivo_pede_conferencia():
    datajud = [inst("JE", [mov(26, "2026-03-01"), mov(246, "2026-07-22", "Arquivamento definitivo")])]
    coms = [{"data": "2026-09-02", "texto": "Intimação da parte autora."}]
    a = analisar("n", datajud, coms, HOJE)
    assert a["situacao"] == "arquivado"
    assert any(p["tipo"] == "intimacao_apos_arquivamento" for p in a["pendencias"])


def test_caso_ramona_cumprimento_e_extincao_pelo_pagamento_so_no_djen():
    # Caso real 1028965-77.2026.8.11.0001 (auditoria 26/09/2026): DataJud parado
    # em 10/08; o DJEN já trazia a intimação do art. 523, a extinção pelo
    # pagamento (art. 924, II) e o alvará. O CRM mostrava "Sentença".
    datajud = [inst("JE", [mov(26, "2026-05-01"), mov(219, "2026-07-10"), mov(1051, "2026-08-10")])]
    coms = [
        {"data": "2026-08-13", "texto": "impulsiono estes autos com a finalidade de intimar a parte EXECUTADA para que "
         "no prazo de 15 (quinze) dias efetue o pagamento voluntário do débito, sob pena de incidência de multa "
         "de 10%, consoante art. 523, § 1º"},
        {"data": "2026-09-16", "texto": "não apresentou impugnação ao cumprimento de sentença, de modo que imperiosa a "
         "extinção do processo pelo pagamento. De conseguinte, satisfeita a execução por meio da penhora, com "
         "fulcro no artigo 924, II, do Código de Processo Civil, DECLARO EXTINTO O PROCESSO. Nesta oportunidade, "
         "expeço o competente alvará. Arquivem-se os autos."},
    ]
    a = analisar("n", datajud, coms, HOJE)
    assert a["fase"] == "cumprimento_sentenca" and a["status_crm"] == "cumprimento"
    assert a["situacao"] == "ativo" and a["execucao_extinta_em"] == "2026-09-16"
    assert "2026-09-16" in a["alvaras"]


def test_so_a_intimacao_do_523_ja_e_cumprimento():
    datajud = [inst("JE", [mov(26, "2026-05-01"), mov(219, "2026-07-10")])]
    coms = [{"data": "2026-08-13", "texto": "intimar a parte executada para pagamento voluntário do débito (art. 523)"}]
    assert analisar("n", datajud, coms, HOJE)["status_crm"] == "cumprimento"


def test_extincao_sem_merito_nao_vira_execucao_extinta():
    # "Declaro extinto o processo" sozinho é desistência/abandono (art. 485), não pagamento.
    datajud = [inst("JE", [mov(26, "2026-05-01"), mov(1051, "2026-08-10")])]
    coms = [{"data": "2026-09-16", "texto": "homologo a desistência e DECLARO EXTINTO O PROCESSO sem resolução "
             "do mérito, com fulcro no art. 485, VIII"}]
    a = analisar("n", datajud, coms, HOJE)
    assert a["execucao_extinta_em"] is None and a["fase"] != "cumprimento_sentenca"


def _luana():
    # Caso real 0000691-24.2026.5.23.0006 (auditoria 26/09/2026): DataJud e DJEN
    # param em 07/07 com a audiência INICIAL designada para 20/08. A instrução
    # (18/11) só existe na agenda do escritório.
    datajud = [inst("G1", [mov(26, "2026-07-03"), mov(12740, "2026-07-03", "de Conciliação",
                                                         situacao_da_audiencia=("designada", 1))],
                    classe=(985, "Ação Trabalhista - Rito Ordinário"))]
    coms = [{"data": "2026-07-07", "texto": "DESIGNO audiência INICIAL a realizar-se no formato HÍBRIDO no dia "
             "20/08/2026, às 08:25 horas"}]
    agenda = [{"quando": "2026-08-20T12:25:00+00:00", "titulo": "AUDIÊNCIA INICIAL - LUANA ALENCAR", "status": "pendente"},
              {"quando": "2026-11-18T13:00:00+00:00", "titulo": "AUDIÊNCIA PRESENCIAL - LUANA", "status": "pendente"}]
    return datajud, coms, agenda


def test_caso_luana_audiencia_depois_da_inicial_e_instrucao():
    datajud, coms, agenda = _luana()
    a = analisar("n", datajud, coms, HOJE, agenda=agenda)
    assert a["fase"] == "instrucao" and a["status_crm"] == "instrucao"
    assert a["audiencia"]["tipo"] == "instrução" and a["audiencia"]["data"] == "2026-11-18"


def test_conciliacao_designada_que_ja_passou_nao_prende_o_estagio():
    # Sem a agenda: a inicial de 20/08 já passou; não é mais "conciliação" pendente.
    datajud, coms, _ = _luana()
    a = analisar("n", datajud, coms, HOJE)
    assert a["status_crm"] != "conciliacao"
    assert a["audiencia"] is None


def test_agenda_titulo_de_instrucao_e_conciliacao_futura():
    datajud = [inst("JE", [mov(26, "2026-07-03")])]
    a = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-10-10T12:00:00Z",
                                                  "titulo": "Audiência de Instrução - Joanil", "status": "pendente"}])
    assert a["status_crm"] == "instrucao"
    b = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-10-10T12:00:00Z",
                                                  "titulo": "AUDIÊNCIA DE CONCILIAÇÃO - JOSENIL", "status": "pendente"}])
    assert b["status_crm"] == "conciliacao" and b["audiencia"]["data"] == "2026-10-10"


def test_audiencia_generica_sem_conciliacao_antes_nao_vira_instrucao():
    datajud = [inst("JE", [mov(26, "2026-07-03")])]
    a = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-10-10T12:00:00Z",
                                                  "titulo": "Audiência Online — OSMAR", "status": "pendente"}])
    assert a["fase"] != "instrucao" and a["audiencia"]["data"] == "2026-10-10"


def test_agenda_cancelada_nao_conta():
    datajud = [inst("JE", [mov(26, "2026-07-03")])]
    a = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-10-10T12:00:00Z",
                                                  "titulo": "Audiência de Instrução", "status": "cancelado"}])
    assert a["fase"] != "instrucao" and a["audiencia"] is None


def test_audiencia_da_agenda_no_mesmo_dia_da_conciliacao_nao_e_instrucao():
    # Caso real 1027933-37.2026.8.11.0001: a conciliação realizada em 17/07 está
    # na agenda como "Audiência Online — DEYVID".
    datajud = [inst("JE", [mov(26, "2026-05-01"),
                           mov(12740, "2026-07-17", "de Conciliação", situacao_da_audiencia=("realizada", 1))])]
    a = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-07-17T14:00:00Z",
                                                  "titulo": "Audiência Online — DEYVID", "status": "pendente"}])
    assert a["fase"] != "instrucao" and a["status_crm"] == "contestacao"


def test_audiencia_generica_na_agenda_herda_o_tipo_do_tribunal():
    # Casos reais (Dario, Osmar, Douglas, Jeniffer): DataJud "conciliação designada"
    # sem data; a agenda tem a data com título genérico.
    datajud = [inst("JE", [mov(26, "2026-08-01"),
                           mov(12740, "2026-08-14", "de Conciliação", situacao_da_audiencia=("designada", 1))])]
    a = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-10-14T14:00:00Z",
                                                  "titulo": "Audiência Online — DARIO", "status": "pendente"}])
    assert a["status_crm"] == "conciliacao" and a["audiencia"]["data"] == "2026-10-14"


def test_conciliacao_que_passou_leva_a_contestacao():
    # Casos reais (Lisliandra, Hiago, Juliana): designada no DataJud, feita pela agenda.
    datajud = [inst("JE", [mov(26, "2026-08-01"),
                           mov(12740, "2026-08-14", "de Conciliação", situacao_da_audiencia=("designada", 1))])]
    a = analisar("n", datajud, [], HOJE, agenda=[{"quando": "2026-09-18T14:00:00Z",
                                                  "titulo": "Audiência Online — JULIANA", "status": "pendente"}])
    assert a["status_crm"] == "contestacao" and a["audiencia"] is None


def test_status_desde_diz_de_quando_e_o_fato():
    datajud, coms, agenda = _luana()
    agenda[1]["criado_em"] = "2026-08-20T15:00:00+00:00"   # instrução lançada na agenda no dia da inicial
    a = analisar("n", datajud, coms, HOJE, agenda=agenda)
    assert a["status_crm"] == "instrucao" and a["status_desde"] == "2026-08-20"
