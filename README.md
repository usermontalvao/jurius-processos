# Jurius Processos

O cérebro do acervo processual do escritório. Roda fora do CRM, no servidor próprio,
e entrega tudo pronto no Supabase. O CRM só lê.

```
DJEN (por OAB + nome)  →  DataJud (em lote)  →  fase + pendências  →  vínculo  →  Supabase
   descobrir                enriquecer             analisar            analisar     publicar
```

## O que cada etapa faz

| Etapa | Fonte | O que sai |
|---|---|---|
| **descobrir** | DJEN (`comunicaapi.pje.jus.br`) | toda intimação e todo processo em que o advogado aparece, inclusive arquivados |
| **enriquecer** | DataJud (API pública do CNJ) | todas as instâncias e andamentos de cada processo |
| **analisar** | local (sem rede) | fase, situação, pendências, partes, cliente, processos relacionados |
| **publicar** | Supabase do CRM | `acervo_processos`, `acervo_eventos` e o cadastro automático em `processes` |

A OAB vem do cadastro do CRM (`profiles.oab`).

## Descobertas que moldaram o desenho (24/09/2026)

- **A OAB é guardada como `30021/O` no DJEN.** A busca por `30021` trouxe 4 intimações
  em março/2026, e por `30021/O`, 219. O sistema busca pelas duas formas e pelo nome,
  junta pelo id e descarta o que não é do advogado.
- **O DataJud leva de 18 a 53 s por consulta** e devolve 429 com mais de 2–3 simultâneas.
  A Edge Function antiga desistia em 12 s. Aqui a consulta é em **lote** (`terms`, 50 números
  por tribunal): 40 processos em 35 s.
- **Um número tem várias instâncias** (1º + 2º grau, Juizado + Turma Recursal). A versão antiga
  pedia `size: 1` e perdia metade.
- **O DataJud não traz partes** (LGPD). O cliente sai dos destinatários do DJEN.

## Regras de vínculo

Vincula sozinho só o que é certo:

- `cadastro`: o número já está em `processes`.
- `nome_exato`: nome da parte idêntico ao de **um** cliente cadastrado (sem acento, sem caixa).
  O sistema cadastra o processo no CRM e grava o aviso "já existia e foi vinculado a Fulano".
- `sugestao`: homônimo, pré-cadastro, nome abreviado ou parecido. Alguém confirma.
- `sem_cliente`: aparece na aba com o nome da parte.
- `divergente`: o CRM aponta um cliente e as partes apontam outro.

Quando um cliente é cadastrado, o CRM chama `POST /clientes/{id}/vincular`, e os processos
dele que já estavam no acervo são vinculados na hora.

## Rodar local

```sh
python3 -m venv .venv && .venv/bin/pip install httpx fastapi uvicorn pytest
./run.sh descobrir --desde 2023-01-01   # carga completa (~2 min)
./run.sh enriquecer                      # DataJud em lote
./run.sh analisar
./run.sh relatorio                       # dados/relatorio.md
PYTHONPATH=src .venv/bin/pytest -q
```

`run.sh` lê as chaves do `.env` do CRM (`../CRMlaw/.env`) sem copiá-las para cá.

## Servidor (Portainer + Cloudflare Tunnel)

O servidor do escritório não tem este repositório e a bridge do Docker dele
está quebrada. Por isso a stack leva o código dentro dela e usa rede de host
presa a 127.0.0.1 (o mesmo desenho do Jurius Call).

1. `.venv/bin/python deploy/gerar-stack.py` → `deploy/docker-compose.portainer.yml`
   (gerado, fora do git; nunca editar à mão).
2. Portainer → Stacks → Add stack → colar o arquivo. Variáveis da stack:
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` e `JURIUS_TOKEN_API`
   (o valor está em `deploy/token.secret`, fora do git).
3. Cloudflare Zero Trust → Tunnels → túnel do servidor → Public hostname:
   `processos.jurius-api.com` → `http://localhost:8792`.
4. Abrir `https://processos.jurius-api.com/`. Com o volume vazio, a primeira
   carga (histórico desde 2023) leva ~30 min e NADA é publicado antes dela.
5. Depois que o painel mostrar a carga concluída, parar a cópia local
   (`pkill -f "uvicorn jurius_processos.api:app"`): dois cérebros publicando
   ao mesmo tempo é exatamente o que este serviço veio acabar.

O DJEN recusa IP fora do Brasil (403). Se o servidor não tiver saída brasileira,
o painel mostra "O DJEN recusou o acesso" em vez de fingir que não há intimação.

Atualizar o serviço = gerar a stack de novo e "Update the stack" no Portainer
(o volume `jurius_processos_dados` guarda o banco entre as versões).
