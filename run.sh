#!/bin/sh
# Desenvolvimento local: pega as chaves do .env do CRM sem copiá-las para cá.
set -a
[ -f ../CRMlaw/.env ] && . ../CRMlaw/.env
[ -f .env ] && . ./.env
set +a
PYTHONPATH=src exec .venv/bin/python -m jurius_processos "$@"
