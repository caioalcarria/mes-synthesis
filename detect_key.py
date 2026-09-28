import sys, time, select
from evdev import InputDevice, list_devices, categorize, ecodes

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0

devs = []
for path in list_devices():
    try:
        d = InputDevice(path)
        caps = d.capabilities()
        if ecodes.EV_KEY in caps:
            devs.append(d)
    except Exception:
        pass

if not devs:
    print("NENHUM dispositivo de input acessivel (rodou como root?).")
    sys.exit(1)

print(f"escutando {len(devs)} dispositivo(s) por {SECS:.0f}s — APERTE A TECLA COPILOT (2-3x):", flush=True)
for d in devs:
    print(f"  - {d.path}: {d.name}", flush=True)
print("-" * 60, flush=True)

fd_to_dev = {d.fd: d for d in devs}
seen = {}
deadline = time.time() + SECS
while time.time() < deadline:
    r, _, _ = select.select(fd_to_dev, [], [], 0.2)
    for fd in r:
        try:
            for ev in fd_to_dev[fd].read():
                if ev.type == ecodes.EV_KEY and ev.value == 1:
                    names = ecodes.KEY.get(ev.code, ecodes.BTN.get(ev.code, "?"))
                    if isinstance(names, list):
                        names = "/".join(names)
                    dev = fd_to_dev[fd]
                    print(f"[KEYDOWN] code={ev.code:<5} name={names:<22} dev={dev.name}", flush=True)
                    seen[ev.code] = (names, dev.name)
        except Exception:
            pass

print("-" * 60, flush=True)
print("RESUMO (teclas vistas):", flush=True)
for code, (name, dev) in seen.items():
    print(f"  code={code}  name={name}  ({dev})", flush=True)
