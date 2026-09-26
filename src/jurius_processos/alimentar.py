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

import json
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from . import cnj
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
    if not aplicar or not novas:
        return resumo
    for i in range(0, len(novas), 100):
        http.post("/djen_comunicacoes", params={"on_conflict": "hash"},
                  headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                  json=novas[i:i + 100]).raise_for_status()
    # As mesmas marcas que a rotina antiga punha no processo (nunca o status).
    marcas = {"djen_synced": True, "djen_last_sync": _agora(), "djen_has_data": True}
    for pid in vinculadas:
        http.patch("/processes", params={"id": f"eq.{pid}"}, json=marcas).raise_for_status()
    return resumo


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
    carimbo = _agora()
    for pid, cache in caches:
        http.patch("/processes", params={"id": f"eq.{pid}"},
                   json={"datajud_cache": cache, "datajud_synced_at": carimbo}).raise_for_status()
    return resumo


# ── 3. IA (cron 11) ─────────────────────────────────────────────────────────
# O MESMO texto da analyze-intimations: é a calibração que o escritório já usa.
PROMPT_SISTEMA = """Você é um assistente jurídico. Analise a intimação e retorne APENAS um JSON válido:
{
  "urgency": "baixa" | "media" | "alta" | "critica",
  "deadline": { "days": número de dias para o prazo },
  "summary": "resumo curto da intimação em 1-2 frases"
}
Critérios de urgência: critica = prazo <= 2 dias; alta = prazo <= 5 dias; media = prazo <= 15 dias; baixa = prazo > 15 dias ou sem prazo."""

ROTULO_URGENCIA = {"critica": "🚨 CRÍTICA", "alta": "⚠️ Urgente", "media": "📋 Atenção", "baixa": "📄 Nova"}


def _json_da_resposta(texto: str) -> dict | None:
    ini, fim = texto.find("{"), texto.rfind("}")
    if ini < 0 or fim <= ini:
        return None
    try:
        return json.loads(texto[ini:fim + 1])
    except ValueError:
        return None


def analisar_com_ia(cfg: Config, texto: str, cliente: httpx.Client | None = None) -> dict | None:
    http = cliente or httpx.Client(timeout=90)
    r = http.post("https://api.deepseek.com/chat/completions",
                  headers={"Authorization": f"Bearer {cfg.deepseek_key}", "Content-Type": "application/json"},
                  json={"model": cfg.deepseek_modelo, "temperature": 0.1, "max_tokens": 500,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "system", "content": PROMPT_SISTEMA},
                                     {"role": "user", "content": f"Analise esta intimação:\n\n{(texto or '')[:3000]}"}]})
    r.raise_for_status()
    return _json_da_resposta(r.json()["choices"][0]["message"]["content"])


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
        "select": "id,texto,numero_processo,numero_processo_mascara,sigla_tribunal,data_disponibilizacao,process_id",
        "order": "data_disponibilizacao.desc", "limit": "150", **filtro}).json()
    ids = [x["id"] for x in recentes]
    feitas = {a["intimation_id"] for a in http.get("/intimation_ai_analysis", params={
        "select": "intimation_id", "intimation_id": f"in.({','.join(ids)})"}).json()} if ids else set()
    pendentes = [x for x in recentes if x["id"] not in feitas][:limite]
    resumo = {"pendentes": len(pendentes), "analisadas": 0, "avisos": 0, "aplicado": aplicar}
    if not aplicar or not pendentes:
        return resumo

    feriados = {str(h["date"])[:10] for h in _tudo(http, "holidays", "date")}
    usuarios = [u["user_id"] for u in _tudo(http, "profiles", "user_id", {"is_active": "eq.true"}) if u.get("user_id")]
    ia_http = httpx.Client(timeout=90)
    for it in pendentes:
        try:
            a = analisar_com_ia(cfg, it.get("texto") or "", ia_http)
        except Exception as e:  # noqa: BLE001 — uma intimação não derruba as outras
            log.warning("IA falhou em %s: %s", it["id"][:8], e)
            continue
        if not a:
            continue
        dias = (a.get("deadline") or {}).get("days")
        dias = dias if isinstance(dias, (int, float)) else None
        agora = _agora()
        contagem = contar_prazo_da_intimacao(it.get("data_disponibilizacao") or agora, dias, feriados)
        r = http.post("/intimation_ai_analysis", params={"on_conflict": "intimation_id"},
                      headers={"Prefer": "resolution=ignore-duplicates,return=representation"}, json={
            "intimation_id": it["id"], "summary": a.get("summary"), "urgency": a.get("urgency"),
            "deadline_days": dias,
            "deadline_due_date": f"{contagem['vencimento']}T00:00:00.000Z" if contagem else None,
            "analyzed_at": agora, "model_used": f"deepseek/{cfg.deepseek_modelo} (cérebro)",
            "created_at": agora, "updated_at": agora})
        r.raise_for_status()
        if not r.json():
            continue  # a rotina antiga chegou antes: nada de aviso em dobro
        resumo["analisadas"] += 1
        resumo["avisos"] += _avisar_equipe(http, usuarios, it, a)
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
                  json={"model": cfg.deepseek_modelo, "temperature": 0.2, "max_tokens": 4000,
                        "messages": [{"role": "system", "content": ficha_mod.PROMPT_SISTEMA},
                                     {"role": "user", "content": texto}]})
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
        d = dados.get(proc.id, {})
        movs = [m for i in instancias for m in i.get("movimentos") or []]
        e = ficha_mod.entradas({"codigo": proc.codigo, "area": d.get("area"), "cliente": nomes.get(proc.client_id)},
                               analise, f, d.get("intimacoes") or [], movs,
                               _prazos_que_contam(d.get("prazos") or [], hoje), agenda.get(proc.id) or [],
                               financeiro.get(proc.id) or [], d.get("notas"))
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
    if not cfg.deepseek_key:
        resumo["resumos_pulados"] = "sem DEEPSEEK_API_KEY"
        return resumo
    ia_http = httpx.Client(timeout=120)
    for _, pid, e, ass in a_resumir[:limite]:
        try:
            texto = gerar_resumo(cfg, ficha_mod.prompt(e), ia_http)
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
                                 agenda.get(proc.id) or [], analise.get("audiencia"), agora):
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
