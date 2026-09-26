"""Relatório de validação: o que o cérebro concluiu × o que o CRM tem hoje.

Fica em dados/ (fora do git): tem nome de cliente e de parte.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import cnj
from .banco import Banco


def _tab(linhas: list[list], cab: list[str]) -> str:
    out = ["| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    out += ["| " + " | ".join("" if c is None else str(c).replace("|", "/") for c in l) + " |" for l in linhas]
    return "\n".join(out)


def gerar(banco: Banco, clientes, crm_processos, saida: str) -> str:
    procs = [dict(r) for r in banco.processos()]
    for p in procs:
        p["a"] = json.loads(p["analise"]) if p["analise"] else {}
        p["v"] = json.loads(p["vinculo"]) if p["vinculo"] else {}
    crm = {p.numero: p for p in crm_processos if p.numero}
    L: list[str] = ["# Jurius Processos — relatório de validação", ""]

    no_crm = [p for p in procs if p["numero"] in crm]
    fora = [p for p in procs if p["numero"] not in crm]
    L += ["## Acervo", "",
          f"- Processos no acervo: **{len(procs)}**",
          f"- Já cadastrados no CRM: **{len(no_crm)}** (o CRM tem {len(crm)} com número válido)",
          f"- Descobertos fora do CRM: **{len(fora)}**",
          f"- Intimações do DJEN guardadas: **{banco.con.execute('select count(*) from comunicacoes').fetchone()[0]}**", ""]

    L += ["## DataJud", "", _tab([[k or "não consultado", v] for k, v in Counter(p["datajud_status"] for p in procs).most_common()],
                                 ["resultado", "processos"]), ""]
    execs = banco.con.execute("select etapa, inicio, fim, resumo from execucoes where ok is not null order by id desc limit 6").fetchall()
    L += ["Últimas execuções:", "", _tab([[e["etapa"], e["inicio"], e["fim"], e["resumo"][:160]] for e in execs],
                                         ["etapa", "início", "fim", "resumo"]), ""]

    L += ["## Fase e situação", "",
          _tab([[k, v] for k, v in Counter(p["a"].get("fase") for p in procs).most_common()], ["fase", "processos"]), "",
          _tab([[k, v] for k, v in Counter(p["a"].get("situacao") for p in procs).most_common()], ["situação", "processos"]), ""]

    # ── status novo × status do CRM ──
    comp = []
    for p in no_crm:
        c = crm[p["numero"]]
        novo = p["a"].get("status_crm")
        comp.append((c.status, novo, c.status_manual, p))
    iguais = sum(1 for a, b, _, _ in comp if a == b)
    L += ["## Status: CRM hoje × cérebro", "",
          f"Dos {len(comp)} processos que estão nos dois lados, **{iguais}** têm o mesmo status "
          f"e **{len(comp) - iguais}** divergem.", "",
          _tab([[f"{a} → {b}", n] for (a, b), n in Counter((a, b) for a, b, _, _ in comp if a != b).most_common()],
               ["CRM → cérebro", "processos"]), "",
          "Amostra das divergências (para conferir à mão):", "",
          _tab([[cnj.formatar(p["numero"]), a, b, "sim" if m else "", p["a"].get("classe"),
                 (p["a"].get("ultimo_movimento") or {}).get("nome"), (p["a"].get("ultimo_movimento") or {}).get("data")]
                for a, b, m, p in comp if a != b][:40],
               ["processo", "CRM", "cérebro", "manual", "classe", "último movimento", "em"]), ""]

    # ── vínculos ──
    L += ["## Vínculo com cliente", "",
          _tab([[k, v] for k, v in Counter(p["v"].get("tipo") for p in procs).most_common()], ["tipo", "processos"]), ""]
    auto = [p for p in fora if p["v"].get("tipo") == "nome_exato"]
    L += [f"### Vinculados sozinhos (fora do CRM, cliente cadastrado): {len(auto)}", "",
          "Estes o sistema cadastraria no CRM já ligados ao cliente.", "",
          _tab([[cnj.formatar(p["numero"]), p["v"]["client_nome"], p["v"].get("polo_cliente"), p["a"].get("fase"),
                 p["a"].get("situacao"), p["a"].get("classe")] for p in auto],
               ["processo", "cliente", "polo", "fase", "situação", "classe"]), ""]
    sug = [p for p in procs if p["v"].get("tipo") == "sugestao"]
    L += [f"### Sugestões para confirmar: {len(sug)}", "",
          _tab([[cnj.formatar(p["numero"]), s["parte"], s["nome"], s["score"], s["motivo"]]
                for p in sug for s in p["v"]["sugestoes"][:3]],
               ["processo", "parte no processo", "cliente sugerido", "confiança", "motivo"]), ""]
    div = [p for p in procs if p["v"].get("tipo") == "divergente"]
    L += [f"### Divergentes (CRM diz um cliente, as partes dizem outro): {len(div)}", "",
          _tab([[cnj.formatar(p["numero"]), p["v"]["client_nome"], "; ".join(s["nome"] for s in p["v"]["sugestoes"]),
                 "; ".join(p["v"]["partes"]["A"][:2])] for p in div],
               ["processo", "cliente no CRM", "cliente pelas partes", "polo ativo"]), ""]
    sem = [p for p in procs if p["v"].get("tipo") == "sem_cliente"]
    L += [f"### Sem cliente cadastrado: {len(sem)}", "",
          "Aparecem na aba com o nome da parte e o botão \"cadastrar cliente\". Primeiros 30:", "",
          _tab([[cnj.formatar(p["numero"]), p["v"].get("parte_principal"), p["a"].get("fase"), p["a"].get("situacao"),
                 p["a"].get("ultima_atividade")] for p in sem[:30]],
               ["processo", "parte principal", "fase", "situação", "última atividade"]), ""]

    rel = [(p, r) for p in procs for r in p["v"].get("relacionados", [])]
    L += [f"### Processos relacionados pelo texto: {len(rel)} ligações", "",
          _tab([[cnj.formatar(p["numero"]), cnj.formatar(r), "sim" if banco.processo(r) else "não"] for p, r in rel[:30]],
               ["processo", "cita", "o citado está no acervo?"]), ""]

    # ── pendências ──
    pend = Counter(x["tipo"] for p in procs for x in p["a"].get("pendencias", []))
    L += ["## Pendências encontradas", "", _tab([[k, v] for k, v in pend.most_common()], ["pendência", "processos"]), ""]
    for tipo in ("execucao_pendente", "valor_a_levantar", "audiencia_designada"):
        lista = [(p, x) for p in procs for x in p["a"].get("pendencias", []) if x["tipo"] == tipo]
        if lista:
            L += [f"### {tipo} ({len(lista)})", "",
                  _tab([[cnj.formatar(p["numero"]), p["v"].get("client_nome") or p["v"].get("parte_principal"),
                         x["descricao"], x["desde"]] for p, x in lista[:25]],
                       ["processo", "cliente/parte", "o quê", "desde"]), ""]

    Path(saida).parent.mkdir(parents=True, exist_ok=True)
    Path(saida).write_text("\n".join(L), encoding="utf-8")
    return saida
