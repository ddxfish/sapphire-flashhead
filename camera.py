"""Her face as a webcam and her voice as a mic, for a call (Zoom, Meet, Discord, OBS).

A viewer of the FlashHead server, like screens.py, on this computer (client
mode too). Each 0.96 s packet is cut into frames, each with its slice of her
voice, and one 25 fps tick sends both: the frame to a virtual camera
(pyvirtualcam: OBS's on Windows, a v4l2loopback device on Linux), her voice to
a virtual mic (VB-Cable on Windows, a PipeWire source on Linux). A queue past
MAX_S loses its oldest, so she stays near now. Between her words her idle face
plays on; with no stream her still face holds: a call wants a steady picture.

Settings (Webcam tab): `camera` on/off; `camera_voice` the camera carries her
voice (no other viewer plays it then, server.py call=1); `camera_hear` her
voice on this computer's speakers as well; `camera_device` and `camera_mic`
name the devices, empty finds them; `camera_delay` holds her picture back by
that many ms for a mic path that is late (VB-Cable, the call's own buffer),
taken as she plays; `camera_low_latency` asks the mic's device for its
shortest buffer.
"""
import io
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import threading
from collections import deque

import numpy as np

from plugins.flashhead import faces

logger = logging.getLogger(__name__)

W, H, FPS, SR = 1280, 720, 25, 24000     # 16:9: a call crops a square camera; SR = server.py PLAY_SR
TICK = SR // FPS                          # her voice per frame
MAX_S = 2.0
MAX_DELAY_MS = 2000
CABLE = "CABLE Input"                     # VB-Cable's playback end: the call picks "CABLE Output" as its mic
PW_PROPS = '{ media.class=Audio/Source node.name=sapphire-voice node.description="Sapphire Voice" }'

_stop = threading.Event()
_again = threading.Event()                # settings saved or Retry: start over
_state = {"status": "off", "error": "", "device": "", "mic": "", "mic_ms": 0}
_lag = 0                                  # ticks her picture waits for her voice (camera_delay)
_opened = None                            # what the camera is open with, her delay aside: only a change in these reopens it


def start(plugin_loader, settings):
    _stop.clear()
    threading.Thread(target=_run, daemon=True, name="flashhead-camera").start()


def stop():
    _stop.set()
    _again.set()


def on_settings_saved(settings):
    """A new delay alone is taken as she plays (a call would see the camera
    blink at every step of tuning it); anything else opens the camera again."""
    global _lag
    s = faces.settings()
    _lag = _ticks(s)
    if _rest(s) != _opened:
        _again.set()


def retry():
    _again.set()


def _ticks(s):
    try:
        ms = float(s.get("camera_delay") or 0)
    except (TypeError, ValueError):
        ms = 0
    return round(min(max(ms, 0), MAX_DELAY_MS) * FPS / 1000)


def _rest(s):
    return {k: v for k, v in s.items() if k != "camera_delay"}


def _late(line, pic, lag):
    """Her picture, `lag` ticks late: a virtual cable and the call's mic buffer
    make her voice late, so her face waits for it. `pic` is this tick's frame
    (None: the last one holds). Gives the frame whose turn it is, else None.
    A stop leaves the line alone: the frames in it belong to the voice that is
    already in the cable, and both run out together."""
    line.append(pic)
    out = None
    while len(line) > lag:
        got = line.popleft()
        out = got if got is not None else out
    return out


def status():
    return dict(_state)


def _run():
    while not _stop.is_set():
        _again.clear()
        s = faces.settings()
        if not s.get("camera"):
            _state.update(status="off", error="", device="", mic="", mic_ms=0)
            _again.wait()
            continue
        try:
            _serve(s)
        except Exception as e:
            _state.update(status="error", error=_words(e), device="", mic="", mic_ms=0)
            logger.warning(f"[flashhead] webcam: {_state['error']}")
            _again.wait(30)


def _words(e):
    if isinstance(e, ImportError):
        return "The webcam needs the pyvirtualcam package: Install, here or on the FlashHead card in Plugins."
    text = str(e) or type(e).__name__
    if "camera" in text.lower() and sys.platform == "win32":
        return (f"No virtual camera ({text}). Install OBS Studio 26 or newer (its camera is the one she uses), "
                "and keep OBS's own Start Virtual Camera off.")
    if "camera" in text.lower() or "/dev/video" in text:
        return (f"No free virtual camera ({text}). Linux needs v4l2loopback: sudo modprobe v4l2loopback "
                "exclusive_caps=1 card_label=\"Sapphire Cam\"")
    return text


def _serve(s):
    global _lag, _opened
    import pyvirtualcam
    outs = []
    _lag = _ticks(s)
    device = str(s.get("camera_device") or "").strip() or None
    with pyvirtualcam.Camera(W, H, FPS, device=device) as cam:
        feed = None
        try:
            if s.get("camera_voice"):
                outs.append(_mic(s))
                if s.get("camera_hear"):
                    outs.append(_Speaker(None))
            mic_ms = round(outs[0].latency * 1000) if outs else 0
            _state.update(status="on", error="", device=cam.device, mic=outs[0].name if outs else "", mic_ms=mic_ms)
            _opened = _rest(s)
            logger.info(f"[flashhead] her face on webcam {cam.device}"
                        + (f", her voice on {outs[0].name} ({mic_ms} ms to the device)" if outs else ", no voice")
                        + f", picture delay {_lag * 1000 // FPS} ms")
            feed = _Feed(bool(outs))
            still = shown = _canvas(_still(s))
            hush = np.zeros(TICK, np.int16)
            line = deque()                       # her picture on its way to the camera: JPEGs, small
            while not _stop.is_set() and not _again.is_set():
                unit = feed.next()
                pcm, pic = hush, None
                if unit:
                    pic, voice = unit            # pic None: her voice came without a face
                    pcm = voice if voice is not None and len(voice) == TICK else hush
                elif not feed.live:
                    pic = still
                pic = _late(line, pic, _lag)
                if pic is not None:
                    try:
                        shown = pic if isinstance(pic, np.ndarray) else _canvas(pic)
                    except Exception:
                        pass                     # a broken frame: the last one holds
                cam.send(shown)
                for o in outs:
                    o.write(pcm)
                cam.sleep_until_next_frame()
        finally:
            if feed:
                feed.close()
            for o in outs:
                o.close()
            _opened = None
            _state.update(status="off", device="", mic="", mic_ms=0)
            logger.info("[flashhead] webcam off")


def _canvas(img):
    """Her square face in the middle of a 16:9 picture, as RGB for the camera."""
    from PIL import Image
    if isinstance(img, bytes):
        img = Image.open(io.BytesIO(img))
    out = np.zeros((H, W, 3), np.uint8)
    x = (W - H) // 2
    out[:, x:x + H] = np.asarray(img.convert("RGB").resize((H, H)))
    return out


def _still(s):
    from PIL import Image
    try:
        path, _ = faces.current(s)
        if path:
            return Image.open(path)
    except Exception as e:
        logger.debug(f"[flashhead] webcam still: {e}")
    return Image.new("RGB", (H, H))


def _read(raw, n):
    b = b""
    while len(b) < n:
        got = raw.read(n - len(b))
        if not got:
            raise EOFError("the face server closed the stream")
        b += got
    return b


class _Feed:
    """The server's stream cut into (frame, voice) ticks. Reconnects on its own;
    `live` is False while there is no stream."""

    def __init__(self, voice):
        self.voice = voice
        self.units, self.lock = deque(), threading.Lock()
        self.live, self.done, self.resp = False, threading.Event(), None
        threading.Thread(target=self._run, daemon=True, name="flashhead-camera-feed").start()

    def next(self):
        with self.lock:
            return self.units.popleft() if self.units else None

    def close(self):
        self.done.set()
        try:
            if self.resp is not None:
                self.resp.close()               # wakes the read below
        except Exception:
            pass

    def _run(self):
        delay = 2
        while not self.done.is_set():
            try:
                self._watch()
            except Exception as e:
                if self.done.is_set():
                    return
                logger.info(f"[flashhead] webcam stream ended ({type(e).__name__}: {e}), again in {delay} s")
                faces.forget()
            self.live = False
            with self.lock:
                self.units.clear()
            self.done.wait(delay)
            delay = min(delay * 2, 30)

    def _watch(self):
        s = faces.settings()
        base, http = faces.base_url(s), faces.http(s)
        path, _ = faces.current(s)
        if path:                                # the server renders nothing without a face
            url, body, headers = faces.face_body(path, s)
            http.post(f"{base}{url}", data=body, headers=headers, timeout=5)
        with http.get(f"{base}/stream?" + ("audio=1&call=1" if self.voice else "audio=0"),
                      stream=True, timeout=(5, 30)) as r:
            r.raise_for_status()
            self.resp, self.live = r, True
            raw = r.raw
            while not self.done.is_set():
                head = json.loads(_read(raw, struct.unpack(">I", _read(raw, 4))[0]))
                if head.get("kind") == "flush":     # she was stopped: her face and voice stop together
                    with self.lock:
                        self.units.clear()
                    continue
                body = _read(raw, head.get("audio", 0) + sum(head.get("frames", [])))
                frames = head.get("frames")
                voice = np.frombuffer(body[:head["audio"]], np.int16) if head["audio"] else None
                if not frames:                  # her voice without a face (a failed render): the last frame holds
                    if voice is not None:
                        with self.lock:
                            self.units.extend((None, voice[i:i + TICK]) for i in range(0, len(voice) - TICK + 1, TICK))
                    continue
                n, at, units = len(frames), head["audio"], []
                for i, size in enumerate(frames):
                    units.append((body[at:at + size],
                                  None if voice is None else voice[len(voice) * i // n:len(voice) * (i + 1) // n]))
                    at += size
                with self.lock:
                    self.units.extend(units)
                    while len(self.units) > MAX_S * FPS:
                        self.units.popleft()


def _mic(s):
    """Where her voice goes for the call: the named output device; else
    VB-Cable on Windows, a PipeWire source of our own on Linux."""
    name = str(s.get("camera_mic") or "").strip()
    if name or sys.platform != "linux":
        return _Speaker(name or CABLE, "low" if s.get("camera_low_latency") else None)
    return _PipeWireMic()


class _Speaker:
    """Her voice to one output device (sounddevice), a tick at a time. At most
    0.4 s waits: a slow device loses her oldest words, never drifts from her face."""

    def __init__(self, name, latency=None):     # None: the device's default, its long one
        import sounddevice as sd
        self.buf, self.lock = deque(), threading.Lock()
        tries = [None] if name is None else [i for i, d in enumerate(sd.query_devices())
                                              if d["max_output_channels"] > 0 and name.lower() in d["name"].lower()]
        if not tries:
            raise RuntimeError(f"No output device called '{name}'"
                               + (". On Windows install VB-Cable (free, vb-audio.com/Cable), then pick "
                                  "\"CABLE Output\" as the mic in your call." if name == CABLE else "."))
        err = None
        for dev in tries:                       # Windows lists a device per host API; not all take 24 kHz mono
            try:
                self.stream = sd.OutputStream(samplerate=SR, channels=1, dtype="int16", device=dev,
                                              latency=latency, callback=self._fill)
                break
            except Exception as e:
                err = e
        else:
            raise RuntimeError(f"'{name}' would not open: {err}")
        self.name = sd.query_devices(self.stream.device)["name"]
        self.latency = self.stream.latency      # what the device says of itself; a cable adds its own, unseen
        self.stream.start()

    def write(self, pcm):
        with self.lock:
            self.buf.append(pcm)
            while len(self.buf) > 10:
                self.buf.popleft()

    def _fill(self, out, frames, t, status):
        n = 0
        with self.lock:
            while n < frames and self.buf:
                c = self.buf[0]
                k = min(len(c), frames - n)
                out[n:n + k, 0] = c[:k]
                if k == len(c):
                    self.buf.popleft()
                else:
                    self.buf[0] = c[k:]
                n += k
        out[n:, 0] = 0

    def close(self):
        self.stream.close()


class _PipeWireMic:
    """A mic called "Sapphire Voice" that lives as long as this pw-cat does.
    The pipe is kept to a page (85 ms of her): nobody listening drops her
    words here instead of saving them up to play late."""
    name = "Sapphire Voice"
    latency = 0.04                          # what pw-cat is asked for below

    def __init__(self):
        if not shutil.which("pw-cat"):
            raise RuntimeError("Her voice as a mic needs PipeWire (pw-cat), or name an output device in Mic device.")
        self.p = subprocess.Popen(["pw-cat", "--playback", "--target", "0", "--rate", str(SR), "--channels", "1",
                                   "--format", "s16", "--latency", "40ms", "-P", PW_PROPS, "-"],
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.fd = self.p.stdin.fileno()
        try:
            import fcntl
            fcntl.fcntl(self.fd, 1031, 4096)   # F_SETPIPE_SZ
        except Exception:
            pass
        os.set_blocking(self.fd, False)

    def write(self, pcm):
        try:
            os.write(self.fd, pcm.tobytes())    # 1920 bytes: under PIPE_BUF, all or nothing
        except BlockingIOError:
            pass
        except BrokenPipeError:
            raise RuntimeError("the PipeWire mic (pw-cat) stopped")

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.p.terminate()
        try:
            self.p.wait(2)
        except Exception:
            self.p.kill()
