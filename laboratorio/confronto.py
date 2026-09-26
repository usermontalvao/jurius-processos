# Rodar: .venv/bin/python laboratorio/confronto.py > dados/laboratorio.json  (só leitura nos dois lados)
"""Laboratório: confronto do serviço jurius-processos com o que está no Supabase.

Só leitura nos dois lados. Saída: JSON com as métricas + amostras, em stdout.
"""
import json, os, re, sqlite3, sys
from collections import Counter, defaultdict
import httpx

ENV = {}
for linha in open(os.path.expanduser("~/Documents/GitHub/CRMlaw/.env")):
    if "=" in linha and not linha.lstrip().startswith("#"):
        k, v = linha.rstrip("\n").split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")
URL = (ENV.get("SUPABASE_URL") or ENV["VITE_SUPABASE_URL"]).rstrip("/")
KEY = ENV["SUPABASE_SERVICE_ROLE_KEY"]
http = httpx.Client(base_url=f"{URL}/rest/v1", timeout=120, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})

def tudo(tabela, select, **filtros):
    out, ini = [], 0
    while True:
        r = http.get(f"/{tabela}", params={"select": select, **filtros}, headers={"Range": f"{ini}-{ini+999}"})
        r.raise_for_status(); lote = r.json(); out += lote
        if len(lote) < 1000: return out
        ini += 1000

dig = lambda s: re.sub(r"\D", "", s or "")
db = sqlite3.connect(os.path.expanduser("~/Documents/GitHub/jurius-processos/dados/cerebro.sqlite3"))
db.row_factory = sqlite3.Row

# ── Nosso lado ────────────────────────────────────────────────────────────
nossos = {}
for r in db.execute("select numero, datajud_status, datajud, analise, vinculo from processos"):
    a = json.loads(r["analise"] or "{}"); inst = json.loads(r["datajud"] or "[]")
    movs = [m for i in inst for m in (i.get("movimentos") or [])]
    datas = [m.get("dataHora", "")[:10] for m in movs if m.get("dataHora")]
    nossos[r["numero"]] = {"a": a, "dj": r["datajud_status"], "n_movs": len(movs),
                           "ult_mov": max(datas) if datas else None, "graus": sorted({i.get("grau") for i in inst if i.get("grau")})}
nossas_int = {r["id"]: (r["numero"], r["data"][:10], r["motivo"]) for r in db.execute("select id, numero, data, motivo from comunicacoes")}

# ── Supabase ──────────────────────────────────────────────────────────────
procs = tudo("processes", "id,process_code,status,status_manual,client_id")
antes = {b["id"]: b["status"] for b in tudo("processes_status_backup_20260926", "id,status")}
djen = tudo("djen_comunicacoes", "djen_id,numero_processo,data_disponibilizacao,process_id,sigla_tribunal")
movs = tudo("datajud_movimentos", "process_id,process_code,grau,codigo,nome,data_hora")
mov_por_proc = defaultdict(list)
for m in movs: mov_por_proc[m["process_id"]].append(m)

res = {}

# A. Cobertura de processos
crm_nums = {dig(p["process_code"]): p for p in procs if len(dig(p["process_code"])) == 20}
res["A_processos"] = {
    "crm_com_numero_valido": len(crm_nums),
    "crm_no_acervo": sum(1 for n in crm_nums if n in nossos),
    "crm_fora_do_acervo": [n for n in crm_nums if n not in nossos],
    "acervo_total": len(nossos),
    "acervo_fora_do_crm": sum(1 for n in nossos if n not in crm_nums),
    "datajud_status_nosso": dict(Counter(v["dj"] for v in nossos.values())),
}

# B. Intimações (mesma janela: desde a 1ª do Supabase)
desde = min(d["data_disponibilizacao"][:10] for d in djen)
sup_ids = {int(d["djen_id"]): d for d in djen if d.get("djen_id")}
nos_ids = {i for i, (_, data, _) in nossas_int.items() if data >= desde}
so_sup = [i for i in sup_ids if i not in nossas_int]
so_nos = [i for i in nos_ids if i not in sup_ids]
res["B_intimacoes"] = {
    "janela_desde": desde,
    "supabase": len(sup_ids), "nossas_na_janela": len(nos_ids), "em_ambos": len(set(sup_ids) & nos_ids),
    "so_no_supabase": len(so_sup), "so_nossas": len(so_nos),
    "so_nossas_por_ano": dict(sorted(Counter(nossas_int[i][1][:4] for i in so_nos).items())),
    "so_nossas_motivo": dict(Counter(nossas_int[i][2] for i in so_nos)),
    "so_nossas_de_processo_do_crm": sum(1 for i in so_nos if nossas_int[i][0] in crm_nums),
    "so_supabase_amostra": [{"djen_id": i, "numero": dig(sup_ids[i]["numero_processo"]), "data": sup_ids[i]["data_disponibilizacao"][:10],
                             "processo_no_acervo": dig(sup_ids[i]["numero_processo"]) in nossos} for i in so_sup[:15]],
}

# C. Movimentos DataJud: quem está mais completo e mais fresco
mais_nos = mais_sup = iguais = sem_ambos = 0
fresco_nos = fresco_sup = fresco_igual = 0
exemplos_sup_mais_fresco = []
for n, p in crm_nums.items():
    v = nossos.get(n); ms = mov_por_proc.get(p["id"], [])
    if not v: continue
    ns = len(ms); ult_s = max((m["data_hora"] or "")[:10] for m in ms) if ms else None
    if not ns and not v["n_movs"]: sem_ambos += 1
    elif v["n_movs"] > ns: mais_nos += 1
    elif v["n_movs"] < ns: mais_sup += 1
    else: iguais += 1
    if ult_s or v["ult_mov"]:
        if (v["ult_mov"] or "") > (ult_s or ""): fresco_nos += 1
        elif (v["ult_mov"] or "") < (ult_s or ""):
            fresco_sup += 1
            if len(exemplos_sup_mais_fresco) < 10: exemplos_sup_mais_fresco.append({"numero": n, "nosso": v["ult_mov"], "supabase": ult_s})
        else: fresco_igual += 1
res["C_datajud"] = {"mais_movimentos_nosso": mais_nos, "mais_movimentos_supabase": mais_sup, "iguais": iguais, "nenhum_dos_dois": sem_ambos,
                    "ultimo_mov_mais_recente_nosso": fresco_nos, "ultimo_mov_mais_recente_supabase": fresco_sup, "mesma_data": fresco_igual,
                    "exemplos_supabase_mais_fresco": exemplos_sup_mais_fresco,
                    "total_movs_nosso_crm": sum(nossos[n]["n_movs"] for n in crm_nums if n in nossos), "total_movs_supabase": len(movs)}

# D. Arquivamento, caso a caso, com o PORQUÊ de cada lado.
# Prova = DataJud na instância de ORIGEM (1º grau / Juizado). "Baixa Definitiva"
# em Turma Recursal, TJ (G2) ou tribunal superior é a volta dos autos à origem,
# não arquivamento do processo.
ARQ = re.compile(r"arquiv|baixa definitiva", re.I)
DESARQ = re.compile(r"desarquiv|reativa", re.I)
MIUDO = re.compile(r"publica|disponibiliza|expedi|certid|decurso|juntada|remessa|recebimento|conclus|documento|devolvid|peti[cç]|ato ordinat|tr[aâ]nsito", re.I)
ORIGEM = {"G1", "JE", None, ""}

def prova(ms):
    ms = sorted([m for m in ms if m.get("grau") in ORIGEM], key=lambda m: m["data_hora"] or "")
    if not ms: return None, None
    # Pelo CÓDIGO: 246 = arquivamento definitivo (o nome no DataJud é só "Definitivo"), 22 = baixa definitiva.
    idx = [k for k, m in enumerate(ms) if m.get("codigo") in (246, 22)]
    reab = [k for k, m in enumerate(ms) if m.get("codigo") in (893, 849)]
    if idx and reab and reab[-1] > idx[-1]: return False, None
    if not idx: return False, None
    k = idx[-1]
    vivo = [m for m in ms[k + 1:] if not MIUDO.search(m["nome"] or "")]
    return (not vivo), (ms[k]["data_hora"] or "")[:10]

cat = Counter(); ex = defaultdict(list)
for n, p in crm_nums.items():
    v = nossos.get(n)
    if not v: continue
    a = v["a"]; velho = antes.get(p["id"]); nosso = a.get("status_crm")
    pr, quando = prova(mov_por_proc.get(p["id"], []))
    if pr is None:
        cat["sem andamento de origem"] += 1; continue
    v_arq, n_arq = velho == "arquivado", nosso == "arquivado"
    if v_arq == n_arq == pr:
        cat["os dois certos"] += 1; continue
    # Por que o NOSSO disse arquivado sem prova no DataJud?
    if n_arq and not pr:
        chave = ("nosso arquivado; a cópia do DataJud no Supabase não tem arquivo na origem" if not quando
                 else "nosso arquivado; houve ato de processo vivo depois do arquivo")
    elif not n_arq and pr:
        chave = "nosso deixa ativo, DataJud mostra arquivado"
    else:
        chave = "nosso certo"
    lado_antigo = "antigo certo" if v_arq == pr else "antigo errado"
    k = f"{chave} | {lado_antigo}"
    cat[k] += 1
    if len(ex[k]) < 6: ex[k].append({"numero": n, "antigo": velho, "nosso": nosso, "fase": a.get("fase"), "arquivado_em": a.get("arquivado_em"), "prova_arquivo_em": quando})
res["D_arquivamento"] = {"categorias": dict(cat.most_common()), "exemplos": ex}

# E. Transições de status (antes x depois)
res["E_transicoes"] = dict(Counter(f"{antes.get(p['id'])} → {p['status']}" for p in procs if antes.get(p["id"]) != p["status"]).most_common(25))
print(json.dumps(res, ensure_ascii=False, indent=1))
