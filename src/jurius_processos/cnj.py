"""Número único do processo (Resolução CNJ 65/2008): NNNNNNN-DD.AAAA.J.TR.OOOO.

É a chave que amarra DJEN, DataJud e CRM. Tudo que entra no sistema passa por
`limpar()` e só é aceito se o dígito verificador fecha.
"""

from __future__ import annotations

import re

_MENCAO = re.compile(r"(?<!\d)(\d{7})-?(\d{2})\.?(\d{4})\.?(\d)\.?(\d{2})\.?(\d{4})(?!\d)")


def limpar(numero: str | None) -> str | None:
    """Devolve os 20 dígitos, ou None se não for um número CNJ válido."""
    if not numero:
        return None
    digitos = re.sub(r"\D", "", numero)
    if len(digitos) != 20 or not digito_confere(digitos):
        return None
    return digitos


def digito_confere(d: str) -> bool:
    # DV = 98 - (NNNNNNN AAAA J TR OOOO 00 mod 97)
    base = d[0:7] + d[9:13] + d[13] + d[14:16] + d[16:20]
    return int(d[7:9]) == 98 - (int(base + "00") % 97)


def formatar(d: str) -> str:
    return f"{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}"


def mencoes(texto: str | None) -> set[str]:
    """Números CNJ válidos citados num texto (ex.: "processo de origem nº ...")."""
    if not texto:
        return set()
    achados = set()
    for m in _MENCAO.finditer(texto):
        d = "".join(m.groups())
        if digito_confere(d):
            achados.add(d)
    return achados


_ESTADUAIS = {
    1: "tjac", 2: "tjal", 3: "tjap", 4: "tjam", 5: "tjba", 6: "tjce", 7: "tjdft",
    8: "tjes", 9: "tjgo", 10: "tjma", 11: "tjmt", 12: "tjms", 13: "tjmg", 14: "tjpa",
    15: "tjpb", 16: "tjpr", 17: "tjpe", 18: "tjpi", 19: "tjrj", 20: "tjrn", 21: "tjrs",
    22: "tjro", 23: "tjrr", 24: "tjsc", 25: "tjsp", 26: "tjse", 27: "tjto",
}
_UF = ["ac", "al", "am", "ap", "ba", "ce", "dft", "es", "go", "ma", "mg", "ms", "mt",
       "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc", "se", "sp", "to"]
_ELEITORAIS = {i + 1: f"tre-{uf}" for i, uf in enumerate(_UF)}
_MILITARES = {13: "tjmmg", 21: "tjmrs", 25: "tjmsp"}


def indice_datajud(d: str) -> str | None:
    """Índice da API pública do DataJud onde o processo mora (segmento J + tribunal TR)."""
    j, tr = d[13], int(d[14:16])
    nome = None
    if j == "8":
        nome = _ESTADUAIS.get(tr)
    elif j == "4":
        nome = f"trf{tr}" if 1 <= tr <= 6 else None
    elif j == "5":
        nome = "tst" if tr == 0 else (f"trt{tr}" if 1 <= tr <= 24 else None)
    elif j == "6":
        nome = "tse" if tr == 0 else _ELEITORAIS.get(tr)
    elif j == "3":
        nome = "stj"
    elif j == "7":
        nome = "stm"
    elif j == "9":
        nome = _MILITARES.get(tr)
    return f"api_publica_{nome}" if nome else None


def ano(d: str) -> int:
    return int(d[9:13])
