"""Check a FlashHead server from any computer, the way a Sapphire would use it.

    python door_test.py "<pairing string>" [face image]

With no pairing string it asks for one (handy on Windows, where & breaks a
command line). Run by flashhead-test.sh / flashhead-test.bat, in the env
flashhead-server made. Steps, each said as it passes or fails:
  1. the server's certificate matches the pairing's pin (TLS, pinned)
  2. a wrong key is refused
  3. health: ready, or why it can't render on that computer
  4. if ready: a face (yours, or a test card), her idle, then a few seconds
     of sound mouthed back with frames; one frame saved to user/test-frame.jpg
Exit 0 when everything that computer can do worked.
"""
import io
import json
import math
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import requests

import worker_door


def say(ok, what):
    print(f"  [{'ok' if ok else 'FAIL'}] {what}")
    return ok


def tone(seconds=4.0, rate=16000):
    """A WAV of something voice-like: a hum whose loudness comes and goes like syllables."""
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(9000 * abs(math.sin(math.pi * 4 * i / rate))
                                                     * math.sin(2 * math.pi * 180 * i / rate)))
                               for i in range(int(seconds * rate))))
    return b.getvalue()


def test_card():
    """A face-shaped stand-in when no picture is given: it proves the pipeline, not the beauty."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (512, 512), (90, 110, 140))
    d = ImageDraw.Draw(im)
    d.ellipse((136, 96, 376, 416), fill=(225, 190, 165))
    d.ellipse((196, 210, 236, 240), fill=(40, 40, 40))
    d.ellipse((276, 210, 316, 240), fill=(40, 40, 40))
    d.ellipse((226, 320, 286, 350), fill=(150, 60, 60))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue(), "image/png"


def main(argv):
    pairing = argv[1] if len(argv) > 1 else input("Pairing string: ").strip()
    try:
        base, key, pin = worker_door.parse(pairing)
    except ValueError as e:
        return not say(False, str(e))
    print(f"FlashHead server at {base}")

    try:
        s, _ = worker_door.session(pairing)
    except Exception as e:
        return not say(False, f"certificate: {e}")
    say(True, "certificate matches the pin (TLS, pinned)")

    bad, _ = worker_door.session(pairing.replace("key=", "key=wrong"))
    code = bad.get(f"{base}/health", timeout=5).status_code
    if not say(code == 401, f"a wrong key is refused (HTTP {code})"):
        return 1

    h = s.get(f"{base}/health", timeout=5).json()
    if h.get("status") != "ready":
        say(True, f"health answers: {h.get('status')}")
        print(f"\nThe door works. This server can't render here: {h.get('error') or h.get('status')}")
        return 0 if h.get("status") in ("unsupported",) or "download" in str(h.get("status")) else 1
    say(True, "health: ready")

    if len(argv) > 2:
        body, kind = Path(argv[2]).read_bytes(), "image/" + Path(argv[2]).suffix.lower().lstrip(".").replace("jpg", "jpeg")
    else:
        body, kind = test_card()
    r = s.post(f"{base}/face?idle=loop", data=body, headers={"Content-Type": kind}, timeout=10).json()
    if not say(r.get("ok"), f"face taken ({kind}, {len(body) // 1024} KB)"):
        return 1

    got, frame = {"idle": 0, "speech": 0, "sound": 0.0}, {}
    stop = threading.Event()

    def view():
        with s.get(f"{base}/stream?audio=1", stream=True, timeout=(5, 60)) as resp:
            raw = resp.raw
            while not stop.is_set():
                n = struct.unpack(">I", raw.read(4))[0]
                head = json.loads(raw.read(n))
                audio = raw.read(head.get("audio", 0))
                jpegs = [raw.read(k) for k in head.get("frames", [])]
                kind_ = head.get("kind")
                if kind_ in ("idle", "speech"):
                    got[kind_] += 1
                if kind_ == "speech":
                    got["sound"] += len(audio) / 2 / head["sr"]
                    if jpegs:
                        frame["jpeg"] = jpegs[len(jpegs) // 2]
    threading.Thread(target=view, daemon=True).start()

    t0 = time.time()
    while not got["idle"] and time.time() - t0 < 90:
        time.sleep(0.3)
    if not say(got["idle"], f"her idle arrives ({time.time() - t0:.1f} s: the first time makes her calm loop)"):
        return 1

    t0 = time.time()
    r = s.post(f"{base}/speak?take=1", data=tone(), headers={"Content-Type": "audio/wav"}, timeout=10).json()
    while got["speech"] < 4 and time.time() - t0 < 30:
        time.sleep(0.2)
    stop.set()
    ok = say(got["speech"] >= 4, f"4 s of sound came back mouthed: {got['speech']} chunks, "
                                 f"{got['sound']:.1f} s of audio, in {time.time() - t0:.1f} s")
    if frame:
        out = Path("user") / "test-frame.jpg"
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(frame["jpeg"])
        print(f"  one frame of it: {out.resolve()}")
    print("\nEverything works." if ok else "\nRendering did not keep up: see the server's window.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
