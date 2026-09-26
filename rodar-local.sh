#!/bin/sh
# Serviço completo nesta máquina: API em 127.0.0.1:8792 + agendador interno
# (ciclo a cada 2 h, 06h–22h de Cuiabá), publicando no Supabase do CRM.
# Chaves: as do .env do CRM, sem copiá-las para cá. Log em dados/servico.log.
cd "$(dirname "$0")"
set -a
[ -f ../CRMlaw/.env ] && . ../CRMlaw/.env
[ -f .env ] && . ./.env
set +a
export JURIUS_PUBLICAR=1 JURIUS_ATUALIZAR_STATUS=1
PYTHONPATH=src exec .venv/bin/uvicorn jurius_processos.api:app --host 127.0.0.1 --port 8792 >> dados/servico.log 2>&1
