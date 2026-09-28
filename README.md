# MES Synthesis

GPU-accelerated capture & synthesis utility for Linux / GNOME (Wayland).

A lightweight tray application (AppIndicator) that captures audio sessions in
separate tracks, stamps screenshots and screen recordings inline, and produces
labelled, timestamped synthesis output per project — with an optional post-run
analysis pass.

## Layout
- `app.py` — tray app, audio graph, hotkeys, session pipeline
- `synth_core.py` — GPU-first batched synthesis engine
- `capcmd.py` — voice-command post-processor (pt-BR punctuation/formatting)
- `noise.py` — curated noise/artifact blocklist (second-pass filter)
- `detect_key.py` — input-device key probe (debug utility)
- `run.sh` — launcher (sets CUDA lib path + model cache, runs in the venv)

## Requirements
NVIDIA GPU (CUDA) recommended, Python 3.12 venv, GTK3 + AyatanaAppIndicator3,
PipeWire/PulseAudio (`pactl`/`parecord`), and the model runtime dependencies.
