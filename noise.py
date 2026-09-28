
import re
import unicodedata

__all__ = ["is_noise", "clean_segments", "NOISE_PHRASES"]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

def _norm(text) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = _NON_ALNUM.sub(" ", t)
    return t.strip()

_RAW_EXACT = (
    "Amém", "Amém amém", "Amém amém amém",
    "Obrigado", "Obrigada", "Muito obrigado", "Muito obrigada",
    "Obrigado a todos", "Muito obrigado a todos", "Obrigado a todos vocês",
    "Obrigado pessoal", "Obrigado gente", "Obrigado galera",
    "Obrigado obrigado", "Obrigado obrigado obrigado",
    "Obrigado e até a próxima", "Obrigado e até logo", "Obrigado por tudo",
    "Obrigado por assistir", "Obrigada por assistir", "Obrigado por assistirem",
    "Obrigado por assistir ao vídeo", "Obrigado por assistir até a próxima",
    "Obrigado por assistir e até a próxima", "Obrigado por ver",
    "Obrigado por ver o vídeo", "Obrigado por ver até a próxima",
    "Valeu", "Valeu pessoal", "Valeu galera",
    "Tchau", "Tchau tchau", "Tchau pessoal", "Tchau gente",
    "Até mais", "Até a próxima", "Até logo", "Até breve",
    "Até o próximo vídeo", "Até o próximo", "Nos vemos no próximo vídeo",
    "Nos vemos na próxima",
    "Inscreva-se", "Inscreva-se no canal", "Se inscreva no canal",
    "Não se esqueça de se inscrever", "Não se esqueça de se inscrever no canal",
    "Não se esqueça de curtir e se inscrever",
    "Deixe seu like", "Deixe o seu like", "Curta o vídeo",
    "Compartilhe o vídeo", "Ative o sininho",
    "Fim do vídeo",
    "Thank you", "Thanks", "Thank you very much", "Thanks a lot",
    "Bye", "Bye bye", "Goodbye", "You", "The end",
    "Subscribe", "Like and subscribe", "See you next time",
    "See you in the next video",
)

_RAW_PREFIX = (
    "Legendas pela comunidade Amara",
    "Legendado pela comunidade Amara",
    "Legenda pela comunidade Amara",
    "Legendas pela Amara",
    "Legendas pelo Amara",
    "Legendas Amara org",
    "Tradução e legendas pela comunidade Amara",
    "Subtitles by the Amara",
    "Subtitles by Amara",
    "Subtitles by",
    "Subtitle by",
    "Transcribed by",
    "Transcription by",
    "Thanks for watching",
    "Thank you for watching",
    "Please subscribe",
    "Please like and subscribe",
    "Don't forget to subscribe",
)

_EXACT = frozenset(_norm(p) for p in _RAW_EXACT) - {""}
_PREFIX = tuple(sorted({_norm(p) for p in _RAW_PREFIX} - {""}, key=len, reverse=True))

NOISE_PHRASES = frozenset(_EXACT | set(_PREFIX))

def is_noise(text) -> bool:
    n = _norm(text)
    if not n:
        return True
    if n in _EXACT:
        return True
    for p in _PREFIX:
        if n == p or n.startswith(p + " "):
            return True
    return False

def _seg_text(seg) -> str:
    if isinstance(seg, str):
        return seg
    if isinstance(seg, dict):
        return seg.get("text", "") or ""
    return getattr(seg, "text", "") or ""

def clean_segments(segs):
    return [s for s in (segs or []) if not is_noise(_seg_text(s))]

if __name__ == "__main__":
    import sys

    DROP = [
        "Amém.",
        "Obrigado por assistir!",
        "Legendas pela comunidade Amara.org",
        "Obrigado.",
        "Tchau",
        "Inscreva-se no canal",
        "♪",
        "♪♪♪",
        "...",
        "Thanks for watching!",
        "Subtitles by the Amara.org community",
    ]
    KEEP = [
        "Muito obrigado pela ajuda com o IDOC",
        "Amém, mas voltando ao cronograma...",
        "Vou me inscrever no relatório amanhã",
        "O cão do vizinho latiu de novo",
        "Obrigado por assistir à apresentação de hoje e por trazer os números",
        "Sim, pode seguir com o deploy",
    ]

    ok = True
    print("== DEVE DERRUBAR (is_noise == True) ==")
    for t in DROP:
        r = is_noise(t)
        ok &= r
        print(f"  [{'PASS' if r else 'FAIL'}] is_noise={r!s:5}  {t!r}")
    print("== DEVE MANTER (is_noise == False) ==")
    for t in KEEP:
        r = is_noise(t)
        ok &= (not r)
        print(f"  [{'PASS' if not r else 'FAIL'}] is_noise={r!s:5}  {t!r}")

    demo = [
        {"start": 0.0, "end": 1.0, "text": "Amém."},
        {"start": 1.0, "end": 3.0, "text": "Muito obrigado pela ajuda com o IDOC"},
        {"start": 3.0, "end": 4.0, "text": "♪"},
        {"start": 4.0, "end": 6.0, "text": "Amém, mas voltando ao cronograma..."},
    ]
    cleaned = clean_segments(demo)
    kept_txt = [s["text"] for s in cleaned]
    demo_ok = kept_txt == [
        "Muito obrigado pela ajuda com o IDOC",
        "Amém, mas voltando ao cronograma...",
    ]
    ok &= demo_ok
    print("== clean_segments ==")
    print(f"  [{'PASS' if demo_ok else 'FAIL'}] {len(demo)} -> {len(cleaned)} | mantidos: {kept_txt}")
    print(f"\n#blocklist: {len(_EXACT)} exact + {len(_PREFIX)} prefix = {len(NOISE_PHRASES)} total")
    print("RESULT:", "ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)
