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
    # Alimentar o CRM no lugar das rotinas do Supabase (crons 5, 16 e 11).
    # Cada um liga sozinho; desligado, a rotina antiga continua valendo.
    alimentar_intimacoes: bool
    alimentar_datajud: bool
    alimentar_ia: bool
    deepseek_key: str
    deepseek_modelo: str


def _limpo(nome: str, padrao: str = "") -> str:
    """Valor de variável sem aspas nem espaço/quebra de linha nas pontas.

    Colar a chave no Portainer com aspas ou com um espaço no fim fazia o
    Supabase responder 401 sem pista nenhuma do porquê.
    """
    return (os.environ.get(nome) or padrao).strip().strip('"').strip("'").strip()


def cabecalhos_supabase(chave: str) -> dict:
    """Cabeçalhos de autenticação do PostgREST para a chave de serviço.

    As chaves novas do painel do Supabase (`sb_secret_…`) NÃO são JWT: vão só
    em `apikey`. Mandá-las também como `Authorization: Bearer` dá 401. A
    service_role antiga (JWT, `eyJ…`) vai nos dois.
    """
    if chave.startswith("sb_"):
        return {"apikey": chave}
    return {"apikey": chave, "Authorization": f"Bearer {chave}"}


def carregar() -> Config:
    url = _limpo("SUPABASE_URL") or _limpo("VITE_SUPABASE_URL")
    return Config(
        supabase_url=url.rstrip("/"),
        supabase_key=_limpo("SUPABASE_SERVICE_ROLE_KEY"),
        datajud_key=os.environ.get("DATAJUD_API_KEY") or _DATAJUD_CHAVE_PUBLICA,
        banco=Path(os.environ.get("JURIUS_BANCO", "dados/cerebro.sqlite3")),
        token_api=_limpo("JURIUS_TOKEN_API"),
        djen_inicio=os.environ.get("DJEN_INICIO", "2023-01-01"),
        publicar=os.environ.get("JURIUS_PUBLICAR", "0") == "1",
        cadastrar_auto=os.environ.get("JURIUS_CADASTRAR_AUTO", "0") == "1",
        atualizar_status=os.environ.get("JURIUS_ATUALIZAR_STATUS", "0") == "1",
        alimentar_intimacoes=_limpo("JURIUS_ALIMENTAR_INTIMACOES", "0") == "1",
        alimentar_datajud=_limpo("JURIUS_ALIMENTAR_DATAJUD", "0") == "1",
        alimentar_ia=_limpo("JURIUS_ALIMENTAR_IA", "0") == "1",
        deepseek_key=_limpo("DEEPSEEK_API_KEY"),
        # A mesma calibração da analyze-intimations: o degrau "rápido" da escada.
        deepseek_modelo=_limpo("DEEPSEEK_MODELO", "deepseek-flash"),
    )
