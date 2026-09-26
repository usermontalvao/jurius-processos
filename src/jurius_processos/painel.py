"""O painel da raiz (GET /): o que o serviço sabe e o que fez, sem precisar de token.

Abrir o endereço do serviço e receber `{"detail":"Not Found"}` não informa
nada. Aqui só vão números agregados (nenhum nome de parte), por isso a página
é aberta; o que expõe dado de processo continua atrás do token.
"""

from __future__ import annotations

import json
from collections import Counter

from .banco import Banco


def dados(banco: Banco, ocupado: bool) -> dict:
    fases, vinculos, situacoes = Counter(), Counter(), Counter()
    fora = 0
    for r in banco.processos("analise is not null"):
        a, v = json.loads(r["analise"]), json.loads(r["vinculo"] or "{}")
        fases[a.get("fase") or "?"] += 1
        situacoes[a.get("situacao") or "?"] += 1
        vinculos[v.get("tipo") or "?"] += 1
        if not v.get("crm_process_id"):
            fora += 1
    con = banco.con
    execs = []
    for r in con.execute("select etapa, inicio, fim, ok, resumo from execucoes order by id desc limit 12"):
        d = dict(r)
        try:
            d["resumo"] = json.loads(d["resumo"] or "{}")
        except ValueError:
            pass
        execs.append(d)
    return {
        "processos": con.execute("select count(*) from processos").fetchone()[0],
        "intimacoes": con.execute("select count(*) from comunicacoes").fetchone()[0],
        "fora_do_crm": fora,
        "fases": dict(fases.most_common()),
        "situacoes": dict(situacoes.most_common()),
        "vinculos": dict(vinculos.most_common()),
        "datajud": dict(con.execute("select coalesce(datajud_status,'nunca'), count(*) from processos group by 1").fetchall()),
        "execucoes": execs,
        "ocupado": ocupado,
        "carga_feita": banco.carga_feita(),
        "versao": __import__("os").environ.get("JURIUS_VERSAO", "local"),
        "agendador": __import__("jurius_processos.agendador", fromlist=["ESTADO"]).ESTADO,
        "ultimo_erro": next((e["resumo"].get("erro") for e in execs
                             if not e["ok"] and e["fim"] and isinstance(e["resumo"], dict) and e["resumo"].get("erro")), None),
    }


HTML = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jurius Processos</title>
<style>
:root{--bg:#f8f7f5;--card:#fff;--tx:#1a1613;--mut:#6b625a;--ln:#e7e5df;--acc:#f97316;--ok:#16a34a;--err:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#0c0c0e;--card:#18181b;--tx:#f4f4f5;--mut:#a1a1aa;--ln:#2e2e33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0}header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.sub{color:var(--mut);font-size:12px}.sp{flex:1}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:14px 16px}
.n{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}.l{color:var(--mut);font-size:12px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-bottom:12px}
h2{font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px dashed var(--ln)}.row:last-child{border:0}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--ln);vertical-align:top}
th{color:var(--mut);font-weight:600}.ok{color:var(--ok);font-weight:700}.err{color:var(--err);font-weight:700}
code{font-size:11px;color:var(--mut);word-break:break-word}
</style></head><body><main>
<header><div><h1>Jurius Processos</h1>
<div class="sub">Descobre pelo DJEN (OAB e nome completo), enriquece no DataJud, analisa e publica no CRM. Ciclo automático a cada 2 h, das 06h às 22h (Cuiabá).</div></div>
<div class="sp"></div><span id="estado" class="sub"></span><button id="rodar">Rodar ciclo agora</button></header>
<div id="aviso"></div>
<div class="grid" id="nums"></div>
<div class="cols" id="dist"></div>
<div class="card"><h2>Últimas execuções</h2><table><thead><tr><th>Etapa</th><th>Início</th><th>Duração</th><th></th><th>Resumo</th></tr></thead><tbody id="execs"></tbody></table></div>
</main>
<script>
const FASE={distribuicao:'Distribuição',conhecimento:'Conhecimento',instrucao:'Instrução',sentenciado:'Sentenciado',recursal:'Recursal',transitado:'Transitado',cumprimento_sentenca:'Cumprimento de sentença',execucao:'Execução',desconhecida:'Sem dados'};
const VINC={cadastro:'Já no CRM',nome_exato:'Cliente identificado',sugestao:'Cliente provável',sem_cliente:'Sem cliente',divergente:'Divergente'};
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const hora=t=>t?new Date(t).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const dur=(a,b)=>{if(!a||!b)return'em curso';const s=Math.round((new Date(b)-new Date(a))/1000);return s<60?s+' s':Math.floor(s/60)+' min '+(s%60)+' s'};
const bloco=(t,o,map)=>'<div class="card"><h2>'+t+'</h2>'+Object.entries(o).map(([k,v])=>'<div class="row"><span>'+esc((map&&map[k])||k)+'</span><b>'+v+'</b></div>').join('')+'</div>';
async function carregar(){
  const d=await (await fetch('painel.json')).json();
  document.getElementById('nums').innerHTML=[['Processos no acervo',d.processos],['Fora do CRM',d.fora_do_crm],['Intimações guardadas',d.intimacoes],['Com DataJud',d.datajud.ok||0]]
    .map(([l,n])=>'<div class="card"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join('');
  document.getElementById('dist').innerHTML=bloco('Fase',d.fases,FASE)+bloco('Vínculo com o CRM',d.vinculos,VINC)+bloco('Situação',d.situacoes);
  document.getElementById('execs').innerHTML=d.execucoes.map(e=>'<tr><td>'+esc(e.etapa)+'</td><td>'+hora(e.inicio)+'</td><td>'+dur(e.inicio,e.fim)+'</td><td class="'+(e.ok?'ok':'err')+'">'+(e.fim?(e.ok?'ok':'falhou'):'…')+'</td><td><code>'+esc(JSON.stringify(e.resumo))+'</code></td></tr>').join('');
  const av=[];
  if(!d.carga_feita) av.push('Primeira carga em andamento: o histórico desde 2023 é buscado antes de publicar qualquer coisa no CRM (≈30 min).');
  if(d.ultimo_erro) av.push('Última falha: '+esc(d.ultimo_erro));
  const ag=d.agendador||{};
  if(ag.ultima_falha) av.push('Agendador falhou em '+hora(ag.ultima_falha_em)+': '+esc(ag.ultima_falha)+' — nova tentativa às '+hora(ag.proximo));
  if(!ag.iniciado_em) av.push('O agendador não está rodando.');
  document.getElementById('aviso').innerHTML=av.map(t=>'<div class="card" style="margin-bottom:12px;border-color:var(--acc)">'+t+'</div>').join('');
  document.getElementById('estado').textContent=d.ocupado?'Trabalhando agora…':'Parado · atualizado '+hora(new Date());
  document.getElementById('rodar').disabled=d.ocupado;
}
document.getElementById('rodar').onclick=async()=>{
  const r=await fetch('ciclo',{method:'POST'});
  if(!r.ok){alert((await r.json()).detail||'Não foi possível');return}
  carregar();
};
carregar();setInterval(carregar,15000);
</script></body></html>"""
