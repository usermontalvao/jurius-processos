import json
from dataclasses import dataclass

from jurius_processos.vinculo import IndiceClientes, processos_do_nome, vincular, partes


@dataclass(frozen=True)
class C:
    id: str
    nome: str
    pre_cadastro: bool = False
    cpf_cnpj: str | None = None


@dataclass(frozen=True)
class P:
    id: str
    numero: str
    client_id: str
    status: str = "andamento"
    status_manual: bool = False


CLIENTES = [
    C("c1", "Maria José da Silva"),
    C("c2", "JOÃO PEDRO SOUZA"),
    C("c3", "João Pedro Souza"),          # homônimo de c2
    C("c4", "Ana Paula Rocha", pre_cadastro=True),
    C("c5", "Carlos Eduardo Campos Ferreira"),
]
IDX = IndiceClientes(CLIENTES)


def com(*dest, advs=("PEDRO RODRIGUES MONTALVAO NETO",), texto=""):
    return {"destinatarios": json.dumps([{"nome": n, "polo": p} for n, p in dest]),
            "advogados": json.dumps([{"nome": a} for a in advs]), "texto": texto}


def test_nome_identico_a_um_cliente_vincula_sozinho():
    v = vincular("n", [com(("MARIA JOSE DA SILVA", "A"), ("BANCO X S.A.", "P"))], IDX)
    assert v["tipo"] == "nome_exato"
    assert v["client_id"] == "c1"
    assert v["polo_cliente"] == "A"


def test_homonimo_nao_vincula_sozinho():
    v = vincular("n", [com(("JOAO PEDRO SOUZA", "A"))], IDX)
    assert v["tipo"] == "sugestao"
    assert {s["client_id"] for s in v["sugestoes"]} == {"c2", "c3"}


def test_pre_cadastro_so_sugere():
    v = vincular("n", [com(("ANA PAULA ROCHA", "A"))], IDX)
    assert v["tipo"] == "sugestao"


def test_nome_do_meio_faltando_so_sugere():
    v = vincular("n", [com(("CARLOS EDUARDO FERREIRA", "A"))], IDX)
    assert v["tipo"] == "sugestao"
    assert v["sugestoes"][0]["client_id"] == "c5"


def test_parte_desconhecida_fica_sem_cliente_com_nome_da_parte():
    v = vincular("n", [com(("FULANO DE TAL", "A"), ("CLARO S.A.", "P"))], IDX)
    assert v["tipo"] == "sem_cliente"
    assert v["parte_principal"] == "FULANO DE TAL"


def test_processo_do_crm_manda():
    v = vincular("n", [com(("MARIA JOSE DA SILVA", "A"))], IDX, P("p1", "n", "c1"))
    assert v["tipo"] == "cadastro"
    assert v["crm_process_id"] == "p1"


def test_crm_com_cliente_que_nao_aparece_e_partes_de_outro_cliente_e_divergente():
    v = vincular("n", [com(("MARIA JOSE DA SILVA", "A"))], IDX, P("p1", "n", "c5"))
    assert v["tipo"] == "divergente"


def test_advogado_destinatario_nao_vira_parte():
    ps = partes([com(("PEDRO RODRIGUES MONTALVAO NETO", "A"), ("DARIANE SILVA E SILVA", "A"))])
    assert ps["A"] == ["DARIANE SILVA E SILVA"]


def test_relacionados_pelo_texto():
    v = vincular("10310416020244010000", [com(("X", "A"), texto="Processo de origem: 1019760-74.2024.4.01.3600")], IDX)
    assert v["relacionados"] == ["10197607420244013600"]


def test_cliente_novo_acha_processos_que_ja_estavam_no_acervo():
    acervo = {"n1": {"A": ["MARIA JOSÉ DA SILVA"], "P": []}, "n2": {"A": [], "P": ["Maria Jose da Silva"]},
              "n3": {"A": ["OUTRA PESSOA"], "P": []}}
    assert processos_do_nome("Maria José da Silva", acervo) == [("n1", "A"), ("n2", "P")]


def test_nome_curto_identico_so_sugere():
    idx = IndiceClientes([C("c9", "José da Silva")])
    v = vincular("n", [com(("JOSE DA SILVA", "A"))], idx)
    assert v["tipo"] == "sugestao"
    assert "curto" in v["sugestoes"][0]["motivo"]
