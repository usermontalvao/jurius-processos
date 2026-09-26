"""Agendador escrevendo enquanto o painel e o healthcheck leem (1º boot no servidor)."""

import threading

from jurius_processos import painel
from jurius_processos.banco import Banco


def test_leitura_e_escrita_em_threads_diferentes_nao_quebram(tmp_path):
    b = Banco(tmp_path / "t.sqlite3")
    erros = []

    def escritor():
        try:
            for i in range(4000):
                item = {"id": i, "data_disponibilizacao": "2026-01-01", "texto": "x" * 200,
                        "destinatarioadvogados": [], "destinatarios": []}
                b.gravar_comunicacao(item, f"{i % 300:020d}", "oab")
                b.garantir_processo(f"{i % 300:020d}", "djen")
        except Exception as e:  # noqa: BLE001
            erros.append(("escritor", repr(e)))

    def leitor():
        try:
            for _ in range(150):
                painel.dados(b, True)
                assert b.con.execute("select count(*) from processos").fetchone() is not None
        except Exception as e:  # noqa: BLE001
            erros.append(("leitor", repr(e)))

    ts = [threading.Thread(target=escritor)] + [threading.Thread(target=leitor) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert erros == []
    assert b.con.execute("select count(*) from comunicacoes").fetchone()[0] == 4000


def test_execucao_sem_fim_e_fechada_ao_subir(tmp_path):
    b = Banco(tmp_path / "t.sqlite3")
    b.abrir_execucao("carga_completa")
    assert b.fechar_interrompidas() == 1
    r = b.con.execute("select ok, json_extract(resumo,'$.erro') e from execucoes").fetchone()
    assert r["ok"] == 0 and "interrompida" in r["e"]
    assert not b.carga_feita()


def test_carga_em_curso_nao_conta_como_feita(tmp_path):
    b = Banco(tmp_path / "t.sqlite3")
    b.abrir_execucao("carga_completa")  # em curso
    i = b.abrir_execucao("descobrir")
    b.fechar_execucao(i, True, {"inicio": "2023-01-01"})  # 1ª etapa dela terminou
    assert not b.carga_feita()
