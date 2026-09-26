-- Jurius Processos: o que o cérebro publica para o CRM ler.
-- Aplicada no Supabase do CRM em 26/09/2026.
--
-- `processes` continua com client_id obrigatório: é o registro dos processos
-- de cliente do escritório. `acervo_processos` é TODO processo do advogado,
-- com ou sem cliente, e aponta para `processes` quando ele existe.

create table if not exists public.acervo_processos (
  numero            text primary key check (numero ~ '^\d{20}$'),
  numero_formatado  text not null,
  tribunal          text,
  classe            text,
  orgao             text,
  graus             text[] not null default '{}',
  ajuizado_em       date,
  fase              text not null,
  fase_desde        date,
  situacao          text not null check (situacao in ('ativo', 'suspenso', 'arquivado')),
  arquivado_em      date,
  status_crm        text,
  saude             text not null check (saude in ('ok', 'atencao', 'critico', 'sem_dados')),
  fonte             text not null check (fonte in ('datajud', 'djen', 'nenhuma')),
  ultima_atividade  date,
  ultimo_movimento  jsonb,
  audiencia         jsonb,
  pendencias        jsonb not null default '[]',
  marcos            jsonb not null default '[]',
  partes            jsonb not null default '{}',
  parte_principal   text,
  vinculo_tipo      text not null check (vinculo_tipo in ('cadastro', 'nome_exato', 'sugestao', 'sem_cliente', 'divergente')),
  client_id         uuid references public.clients(id) on delete set null,
  crm_process_id    uuid references public.processes(id) on delete set null,
  sugestoes         jsonb not null default '[]',
  relacionados      text[] not null default '{}',
  total_movimentos  int not null default 0,
  total_intimacoes  int not null default 0,
  atualizado_em     timestamptz not null default now()
);

create index if not exists acervo_processos_client on public.acervo_processos(client_id);
create index if not exists acervo_processos_crm on public.acervo_processos(crm_process_id);
create index if not exists acervo_processos_situacao on public.acervo_processos(situacao, fase);

-- Avisos que o CRM mostra ("já existia e foi vinculado a Fulano").
create table if not exists public.acervo_eventos (
  id          bigint generated always as identity primary key,
  em          timestamptz not null default now(),
  numero      text not null,
  tipo        text not null,
  client_id   uuid references public.clients(id) on delete set null,
  process_id  uuid references public.processes(id) on delete set null,
  mensagem    text not null,
  lido_em     timestamptz
);
create index if not exists acervo_eventos_pendentes on public.acervo_eventos(em desc) where lido_em is null;

alter table public.acervo_processos enable row level security;
alter table public.acervo_eventos enable row level security;

-- Leitura para a equipe (mesma régua de `processes`). Escrita só pelo cérebro (service role).
create policy acervo_processos_ler on public.acervo_processos for select using (public.is_office_staff());
create policy acervo_eventos_ler on public.acervo_eventos for select using (public.is_office_staff());
create policy acervo_eventos_marcar_lido on public.acervo_eventos for update
  using (public.is_office_staff()) with check (public.is_office_staff());

-- "Selecionar e importar" na aba Acervo: o CRM cadastra em `processes` e grava
-- aqui o vínculo (client_id, crm_process_id, vinculo_tipo) na hora, sem esperar
-- o próximo ciclo do cérebro — que depois confirma o mesmo vínculo como 'cadastro'.
create policy acervo_processos_vincular on public.acervo_processos for update
  using (public.is_office_staff()) with check (public.is_office_staff());
