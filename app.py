import os, re, sys, time, signal, threading, subprocess, tempfile, shutil
from pathlib import Path
from datetime import datetime

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from noise import is_noise
except Exception:
    def is_noise(_t): return False
try:
    import capcmd
except Exception:
    capcmd = None
from evdev import InputDevice, list_devices, ecodes

MODEL       = os.environ.get("MES_MODEL", "large-v3")
LANG        = "pt"
KEY_TRIGGER = 193
KEY_SHOT    = 57
KEY_ALTS    = (56, 100)
KEY_CTRLS   = (29, 97)
WATCH_KEYS  = (KEY_TRIGGER, KEY_SHOT) + KEY_ALTS + KEY_CTRLS
DEBOUNCE_S  = 0.6
SHOT_DEBOUNCE_S = 0.8
PRINTS_SUFFIX   = " - prints"
CASTS_SUFFIX    = " - telas"
CAST_DEBOUNCE_S = 1.2
CAST_FPS        = 30
DUCK_PCT        = 30
DUCK_FADE_S     = 0.40
DUCK_FADE_STEPS = 16

CLAUDE_BIN     = os.environ.get("MES_CLAUDE", str(Path.home()/".local/bin/claude"))
CLAUDE_MODEL   = "opus"
CLAUDE_EFFORT  = "high"
CLAUDE_TIMEOUT = 900
CLAUDE_MAXCH   = 150_000
ANALYSIS_PROMPT = """Você é o Claude Code rodando DENTRO do projeto {proj} do Caio \
(engenheiro de software SAP/MII na Accenture). Acabou de sair a synthesis de uma sessao \
deste projeto, salva em:
    {rel}

Nas falas, "Eu:" é o Caio e "Participante:" são as outras pessoas. Linhas "📷 print:" e
"🎥 tela:" apontam pra imagens e vídeos capturados no momento exato da conversa — os
caminhos são relativos à pasta da sessao e você pode ABRIR os prints com o Read se
ajudarem a entender o que estava na tela.

Leia a synthesis abaixo E o projeto (você tem Read, Grep e Glob) e produza uma análise
do QUE TEM QUE SER FEITO. Responda em MARKDOWN, português do Brasil, com estas seções:

## O que foi pedido
## O que isso significa neste projeto
Aponte os ARQUIVOS e funções reais que precisam mudar (use caminho:linha quando der).
## Plano de ação
- [ ] passos concretos, na ordem de execução
## Dúvidas e riscos
O que ficou ambíguo na sessao e precisa ser confirmado antes de mexer.

Regras: seja FIEL à synthesis, não invente nada. Se algo não ficou claro, escreva
"(não ficou claro na sessao)". Só afirme coisas sobre o código com base em arquivos que você
REALMENTE leu. Sem preâmbulo — comece direto no "## O que foi pedido".

SYNTHESIS:
"""
LAT_CAP_MS = 30
LAT_MEET_MS = 200
NAME_TIMEOUT_S = 180
HIST_MAX = 15
CAP_PROMPT = "Synthesis em português do Brasil, com pontuação e acentuação corretas."
TMP_DIR     = Path(tempfile.gettempdir()) / "mes-synthesis"; TMP_DIR.mkdir(parents=True, exist_ok=True)
ICONS_DIR   = str(Path(__file__).resolve().parent / "icons")
STATE_SINK  = Path("/tmp/mes_real_sink.txt")
STATE_SRC   = Path("/tmp/mes_real_src.txt")
STATE_DUCK  = Path("/tmp/mes_duck.txt")

BACKUP_BASE = Path.home() / "Downloads/DEL/PROJECTS"
LOCAL_BASE  = Path.home() / "PROJECTS"
PROJECTS = [
    ("accenture", "Accenture",  "ACCENTURE"), ("melitta", "Melitta", "MELITTA"),
    ("multi", "Multilaser", "MULTI"), ("tekno", "Tekno", "TEKNO"),
    ("cmpc", "CMPC", "CMPC"), ("cvale", "CVale", "CVale"), ("ufba", "UFBA", "UFBA"),
]

def log(*a): print(f"{time.strftime('%H:%M:%S')}", *a, file=sys.stderr, flush=True)
def _sh(*args):
    try: return subprocess.check_output(list(args), text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return ""

def _has_source(name):
    return name in _sh("pactl", "list", "short", "sources")

def _short(kind):
    out=[]
    for l in _sh("pactl", "list", "short", kind).splitlines():
        p=l.split("\t")
        if len(p)>1: out.append((p[0], p[1]))
    return out

BT_MIC_PROFILES  = ("headset-head-unit-msbc", "headset-head-unit", "headset-head-unit-cvsd")
BT_HIFI_PROFILES = ("a2dp-sink", "a2dp-sink-sbc_xq")

def bt_card():
    for _, name in _short("cards"):
        if name.startswith("bluez_card."): return name
    return None

def _bt_node(card, kind):
    mac = card.split(".", 1)[1]
    pref = ("bluez_input." if kind == "sources" else "bluez_output.") + mac
    for _, name in _short(kind):
        if name.startswith(pref): return name
    return None

def _set_profile(card, prof):
    try:
        return subprocess.run(["pactl", "set-card-profile", card, prof],
                              capture_output=True, text=True, timeout=3).returncode == 0
    except Exception: return False

def bt_mic_on(card):
    src = _bt_node(card, "sources")
    if src: return src
    for prof in BT_MIC_PROFILES:
        if not _set_profile(card, prof): continue
        t0 = time.time()
        while time.time() - t0 < 1.0:
            src = _bt_node(card, "sources")
            if src:
                log(f"[bt] {prof} → gravando pelo mic do fone ({src})")
                return src
            time.sleep(0.01)
    log("[bt] nenhum perfil com microfone pegou — seguindo com o mic do PC")
    return None

_bt_last_fix = 0.0

def _bt_a2dp(card):
    for prof in BT_HIFI_PROFILES:
        if _set_profile(card, prof):
            log(f"[bt] {prof} → som do fone de volta em alta fidelidade"); return True
    return False

def _bt_reconnect(card):
    global _bt_last_fix
    if time.time() - _bt_last_fix < 60: return
    _bt_last_fix = time.time()
    mac = card.split(".", 1)[1].replace("_", ":")
    log("[bt] o A2DP sumiu da lista — reconectando o fone pra trazer de volta")
    try:
        subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=20)
    except Exception as e:
        log("[bt] reconexão falhou:", repr(e)[:120]); return
    for _ in range(60):
        time.sleep(0.25)
        c = bt_card()
        if c and _bt_a2dp(c): return
    log("[bt] reconectei e mesmo assim o A2DP não voltou")

def _bt_mic_ocupado(card):
    src = _bt_node(card, "sources")
    if not src: return False
    idx = next((i for i, n in _short("sources") if n == src), None)
    return idx is not None and any(sidx == idx for _, sidx in _short("source-outputs"))

def bt_hifi():
    card = bt_card()
    if not card or not _bt_node(card, "sources"): return
    if _bt_mic_ocupado(card):
        log("[bt] outro app está usando o mic do fone (sessao?) — deixo o perfil como está")
        return
    if _bt_a2dp(card): return
    threading.Thread(target=_bt_reconnect, args=(card,), daemon=True).start()

def _pick_master(kind):
    d = _sh("pactl", "get-default-source" if kind == "sources" else "get-default-sink")
    return "" if (not d or d.startswith("bluez_")) else d

def audio_setup():
    try:
        if not _has_source("mic_limpo"):
            real_sink = _pick_master("sinks")
            real_src  = _pick_master("sources")
            if not real_sink or not real_src:
                log("[audio] fone BT é o padrão agora — echo-cancel fica pra quando ele sair")
                bt_hifi(); return
            STATE_SINK.write_text(real_sink); STATE_SRC.write_text(real_src)
            subprocess.run(["pactl", "load-module", "module-echo-cancel", "aec_method=webrtc",
                            "aec_args=noise_suppression=1 high_pass_filter=1 extended_filter=1 gain_control=1 voice_detection=1",
                            "source_name=mic_limpo", f"source_master={real_src}",
                            "sink_name=saida_call", f"sink_master={real_sink}",
                            "source_properties=device.description=MicLimpo",
                            "sink_properties=device.description=SaidaCall"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pactl", "set-source-volume", "mic_limpo", "100%"], check=False)
            log("[audio] module-echo-cancel carregado")
        subprocess.run(["pactl", "set-sink-volume", "saida_call", "100%"], check=False)
        if bt_card():
            bt_hifi()
            log("[audio] mic_limpo pronto; fone BT conectado — padrões do sistema mantidos")
        else:
            subprocess.run(["pactl", "set-default-sink", "saida_call"], check=False)
            subprocess.run(["pactl", "set-default-source", "mic_limpo"], check=False)
            log("[audio] mic_limpo + saida_call.monitor prontos (padrão)")
    except Exception as e:
        log("[audio] setup falhou (usando fontes padrão):", repr(e)[:120])

def audio_teardown():
    try:
        bt_hifi()
        if STATE_SINK.exists(): subprocess.run(["pactl", "set-default-sink", STATE_SINK.read_text().strip()], check=False)
        if STATE_SRC.exists():  subprocess.run(["pactl", "set-default-source", STATE_SRC.read_text().strip()], check=False)
        for line in _sh("pactl", "list", "short", "modules").splitlines():
            if "module-echo-cancel" in line:
                subprocess.run(["pactl", "unload-module", line.split("\t")[0]], check=False)
        log("[audio] restaurado")
    except Exception: pass

def mic_target(activate=False):
    card = bt_card()
    if card:
        src = bt_mic_on(card) if activate else _bt_node(card, "sources")
        if src: return src
    elif activate and not _has_source("mic_limpo"):
        audio_setup()
    return "mic_limpo" if _has_source("mic_limpo") else (_sh("pactl", "get-default-source") or None)

def sys_target():
    card = bt_card()
    if card:
        out = _bt_node(card, "sinks")
        if out and _sh("pactl", "get-default-sink") == out: return out + ".monitor"
    if _has_source("saida_call.monitor"): return "saida_call.monitor"
    s = _sh("pactl", "get-default-sink"); return (s + ".monitor") if s else None

def _pactl(*args, timeout=2.0):
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception as e:
        log("[volume] pactl falhou:", repr(e)[:120]); return ""

def _duck_targets():
    try: real = STATE_SINK.read_text().strip() if STATE_SINK.exists() else ""
    except Exception: real = ""
    targets = []
    for sink in (_pactl("get-default-sink"), real):
        if sink and sink not in targets: targets.append(sink)
    if real and real != "saida_call" and real in targets and "saida_call" in targets:
        targets.remove("saida_call")
    return targets

def _sink_vols(sink):
    return [int(v) for v in re.findall(r"(\d+)\s*/\s*\d+%", _pactl("get-sink-volume", sink))]

def _duck_alvo(vols):
    teto = round(65536 * DUCK_PCT / 100)
    pico = max(vols) if vols else 0
    if pico <= teto: return list(vols)
    return [round(v * teto / pico) for v in vols]

_fade_gen  = 0
_fade_lock = threading.RLock()

def _fade_begin():
    global _fade_gen
    with _fade_lock:
        _fade_gen += 1
        return _fade_gen

def _fade_run(plan, gen):
    t0 = time.monotonic()
    for i in range(1, DUCK_FADE_STEPS + 1):
        with _fade_lock:
            if gen != _fade_gen: return False
            for sink, a, b in plan:
                _pactl("set-sink-volume", sink,
                       *[str(round(x + (y - x) * i / DUCK_FADE_STEPS)) for x, y in zip(a, b)])
        dorme = t0 + DUCK_FADE_S * i / DUCK_FADE_STEPS - time.monotonic()
        if dorme > 0 and i < DUCK_FADE_STEPS: time.sleep(dorme)
    return True

def _duck_pendente():
    try:
        if not STATE_DUCK.exists(): return None
        saved = [(ln.split()[0], [int(x) for x in ln.split()[1:]])
                 for ln in STATE_DUCK.read_text().splitlines() if len(ln.split()) >= 2]
        return saved or None
    except Exception: return None

def _duck_limpa():
    try: STATE_DUCK.unlink()
    except Exception: pass

def duck_start():
    saved = _duck_pendente()
    if not saved:
        saved = [(sink, vols) for sink, vols in ((s, _sink_vols(s)) for s in _duck_targets())
                 if vols and _duck_alvo(vols) != vols]
    if not saved: return None
    try: STATE_DUCK.write_text("\n".join(k + " " + " ".join(map(str, v)) for k, v in saved))
    except Exception: pass
    gen = _fade_begin()
    plan = [(sink, _sink_vols(sink) or list(vols), _duck_alvo(vols)) for sink, vols in saved]
    threading.Thread(target=_fade_run, args=(plan, gen), daemon=True).start()
    log(f"[volume] descendo pra {DUCK_PCT}% durante o mes-synthesis (era " +
        ", ".join(str(v[0]) for _, v in saved) + ")")
    return saved

def duck_stop(saved, fade=True):
    if not saved: return
    gen = _fade_begin()
    if not fade:
        with _fade_lock:
            _fade_begin()
            for sink, vols in saved: _pactl("set-sink-volume", sink, *map(str, vols))
        _duck_limpa(); log("[volume] devolvido:", ", ".join(str(v[0]) for _, v in saved))
        return
    plan = [(sink, _sink_vols(sink) or list(vols), list(vols)) for sink, vols in saved]
    def sobe():
        if _fade_run(plan, gen): _duck_limpa()
    threading.Thread(target=sobe, daemon=True).start()
    log("[volume] devolvendo:", ", ".join(str(v[0]) for _, v in saved))

def duck_recover():
    saved = _duck_pendente()
    if saved: duck_stop(saved, fade=False); log("[volume] duck órfão desfeito")
    else: _duck_limpa()

def cap_wav():
    return TMP_DIR / f"rec_{time.time_ns()}.wav"

def rec_start(wav, target=None, latency_ms=LAT_MEET_MS):
    args = ["parecord", "--rate=16000", "--channels=1", "--format=s16le",
            f"--latency-msec={latency_ms}"]
    if target: args.append(f"--device={target}")
    args.append(str(wav))
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def rec_stop(p):
    if not p: return
    try: p.send_signal(signal.SIGINT); p.wait(timeout=3)
    except Exception:
        try: p.kill()
        except Exception: pass

def normalize(src, dst):
    r = subprocess.run(["ffmpeg", "-y", "-i", str(src), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-ar", "16000", "-ac", "1", str(dst)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst if (r.returncode == 0 and Path(dst).exists()) else src

def safe_name(name, fallback):
    n = re.sub(r"[/\\\x00-\x1f]", " ", name or "")
    n = re.sub(r"\s+", " ", n).strip(" .")
    return n[:120] or fallback

def unique_dir(parent, stem):
    d = parent / stem
    i = 2
    while d.exists():
        d = parent / f"{stem} ({i})"; i += 1
    return d

def notify(title, body=""):
    try:
        subprocess.Popen(["notify-send", "-a", "mes-synthesis",
                          "-i", str(Path(ICONS_DIR)/"mes-blue.png"), title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception: pass

def fmt_stamp(rel):
    sec=int(max(0.0, rel)); h,r=divmod(sec,3600); m,sec=divmod(r,60)
    return f"{h:02d}-{m:02d}-{sec:02d}"

def grab_screen(dst):
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        r=subprocess.run(["gnome-screenshot", "-f", str(dst)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode==0 and dst.exists() and dst.stat().st_size>1000
    except Exception as e:
        log("[print] captura falhou:", repr(e)[:120]); return False

def claude_analyze(project_dir, content, out_md, proj):
    try:
        text=(content or "").strip()
        if len(text) < 40:
            log("[claude] synthesis curta demais — não enviei"); return False
        if len(text) > CLAUDE_MAXCH:
            text=text[:CLAUDE_MAXCH]+"\n[...synthesis cortada por tamanho...]"
        if not Path(CLAUDE_BIN).exists() and not shutil.which("claude"):
            log(f"[claude] binário não encontrado ({CLAUDE_BIN})"); return False
        exe=CLAUDE_BIN if Path(CLAUDE_BIN).exists() else shutil.which("claude")
        rel=str(Path(out_md).parent.relative_to(project_dir)) if str(out_md).startswith(str(project_dir)) else str(Path(out_md).parent)
        prompt=ANALYSIS_PROMPT.format(proj=proj, rel=rel)+text
        cmd=[exe, "-p", "--output-format", "text",
             "--model", CLAUDE_MODEL, "--effort", CLAUDE_EFFORT,
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--allowed-tools", "Read Grep Glob",
             "--permission-mode", "dontAsk"]
        log(f"[claude] enviando ({len(text)} ch) — {CLAUDE_MODEL}/{CLAUDE_EFFORT} em {project_dir}")
        t0=time.time()
        r=subprocess.run(cmd, input=prompt, cwd=str(project_dir), capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=CLAUDE_TIMEOUT)
        out=(r.stdout or "").strip()
        if r.returncode!=0 or len(out)<40:
            log(f"[claude] falhou rc={r.returncode} len={len(out)} err={(r.stderr or '')[-200:]}")
            return False
        Path(out_md).write_text(out+"\n", encoding="utf-8")
        log(f"[claude] análise salva em {time.time()-t0:.0f}s → {Path(out_md).name} ({len(out)} ch)")
        return True
    except subprocess.TimeoutExpired:
        log(f"[claude] estourou o timeout de {CLAUDE_TIMEOUT}s"); return False
    except Exception as e:
        log("[claude] erro:", repr(e)[:180]); return False

class Screencast:
    BUS = ("org.gnome.Shell.Screencast", "/org/gnome/Shell/Screencast",
           "org.gnome.Shell.Screencast")

    def __init__(self):
        self.conn=None

    def _call(self, method, params=None, sig=None):
        from gi.repository import Gio
        if self.conn is None:
            self.conn=Gio.bus_get_sync(Gio.BusType.SESSION, None)
        name, path, iface = self.BUS
        return self.conn.call_sync(name, path, iface, method,
                                   GLib.Variant(sig, params) if sig else None,
                                   None, Gio.DBusCallFlags.NONE, -1, None)

    @staticmethod
    def _pipeline(mic, sysm):
        legs=[]
        for dev in (mic, sysm):
            if dev: legs.append(f"pulsesrc device={dev} ! audioconvert ! audioresample ! amix. ")
        if not legs:
            return ("videoconvert ! queue ! vp8enc cpu-used=16 max-quantizer=17 deadline=1 "
                    "! queue ! webmmux name=mux")
        return ("videoconvert ! queue ! vp8enc cpu-used=16 max-quantizer=17 deadline=1 "
                "! queue ! mux. " + "".join(legs) +
                "audiomixer name=amix ! audioconvert ! opusenc ! queue ! mux. webmmux name=mux")

    def start(self, template):
        try:
            opts={"framerate": GLib.Variant("i", CAST_FPS),
                  "draw-cursor": GLib.Variant("b", True),
                  "pipeline": GLib.Variant("s", self._pipeline(mic_target(), sys_target()))}
            ok, used = self._call("Screencast", (str(template), opts), "(sa{sv})").unpack()
            if not ok:
                log("[tela] o GNOME recusou iniciar (outra gravação de tela ativa?)")
                return None
            return Path(used)
        except Exception as e:
            log("[tela] falha ao iniciar:", repr(e)[:160]); return None

    def stop(self):
        try:
            return bool(self._call("StopScreencast").unpack()[0])
        except Exception as e:
            log("[tela] falha ao parar:", repr(e)[:160]); return False

    @staticmethod
    def finish(src, dst):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            r=subprocess.run(["ffmpeg","-y","-hide_banner","-loglevel","error",
                              "-i",str(src),"-c","copy",str(dst)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            if r.returncode==0 and dst.exists() and dst.stat().st_size>1000:
                try: Path(src).unlink()
                except Exception: pass
                return True
            log("[tela] remux falhou — salvando o arquivo cru")
            shutil.move(str(src), str(dst)); return True
        except Exception as e:
            log("[tela] finish falhou:", repr(e)[:160])
            try: shutil.move(str(src), str(dst)); return True
            except Exception: return False

def clean_session_tmp():
    for n in ("m_mic.wav", "m_sys.wav", "m_mic_n.wav", "m_sys_n.wav"):
        try: (TMP_DIR/n).unlink()
        except Exception: pass
    for pat in ("shot_*.png", "cast_*"):
        for p in TMP_DIR.glob(pat):
            try: p.unlink()
            except Exception: pass

def list_recordings(limit=HIST_MAX):
    out=[]
    for _key, name, d in PROJECTS:
        calls = LOCAL_BASE/d/"SESSIONS"
        if not calls.is_dir(): continue
        try: entries = list(calls.iterdir())
        except OSError: continue
        for path in entries:
            if not path.is_dir(): continue
            try: out.append((path.stat().st_mtime, name, path))
            except OSError: pass
    out.sort(key=lambda t: t[0], reverse=True)
    return out[:limit]

def open_path(path):
    subprocess.Popen(["xdg-open", str(path)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def human_dur(secs):
    m=int(secs//60); sec=int(secs%60)
    return f"{m} min {sec:02d}s" if m else f"{sec}s"

def fmt_ts(t):
    t = max(0, t); h=int(t//3600); m=int((t%3600)//60); s=int(t%60); ms=int(round((t-int(t))*1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def paste_text(text):
    if not text: log("[paste] vazio"); return
    subprocess.run(["wl-copy", "--", text], check=False)
    time.sleep(0.12)
    subprocess.run(["ydotool", "key", "ctrl+v"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

class Engine:
    def __init__(self): self.model=None; self.ready=False
    def load(self):
        from faster_whisper import WhisperModel
        import numpy as np
        for dev, comp in (("cuda","float16"),("cuda","int8_float16"),("cpu","int8")):
            try:
                log(f"[engine] carregando {MODEL} em {dev}/{comp} ...")
                m = WhisperModel(MODEL, device=dev, compute_type=comp)
                m.transcribe(np.zeros(16000, np.float32), language=LANG, beam_size=1)
                self.model=m; self.ready=True; log(f"[engine] pronto em {dev}/{comp}"); return
            except Exception as e:
                log(f"[engine] {dev}/{comp} falhou: {repr(e)[:160]}")
        raise RuntimeError("sem backend de synthesis")

    def _segments(self, wav, with_ts):
        segs,_ = self.model.transcribe(
            str(wav), language=LANG, beam_size=5, initial_prompt=CAP_PROMPT,
            without_timestamps=(not with_ts), condition_on_previous_text=False, vad_filter=True)
        out=[]
        for s in segs:
            t=(s.text or "").strip()
            if not t: continue
            nsp=getattr(s,"no_speech_prob",0.0) or 0.0; alp=getattr(s,"avg_logprob",0.0) or 0.0
            if nsp>0.6 and alp<-0.7: continue
            if is_noise(t): continue
            out.append((float(getattr(s,"start",0.0)), float(getattr(s,"end",0.0)), t))
        return out

    def capture(self, wav):
        text=" ".join(" ".join(t for _,_,t in self._segments(wav, False)).split()).strip()
        if capcmd and text:
            try: text=capcmd.apply(text)
            except Exception: pass
        return text

    def track(self, wav, speaker):
        return [(a,b,speaker,t) for a,b,t in self._segments(wav, True)]

class App:
    def __init__(self):
        self.engine=Engine()
        self.d_recording=False; self.d_proc=None; self.d_wav=None; self.last_trigger=0.0
        self.d_duck=None
        self.m_recording=False; self.m_mic=None; self.m_sys=None; self.m_proj=None
        self.m_calls=None; self.m_ts=None; self.m_t0=0.0
        self.m_shots=[]
        self.m_casts=[]
        self.cast=Screencast()
        self.c_active=False; self.c_file=None; self.c_rel0=0.0
        self.alt_down=False; self.ctrl_down=False
        self.last_shot=0.0; self.last_cast=0.0
        self.send_claude=False
        self.d_synth=0; self.m_synth=0
        self.lock=threading.Lock()
        self.gpu=threading.Lock()
        self.ind=None

    def _icon(self):
        if self.d_recording: return "mes-blue"
        if self.c_active: return "mes-red"
        if self.d_synth>0 or self.m_synth>0: return "mes-orange"
        if self.m_recording: return "mes-green"
        return "mes-white"
    def refresh_icon(self): GLib.idle_add(lambda: self.ind.set_icon_full(self._icon(), "mes-synthesis"))
    def build_menu(self):
        menu=Gtk.Menu()
        h=Gtk.MenuItem(label="Captura: tecla Copilot (TAP grava/cola) — vale durante a sessao")
        h.set_sensitive(False); menu.append(h)
        menu.append(Gtk.SeparatorMenuItem())
        if self.m_recording:
            it=Gtk.MenuItem(label=f"⏹  Parar e processar  ({self.m_proj[1]})")
            it.connect("activate", lambda _w: self.stop_session()); menu.append(it)
        else:
            sub=Gtk.Menu()
            for key,name,d in PROJECTS:
                mi=Gtk.MenuItem(label=name)
                mi.connect("activate", lambda _w,p=(key,name,d): self.start_session(p)); sub.append(mi)
            g=Gtk.MenuItem(label="🎙  Gravar sessao no projeto…"); g.set_submenu(sub); menu.append(g)
        menu.append(Gtk.SeparatorMenuItem())
        hsub=Gtk.Menu()
        hist=list_recordings()
        if hist:
            for mt, proj, path in hist:
                when=datetime.fromtimestamp(mt).strftime("%d/%m  %Hh%M")
                mi=Gtk.MenuItem(label=f"{when}   ·   {proj}   ·   {path.name}")
                mi.connect("activate", lambda _w, p=path: open_path(p))
                hsub.append(mi)
        else:
            e=Gtk.MenuItem(label="— nenhuma gravação ainda —"); e.set_sensitive(False); hsub.append(e)
        hi=Gtk.MenuItem(label="🕘  Histórico de gravações"); hi.set_submenu(hsub); menu.append(hi)
        menu.append(Gtk.SeparatorMenuItem())
        q=Gtk.MenuItem(label="Sair"); q.connect("activate", lambda _w: self.quit()); menu.append(q)
        menu.show_all(); return menu
    def refresh_menu(self): GLib.idle_add(lambda: self.ind.set_menu(self.build_menu()))

    def start_session(self, proj):
        if self.m_recording or self.m_synth>0: return
        key,name,d = proj
        calls=(LOCAL_BASE/d/"SESSIONS"); calls.mkdir(parents=True, exist_ok=True)
        ts=datetime.now().strftime("%Y-%m-%d %Hh%M")
        self.m_calls=calls; self.m_ts=ts; self.m_proj=proj; self.m_t0=time.time()
        self.m_shots=[]; self.m_casts=[]
        threading.Thread(target=self._ensure_copy, args=(d,), daemon=True).start()
        self.m_mic=rec_start(TMP_DIR/"m_mic.wav", mic_target(activate=True), LAT_MEET_MS)
        self.m_sys=rec_start(TMP_DIR/"m_sys.wav", sys_target(), LAT_MEET_MS)
        self.m_recording=True
        log(f"[session] gravando ({name}) → {calls}/sessao {ts}  (nome definido ao parar)")
        self.refresh_icon(); self.refresh_menu()

    def confirm_discard(self, secs):
        dlg=Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.NONE, text="Descartar esta gravação?")
        dlg.format_secondary_text(
            f"{human_dur(secs)} de áudio serão apagados agora e não há como recuperar. "
            "Nada será processado nem salvo no projeto.")
        dlg.add_button("Manter", Gtk.ResponseType.CANCEL)
        bd=dlg.add_button("Descartar", Gtk.ResponseType.OK)
        bd.get_style_context().add_class("destructive-action")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        dlg.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        dlg.set_keep_above(True)
        r=dlg.run(); dlg.destroy()
        return r==Gtk.ResponseType.OK

    def ask_name(self, default, proj, secs):
        dlg=Gtk.Dialog(title="Nomear gravação", modal=True)
        dlg.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        dlg.add_button("Salvar", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        dlg.set_keep_above(True); dlg.set_urgency_hint(True)

        box=dlg.get_content_area(); box.set_spacing(8)
        box.set_margin_top(14); box.set_margin_bottom(6)
        box.set_margin_start(16); box.set_margin_end(16)
        lb=Gtk.Label(); lb.set_markup(f"<b>Gravação encerrada · {GLib.markup_escape_text(proj)}</b>")
        lb.set_xalign(0.0); box.add(lb)
        hint=Gtk.Label(label="Como quer chamar esta gravação?"); hint.set_xalign(0.0)
        box.add(hint)
        entry=Gtk.Entry(text=default)
        entry.set_activates_default(True)
        entry.set_width_chars(46)
        box.add(entry)
        done={"s": False}
        def _sel(*_a):
            if not done["s"]:
                done["s"]=True; entry.select_region(0, -1)
            return False
        entry.connect("focus-in-event", _sel)
        chk=Gtk.CheckButton(label="Enviar pro Claude Code analisar quando terminar")
        chk.set_active(self.send_claude)
        chk.set_tooltip_text(
            f"Roda `claude -p` ({CLAUDE_MODEL}, esforço {CLAUDE_EFFORT}) dentro da pasta do "
            "projeto com a synthesis, e salva '<nome> - analise.md' com o que precisa ser "
            "feito. Só leitura — não altera o código.")
        box.add(chk)
        tip=Gtk.Label(); tip.set_xalign(0.0)
        tip.set_markup("<small>Enter salva  ·  <b>Cancelar descarta a gravação</b></small>")
        box.add(tip)

        result=""
        while True:
            fired={"t": False}
            def _timeout():
                fired["t"]=True
                log("[session] pop-up sem resposta — salvando com o que estava no campo")
                dlg.response(Gtk.ResponseType.OK); return False
            tid=GLib.timeout_add_seconds(NAME_TIMEOUT_S, _timeout)

            dlg.show_all(); dlg.present(); entry.grab_focus()
            resp=dlg.run()
            if not fired["t"]:
                try: GLib.source_remove(tid)
                except Exception: pass

            if resp==Gtk.ResponseType.OK:
                result=entry.get_text(); break
            dlg.hide()
            if self.confirm_discard(secs):
                result=None; break
        send=chk.get_active() and result is not None
        self.send_claude=chk.get_active()
        dlg.destroy()
        return result, send

    def stop_session(self):
        if not self.m_recording: return
        if self.c_active: self.on_cast()
        self.m_recording=False
        rec_stop(self.m_mic); rec_stop(self.m_sys); self.m_mic=self.m_sys=None
        if not self.d_recording: bt_hifi()
        with self.lock: self.m_synth+=1
        self.refresh_icon(); self.refresh_menu()
        default=f"sessao {self.m_ts}"
        typed, send=self.ask_name(default, self.m_proj[1], time.time()-self.m_t0)
        if typed is None:
            clean_session_tmp()
            self.m_shots=[]; self.m_casts=[]
            log("[session] DESCARTADA — nada salvo")
            with self.lock: self.m_synth-=1
            self.refresh_icon(); self.refresh_menu()
            return
        mdir=unique_dir(self.m_calls, safe_name(typed, default))
        mdir.mkdir(parents=True, exist_ok=True)
        log(f"[session] nome: {mdir.name}")
        shots=self.m_shots; self.m_shots=[]
        casts=self.m_casts; self.m_casts=[]
        threading.Thread(target=self._process_session,
                         args=(mdir, self.m_ts, self.m_proj[1], shots, casts, send,
                               LOCAL_BASE/self.m_proj[2]), daemon=True).start()

    def _process_session(self, mdir, ts, proj, shots=(), casts=(), send=False, pdir=None):
        try:
            stem=mdir.name
            mic=TMP_DIR/"m_mic.wav"; sysw=TMP_DIR/"m_sys.wav"
            events=[]
            if mic.exists() and mic.stat().st_size>2000:
                nrm=normalize(mic, TMP_DIR/"m_mic_n.wav")
                with self.gpu: events+=self.engine.track(nrm, "Eu")
                shutil.copy(mic, mdir/f"{stem} - mic.wav")
            if sysw.exists() and sysw.stat().st_size>2000:
                nrm=normalize(sysw, TMP_DIR/"m_sys_n.wav")
                with self.gpu: events+=self.engine.track(nrm, "Participante")
                shutil.copy(sysw, mdir/f"{stem} - sistema.wav")
            nshots=0
            for rel, tmp in shots:
                if not Path(tmp).exists(): continue
                pdir=mdir/(stem+PRINTS_SUFFIX)
                pdir.mkdir(parents=True, exist_ok=True)
                final=pdir/f"print_{fmt_stamp(rel)}.png"
                i=2
                while final.exists():
                    final=pdir/f"print_{fmt_stamp(rel)}_{i}.png"; i+=1
                try: shutil.move(str(tmp), str(final))
                except Exception as e:
                    log("[print] não consegui mover:", repr(e)[:120]); continue
                rl=f"{final.parent.name}/{final.name}"
                events.append((rel, rel+2.0, "📷 print", rl))
                nshots+=1
            if nshots: log(f"[session] {nshots} print(s) indexado(s)")

            ncasts=0
            for rel0, rel1, tmp in casts:
                if not tmp or not Path(tmp).exists(): continue
                cdir=mdir/(stem+CASTS_SUFFIX)
                final=cdir/f"tela_{fmt_stamp(rel0)}.webm"
                i=2
                while final.exists():
                    final=cdir/f"tela_{fmt_stamp(rel0)}_{i}.webm"; i+=1
                if not Screencast.finish(Path(tmp), final): continue
                rl=f"{final.parent.name}/{final.name}"
                events.append((rel0, max(rel1, rel0+2.0), "🎥 tela",
                               f"{rl}  ({human_dur(rel1-rel0)})"))
                ncasts+=1
            if ncasts: log(f"[session] {ncasts} gravação(ões) de tela indexada(s)")

            if not events:
                log("[session] sem fala, print ou tela capturados"); return
            events.sort(key=lambda e: e[0])
            base=mdir/f"{stem} - synthesis"
            with open(base.with_suffix(".txt"),"w",encoding="utf-8") as tf, \
                 open(base.with_suffix(".srt"),"w",encoding="utf-8") as sf:
                title=stem if ts in stem else f"{stem} — {ts}"
                tf.write(f"{title}  ·  {proj}\n")
                tf.write(f"Pasta: {mdir}\n")
                if nshots or ncasts:
                    bits=[]
                    if nshots: bits.append(f"{nshots} print"+("s" if nshots>1 else ""))
                    if ncasts: bits.append(f"{ncasts} gravaç"+("ões" if ncasts>1 else "ão")+" de tela")
                    tf.write("Anexos: "+" · ".join(bits)+"  (caminhos relativos a esta pasta)\n")
                tf.write("="*70+"\n\n")
                for i,(a,b,who,t) in enumerate(events,1):
                    tf.write(f"[{fmt_ts(a)[:8]}] {who}: {t}\n")
                    sf.write(f"{i}\n{fmt_ts(a)} --> {fmt_ts(b)}\n{who}: {t}\n\n")
            log(f"[session] pronto ({len(events)} falas) → {mdir}")
            if send:
                notify(f"🤖 Enviado pro Claude Code  ·  {proj}",
                       f"{CLAUDE_MODEL}, esforço {CLAUDE_EFFORT}. Analisando a sessao…")
                ana=mdir/f"{stem} - analise.md"
                if claude_analyze(pdir or mdir.parent.parent,
                                  base.with_suffix(".txt").read_text(encoding="utf-8"),
                                  ana, proj):
                    notify("🤖 Análise pronta", ana.name)
                else:
                    notify("🤖 A análise do Claude Code falhou",
                           "A synthesis está salva normalmente.")
        except Exception as e:
            log("[session] falhou:", repr(e)[:200])
        finally:
            clean_session_tmp()
            with self.lock: self.m_synth-=1
            self.refresh_icon()
            self.refresh_menu()

    def on_shot(self):
        now=time.time()
        if now-self.last_shot < SHOT_DEBOUNCE_S: return
        with self.lock:
            if not self.m_recording:
                log("[print] sem sessao ativa — ignorado")
                notify("📷 Nenhuma gravação ativa",
                       "O print só é carimbado durante uma gravação de sessao.")
                return
            self.last_shot=now
            rel=max(0.0, now-self.m_t0)
            n=len(self.m_shots)+1
            dst=TMP_DIR/f"shot_{n:02d}_{fmt_stamp(rel)}.png"
            self.m_shots.append((rel, dst))
        threading.Thread(target=self._do_shot, args=(rel, dst, n), daemon=True).start()

    def _do_shot(self, rel, dst, n):
        if grab_screen(dst):
            log(f"[print] {n:02d} @ {fmt_stamp(rel)} → {dst.name}")
            notify(f"📷 Print {n}  ·  {int(rel//60):02d}:{int(rel%60):02d}",
                   "Entra na synthesis neste momento exato.")
        else:
            with self.lock:
                self.m_shots=[(r,p) for r,p in self.m_shots if p!=dst]
            notify("📷 Falha ao capturar o print", "A gravação continua normal.")

    def on_cast(self):
        now=time.time()
        if now-self.last_cast < CAST_DEBOUNCE_S: return
        self.last_cast=now
        with self.lock:
            if self.c_active:
                rel0=self.c_rel0; f=self.c_file
                self.c_active=False; self.c_file=None
                rel1=max(rel0, now-self.m_t0) if self.m_recording else rel0
                self.m_casts.append((rel0, rel1, f))
                stopping=True
            else:
                if not self.m_recording:
                    log("[tela] sem sessao ativa — ignorado")
                    notify("🎥 Nenhuma gravação ativa",
                           "A gravação de tela acompanha uma sessao em andamento.")
                    return
                stopping=False
                rel0=max(0.0, now-self.m_t0)
                n=len(self.m_casts)+1
                template=TMP_DIR/f"cast_{n:02d}_{fmt_stamp(rel0)}.webm"
        if stopping:
            ok=self.cast.stop()
            dur=rel1-rel0
            log(f"[tela] parada ({human_dur(dur)}) ok={ok} → {Path(f).name if f else '?'}")
            notify(f"🎥 Tela gravada  ·  {human_dur(dur)}",
                   "Vai pra pasta da sessao e entra na synthesis.")
            self.refresh_icon()
            return
        used=self.cast.start(template)
        if not used:
            notify("🎥 Não consegui gravar a tela", "A sessao continua normal.")
            return
        with self.lock:
            self.c_active=True; self.c_file=used; self.c_rel0=rel0
        log(f"[tela] gravando desde {fmt_stamp(rel0)} → {used.name}")
        notify("🎥 Gravando a tela…", "Ctrl+Alt+Espaço de novo para parar.")
        self.refresh_icon()

    def _ensure_copy(self, d):
        src=BACKUP_BASE/d; dst=LOCAL_BASE/d; marker=dst/".conteudo_copiado"
        if marker.exists() or not src.exists(): return
        try:
            log(f"[projeto] copiando {d} → {dst} …")
            shutil.copytree(src, dst, dirs_exist_ok=True); marker.write_text(datetime.now().isoformat())
            log(f"[projeto] {d} copiado")
        except Exception as e: log(f"[projeto] cópia {d} falhou:", repr(e)[:150])

    def on_trigger(self):
        now=time.time(); log("[hotkey] F23 detectado")
        if now-self.last_trigger < DEBOUNCE_S: return
        self.last_trigger=now
        with self.lock:
            if not self.d_recording:
                if not self.engine.ready: log("[capture] modelo carregando"); return
                self.d_recording=True; self.d_wav=cap_wav()
                self.d_proc=rec_start(self.d_wav, mic_target(activate=True), LAT_CAP_MS)
                self.d_duck=duck_start()
                log("[capture] gravando…" + (" (sessao segue rodando)" if self.m_recording else ""))
            else:
                self.d_recording=False; rec_stop(self.d_proc); self.d_proc=None
                if not self.m_recording: bt_hifi()
                duck_stop(self.d_duck); self.d_duck=None
                wav=self.d_wav; self.d_wav=None
                self.d_synth+=1
                threading.Thread(target=self._process_capture, args=(wav,), daemon=True).start()
        self.refresh_icon()

    def _process_capture(self, wav):
        text=""
        try:
            if self.m_synth>0: log("[capture] aguardando a GPU (sessao processando)…")
            with self.gpu: text=self.engine.capture(wav)
            log("[capture] processado:", repr(text[:100]))
        except Exception as e: log("[capture] falhou:", e)
        finally:
            with self.lock: self.d_synth-=1
            self.refresh_icon()
            try: Path(wav).unlink()
            except Exception: pass
        paste_text(text)

    def hotkey_loop(self):
        import select
        while True:
            devs=[]
            for path in list_devices():
                try:
                    dv=InputDevice(path); caps=dv.capabilities()
                    keys=caps.get(ecodes.EV_KEY, [])
                    if any(k in keys for k in WATCH_KEYS):
                        devs.append(dv)
                except Exception: pass
            if not devs: log("[hotkey] sem teclado utilizável — retry 3s"); time.sleep(3); continue
            log("[hotkey] escutando:", ", ".join(d.name for d in devs))
            fdmap={d.fd:d for d in devs}
            try:
                while True:
                    r,_,_=select.select(fdmap,[],[],1.0)
                    for fd in r:
                        for ev in fdmap[fd].read():
                            if ev.type==ecodes.EV_KEY:
                                self.on_key(ev.code, ev.value)
            except OSError: log("[hotkey] device sumiu — re-scan"); time.sleep(1)

    def on_key(self, code, value):
        if code in KEY_ALTS:
            self.alt_down = (value != 0)
            return
        if code in KEY_CTRLS:
            self.ctrl_down = (value != 0)
            return
        if value != 1:
            return
        if code == KEY_TRIGGER:
            self.on_trigger()
        elif code == KEY_SHOT and self.alt_down:
            if self.ctrl_down: self.on_cast()
            else:              self.on_shot()

    def quit(self):
        try:
            if self.m_recording: rec_stop(self.m_mic); rec_stop(self.m_sys)
            if self.d_recording: rec_stop(self.d_proc)
            if self.c_active: self.cast.stop()
        except Exception: pass
        duck_stop(self.d_duck, fade=False); self.d_duck=None
        audio_teardown(); Gtk.main_quit()

    def run(self):
        duck_recover()
        audio_setup()
        self.ind=AppIndicator.Indicator.new("mes-synthesis","mes-white",AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_icon_theme_path(ICONS_DIR); self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_title("mes-synthesis"); self.ind.set_menu(self.build_menu())
        threading.Thread(target=self.engine.load, daemon=True).start()
        threading.Thread(target=self.hotkey_loop, daemon=True).start()
        log("[app] pronto — bolinha + tecla Copilot")
        Gtk.main()

if __name__ == "__main__":
    App().run()
