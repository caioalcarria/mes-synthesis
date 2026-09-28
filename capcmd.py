
from __future__ import annotations

import re

__all__ = ["apply", "DEFAULT", "STRICT"]

DEFAULT = "default"
STRICT  = "strict"

_OQ = ""
_CQ = ""

_VIRG   = r"v[ií]rgula"
_INTERR = r"interroga(?:ç|c)[aã]o"
_EXCL   = r"exclama(?:ç|c)[aã]o"
_PARENT = r"par[eê]ntese(?:s)?"
_PARAG  = r"par[aá]grafo"
_TRAV   = r"travess[aã]o"
_RETIC  = r"retic[eê]ncias"
_ABRE   = r"(?:abre|abrir|abra)"
_FECHA  = r"(?:fecha|fechar|feche)"

def _c(pattern: str):
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)

_RULES = [
    (_c(r"\bponto\s+de\s+" + _INTERR + r"\b"),      "?"),
    (_c(r"\bponto\s+de\s+" + _EXCL   + r"\b"),      "!"),
    (_c(r"\bponto\s+e\s+"  + _VIRG   + r"\b"),      ";"),
    (_c(r"\bponto\s+final\b"),                      "."),
    (_c(r"\bdois\s+pontos\b"),                      ":"),
    (_c(r"\bquebra\s+de\s+linha\b"),                "\n"),
    (_c(r"\bnova\s+linha\b"),                       "\n"),
    (_c(r"\bnovo\s+" + _PARAG + r"\b"),             "\n\n"),
    (_c(r"\b" + _ABRE  + r"\s+" + _PARENT + r"\b"), "("),
    (_c(r"\b" + _FECHA + r"\s+" + _PARENT + r"\b"), ")"),
    (_c(r"\b" + _ABRE  + r"\s+aspas\b"),            _OQ),
    (_c(r"\b" + _FECHA + r"\s+aspas\b"),            _CQ),
    (_c(r"\b" + _RETIC + r"\b"),                    "…"),
    (_c(r"\b" + _TRAV  + r"\b"),                    "—"),
    (_c(r"\b" + _INTERR + r"\b"),                   "?"),
    (_c(r"\b" + _EXCL   + r"\b"),                   "!"),
    (_c(r"\b" + _VIRG   + r"\b"),                   ","),
]

_PONTO = _c(r"\bponto\b")

_CLOSE = r",.;:!?)…" + _CQ
_OPEN  = r"(" + _OQ
_LET   = r"A-Za-zÀ-ÿ"

def _fix_spacing(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"[ \t]+([" + _CLOSE + r"])", r"\1", s)
    s = re.sub(r"([" + _OPEN + r"])[ \t]+", r"\1", s)
    s = re.sub(r"([" + _CLOSE + r"])(?=[" + _LET + _OPEN + r"])", r"\1 ", s)
    s = re.sub(r"(?<=[" + _LET + _CLOSE + r"])([" + _OPEN + r"])", r" \1", s)
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s

def _capitalize(s: str) -> str:
    s = re.sub(r"^(\W*)(\w)", lambda m: m.group(1) + m.group(2).upper(), s)
    s = re.sub(r"([.!?\n][^\w\n]*)(\w)", lambda m: m.group(1) + m.group(2).upper(), s)
    return s

def apply(text: str, mode: str = DEFAULT) -> str:
    if not text:
        return text
    s = text
    for rx, rep in _RULES:
        s = rx.sub(lambda m, r=rep: r, s)
    if mode == STRICT or mode is True:
        s = _PONTO.sub(".", s)
    s = _fix_spacing(s)
    s = s.replace(_OQ, '"').replace(_CQ, '"')
    s = s.strip()
    s = _capitalize(s)
    return s

def _selftest() -> int:
    cases = [
        (DEFAULT, "olá vírgula tudo bem ponto de interrogação nova linha próximo item",
                  "Olá, tudo bem?\nPróximo item"),
        (DEFAULT, "bom dia ponto novo parágrafo tudo certo ponto final",
                  "Bom dia ponto\n\nTudo certo."),
        (STRICT,  "bom dia ponto novo parágrafo tudo certo ponto final",
                  "Bom dia.\n\nTudo certo."),
        (DEFAULT, "abre aspas relatório fecha aspas dois pontos ok",
                  '"Relatório": ok'),
        (DEFAULT, "primeiro item ponto e vírgula segundo item reticências fim travessão continua",
                  "Primeiro item; segundo item… fim — continua"),
        (DEFAULT, "atenção exclamação abre parênteses urgente fecha parênteses",
                  "Atenção! (Urgente)"),
        (DEFAULT, "vou apontar o compromisso pontual no ponto de encontro",
                  "Vou apontar o compromisso pontual no ponto de encontro"),
        (STRICT,  "isso é um ponto",
                  "Isso é um."),
        (STRICT,  "vou apontar o compromisso pontual no ponto de encontro",
                  "Vou apontar o compromisso pontual no. De encontro"),
    ]
    ok = 0
    for i, (mode, src, exp) in enumerate(cases, 1):
        got = apply(src, mode)
        passed = (got == exp)
        ok += passed
        print(f"[{'PASS' if passed else 'FAIL'}] #{i} mode={mode}")
        print(f"    in : {src!r}")
        print(f"    out: {got!r}")
        if not passed:
            print(f"    EXP: {exp!r}")
    print(f"\n{ok}/{len(cases)} passaram.")
    return 0 if ok == len(cases) else 1

if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
