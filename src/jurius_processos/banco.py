"""Banco próprio do cérebro (SQLite num volume do servidor).

Guarda o material BRUTO — toda resposta do DJEN e do DataJud — e o resultado
de cada etapa. O Supabase só recebe o que já está pronto (ver publicar.py).
Com algumas centenas de processos, SQLite sobra; não há Postgres para manter.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ESQUEMA = """
create table if not exists comunicacoes (
  id               integer primary key,        -- id do DJEN
  numero           text not null,              -- 20 dígitos
  data             text not null,              -- data de disponibilização
  tribunal         text,
  tipo             text,
  orgao            text,
  classe           text,
  texto            text,
  destinatarios    text,                       -- json [{nome, polo}]
  advogados        text,                       -- json [{nome, oab, uf}]
  motivo           text,                       -- 'oab' | 'nome' | 'processo'
  hash             text,
  bruto            text not null,
  visto_em         text not null
);
create index if not exists comunicacoes_numero on comunicacoes(numero);

create table if not exists processos (
  numero                 text primary key,
  origem                 text not null,        -- 'djen' | 'crm' | 'mencao'
  descoberto_em          text not null,
  datajud_status         text,                 -- null | 'ok' | 'vazio' | 'erro' | 'sem_indice'
  datajud_consultado_em  text,
  datajud_erro           text,
  datajud                text,                 -- json: lista de instâncias
  analise                text,                 -- json: saída de fases.analisar
  vinculo                text,                 -- json: saída de vinculo.vincular
  analisado_em           text
);

create table if not exists eventos (
  id        integer primary key autoincrement,
  em        text not null,
  numero    text,
  tipo      text not null,                     -- 'descoberto', 'vinculado', 'fase_mudou', ...
  detalhe   text
);

create table if not exists execucoes (
  id          integer primary key autoincrement,
  etapa       text not null,
  inicio      text not null,
  fim         text,
  ok          integer,
  resumo      text
);
"""


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Banco:
    """Uma conexão SQLite POR THREAD.

    O agendador escreve numa thread e as páginas (painel, /saude — que o
    healthcheck do Docker chama a cada minuto) leem em outras. Com uma conexão
    só, compartilhada, as leituras voltavam None no meio da escrita e a carga
    inicial morreu no 1º boot no servidor (26/09/2026). Em WAL, cada thread com
    a sua conexão lê em paralelo com o escritor; `busy_timeout` espera a vez
    em vez de falhar quando duas escritas se encontram.
    """

    def __init__(self, caminho: Path | str):
        self.caminho = Path(caminho)
        if str(self.caminho) != ":memory:":
            self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.con.execute("pragma journal_mode=wal")
        self.con.executescript(ESQUEMA)

    @property
    def con(self) -> sqlite3.Connection:
        c = getattr(self._local, "con", None)
        if c is None:
            c = sqlite3.connect(self.caminho, isolation_level=None, timeout=60)
            c.row_factory = sqlite3.Row
            c.execute("pragma busy_timeout=60000")
            self._local.con = c
        return c

    # ── comunicações ────────────────────────────────────────────────────────
    def gravar_comunicacao(self, item: dict, numero: str, motivo: str) -> bool:
        """True se é nova."""
        advs = [
            {"nome": (d.get("advogado") or {}).get("nome"),
             "oab": (d.get("advogado") or {}).get("numero_oab"),
             "uf": (d.get("advogado") or {}).get("uf_oab")}
            for d in item.get("destinatarioadvogados") or []
        ]
        dest = [{"nome": d.get("nome"), "polo": d.get("polo")} for d in item.get("destinatarios") or []]
        cur = self.con.execute(
            """insert or ignore into comunicacoes
               (id, numero, data, tribunal, tipo, orgao, classe, texto, destinatarios, advogados, motivo, hash, bruto, visto_em)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item["id"], numero, item.get("data_disponibilizacao"), item.get("siglaTribunal"),
             item.get("tipoComunicacao"), item.get("nomeOrgao"), item.get("nomeClasse"), item.get("texto"),
             json.dumps(dest, ensure_ascii=False), json.dumps(advs, ensure_ascii=False), motivo,
             item.get("hash"), json.dumps(item, ensure_ascii=False), agora()),
        )
        return cur.rowcount == 1

    def comunicacoes(self, numero: str) -> list[sqlite3.Row]:
        return self.con.execute("select * from comunicacoes where numero=? order by data", (numero,)).fetchall()

    # ── processos ───────────────────────────────────────────────────────────
    def garantir_processo(self, numero: str, origem: str) -> bool:
        cur = self.con.execute(
            "insert or ignore into processos (numero, origem, descoberto_em) values (?,?,?)",
            (numero, origem, agora()),
        )
        if cur.rowcount:
            self.evento(numero, "descoberto", {"origem": origem})
        return cur.rowcount == 1

    def processos(self, where: str = "1=1", params: tuple = ()) -> list[sqlite3.Row]:
        return self.con.execute(f"select * from processos where {where} order by numero", params).fetchall()

    def processo(self, numero: str) -> sqlite3.Row | None:
        return self.con.execute("select * from processos where numero=?", (numero,)).fetchone()

    def gravar_datajud(self, numero: str, status: str, instancias: list | None = None, erro: str | None = None):
        self.con.execute(
            """update processos set datajud_status=?, datajud_consultado_em=?, datajud_erro=?,
               datajud=coalesce(?, datajud) where numero=?""",
            (status, agora(), erro, json.dumps(instancias, ensure_ascii=False) if instancias is not None else None, numero),
        )

    def gravar_analise(self, numero: str, analise: dict, vinculo: dict):
        self.con.execute(
            "update processos set analise=?, vinculo=?, analisado_em=? where numero=?",
            (json.dumps(analise, ensure_ascii=False), json.dumps(vinculo, ensure_ascii=False), agora(), numero),
        )

    # ── registro ────────────────────────────────────────────────────────────
    def evento(self, numero: str | None, tipo: str, detalhe: dict | None = None):
        self.con.execute(
            "insert into eventos (em, numero, tipo, detalhe) values (?,?,?,?)",
            (agora(), numero, tipo, json.dumps(detalhe or {}, ensure_ascii=False)),
        )

    def fechar_interrompidas(self) -> int:
        """Execuções sem fim são de um processo que morreu (reinício, erro fatal).

        Sem isto o painel mostrava "em curso" para sempre uma carga que já não
        existia. Chamado ao subir, antes do agendador.
        """
        return self.con.execute(
            "update execucoes set fim=?, ok=0, resumo=json_object('erro', 'interrompida: o serviço parou no meio') "
            "where fim is null", (agora(),)).rowcount

    def carga_feita(self) -> bool:
        """A carga completa (histórico desde DJEN_INICIO) já rodou com sucesso?

        Banco vindo da máquina de desenvolvimento conta: lá a carga foi feita
        etapa por etapa (`descobrir --desde 2023-01-01`).
        """
        # Onde já existe registro de carga_completa, só ela vale: o `descobrir`
        # de 2023 é a 1ª etapa da própria carga e terminava antes do resto
        # (o painel dizia "carga feita" com o DataJud ainda por buscar).
        if self.con.execute("select 1 from execucoes where etapa='carga_completa' limit 1").fetchone():
            return bool(self.con.execute(
                "select 1 from execucoes where etapa='carga_completa' and ok=1 limit 1").fetchone())
        return bool(self.con.execute(
            "select 1 from execucoes where ok=1 and etapa='descobrir' "
            "and json_extract(resumo,'$.inicio') like '2023-%' limit 1").fetchone())

    def abrir_execucao(self, etapa: str) -> int:
        return self.con.execute("insert into execucoes (etapa, inicio) values (?,?)", (etapa, agora())).lastrowid

    def fechar_execucao(self, id_: int, ok: bool, resumo: dict):
        self.con.execute(
            "update execucoes set fim=?, ok=?, resumo=? where id=?",
            (agora(), int(ok), json.dumps(resumo, ensure_ascii=False), id_),
        )
