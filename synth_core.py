import os
import sys
import glob
import json
import time
import shutil
import sysconfig
import subprocess
from datetime import datetime

def add_cuda_dll_dirs():
    try:
        sp = sysconfig.get_paths()["purelib"]
    except Exception:
        return 0
    n = 0
    for d in sorted(glob.glob(os.path.join(sp, "nvidia", "*", "bin"))):
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
                n += 1
            except Exception:
                pass
    return n

_CUDA_DIRS = add_cuda_dll_dirs()

from faster_whisper import WhisperModel, BatchedInferencePipeline

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus", ".flac", ".wma"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

DEFAULT_MODEL = "large-v3"
DEFAULT_LANG = "pt"
DEFAULT_BATCH = 16
DEFAULT_BEAM = 5

BACKENDS = [
    ("cuda", "float16"),
    ("cuda", "int8_float16"),
    ("cpu", "int8"),
]

_CACHE = {}

def _ffbin(name):
    p = shutil.which(name)
    return p or name

LOG_STREAM = sys.stdout

def log(msg, cb=None):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    try:
        print(line, file=LOG_STREAM, flush=True)
    except Exception:
        pass
    if cb:
        try:
            cb(msg)
        except Exception:
            pass

def load_engine(model_size=DEFAULT_MODEL, prefer=None, cb=None):
    backends = prefer or BACKENDS
    for device, compute in backends:
        key = (model_size, device, compute)
        if key in _CACHE:
            return _CACHE[key]
        try:
            log(f"[load] {model_size} em {device}/{compute} ...", cb)
            t0 = time.time()
            model = WhisperModel(model_size, device=device, compute_type=compute)
            pipeline = None
            batched = False
            if device == "cuda":
                try:
                    pipeline = BatchedInferencePipeline(model=model)
                    batched = True
                except Exception as e:
                    log(f"[load] batched indisponivel ({repr(e)[:120]}), usando sequencial", cb)
            info = {"model": model, "pipeline": pipeline, "device": device,
                    "compute": compute, "batched": batched, "model_size": model_size}
            _CACHE[key] = info
            log(f"[load] OK -> {device}/{compute} (batched={batched}) em {time.time()-t0:.1f}s", cb)
            return info
        except Exception as e:
            log(f"[load] {device}/{compute} falhou: {repr(e)[:200]}", cb)
    raise RuntimeError("Nenhum backend de synthesis disponivel (nem GPU nem CPU).")

def extract_audio(src, wav, cb=None):
    if os.path.exists(wav) and os.path.getsize(wav) > 100000:
        return
    import av
    import numpy as np
    import wave as _wave
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []

    def _emit(frames):
        seq = frames if isinstance(frames, (list, tuple)) else ([frames] if frames is not None else [])
        for fr in seq:
            arr = fr.to_ndarray()
            if arr is not None and arr.size:
                chunks.append(np.asarray(arr, dtype=np.int16).reshape(-1))

    with av.open(str(src)) as container:
        astreams = [s for s in container.streams if s.type == "audio"]
        if not astreams:
            raise RuntimeError(f"sem trilha de audio em {src}")
        for frame in container.decode(astreams[0]):
            frame.pts = None
            _emit(resampler.resample(frame))
    try:
        _emit(resampler.resample(None))
    except Exception:
        pass
    if not chunks:
        raise RuntimeError(f"nao consegui decodificar audio de {src}")
    with _wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(np.concatenate(chunks).tobytes())

def probe_duration(path):
    try:
        import av
        with av.open(str(path)) as c:
            if c.duration and c.duration > 0:
                return float(c.duration) / 1_000_000.0
            for s in c.streams:
                if s.duration and s.time_base:
                    return float(s.duration * s.time_base)
    except Exception:
        return 0.0
    return 0.0

def fmt_ts(t, sep=","):
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms -= 1000
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"

def out_paths(src, out_dir=None):
    base = os.path.splitext(os.path.basename(src))[0]
    d = out_dir or os.path.dirname(os.path.abspath(src))
    stem = os.path.join(d, base + " - synthesis")
    return {"srt": stem + ".srt", "txt": stem + ".txt", "json": stem + ".json", "base": base, "dir": d}

def synth_file(src, model_size=DEFAULT_MODEL, language=DEFAULT_LANG,
                    out_dir=None, batch_size=DEFAULT_BATCH, beam_size=DEFAULT_BEAM,
                    write_json=True, tmp_dir=None, engine=None, cb=None,
                    skip_existing=True):
    src = os.path.abspath(src)
    if not os.path.exists(src):
        raise FileNotFoundError(src)

    outp = out_paths(src, out_dir)
    if skip_existing and os.path.exists(outp["srt"]) and os.path.getsize(outp["srt"]) > 80:
        log(f"[skip] ja existe -> {os.path.basename(outp['srt'])}", cb)
        return {"src": src, "skipped": True, **outp}

    os.makedirs(outp["dir"], exist_ok=True)
    eng = engine or load_engine(model_size, cb=cb)

    tmp_dir = tmp_dir or os.environ.get("TEMP") or os.path.dirname(src)
    os.makedirs(tmp_dir, exist_ok=True)
    wav = os.path.join(tmp_dir, f"_synth_{abs(hash(src))%10**8}.wav")
    log(f"[audio] extraindo -> {os.path.basename(src)}", cb)
    extract_audio(src, wav, cb)
    dur = probe_duration(wav) or probe_duration(src)

    t0 = time.time()
    lang = None if (language in (None, "auto", "")) else language
    try:
        if eng["batched"]:
            segments, info = eng["pipeline"].transcribe(
                wav, batch_size=batch_size, language=lang, beam_size=beam_size)
        else:
            segments, info = eng["model"].transcribe(
                wav, language=lang, beam_size=beam_size, vad_filter=True)
    except Exception as e:
        log(f"[warn] decode falhou ({repr(e)[:140]}); tentando sequencial", cb)
        segments, info = eng["model"].transcribe(
            wav, language=lang, beam_size=beam_size, vad_filter=True)

    dur = dur or float(getattr(info, "duration", 0) or 0)

    srt_f = open(outp["srt"], "w", encoding="utf-8")
    txt_f = open(outp["txt"], "w", encoding="utf-8")
    txt_f.write(f"SYNTHESIS - {os.path.basename(src)}\n")
    txt_f.write(f"Origem: {src}\n")
    txt_f.write(f"Gerado: {datetime.now():%Y-%m-%d %H:%M:%S} | "
                f"Modelo: {eng['model_size']} ({eng['device']}/{eng['compute']}"
                f"{', batched x'+str(batch_size) if eng['batched'] else ''})\n")
    txt_f.write("=" * 78 + "\n\n")

    plain = []
    segs_json = []
    n = 0
    last = time.time()
    for seg in segments:
        n += 1
        text = seg.text.strip()
        a, b = fmt_ts(seg.start), fmt_ts(seg.end)
        srt_f.write(f"{n}\n{a} --> {b}\n{text}\n\n")
        txt_f.write(f"[{a} -> {b}] {text}\n")
        plain.append(text)
        if write_json:
            segs_json.append({"i": n, "start": round(seg.start, 3),
                              "end": round(seg.end, 3), "text": text})
        if n % 25 == 0:
            srt_f.flush(); txt_f.flush()
        if time.time() - last > 8:
            pct = (seg.end / dur * 100) if dur else 0
            el = time.time() - t0
            log(f"[..] {os.path.basename(src)[:40]}: {seg.end:.0f}/{dur:.0f}s "
                f"({pct:.0f}%) segs={n} {el:.0f}s", cb)
            last = time.time()

    txt_f.write("\n\n" + "=" * 78 + "\nTEXTO CORRIDO (sem timestamps)\n" + "=" * 78 + "\n\n")
    txt_f.write(" ".join(plain).strip() + "\n")
    srt_f.close(); txt_f.close()

    if write_json:
        with open(outp["json"], "w", encoding="utf-8") as jf:
            json.dump({"src": src, "duration": dur, "language": getattr(info, "language", lang),
                       "model": eng["model_size"], "device": eng["device"],
                       "compute": eng["compute"], "batched": eng["batched"],
                       "segments": segs_json}, jf, ensure_ascii=False, indent=1)

    el = time.time() - t0
    rtf = el / dur if dur else 0
    result = {"src": src, "skipped": False, "segments": n, "duration": dur,
              "elapsed": el, "rtf": rtf, "speed_x": (1/rtf if rtf else 0),
              "device": eng["device"], "compute": eng["compute"],
              "batched": eng["batched"], **outp}
    log(f"[done] {os.path.basename(src)[:45]} | {n} segs | {dur:.0f}s audio em "
        f"{el:.0f}s (x{result['speed_x']:.1f} tempo real) | {eng['device']}", cb)
    try:
        os.remove(wav)
    except Exception:
        pass
    return result

def find_media(target, recursive=True):
    out = []
    if os.path.isfile(target):
        if os.path.splitext(target)[1].lower() in MEDIA_EXTS:
            out.append(os.path.abspath(target))
        return out
    if os.path.isdir(target):
        it = os.walk(target) if recursive else [(target, [], os.listdir(target))]
        for root, _dirs, files in it:
            for f in files:
                if os.path.splitext(f)[1].lower() in MEDIA_EXTS:
                    out.append(os.path.join(root, f))
        return sorted(out)
    for p in glob.glob(target, recursive=True):
        if os.path.isfile(p) and os.path.splitext(p)[1].lower() in MEDIA_EXTS:
            out.append(os.path.abspath(p))
    return sorted(out)

def synth_batch(targets, model_size=DEFAULT_MODEL, language=DEFAULT_LANG,
                     out_dir=None, batch_size=DEFAULT_BATCH, beam_size=DEFAULT_BEAM,
                     skip_existing=True, tmp_dir=None, cb=None, on_file=None):
    if isinstance(targets, str):
        targets = [targets]
    files = []
    for t in targets:
        files.extend(find_media(t))
    seen = set(); uniq = []
    for f in files:
        if f not in seen:
            seen.add(f); uniq.append(f)
    files = uniq

    log(f"[batch] {len(files)} arquivo(s) para processar", cb)
    eng = load_engine(model_size, cb=cb)
    results = []
    t_all = time.time()
    for i, f in enumerate(files, 1):
        log(f"[batch] ({i}/{len(files)}) {os.path.basename(f)}", cb)
        try:
            r = synth_file(f, model_size=model_size, language=language,
                                out_dir=out_dir, batch_size=batch_size, beam_size=beam_size,
                                tmp_dir=tmp_dir, engine=eng, cb=cb, skip_existing=skip_existing)
        except Exception as e:
            log(f"[erro] {os.path.basename(f)}: {repr(e)[:200]}", cb)
            r = {"src": f, "error": str(e)}
        results.append(r)
        if on_file:
            try:
                on_file(i, len(files), r)
            except Exception:
                pass
    log(f"[batch] concluido em {time.time()-t_all:.0f}s ({len(files)} arquivos)", cb)
    return results

def gpu_status():
    exes = ["nvidia-smi", os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                       "System32", "nvidia-smi.exe")]
    last = ""
    for attempt in range(3):
        for exe in exes:
            try:
                r = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                                    "--format=csv,noheader"], capture_output=True, text=True)
                out = (r.stdout or "").strip()
                if out and "NVML" not in out and r.returncode == 0:
                    return out
                last = out or (r.stderr or "").strip()
            except Exception as e:
                last = f"nvidia-smi indisponivel: {e}"
        time.sleep(0.4)
    return last or "GPU status indisponivel"
