"""API HTTP, exposta pelo Cloudflare Tunnel. O CRM NÃO depende dela para abrir a
aba Processos (lê do Supabase). Ela serve para o que precisa ser na hora:

  GET  /saude                         últimas execuções e contagens
  POST /processos/{numero}/atualizar  reprocessa um processo agora
  POST /clientes/{client_id}/vincular cliente acabou de ser cadastrado: acha os
                                      processos dele no acervo e vincula
  GET  /processos/{numero}            análise completa (depuração)

Autenticação: o JWT do usuário logado no CRM (validado no próprio Supabase)
ou, de servidor para servidor, JURIUS_TOKEN_API.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import agendador, cnj, etapas, painel, publicar, vinculo
from .banco import Banco
from .config import carregar
from .datajud import ClienteDataJud
from .djen import ClienteDJEN

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

cfg = carregar()
banco = Banco(cfg.banco)
_trava = threading.Lock()  # uma etapa pesada por vez
_sessoes: dict[str, float] = {}


def autorizado(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "sem token")
    if cfg.token_api and token == cfg.token_api:
        return
    if _sessoes.get(token, 0) > time.time():
        return
    r = httpx.get(f"{cfg.supabase_url}/auth/v1/user", timeout=10,
                  headers={"apikey": cfg.supabase_key, "Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise HTTPException(401, "sessão inválida")
    _sessoes[token] = time.time() + 300


@asynccontextmanager
async def ciclo_de_vida(_app):
    agendador.iniciar(cfg, banco, _trava)
    yield


app = FastAPI(title="Jurius Processos", lifespan=ciclo_de_vida)

# O botão "Atualizar" da Linha do Tempo chama este serviço direto do CRM
# (jurius.com.br), com o login do usuário. Só essas origens podem.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
import os as _os  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _os.environ.get(
        "JURIUS_ORIGENS", "https://jurius.com.br,https://www.jurius.com.br,http://localhost:3000").split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def raiz():
    return painel.HTML


@app.get("/painel.json", include_in_schema=False)
def painel_json():
    return painel.dados(banco, _trava.locked())


@app.post("/ciclo", include_in_schema=False)
def rodar_ciclo(request: Request):
    """Botão "Rodar ciclo agora" do painel.

    Atrás do Cloudflare Tunnel TODO pedido chega de 127.0.0.1 (o cloudflared
    roda na mesma máquina), então "é local?" não basta: pedido que traz o
    cabeçalho CF-Connecting-IP veio da internet e precisa do token.
    """
    local = (request.client is not None and request.client.host in ("127.0.0.1", "::1")
             and "cf-connecting-ip" not in request.headers)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not local and not (cfg.token_api and token == cfg.token_api):
        raise HTTPException(403, "fora da máquina do serviço, rodar o ciclo exige o token")
    if _trava.locked():
        raise HTTPException(409, "já há um ciclo rodando")

    def _rodar():
        with _trava:
            try:
                agendador.ciclo(cfg, banco)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception("ciclo manual falhou")

    threading.Thread(target=_rodar, daemon=True, name="ciclo-manual").start()
    return {"iniciado": True}


@app.get("/saude")
def saude():
    execs = [dict(r) for r in banco.con.execute(
        "select etapa, inicio, fim, ok, resumo from execucoes order by id desc limit 10")]
    total = banco.con.execute("select count(*) from processos").fetchone()[0]
    return {"processos": total, "execucoes": execs, "ocupado": _trava.locked()}


@app.get("/processos/{numero}", dependencies=[Depends(autorizado)])
def processo(numero: str):
    n = cnj.limpar(numero)
    r = banco.processo(n) if n else None
    if not r:
        raise HTTPException(404, "fora do acervo")
    return {"numero": n, "analise": json.loads(r["analise"] or "null"), "vinculo": json.loads(r["vinculo"] or "null"),
            "datajud_status": r["datajud_status"], "intimacoes": len(banco.comunicacoes(n))}


@app.post("/processos/{numero}/atualizar", dependencies=[Depends(autorizado)])
def atualizar(numero: str):
    n = cnj.limpar(numero)
    if not n:
        raise HTTPException(400, "número CNJ inválido")
    # O ciclo de 2 h pode estar no meio (e leva minutos). Esperar por ele
    # estouraria o limite do navegador; melhor dizer "ocupado" logo e a tela
    # cai no caminho antigo (busca ao vivo, sem IA).
    if not _trava.acquire(timeout=10):
        raise HTTPException(409, "ciclo em andamento; tente de novo em alguns minutos")
    try:
        banco.garantir_processo(n, "manual")
        djen = ClienteDJEN()
        inicio = (date.today() - timedelta(days=730)).isoformat()
        for item in djen.do_processo(n, inicio, date.today().isoformat()):
            banco.gravar_comunicacao(item, n, "processo")
        for numero_, status, inst, erro in ClienteDataJud(cfg.datajud_key).lote([n]):
            banco.gravar_datajud(numero_, status, inst, erro)
        crm, _, clientes, procs = etapas.carregar_crm(cfg)
        etapas.analisar(banco, clientes, procs, somente=[n], financeiro=crm.financeiro(), agenda=crm.agenda(procs), prazos=crm.prazos(procs))
        publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key, aplicar=cfg.publicar, somente=[n],
                          cadastrar_auto=cfg.cadastrar_auto, atualizar_status=cfg.atualizar_status)
        # E grava no CRM o que a Linha do Tempo lê (intimações, DataJud, IA)
        # daquele processo: o "Atualizar" da tela reabre já com tudo novo.
        res = agendador.alimentar_crm(cfg, banco, procs, aplicar=cfg.publicar, somente=[n])
    finally:
        _trava.release()
    return {**processo(n), "alimentar": res}


@app.post("/clientes/{client_id}/vincular", dependencies=[Depends(autorizado)])
def vincular_cliente(client_id: str):
    """Chamado pelo CRM logo depois de cadastrar um cliente.

    Devolve os processos que já estavam no acervo em nome dele — e, com a
    publicação ligada, já os cadastra vinculados. O CRM mostra o aviso
    "este cliente já tinha N processos; vinculei".
    """
    with _trava:
        crm, _, clientes, procs = etapas.carregar_crm(cfg)
        cliente = next((c for c in clientes if c.id == client_id), None)
        if not cliente:
            raise HTTPException(404, "cliente não encontrado")
        partes = {}
        for r in banco.processos("vinculo is not null"):
            partes[r["numero"]] = json.loads(r["vinculo"]).get("partes", {})
        achados = vinculo.processos_do_nome(cliente.nome, partes)
        numeros = [n for n, _ in achados]
        if numeros:
            etapas.analisar(banco, clientes, procs, somente=numeros, financeiro=crm.financeiro(), agenda=crm.agenda(procs), prazos=crm.prazos(procs))
            publicar.publicar(banco, procs, cfg.supabase_url, cfg.supabase_key, aplicar=cfg.publicar, somente=numeros,
                              cadastrar_auto=cfg.cadastrar_auto, atualizar_status=cfg.atualizar_status)
    return {"cliente": cliente.nome, "processos": [
        {"numero": cnj.formatar(n), "polo": polo,
         "vinculo": json.loads(banco.processo(n)["vinculo"]).get("tipo")} for n, polo in achados]}
