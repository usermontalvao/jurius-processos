"""Configuração por variáveis de ambiente. Nada de segredo no código."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Chave pública do DataJud, divulgada pelo próprio CNJ na documentação da API.
# Pode ser trocada por DATAJUD_API_KEY quando o CNJ rodar a chave.
_DATAJUD_CHAVE_PUBLICA = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_key: str
    datajud_key: str
    banco: Path
    token_api: str
    # Primeira data que a carga completa varre no DJEN. O diário nacional só
    # tem volume a partir de 2023; antes disso quase não há publicação.
    djen_inicio: str
    # Desligado = ensaio: calcula tudo e não grava no Supabase.
    publicar: bool
    # O que publicar pode fazer ALÉM de atualizar o acervo. Os dois nascem
    # desligados: quem traz um processo para o cadastro do CRM é a pessoa, na
    # aba Acervo ("selecionar e importar"), e o status de `processes` continua
    # sendo do gatilho do banco — duas fontes brigando pela mesma coluna era o
    # risco apontado na avaliação de 24/09.
    cadastrar_auto: bool
    atualizar_status: bool


def carregar() -> Config:
    url = os.environ.get("SUPABASE_URL") or os.environ.get("VITE_SUPABASE_URL") or ""
    return Config(
        supabase_url=url.rstrip("/"),
        supabase_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        datajud_key=os.environ.get("DATAJUD_API_KEY") or _DATAJUD_CHAVE_PUBLICA,
        banco=Path(os.environ.get("JURIUS_BANCO", "dados/cerebro.sqlite3")),
        token_api=os.environ.get("JURIUS_TOKEN_API", ""),
        djen_inicio=os.environ.get("DJEN_INICIO", "2023-01-01"),
        publicar=os.environ.get("JURIUS_PUBLICAR", "0") == "1",
        cadastrar_auto=os.environ.get("JURIUS_CADASTRAR_AUTO", "0") == "1",
        atualizar_status=os.environ.get("JURIUS_ATUALIZAR_STATUS", "0") == "1",
    )
