"""Gera a stack do Portainer com o código embutido: `python deploy/gerar-stack.py`.

O servidor do escritório não tem os repositórios. Como no Jurius Call, os
fontes vão dentro do próprio compose (tar.gz em base64), e o contêiner os
desempacota ao subir. Nunca edite o compose gerado à mão: a próxima geração
apaga a correção. Corrija aqui ou no código e gere de novo.

Rede: a bridge do Docker desse servidor está quebrada (19/08/2026), então a
stack usa `network_mode: host` e o serviço fica preso a 127.0.0.1 — quem o
publica é o Cloudflare Tunnel que já roda no servidor. Com rede de host,
0.0.0.0 exporia a API direto na internet.

Segredos NÃO vão no arquivo: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY e
JURIUS_TOKEN_API são variáveis de ambiente da stack no Portainer.
"""

from __future__ import annotations

import base64
import io
import subprocess
import tarfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "deploy" / "docker-compose.portainer.yml"
PORTA = 8792


def pacote() -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arq in sorted((RAIZ / "src").rglob("*.py")):
            tar.add(arq, arcname=str(arq.relative_to(RAIZ)))
    return base64.b64encode(buf.getvalue()).decode()


def versao() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(RAIZ), "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "sem-git"


def main():
    b64 = pacote()
    # Linhas de 76 colunas: o editor do Portainer não gosta de linha gigante.
    linhas = "\n".join("        " + b64[i:i + 76] for i in range(0, len(b64), 76))
    compose = f"""# GERADO por deploy/gerar-stack.py (commit {versao()}). Não edite à mão.
#
# Portainer → Stacks → Add stack → colar este arquivo. Em "Environment variables":
#   SUPABASE_URL                 https://<projeto>.supabase.co
#   SUPABASE_SERVICE_ROLE_KEY    chave service_role do Supabase do CRM
#   JURIUS_TOKEN_API             qualquer segredo longo (rodar ciclo de fora, API)
# Cloudflare Zero Trust → Tunnels → o túnel do servidor → Public hostname:
#   processos.jurius-api.com  →  http://localhost:{PORTA}
#
# 1º boot com volume vazio: carga completa do histórico (~30 min) ANTES de
# publicar qualquer coisa. Acompanhe em https://processos.jurius-api.com/
services:
  jurius-processos:
    image: python:3.12-slim
    container_name: jurius-processos
    restart: unless-stopped
    network_mode: host
    environment:
      SUPABASE_URL: ${{SUPABASE_URL}}
      SUPABASE_SERVICE_ROLE_KEY: ${{SUPABASE_SERVICE_ROLE_KEY}}
      JURIUS_TOKEN_API: ${{JURIUS_TOKEN_API}}
      JURIUS_PUBLICAR: "1"
      JURIUS_ATUALIZAR_STATUS: "1"
      JURIUS_CADASTRAR_AUTO: "0"
      JURIUS_BANCO: /dados/cerebro.sqlite3
      DJEN_INICIO: "2023-01-01"
      TZ: America/Cuiaba
      PYTHONUNBUFFERED: "1"
      PYTHONPATH: /app/src
      FONTES_B64: |
{linhas}
    volumes:
      - jurius_processos_dados:/dados
    working_dir: /app
    command:
      - bash
      - -c
      - |
        set -e
        mkdir -p /app /dados
        echo "$$FONTES_B64" | tr -d ' \\n' | base64 -d | tar -xz -C /app
        pip install --no-cache-dir --quiet httpx fastapi uvicorn
        if (exec 3<>/dev/tcp/127.0.0.1/{PORTA}) 2>/dev/null; then
          echo "A porta {PORTA} já está em uso neste servidor. Pare o outro serviço ou troque a porta." >&2
          exit 1
        fi
        exec uvicorn jurius_processos.api:app --host 127.0.0.1 --port {PORTA}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:{PORTA}/saude')"]
      interval: 60s
      timeout: 10s
      start_period: 120s

volumes:
  jurius_processos_dados:
"""
    SAIDA.write_text(compose)
    print(f"{SAIDA.relative_to(RAIZ)}: {len(compose)//1024} KB, código {len(b64)//1024} KB em base64")


if __name__ == "__main__":
    main()
