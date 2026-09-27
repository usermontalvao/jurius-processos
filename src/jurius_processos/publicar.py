"""Publicação no Supabase: o CRM só LÊ o que sai daqui.

Três movimentos, nesta ordem:
  1. upsert de todo o acervo em `acervo_processos` (a aba Acervo lê dela);
  2. [desligado por padrão] cadastro automático em `processes` do que é de
     cliente cadastrado e ainda não estava no CRM (vínculo `nome_exato`);
  3. [desligado por padrão] `processes.status` dos já cadastrados.

Sem os dois interruptores (JURIUS_CADASTRAR_AUTO, JURIUS_ATUALIZAR_STATUS) o
cérebro só escreve no acervo: importar é decisão de quem usa o CRM.

Por padrão roda em ensaio (`aplicar=False`): calcula tudo e devolve o plano
sem gravar nada. Precisa da migration sql/001_acervo.sql aplicada.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import httpx

from . import cnj
from .banco import Banco
from .config import cabecalhos_supabase


def partes_para_tela(partes: dict, textos: list) -> dict:
    """{A: [...], P: [...]} para exibir: polo vazio completado pelo texto."""
    from .ficha import limpar_partes, partes_do_texto
    out = {k: list(v or []) for k, v in (partes or {}).items()}
    if not (out.get("A") and out.get("P")):
        lidas = partes_do_texto(textos)
        for polo in ("A", "P"):
            if not out.get(polo):
                out[polo] = lidas[polo]
    return {polo: limpar_partes(nomes) for polo, nomes in out.items()}


def _linha_acervo(p: dict) -> dict:
    a, v = p["a"], p["v"]
    return {
        "numero": p["numero"],
        "numero_formatado": cnj.formatar(p["numero"]),
        "tribunal": a.get("tribunal"),
        "classe": a.get("classe"),
        "orgao": a.get("orgao"),
        "graus": a.get("graus") or [],
        "ajuizado_em": a.get("ajuizado_em"),
        "fase": a.get("fase"),
        "fase_desde": a.get("fase_desde"),
        "situacao": a.get("situacao"),
        "arquivado_em": a.get("arquivado_em"),
        "status_crm": a.get("status_crm"),
        "saude": a.get("saude"),
        "fonte": a.get("fonte"),
        "ultima_atividade": a.get("ultima_atividade"),
        "ultimo_movimento": a.get("ultimo_movimento"),
        "audiencia": a.get("audiencia"),
        "pendencias": a.get("pendencias") or [],
        "marcos": a.get("marcos") or [],
        # O que a tela mostra (autor × réu): destinatários do DJEN completados
        # pelo texto das intimações e limpos. O vínculo com cliente continua
        # usando só v["partes"] (destinatários), que é o dado mais seguro.
        "partes": p.get("partes_tela") or v.get("partes") or {},
        "parte_principal": v.get("parte_principal"),
        "area": p.get("area"),
        "vinculo_tipo": v.get("tipo"),
        "client_id": v.get("client_id"),
        "crm_process_id": v.get("crm_process_id"),
        "sugestoes": v.get("sugestoes") or [],
        "relacionados": v.get("relacionados") or [],
        "total_movimentos": a.get("total_movimentos") or 0,
        "total_intimacoes": a.get("total_intimacoes") or 0,
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
    }


def planejar(banco: Banco, crm_processos, somente: list[str] | None = None,
             arquivados_por_pessoa: set[str] | None = None) -> dict:
    procs = []
    for r in banco.processos("analise is not null"):
        if somente is not None and r["numero"] not in somente:
            continue
        p = dict(r)
        p["a"], p["v"] = json.loads(p["analise"]), json.loads(p["vinculo"])
        p["partes_tela"] = partes_para_tela(p["v"].get("partes") or {}, [c["texto"] for c in banco.comunicacoes(p["numero"])])
        from .ficha import area_provavel
        coms = banco.comunicacoes(p["numero"])
        p["area"] = area_provavel(p["numero"], json.loads(p.get("datajud") or "[]"), p["partes_tela"],
                                  [c["classe"] for c in coms] + [c["orgao"] for c in coms])
        procs.append(p)
    crm = {c.id: c for c in crm_processos}

    cadastrar = [p for p in procs if p["v"].get("tipo") == "nome_exato" and not p["v"].get("crm_process_id")]
    status, conferir = [], []
    for p in procs:
        c = crm.get(p["v"].get("crm_process_id"))
        novo = p["a"].get("status_crm")
        if not c or not novo or c.status == novo:
            continue
        mudanca = {"id": c.id, "numero": p["numero"], "de": c.status, "para": novo,
                   "desde": p["a"].get("status_desde")}
        # Arquivar no CRM pode ser decisão do escritório (segurança concedida,
        # nada mais a fazer) antes de o tribunal arquivar: esse o cérebro nunca
        # desfaz, vira "conferir". Arquivamento de ROBÔ (audit_log sem usuário)
        # é corrigido — era o cron de palavras-chave lendo "sob pena de
        # arquivamento" como arquivamento. Sem a lista, fica o comportamento
        # conservador de antes.
        por_pessoa = c.status == "arquivado" and (
            arquivados_por_pessoa is None or c.id in arquivados_por_pessoa)
        if c.status_manual or por_pessoa:
            conferir.append(mudanca)
        else:
            status.append(mudanca)
    return {"acervo": procs, "cadastrar": cadastrar, "status": status, "conferir": conferir}


# Troca de status avisa o cliente no portal (_trg_process_status_notify). Só é
# novidade se o fato que a justifica é recente; corrigir o estágio de um fato
# antigo (auditoria de 26/09/2026: 15 processos, conciliação de julho etc.)
# vai com o cabeçalho que o gatilho respeita e não avisa ninguém.
SEM_AVISO = {"X-Jurius-Sem-Aviso": "1"}
NOVIDADE_DIAS = 15


def status_e_novidade(desde: str | None, hoje: date | None = None) -> bool:
    if not desde:
        return False
    return ((hoje or date.today()) - date.fromisoformat(desde[:10])).days <= NOVIDADE_DIAS


def publicar(banco: Banco, crm_processos, url: str, chave: str, aplicar: bool = False,
             somente: list[str] | None = None, cadastrar_auto: bool = False,
             atualizar_status: bool = False, arquivados_por_pessoa: set[str] | None = None) -> dict:
    if atualizar_status and arquivados_por_pessoa is None:
        from .crm import CRM
        arquivados_por_pessoa = CRM(url, chave).arquivados_por_pessoa()
    plano = planejar(banco, crm_processos, somente, arquivados_por_pessoa)
    if not cadastrar_auto:
        plano["cadastrar"] = []
    if not atualizar_status:
        plano["status"] = []
    resumo = {"acervo": len(plano["acervo"]), "cadastrar": len(plano["cadastrar"]),
              "status": len(plano["status"]), "conferir": len(plano["conferir"]), "aplicado": aplicar}
    if not aplicar:
        resumo["amostra_status"] = plano["status"][:10]
        resumo["amostra_conferir"] = plano["conferir"][:10]
        return resumo

    http = httpx.Client(base_url=f"{url}/rest/v1", timeout=60,
                        headers={**cabecalhos_supabase(chave), "Content-Type": "application/json"})

    # 2 antes de 1: o processo novo precisa existir para o acervo apontar para ele.
    for p in plano["cadastrar"]:
        a, v = p["a"], p["v"]
        r = http.post("/processes", headers={"Prefer": "return=representation"}, json={
            "client_id": v["client_id"],
            "process_code": cnj.formatar(p["numero"]),
            "status": a.get("status_crm") or "andamento",
            "court": a.get("orgao"),
            "distributed_at": f"{a['ajuizado_em']}T00:00:00Z" if a.get("ajuizado_em") else None,
        })
        r.raise_for_status()
        novo_id = r.json()[0]["id"]
        v["crm_process_id"] = novo_id
        v["tipo"] = "cadastro"
        banco.gravar_analise(p["numero"], a, v)
        banco.evento(p["numero"], "cadastrado_no_crm", {"process_id": novo_id, "client_id": v["client_id"]})
        http.post("/acervo_eventos", json={
            "numero": p["numero"], "tipo": "vinculado_automaticamente", "client_id": v["client_id"],
            "process_id": novo_id,
            "mensagem": f"O processo {cnj.formatar(p['numero'])} já existia e foi vinculado a {v['client_nome']}.",
        }).raise_for_status()

    linhas = [_linha_acervo(p) for p in plano["acervo"]]
    for i in range(0, len(linhas), 200):
        http.post("/acervo_processos", params={"on_conflict": "numero"},
                  headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                  json=linhas[i:i + 200]).raise_for_status()

    for s in plano["status"]:
        # Condição dupla: não sobrescreve se alguém marcou manual entre a leitura
        # e agora, nem se o status mudou nesse meio-tempo.
        extra = {} if status_e_novidade(s.get("desde")) else SEM_AVISO
        r = http.patch("/processes", params={"id": f"eq.{s['id']}", "status_manual": "is.false",
                                             "status": f"eq.{s['de']}"},
                       headers={"Prefer": "return=representation", **extra}, json={"status": s["para"]})
        r.raise_for_status()
        if not r.json():
            continue
        # Todo status que o cérebro muda fica registrado: é o histórico para
        # conferir e o caminho para desfazer (o `de` está aqui).
        http.post("/acervo_eventos", json={
            "numero": s["numero"], "tipo": "status_corrigido", "process_id": s["id"],
            "mensagem": f"Status do processo {cnj.formatar(s['numero'])} passou de "
                        f"'{s['de']}' para '{s['para']}' pela análise do andamento.",
        }).raise_for_status()
    return resumo
