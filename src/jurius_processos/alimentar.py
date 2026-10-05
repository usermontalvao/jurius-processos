"""Alimenta o CRM com o que o cérebro já sabe — no lugar das rotinas do Supabase.

Três entregas, cada uma com o seu interruptor (config.py):

  intimacoes  (cron 5, run-djen-sync)      → djen_comunicacoes
  datajud     (cron 16, datajud-sync)      → datajud_movimentos + processes.datajud_cache
  ia          (cron 11, analyze-intimations) → intimation_ai_analysis + avisos da equipe

Escreve nas MESMAS tabelas e no MESMO formato das rotinas antigas, para que
tudo o que vem depois (gatilhos do banco, Linha do Tempo, Intimações, portal do
cliente) continue igual. As três tabelas têm trava de duplicidade (hash;
process_code+codigo+data_hora; intimation_id), então o cérebro e a rotina
antiga podem rodar juntos durante a troca sem duplicar nada — e religar a
rotina antiga é só reativar o cron.

Dois cuidados que o histórico exige:
  - Intimação: só entra a da janela recente. As antigas que o Supabase nunca
    teve ficam no acervo; despejar 2 mil de uma vez não traria nada de novo.
  - Movimento do DataJud: o gatilho do banco avisa o cliente no portal a cada
    movimento inserido. Só vai COM aviso o que é mais novo que o último já
    gravado daquele processo; a lacuna (movimento antigo que faltava) e a
    primeira carga de um processo entram SEM aviso, pelo cabeçalho
    X-Jurius-Sem-Aviso que o gatilho respeita (migration
    20260926_portal_aviso_sem_historico). Assim o banco nunca fica com buraco
    e o cliente nunca recebe "sentença proferida" de 2024 hoje.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx

from . import cnj
from . import analise as analise_mod
from . import ficha as ficha_mod
from .banco import Banco
from .config import Config, cabecalhos_supabase
from .prazo import contar_prazo_da_intimacao

log = logging.getLogger(__name__)

JANELA_INTIMACOES_DIAS = 15
SEM_AVISO = {"X-Jurius-Sem-Aviso": "1"}  # lido por _portal_notify_on_datajud_movimento
IA_POR_CICLO = 40  # a rotina antiga fazia 10 a cada 30 min = 40 a cada 2 h


def _cliente(cfg: Config) -> httpx.Client:
    return httpx.Client(base_url=f"{cfg.supabase_url}/rest/v1", timeout=120,
                        headers={**cabecalhos_supabase(cfg.supabase_key), "Content-Type": "application/json"})


def _tudo(http: httpx.Client, tabela: str, select: str, filtros: dict | None = None) -> list[dict]:
    out, ini = [], 0
    while True:
        r = http.get(f"/{tabela}", params={"select": select, **(filtros or {})},
                     headers={"Range": f"{ini}-{ini + 999}"})
        r.raise_for_status()
        lote = r.json()
        out += lote
        if len(lote) < 1000:
            return out
        ini += 1000


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 1. INTIMAÇÕES (cron 5) ───────────────────────────────────────────────────
def linha_intimacao(item: dict, numero: str, process_id: str | None, client_id: str | None) -> dict:
    """O mesmo registro que o run-djen-sync gravava (saveCommunications)."""
    return {
        "djen_id": item.get("id"),
        "hash": item.get("hash"),
        "numero_comunicacao": item.get("numeroComunicacao"),
        "numero_processo": item.get("numero_processo") or numero,
        "numero_processo_mascara": item.get("numeroprocessocommascara") or cnj.formatar(numero),
        "codigo_classe": item.get("codigoClasse"),
        "nome_classe": item.get("nomeClasse"),
        "sigla_tribunal": item.get("siglaTribunal"),
        "nome_orgao": item.get("nomeOrgao"),
        "texto": item.get("texto"),
        "tipo_comunicacao": item.get("tipoComunicacao"),
        "tipo_documento": item.get("tipoDocumento"),
        "meio": item.get("meio"),
        "meio_completo": item.get("meiocompleto"),
        "link": item.get("link"),
        "data_disponibilizacao": item.get("data_disponibilizacao") or item.get("datadisponibilizacao"),
        "process_id": process_id,
        "client_id": client_id,
        "lida": False,
        "ativo": True,
    }


def intimacoes(banco: Banco, cfg: Config, crm_processos, aplicar: bool, hoje: date | None = None,
               somente: set[str] | None = None) -> dict:
    """somente: números (sem máscara) a considerar — o "Atualizar" de UM processo.
    A lista de processos do CRM continua inteira: é ela que dá o vínculo, e sem
    ela a intimação de outro processo entraria órfã e ficaria órfã (o hash já
    estaria lá quando o ciclo viesse gravá-la direito)."""
    hoje = hoje or date.today()
    inicio_execucao = _agora()
    corte = (hoje - timedelta(days=JANELA_INTIMACOES_DIAS)).isoformat()
    http = _cliente(cfg)
    ja = {r["hash"] for r in _tudo(http, "djen_comunicacoes", "hash",
                                   {"data_disponibilizacao": f"gte.{corte}"}) if r.get("hash")}
    por_numero = {p.numero: p for p in crm_processos if p.numero}

    novas, vinculadas = [], set()
    for r in banco.con.execute("select numero, bruto from comunicacoes where substr(data,1,10) >= ? order by data",
                               (corte,)):
        item = json.loads(r["bruto"])
        if not item.get("hash") or item["hash"] in ja:
            continue
        numero = r["numero"]
        if somente is not None and numero not in somente:
            continue
        proc = por_numero.get(numero)
        client_id = proc.client_id if proc else None
        if not client_id:
            # Sem processo cadastrado: só o vínculo CERTO do cérebro (nome idêntico
            # a um único cliente) — o mesmo cuidado do casarCliente da rotina antiga.
            v = banco.processo(numero)
            vv = json.loads(v["vinculo"]) if v and v["vinculo"] else {}
            if vv.get("tipo") in ("cadastro", "nome_exato"):
                client_id = vv.get("client_id")
        novas.append(linha_intimacao(item, numero, proc.id if proc else None, client_id))
        ja.add(item["hash"])
        if proc:
            vinculadas.add(proc.id)

    resumo = {"desde": corte, "novas": len(novas), "processos_tocados": len(vinculadas), "aplicado": aplicar}
    if not aplicar:
        return resumo
    for i in range(0, len(novas), 100):
        http.post("/djen_comunicacoes", params={"on_conflict": "hash"},
                  headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                  json=novas[i:i + 100]).raise_for_status()
    # As mesmas marcas que a rotina antiga punha no processo (nunca o status).
    marcas = {"djen_synced": True, "djen_last_sync": _agora(), "djen_has_data": True}
    for pid in vinculadas:
        http.patch("/processes", params={"id": f"eq.{pid}"}, json=marcas).raise_for_status()
    if somente is None:
        _registrar_sincronizacao(http, inicio_execucao, corte, hoje, len(novas))
    return resumo


def _registrar_sincronizacao(http: httpx.Client, inicio: str, corte: str, hoje: date, novas: int) -> None:
    """O card "Sincronização DJEN" da aba Processos lê djen_sync_history; com
    o cron 5 (run-djen-sync) desligado, quem registra é o servidor.

    Vem DEPOIS das intimações e nunca derruba a entrega: de 26 a 28/09/2026 a
    coluna id não tinha default, este POST (então feito antes) dava 23502 e
    nenhuma intimação entrou no CRM. O id vai daqui pelo mesmo motivo."""
    agora = _agora()
    try:
        http.post("/djen_sync_history", headers={"Prefer": "return=minimal"}, json={
            "id": str(uuid.uuid4()),
            "synced_at": agora, "run_started_at": inicio, "run_finished_at": agora,
            "items_found": novas, "items_saved": novas,
            "date_range_start": corte, "date_range_end": hoje.isoformat(),
            "source": "jurius-processos", "origin": "servidor", "trigger_type": "ciclo",
            "status": "success", "success": True,
            "message": f"{novas} intimação(ões) nova(s) desde {corte}",
        }).raise_for_status()
    except httpx.HTTPError as e:
        log.warning("histórico da sincronização não gravou (intimações já entregues): %s", e)


def revincular_orfas(cfg: Config, crm_processos, aplicar: bool, limite: int = 200) -> dict:
    """A 'auto-cura' do run-djen-sync: intimação sem processo nem cliente que
    hoje já casa com um processo cadastrado ganha o vínculo."""
    http = _cliente(cfg)
    por_numero = {p.numero: p for p in crm_processos if p.numero}
    orfas = http.get("/djen_comunicacoes", params={
        "select": "id,numero_processo", "process_id": "is.null", "client_id": "is.null",
        "order": "data_disponibilizacao.desc", "limit": str(limite)}).json()
    ligadas = 0
    for o in orfas:
        proc = por_numero.get(cnj.limpar(o.get("numero_processo")))
        if not proc:
            continue
        ligadas += 1
        if aplicar:
            http.patch("/djen_comunicacoes", params={"id": f"eq.{o['id']}"},
                       json={"process_id": proc.id, "client_id": proc.client_id, "updated_at": _agora()}).raise_for_status()
    return {"examinadas": len(orfas), "revinculadas": ligadas, "aplicado": aplicar}


# ── 2. DATAJUD (cron 16) ────────────────────────────────────────────────────
def categorizar(codigo: int, nome: str) -> str:
    """Cópia fiel de categorizarMovimento (datajud-sync) — o gatilho do portal lê a categoria."""
    n = (nome or "").lower()
    if codigo in (55, 196, 848, 849, 861, 862) or "sentença" in n or "sentenca" in n:
        return "sentenca"
    if codigo in (11, 22, 471, 472) or "decisão" in n or "decisao" in n or "acórdão" in n:
        return "decisao"
    if codigo in (132, 7, 9) or "despacho" in n:
        return "despacho"
    if codigo in (971, 972, 974) or "audiência" in n or "audiencia" in n or "sessão" in n:
        return "audiencia"
    if codigo in (65, 159, 1259) or any(x in n for x in ("citação", "citacao", "intimação", "intimacao")):
        return "citacao"
    if codigo in (197, 237, 238, 239, 240) or any(x in n for x in ("recurso", "apelação", "agravo")):
        return "recurso"
    if codigo in (246, 248) or "arquiv" in n or "extinção" in n or "extincao" in n:
        return "arquivamento"
    return "outro"


def estagio(categoria: str, nome: str) -> str | None:
    """Cópia fiel de detectarEstagioMovimento (datajud-sync)."""
    n = (nome or "").lower()
    if any(x in n for x in ("cumprimento", "execução", "execucao", "liquidação", "liquidacao", "penhora",
                             "alvará", "alvara", "precatório", "precatorio", "rpv",
                             "pagamento do débito", "pagamento de debito", "evolução da classe", "evolucao da classe")):
        return "cumprimento"
    if categoria == "audiencia":
        if any(x in n for x in ("conciliação", "conciliacao", "mediação")):
            return "conciliacao"
        if any(x in n for x in ("instrução", "instrucao", "julgamento")):
            return "instrucao"
        return "andamento"
    return {"sentenca": "sentenca", "recurso": "recurso", "arquivamento": "arquivado", "citacao": "citacao",
            "decisao": "andamento", "despacho": "andamento"}.get(categoria)


def linhas_movimento(proc, instancias: list[dict]) -> list[dict]:
    out = []
    for inst in instancias:
        tribunal = (inst.get("tribunal") or "").upper() or None
        for m in inst.get("movimentos") or []:
            nome, quando = m.get("nome") or "", m.get("dataHora")
            if not nome or not quando:
                continue
            cat = categorizar(m.get("codigo") or 0, nome)
            out.append({
                "process_id": proc.id, "process_code": proc.codigo, "tribunal": tribunal, "grau": inst.get("grau"),
                "codigo": m.get("codigo"), "nome": nome, "data_hora": quando,
                "orgao_julgador": (m.get("orgaoJulgador") or {}).get("nome") or (m.get("orgaoJulgador") or {}).get("nomeOrgao"),
                "complementos": m.get("complementosTabelados") or None,
                "categoria": cat, "process_stage": estagio(cat, nome),
            })
    return out


def cache_da_linha_do_tempo(instancias: list[dict]) -> dict:
    """processes.datajud_cache no formato que a Linha do Tempo já lê
    ({processo: {...movimentos...}, tribunal}), com as instâncias juntas."""
    recente = max(instancias, key=lambda i: i.get("dataHoraUltimaAtualizacao") or "")
    movs = sorted((m for i in instancias for m in i.get("movimentos") or []),
                  key=lambda m: m.get("dataHora") or "")
    return {"processo": {**{k: v for k, v in recente.items() if k != "movimentos"}, "movimentos": movs},
            "tribunal": recente.get("tribunal")}


def _ts(s: str) -> datetime:
    """Data do DataJud ou do banco. Sem fuso (o DataJud às vezes manda assim)
    vale como UTC — é como o Postgres a grava, e comparar com ingênua quebraria."""
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def separar_movimentos(proc, instancias: list[dict], existentes: set, ultimo: datetime | None) -> tuple[list, list]:
    """(com_aviso, sem_aviso) do que ainda falta no CRM para este processo.

    Com aviso só o que é mais novo que o último movimento já gravado. O resto
    que falta — lacuna deixada pela rotina, ou o processo inteiro na primeira
    carga (ultimo=None) — entra calado: é histórico, não novidade."""
    com, sem = [], []
    for m in linhas_movimento(proc, instancias):
        t = _ts(m["data_hora"])
        if (proc.codigo, m["codigo"], t) in existentes:
            continue
        existentes.add((proc.codigo, m["codigo"], t))  # a mesma linha em duas instâncias
        (com if ultimo is not None and t > ultimo else sem).append(m)
    return com, sem


def datajud(banco: Banco, cfg: Config, crm_processos, aplicar: bool, somente: set[str] | None = None) -> dict:
    http = _cliente(cfg)
    ultimo: dict[str, datetime] = {}
    existentes: set = set()
    codigos = None if somente is None else {p.codigo for p in crm_processos if p.numero in somente}
    for r in _tudo(http, "datajud_movimentos", "process_code,codigo,data_hora"):
        if not r.get("process_code") or not r.get("data_hora"):
            continue
        if codigos is not None and r["process_code"] not in codigos:
            continue
        t = _ts(r["data_hora"])
        existentes.add((r["process_code"], r.get("codigo"), t))
        if r["process_code"] not in ultimo or t > ultimo[r["process_code"]]:
            ultimo[r["process_code"]] = t

    novos, lacunas, caches = [], [], []
    for proc in crm_processos:
        if not proc.numero or not proc.codigo or (somente is not None and proc.numero not in somente):
            continue
        r = banco.processo(proc.numero)
        if not r or r["datajud_status"] != "ok" or not r["datajud"]:
            continue
        inst = json.loads(r["datajud"])
        com, sem = separar_movimentos(proc, inst, existentes, ultimo.get(proc.codigo))
        novos += com
        lacunas += sem
        caches.append((proc.id, cache_da_linha_do_tempo(inst)))

    resumo = {"movimentos_novos": len(novos), "lacunas_sem_aviso": len(lacunas), "caches": len(caches),
              "aplicado": aplicar}
    if not aplicar:
        return resumo
    # Primeiro o histórico, calado; depois o novo, com aviso. Na ordem inversa
    # o gatilho de status veria o novo antes do antigo — dá no mesmo, mas assim
    # o banco fica na ordem em que as coisas aconteceram.
    for linhas, extra in ((lacunas, SEM_AVISO), (novos, {})):
        for i in range(0, len(linhas), 200):
            http.post("/datajud_movimentos", params={"on_conflict": "process_code,codigo,data_hora"},
                      headers={"Prefer": "resolution=ignore-duplicates,return=minimal", **extra},
                      json=linhas[i:i + 200]).raise_for_status()
    # A cópia que a Linha do Tempo abre: fresca a cada ciclo, então a tela
    # nunca mais precisa buscar o DataJud ao vivo (18–53 s) ao ser aberta.
    # Só reenvia a cópia que MUDOU: regravar ~14 kB de jsonb de cada processo a
    # cada 2 h, quase sempre igual, era a maior fonte de escrita do banco
    # (aviso de Disk IO do Supabase, 05/10/2026). Nas iguais, só o carimbo
    # anda — num PATCH em lote —, que é o que a tela mostra como "cópia de".
    carimbo = _agora()
    iguais = []
    for pid, cache in caches:
        digital = _digital(cache)
        if _COPIA_GRAVADA.get(pid) == digital:
            iguais.append(pid)
            continue
        http.patch("/processes", params={"id": f"eq.{pid}"},
                   json={"datajud_cache": cache, "datajud_synced_at": carimbo}).raise_for_status()
        _COPIA_GRAVADA[pid] = digital
    for i in range(0, len(iguais), 100):
        http.patch("/processes", params={"id": f"in.({','.join(iguais[i:i + 100])})"},
                   json={"datajud_synced_at": carimbo}).raise_for_status()
    resumo["caches_iguais"] = len(iguais)
    return resumo


# Impressão digital da última cópia gravada de cada processo. Vive enquanto o
# serviço vive: depois de reiniciar, o primeiro ciclo regrava tudo uma vez.
_COPIA_GRAVADA: dict[str, str] = {}


def _digital(cache: dict) -> str:
    return hashlib.sha256(json.dumps(cache, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ── 3. IA (cron 11) ─────────────────────────────────────────────────────────
# O prompt, o contexto e as salvaguardas moram em analise.py (puro, testável).
PROMPT_SISTEMA = analise_mod.PROMPT_SISTEMA

# Título que é rótulo, não providência — não serve de título de prazo.
_TITULOS_GENERICOS = {"prazo", "intimação", "intimacao", "manifestação", "manifestacao", "ciência", "ciencia", "providência"}


def titulo_da_providencia(a: dict | None) -> str | None:
    """O "action" da IA limpo para virar título de prazo: frase curta, com a
    primeira letra maiúscula e sem ponto final. Vazio ou genérico → None."""
    bruto = ((a or {}).get("deadline") or {}).get("action") if isinstance((a or {}).get("deadline"), dict) else None
    if not isinstance(bruto, str):
        return None
    t = " ".join(bruto.split()).strip().rstrip(".;:")
    if len(t) < 6 or t.lower() in _TITULOS_GENERICOS:
        return None
    if len(t) > 60:  # título de prazo é curto; a IA às vezes esquece o limite
        t = t[:60].rsplit(" ", 1)[0]
    return t[0].upper() + t[1:]

ROTULO_URGENCIA = {"critica": "🚨 CRÍTICA", "alta": "⚠️ Urgente", "media": "📋 Atenção", "baixa": "📄 Nova"}


def _json_da_resposta(texto: str) -> dict | None:
    ini, fim = texto.find("{"), texto.rfind("}")
    if ini < 0 or fim <= ini:
        return None
    try:
        return json.loads(texto[ini:fim + 1])
    except ValueError:
        return None


def corpo_deepseek(cfg: Config, mensagens: list[dict], max_tokens: int, temperatura: float, **extra) -> dict:
    """O corpo de toda chamada à DeepSeek. `thinking` desligado SEMPRE: a
    deepseek-flash pensa por padrão e o pensamento gasta o max_tokens — com
    500, a resposta vinha vazia e a intimação ficava sem resumo, calada
    (3 de 7 em 28/09/2026; títulos de prazo, 1 de 20). É o mesmo que o CRM
    faz em supabase/functions/_shared/ai-text-ladder.ts."""
    return {"model": cfg.deepseek_modelo, "temperature": temperatura, "max_tokens": max_tokens,
            "thinking": {"type": "disabled"}, "messages": mensagens, **extra}


class RespostaVazia(Exception):
    """A IA respondeu sem conteúdo aproveitável (vazio, cortado ou sem JSON)."""


LIMITE_TEXTO_IA = 12000


def trecho_para_ia(texto: str, limite: int = LIMITE_TEXTO_IA) -> str:
    """O texto que a IA lê. Até 28/09/2026 eram os 3000 primeiros caracteres, e a
    decisão de perícia do Vicente (1059802-92) tem a ordem e os 15 dias depois da
    doutrina do começo: virou "sem prazo". Agora vai o texto todo; passando do
    limite, cortam-se as pontas de modo a manter o começo e o FIM, onde fica o
    dispositivo."""
    texto = texto or ""
    if len(texto) <= limite:
        return texto
    cabeca = limite // 4
    return f"{texto[:cabeca]}\n[...]\n{texto[-(limite - cabeca):]}"


def _mensagem(texto: str, contexto: str | None) -> str:
    if not contexto:
        return f"Analise esta intimação:\n\n{trecho_para_ia(texto)}"
    return f"{contexto}\n\nINTIMAÇÃO NOVA (texto integral)\n\n{trecho_para_ia(texto)}"


def analisar_com_ia(cfg: Config, texto: str, cliente: httpx.Client | None = None, contexto: str | None = None) -> dict:
    http = cliente or httpx.Client(timeout=90)
    r = http.post("https://api.deepseek.com/chat/completions",
                  headers={"Authorization": f"Bearer {cfg.deepseek_key}", "Content-Type": "application/json"},
                  json=corpo_deepseek(cfg, [{"role": "system", "content": PROMPT_SISTEMA},
                                            {"role": "user", "content": _mensagem(texto, contexto)}],
                                      max_tokens=1400, temperatura=0.1, response_format={"type": "json_object"}))
    r.raise_for_status()
    escolha = r.json()["choices"][0]
    conteudo = (escolha.get("message") or {}).get("content") or ""
    a = _json_da_resposta(conteudo)
    if not a:
        raise RespostaVazia(f"finish_reason={escolha.get('finish_reason')}, {len(conteudo)} caracteres")
    return a


_COLUNAS_IT = ("id,texto,numero_processo,numero_processo_mascara,sigla_tribunal,nome_orgao,nome_classe,"
               "tipo_documento,tipo_comunicacao,data_disponibilizacao,process_id,client_id")


def _contextos(http: httpx.Client, its: list[dict]) -> dict[str, str]:
    """O contexto de cada intimação: cliente, polos e resumo do processo, e as
    intimações anteriores do MESMO processo com o resumo que já têm."""
    if not its:
        return {}
    em = lambda xs: f"in.({','.join(sorted(xs))})"  # noqa: E731
    cids = {x["client_id"] for x in its if x.get("client_id")}
    pids = {x["process_id"] for x in its if x.get("process_id")}
    clientes = {c["id"]: c.get("full_name") for c in http.get("/clients", params={"select": "id,full_name", "id": em(cids)}).json()} if cids else {}
    fichas = {f["process_id"]: f for f in http.get("/process_insights", params={
        "select": "process_id,polo_ativo,polo_passivo,resumo", "process_id": em(pids)}).json()} if pids else {}
    out = {}
    for it in its:
        numero = it.get("numero_processo")
        anteriores = http.get("/djen_comunicacoes", params={
            "select": "id,data_disponibilizacao,tipo_documento,tipo_comunicacao,texto", "numero_processo": f"eq.{numero}",
            "data_disponibilizacao": f"lte.{it.get('data_disponibilizacao')}", "id": f"neq.{it['id']}",
            "order": "data_disponibilizacao.desc", "limit": "8"}).json() if numero else []
        resumos = {a["intimation_id"]: a.get("summary") for a in http.get("/intimation_ai_analysis", params={
            "select": "intimation_id,summary", "intimation_id": em({x["id"] for x in anteriores})}).json()} if anteriores else {}
        historico = [{"data": x.get("data_disponibilizacao"), "tipo": x.get("tipo_documento") or x.get("tipo_comunicacao"),
                      "resumo": resumos.get(x["id"]) or " ".join((x.get("texto") or "").split())[:300]} for x in anteriores]
        f = fichas.get(it.get("process_id")) or {}
        out[it["id"]] = analise_mod.contexto(it, clientes.get(it.get("client_id")), f, f.get("resumo"), historico)
    return out


def _linha_da_analise(cfg: Config, it: dict, a: dict, feriados: set[str]) -> dict:
    """O registro de intimation_ai_analysis a partir da análise JÁ salvaguardada."""
    prazo = a.get("deadline") or {}
    dias = prazo.get("days") if isinstance(prazo.get("days"), (int, float)) else None
    agora = _agora()
    contagem = contar_prazo_da_intimacao(it.get("data_disponibilizacao") or agora, dias, feriados)
    sessao = (a.get("compromisso") or {}).get("data") if (a.get("compromisso") or {}).get("tipo") == "julgamento" else None

    def vencimento(o: dict) -> str | None:
        if o.get("days"):
            return (contar_prazo_da_intimacao(it.get("data_disponibilizacao") or agora, o.get("days"), feriados) or {}).get("vencimento")
        # Sustentação oral/memoriais/destaque: "até 48 horas antes da sessão".
        if sessao and re.search(r"sustenta|memoria|destaque", analise_mod._norm(o.get("action"))):
            return (date.fromisoformat(sessao) - timedelta(days=2)).isoformat()
        return None

    opcoes = [{"acao": titulo_da_providencia({"deadline": o}) or o["action"], "dias": o.get("days"),
               "fundamento": o.get("fundamento"), "vencimento": vencimento(o)}
              for o in a.get("alternativas") or [] if o.get("action")]
    return {"intimation_id": it["id"], "summary": a.get("summary"), "urgency": a.get("urgency") or "media",
            "deadline_days": dias,
            "deadline_description": titulo_da_providencia(a),
            "deadline_due_date": f"{contagem['vencimento']}T00:00:00.000Z" if contagem else None,
            "prazo_fundamento": prazo.get("fundamento"),
            "prazo_opcoes": opcoes,
            "compromisso": a.get("compromisso"),
            "resultado": a.get("resultado"),
            "tutela": a.get("tutela"),
            "document_type": a.get("tipo_ato"),
            "rito": a.get("rito"),
            "analise_versao": analise_mod.VERSAO,
            "analyzed_at": agora, "model_used": f"deepseek/{cfg.deepseek_modelo} (cérebro v{analise_mod.VERSAO})",
            "updated_at": agora}


def _analisar(cfg: Config, it: dict, ctx: str | None, ia_http: httpx.Client) -> dict:
    texto = it.get("texto") or ""
    return analise_mod.salvaguardar(analisar_com_ia(cfg, texto, ia_http, contexto=ctx), it, texto)


def ia(cfg: Config, aplicar: bool, limite: int = IA_POR_CICLO, process_ids: list[str] | None = None) -> dict:
    """process_ids: só as intimações destes processos — o "Atualizar" de UM
    processo não pode esperar a fila do escritório inteiro (40 × IA > 2 min)."""
    if not cfg.deepseek_key:
        return {"pulado": "sem DEEPSEEK_API_KEY"}
    http = _cliente(cfg)
    filtro = {"process_id": f"in.({','.join(process_ids)})"} if process_ids else {}
    if process_ids is not None and not process_ids:
        return {"pendentes": 0, "analisadas": 0, "avisos": 0, "aplicado": aplicar}
    recentes = http.get("/djen_comunicacoes", params={
        "select": _COLUNAS_IT, "order": "data_disponibilizacao.desc", "limit": "150", **filtro}).json()
    ids = [x["id"] for x in recentes]
    feitas = {a["intimation_id"] for a in http.get("/intimation_ai_analysis", params={
        "select": "intimation_id", "intimation_id": f"in.({','.join(ids)})"}).json()} if ids else set()
    pendentes = [x for x in recentes if x["id"] not in feitas][:limite]
    resumo = {"pendentes": len(pendentes), "analisadas": 0, "avisos": 0, "aplicado": aplicar}
    if not aplicar or not pendentes:
        return resumo

    feriados = {str(h["date"])[:10] for h in _tudo(http, "holidays", "date")}
    usuarios = [u["user_id"] for u in _tudo(http, "profiles", "user_id", {"is_active": "eq.true"}) if u.get("user_id")]
    contextos = _contextos_seguros(http, pendentes)
    ia_http = httpx.Client(timeout=90)
    for it in pendentes:
        try:
            a = _analisar(cfg, it, contextos.get(it["id"]), ia_http)
        except Exception as e:  # noqa: BLE001 — uma intimação não derruba as outras
            log.warning("IA falhou em %s: %s", it["id"][:8], e)
            resumo["falhas"] = resumo.get("falhas", 0) + 1
            resumo["ultima_falha"] = f"{type(e).__name__}: {e}"[:200]
            continue
        linha = {**_linha_da_analise(cfg, it, a, feriados), "created_at": _agora()}
        r = http.post("/intimation_ai_analysis", params={"on_conflict": "intimation_id"},
                      headers={"Prefer": "resolution=ignore-duplicates,return=representation"}, json=linha)
        r.raise_for_status()
        if not r.json():
            continue  # a rotina antiga chegou antes: nada de aviso em dobro
        resumo["analisadas"] += 1
        resumo["avisos"] += _avisar_equipe(http, usuarios, it, a)
    return resumo


def _contextos_seguros(http: httpx.Client, its: list[dict]) -> dict[str, str]:
    """Sem contexto a análise ainda sai (pior, mas sai): falha aqui não para a fila."""
    try:
        return _contextos(http, its)
    except Exception as e:  # noqa: BLE001
        log.warning("contexto das intimações falhou: %s", e)
        return {}


REANALISE_JANELA_DIAS = 30


def reanalisar(cfg: Config, aplicar: bool, limite: int = 15, hoje: date | None = None) -> dict:
    """As intimações dos últimos 30 dias analisadas por um prompt antigo são
    refeitas com o atual (sem novo aviso à equipe). É o que faz a perícia do
    Vicente virar compromisso e a sentença do Pedro virar recurso inominado
    sem ninguém clicar em nada depois do deploy."""
    if not cfg.deepseek_key:
        return {"pulado": "sem DEEPSEEK_API_KEY"}
    http = _cliente(cfg)
    desde = ((hoje or date.today()) - timedelta(days=REANALISE_JANELA_DIAS)).isoformat()
    recentes = http.get("/djen_comunicacoes", params={
        "select": _COLUNAS_IT, "data_disponibilizacao": f"gte.{desde}",
        "order": "data_disponibilizacao.desc", "limit": "300"}).json()
    ids = [x["id"] for x in recentes]
    velhas = {a["intimation_id"] for a in http.get("/intimation_ai_analysis", params={
        "select": "intimation_id", "intimation_id": f"in.({','.join(ids)})",
        "or": f"(analise_versao.is.null,analise_versao.lt.{analise_mod.VERSAO})"}).json()} if ids else set()
    alvo = [x for x in recentes if x["id"] in velhas][:limite]
    resumo = {"desatualizadas": len(velhas), "refeitas": 0, "aplicado": aplicar}
    if not aplicar or not alvo:
        return resumo
    feriados = {str(h["date"])[:10] for h in _tudo(http, "holidays", "date")}
    contextos = _contextos_seguros(http, alvo)
    ia_http = httpx.Client(timeout=90)
    for it in alvo:
        try:
            a = _analisar(cfg, it, contextos.get(it["id"]), ia_http)
        except Exception as e:  # noqa: BLE001
            log.warning("reanálise falhou em %s: %s", it["id"][:8], e)
            resumo["falhas"] = resumo.get("falhas", 0) + 1
            resumo["ultima_falha"] = f"{type(e).__name__}: {e}"[:200]
            continue
        linha = _linha_da_analise(cfg, it, a, feriados)
        linha.pop("intimation_id")
        http.patch("/intimation_ai_analysis", params={"intimation_id": f"eq.{it['id']}"},
                   json=linha).raise_for_status()
        resumo["refeitas"] += 1
    return resumo


def completar_titulos(cfg: Config, aplicar: bool, limite: int = 20) -> dict:
    """Análises gravadas antes do "action" no prompt ficaram sem título de
    prazo (deadline_description nulo) — o CRM caía em "Prazo Intimação -
    Processo X". A cada ciclo, as mais recentes com prazo ganham o título.
    Só PREENCHE o vazio: título já gravado (ou editado) nunca é trocado."""
    if not cfg.deepseek_key:
        return {"pulado": "sem DEEPSEEK_API_KEY"}
    http = _cliente(cfg)
    faltando = http.get("/intimation_ai_analysis", params={
        "select": "intimation_id", "deadline_description": "is.null", "deadline_days": "gt.0",
        "order": "created_at.desc", "limit": str(limite)}).json()
    resumo = {"sem_titulo": len(faltando), "preenchidos": 0, "aplicado": aplicar}
    if not aplicar or not faltando:
        return resumo
    textos = {x["id"]: x.get("texto") for x in http.get("/djen_comunicacoes", params={
        "select": "id,texto", "id": f"in.({','.join(f['intimation_id'] for f in faltando)})"}).json()}
    ia_http = httpx.Client(timeout=90)
    for f in faltando:
        texto = textos.get(f["intimation_id"])
        if not texto:
            continue
        try:
            titulo = titulo_da_providencia(analisar_com_ia(cfg, texto, ia_http))
        except Exception as e:  # noqa: BLE001
            log.warning("título falhou em %s: %s", f["intimation_id"][:8], e)
            resumo["falhas"] = resumo.get("falhas", 0) + 1
            resumo["ultima_falha"] = f"{type(e).__name__}: {e}"[:200]
            continue
        if not titulo:
            continue
        http.patch("/intimation_ai_analysis",
                   params={"intimation_id": f"eq.{f['intimation_id']}", "deadline_description": "is.null"},
                   json={"deadline_description": titulo, "updated_at": _agora()}).raise_for_status()
        resumo["preenchidos"] += 1
    return resumo


def _avisar_equipe(http: httpx.Client, usuarios: list[str], it: dict, a: dict) -> int:
    """O mesmo aviso "intimation_new" da analyze-intimations, uma vez por pessoa."""
    ja = {n["user_id"] for n in http.get("/user_notifications", params={
        "select": "user_id", "type": "eq.intimation_new", "intimation_id": f"eq.{it['id']}"}).json()}
    partes = http.get("/djen_destinatarios", params={"select": "nome,polo", "comunicacao_id": f"eq.{it['id']}"}).json()
    nomes = ", ".join(p["nome"] for p in partes[:2])
    if len(partes) > 2:
        nomes += f" e +{len(partes) - 2}"
    dias = (a.get("deadline") or {}).get("days")
    processo = it.get("numero_processo_mascara") or it.get("numero_processo")
    msg = [f"Prazo: {dias} dia(s)" if dias else "Prazo não identificado", f"Processo: {processo}"]
    if nomes:
        msg.append(f"Partes: {nomes}")
    if it.get("sigla_tribunal"):
        msg.append(f"Tribunal: {it['sigla_tribunal']}")
    titulo = f"{ROTULO_URGENCIA.get(a.get('urgency'), '📄 Nova')}: {(a.get('summary') or 'Nova Intimação')[:50]}"
    linhas = [{"user_id": u, "title": titulo, "message": " • ".join(msg), "type": "intimation_new",
               "intimation_id": it["id"], "read": False, "created_at": _agora(),
               "metadata": {"urgency": a.get("urgency"), "deadline_days": dias, "tribunal": it.get("sigla_tribunal"),
                            "summary": a.get("summary"), "partes": partes, "processo": processo}}
              for u in usuarios if u not in ja]
    if linhas:
        http.post("/user_notifications", headers={"Prefer": "return=minimal"}, json=linhas).raise_for_status()
    return len(linhas)


# ── 4. FICHA E RESUMO DO PROCESSO (process_insights) ────────────────────────
RESUMOS_POR_CICLO = 60  # 1º ciclo: ~200 processos em 4 ciclos; depois só o que mudou


class ResumoIncompleto(Exception):
    """A IA parou antes do fim (limite de tokens) ou não escreveu nada."""


def gerar_resumo(cfg: Config, texto: str, cliente: httpx.Client | None = None) -> str:
    # 26/09/2026: com max_tokens 700, 55 de 60 respostas vieram vazias e 2
    # cortadas no meio — o modelo gasta parte do limite pensando antes de
    # escrever. Resposta que não terminou nunca é gravada.
    http = cliente or httpx.Client(timeout=180)
    r = http.post("https://api.deepseek.com/chat/completions",
                  headers={"Authorization": f"Bearer {cfg.deepseek_key}", "Content-Type": "application/json"},
                  json=corpo_deepseek(cfg, [{"role": "system", "content": ficha_mod.PROMPT_SISTEMA},
                                            {"role": "user", "content": texto}],
                                      max_tokens=4000, temperatura=0.2))
    r.raise_for_status()
    escolha = r.json()["choices"][0]
    conteudo = ((escolha.get("message") or {}).get("content") or "").strip()
    if escolha.get("finish_reason") != "stop" or not conteudo:
        raise ResumoIncompleto(f"finish_reason={escolha.get('finish_reason')}, {len(conteudo)} caracteres")
    return conteudo


def _prazos_que_contam(prazos: list[dict], hoje: date) -> list[dict]:
    """Os em aberto e os resolvidos nos últimos 30 dias: prazo novo ou cumprido muda o resumo."""
    corte = (hoje - timedelta(days=30)).isoformat()
    return [p for p in prazos if p.get("status") == "pendente" or (p.get("due_date") or "")[:10] >= corte]


def ficha(banco: Banco, cfg: Config, crm_processos, aplicar: bool, somente: set[str] | None = None,
          limite: int = RESUMOS_POR_CICLO, hoje: date | None = None, crm=None) -> dict:
    """Escreve a ficha de cada processo do CRM e refaz o resumo só onde a
    impressão digital das entradas mudou (ficha.assinatura)."""
    from .crm import CRM
    hoje = hoje or date.today()
    crm = crm or CRM(cfg.supabase_url, cfg.supabase_key)
    dados, agenda, financeiro = crm.para_a_ficha(), crm.agenda(crm_processos), crm.financeiro()
    nomes = {c.id: c.nome for c in crm.clientes()}
    http = _cliente(cfg)
    guardadas = {r["process_id"]: r.get("resumo_assinatura")
                 for r in _tudo(http, "process_insights", "process_id,resumo_assinatura")}

    agora = _agora()
    linhas, a_resumir = [], []
    for proc in crm_processos:
        if not proc.numero or (somente is not None and proc.numero not in somente):
            continue
        r = banco.processo(proc.numero)
        if not r or not r["analise"]:
            continue
        analise, vinc = json.loads(r["analise"]), json.loads(r["vinculo"] or "{}")
        instancias = json.loads(r["datajud"] or "[]")
        orgaos = [(i.get("orgaoJulgador") or {}).get("nome") for i in instancias]
        comunicacoes = banco.comunicacoes(proc.numero)
        orgaos += [c["orgao"] for c in comunicacoes if c["orgao"]]
        textos = [c["texto"] for c in comunicacoes] + [i.get("texto") for i in (dados.get(proc.id) or {}).get("intimacoes") or []]
        f = ficha_mod.montar(analise, vinc, hoje, orgaos, textos)
        partes_f = {"A": (f.get("polo_ativo") or "").split(", ") if f.get("polo_ativo") else [],
                    "P": (f.get("polo_passivo") or "").split(", ") if f.get("polo_passivo") else []}
        f["area"] = ficha_mod.area_provavel(proc.numero, instancias, partes_f,
                                            [c["classe"] for c in comunicacoes] + orgaos)
        d = dados.get(proc.id, {})
        movs = [m for i in instancias for m in i.get("movimentos") or []]
        e = ficha_mod.entradas({"codigo": proc.codigo, "area": d.get("area"), "cliente": nomes.get(proc.client_id)},
                               analise, f, d.get("intimacoes") or [], movs,
                               _prazos_que_contam(d.get("prazos") or [], hoje), agenda.get(proc.id) or [],
                               financeiro.get(proc.id) or [], d.get("notas"), hoje)
        linha = {"process_id": proc.id, **f, "atualizado_em": agora}
        linhas.append(linha)
        ass = ficha_mod.assinatura(e)
        if ass != guardadas.get(proc.id):
            ultima = max([i["data"] for i in e["intimacoes"]] + [m["data"] for m in e["movimentos"]] + [""])
            a_resumir.append((ultima, proc.id, e, ass))

    # Mais recente primeiro: é o que alguém vai abrir hoje.
    a_resumir.sort(key=lambda x: x[0], reverse=True)
    resumo = {"fichas": len(linhas), "resumos_desatualizados": len(a_resumir), "resumos_feitos": 0,
              "aplicado": aplicar}
    if not aplicar:
        return resumo
    for i in range(0, len(linhas), 100):
        http.post("/process_insights", params={"on_conflict": "process_id"},
                  headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                  json=linhas[i:i + 100]).raise_for_status()
    # Vara vazia no processo: o servidor preenche com o órgão (o datajud-sync,
    # cron 16, fazia isso). Só VAZIA — a digitada pelo escritório vale. Duas
    # chamadas em vez de "or": filtro "or" em UPDATE do PostgREST dá 42703.
    varas = 0
    for linha in linhas:
        if linha.get("orgao") and not ((dados.get(linha["process_id"]) or {}).get("vara") or "").strip():
            for filtro in ({"court": "is.null"}, {"court": "eq."}):
                http.patch("/processes", params={"id": f"eq.{linha['process_id']}", **filtro},
                           json={"court": linha["orgao"]}).raise_for_status()
            varas += 1
    resumo["varas_preenchidas"] = varas
    # Área: só troca o "cível" (o chute antigo pelo número CNJ, e o padrão da
    # tela) quando o servidor apurou outra. Área escolhida pelo escritório fica.
    areas = 0
    for linha in linhas:
        atual = (dados.get(linha["process_id"]) or {}).get("area")
        if linha.get("area") and linha["area"] != "civel" and atual in (None, "", "civel"):
            for filtro in ({"practice_area": "eq.civel"}, {"practice_area": "is.null"}):
                http.patch("/processes", params={"id": f"eq.{linha['process_id']}", **filtro},
                           json={"practice_area": linha["area"]}).raise_for_status()
            areas += 1
    resumo["areas_corrigidas"] = areas
    if not cfg.deepseek_key:
        resumo["resumos_pulados"] = "sem DEEPSEEK_API_KEY"
        return resumo
    ia_http = httpx.Client(timeout=120)
    for _, pid, e, ass in a_resumir[:limite]:
        try:
            texto = gerar_resumo(cfg, ficha_mod.prompt(e, hoje), ia_http)
        except Exception as erro:  # noqa: BLE001 — um processo não derruba os outros
            log.warning("resumo falhou em %s: %s", pid[:8], erro)
            resumo.setdefault("falhas", 0)
            resumo["falhas"] += 1
            continue
        if not texto:
            continue
        http.patch("/process_insights", params={"process_id": f"eq.{pid}"}, json={
            "resumo": texto, "resumo_gerado_em": _agora(), "resumo_assinatura": ass,
            "resumo_modelo": f"deepseek/{cfg.deepseek_modelo}"}).raise_for_status()
        resumo["resumos_feitos"] += 1
    return resumo


# ── 5. ALERTAS DE CADASTRO (process_alerts) ─────────────────────────────────
def alertas(banco: Banco, cfg: Config, crm_processos, aplicar: bool, somente: set[str] | None = None,
            agora: datetime | None = None, crm=None) -> dict:
    """Prazo/audiência achado numa intimação e não cadastrado em 24 h.

    Abre o que é novo, atualiza o aberto, resolve sozinho o que foi cadastrado,
    reabre o resolvido que voltou a faltar — e NUNCA reabre o ignorado (a chave
    única é por intimação/audiência)."""
    from . import alertas as regras
    from .crm import CRM
    agora = agora or datetime.now(timezone.utc)
    crm = crm or CRM(cfg.supabase_url, cfg.supabase_key)
    dados, agenda = crm.para_a_ficha(), crm.agenda(crm_processos)
    nomes = {c.id: c.nome for c in crm.clientes()}
    http = _cliente(cfg)

    achados: dict[str, dict] = {}
    for proc in crm_processos:
        if not proc.numero or (somente is not None and proc.numero not in somente):
            continue
        r = banco.processo(proc.numero)
        analise = json.loads(r["analise"]) if r and r["analise"] else {}
        if analise.get("situacao") == "arquivado":
            continue
        d = dados.get(proc.id) or {}
        for a in regras.detectar({"id": proc.id, "client_id": proc.client_id, "codigo": proc.codigo,
                                  "cliente": nomes.get(proc.client_id)},
                                 d.get("intimacoes") or [],
                                 (d.get("prazos") or []) + (dados.get(f"cliente:{proc.client_id}") or {}).get("prazos", []),
                                 agenda.get(proc.id) or [], analise.get("audiencia"), agora,
                                 financeiro=d.get("financeiro") or []):
            achados[a["chave"]] = a

    filtro = {} if somente is None else {"process_id": f"in.({','.join(p.id for p in crm_processos if p.numero in somente) or '00000000-0000-0000-0000-000000000000'})"}
    guardados = {g["chave"]: g for g in _tudo(http, "process_alerts", "id,chave,situacao,process_id", filtro)}
    novos = [a for k, a in achados.items() if k not in guardados]
    atualizar = [a for k, a in achados.items() if k in guardados and guardados[k]["situacao"] in ("aberto", "resolvido")]
    resolver = [g for k, g in guardados.items() if g["situacao"] == "aberto" and k not in achados]
    resumo = {"abertos_agora": len(achados), "novos": len(novos), "atualizados": len(atualizar),
              "resolvidos": len(resolver), "aplicado": aplicar}
    if not aplicar:
        return resumo
    carimbo = agora.isoformat()
    if novos:
        http.post("/process_alerts", params={"on_conflict": "chave"},
                  headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                  json=[{**a, "situacao": "aberto", "detectado_em": carimbo, "atualizado_em": carimbo}
                        for a in novos]).raise_for_status()
    for a in atualizar:
        g = guardados[a["chave"]]
        http.patch("/process_alerts", params={"id": f"eq.{g['id']}", "situacao": "in.(aberto,resolvido)"}, json={
            "titulo": a["titulo"], "descricao": a["descricao"], "data": a["data"], "hora": a["hora"],
            "dados": a["dados"], "situacao": "aberto", "resolvido_em": None, "atualizado_em": carimbo,
        }).raise_for_status()
    for g in resolver:
        http.patch("/process_alerts", params={"id": f"eq.{g['id']}", "situacao": "eq.aberto"},
                   json={"situacao": "resolvido", "resolvido_em": carimbo, "atualizado_em": carimbo}).raise_for_status()
    return resumo
