"""Her face as a webcam and her voice as a mic, for a call (Zoom, Meet, Discord, OBS).

A viewer of the FlashHead server, like screens.py, on this computer (client
mode too). Each 0.96 s packet is cut into frames, each with its slice of her
voice, and one 25 fps tick sends both: the frame to a virtual camera
(pyvirtualcam: OBS's on Windows, a v4l2loopback device on Linux), her voice to
a virtual mic (VB-Cable on Windows, a PipeWire source on Linux). Her words are
kept whole: the server sends them seconds early (its TALK_AHEAD) and the queue
holds them all, as a browser does, so her voice here and her face there stay
together. A queue past MAX_S in the quiet skips her idle, so she is near now
again. Between her words her idle face plays on; with no stream her still face
holds: a call wants a steady picture.

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
import time
from collections import deque

import numpy as np

from plugins.flashhead import faces

logger = logging.getLogger(__name__)

W, H, FPS, SR = 1280, 720, 25, 24000     # 16:9: a call crops a square camera; SR = server.py PLAY_SR
TICK = SR // FPS                          # her voice per frame
MAX_S = 2.0                               # in the quiet: a queue past this skips her idle
BANK_S = 8.0                              # her words: all the server sends early (TALK_AHEAD 4 s and a chunk) behind a camera
                                          # as late as the quiet lets it be (MAX_S and a chunk): 7.9 s. At 6 a late camera lost a word
MAX_DELAY_MS = 2000
CABLE = "CABLE Input"                     # VB-Cable's playback end: the call picks "CABLE Output" as its mic
CUSHION = 3                               # ticks of her voice a sound device's buffer keeps in hand (120 ms): what covers a late tick
OVER_S = 0.75                             # in the quiet: this much more than her idle is sent ahead (the packet's "lead") is a camera behind
PW_PROPS = '{ media.class=Audio/Source node.name=sapphire-voice node.description="Sapphire Voice" }'

_stop = threading.Event()                 # the camera thread's own: each start makes a new one
_thread = None
_again = threading.Event()                # settings saved or Retry: start over
_state = {"status": "off", "error": "", "device": "", "mic": "", "mic_ms": 0, "hear": ""}
_lag = 0                                  # ticks her picture waits for her voice (camera_delay)
_opened = None                            # what the camera is open with, her delay aside: only a change in these reopens it
_sync = {"at": 0.0, "text": ""}           # the last time this camera fell behind her words, for the panel
SYNC_S = 600


def sync():
    """{"text", "at", "ago"} for ten minutes after her webcam lost words, else None (routes/api.py status)."""
    ago = time.time() - _sync["at"]
    return {**_sync, "ago": int(ago)} if _sync["text"] and ago < SYNC_S else None


def start(plugin_loader, settings):
    global _stop, _thread
    old = _thread
    stop()                                # a thread from before a quick off and on ends: clearing its flag kept it, two cameras
    if old is not None and old.is_alive():
        old.join(2)                       # and has let go of the camera: OBS's takes one sender, the new thread found it held and went dark for 30 s
    _stop = threading.Event()
    _thread = threading.Thread(target=_run, args=(_stop,), daemon=True, name="flashhead-camera")
    _thread.start()


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


def _run(stop):
    while not stop.is_set():
        _again.clear()
        s = faces.settings()
        if not s.get("camera"):
            _state.update(status="off", error="", device="", mic="", mic_ms=0, hear="")
            _again.wait()
            continue
        try:
            _serve(s, stop)
        except Exception as e:
            _state.update(status="error", error=_words(e), device="", mic="", mic_ms=0, hear="")
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


def _serve(s, stop=None):
    global _lag, _opened
    import pyvirtualcam
    stop = stop or _stop
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
            hear = outs[1].name if len(outs) > 1 else ""
            _state.update(status="on", error="", device=cam.device, mic=outs[0].name if outs else "", mic_ms=mic_ms, hear=hear)
            _opened = _rest(s)
            logger.info(f"[flashhead] her face on webcam {cam.device}"
                        + (f", her voice on {outs[0].name} ({mic_ms} ms to the device)" if outs else ", no voice")
                        + (f" and on this computer's speakers ({hear})" if hear else "")
                        + f", picture delay {_lag * 1000 // FPS} ms")
            feed = _Feed(bool(outs))
            still = shown = _canvas(_still(s))
            hush = np.zeros(TICK, np.int16)
            line = deque()                       # her picture on its way to the camera: JPEGs, small
            while not stop.is_set() and not _again.is_set():
                unit = feed.next()
                pcm, pic = hush, None
                if unit:
                    pic, voice = unit            # pic None: her voice came without a face
                    pcm = voice if voice is not None and len(voice) == TICK else hush
                elif not feed.live:
                    pic = still
                pic = _late(line, pic, _lag + (getattr(outs[0], "ticks", 0) if outs else 0))   # her voice waits in its device's buffer: her picture as long
                if pic is not None:
                    try:
                        shown = pic if isinstance(pic, np.ndarray) else _canvas(pic)
                    except Exception:
                        pass                     # a broken frame: the last one holds
                cam.send(shown)
                for o in outs:
                    o.write(pcm)
                    if pcm is hush and getattr(o, "lost", 0):       # said once she is quiet
                        text = (f"her voice on {o.name} fell behind and lost {o.lost / FPS:.1f} s of her words: that device takes "
                                "sound slower than it is sent, or this computer was too busy")
                        logger.warning(f"[flashhead] {text}")
                        _sync.update(at=time.time(), text=text)
                        o.lost = 0
                cam.sleep_until_next_frame()
        finally:
            if feed:
                feed.close()
            for o in outs:
                o.close()
            if stop is _stop:                    # a thread a new start replaced leaves the new one's state alone
                _opened = None
                _state.update(status="off", device="", mic="", mic_ms=0, hear="")
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
        self.lost = 0                           # ticks of her words dropped at BANK_S, said when she is quiet again
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
            self.fed = False
            try:
                self._watch()
            except Exception as e:
                if self.done.is_set():
                    return
                if self.fed:
                    delay = 2                   # the stream had been good: the wait starts over
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
        with http.get(f"{base}/stream?who=webcam&" + ("audio=1&call=1" if self.voice else "audio=0"),
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
                frames, size = head.get("frames") or [], head.get("audio", 0)
                if not frames and not size:
                    continue                    # the server's keepalive: nothing to show for 5 s
                self.fed = True
                body = _read(raw, size + sum(frames))
                voice = np.frombuffer(body[:size], np.int16) if size else None
                n, at = len(frames), size
                units = [(None, voice[i:i + TICK]) for i in range(0, len(voice) - TICK + 1, TICK)] if not frames else []
                for i, size in enumerate(frames):   # none: her voice without a face (a failed render), the last frame holds
                    units.append((body[at:at + size],
                                  None if voice is None else voice[len(voice) * i // n:len(voice) * (i + 1) // n]))
                    at += size
                if head.get("kind") != "speech" and self.lost:
                    text = (f"her webcam fell behind and dropped {self.lost / FPS:.1f} s of her words: this computer was too busy "
                            f"to send the camera its {FPS} pictures a second (its CPU), or the camera stalled")
                    logger.warning(f"[flashhead] {text}")
                    _sync.update(at=time.time(), text=text)
                    self.lost = 0
                with self.lock:
                    if head.get("kind") != "speech" and len(self.units) > min(MAX_S, head.get("lead", MAX_S) + OVER_S) * FPS:
                        continue                # behind in the quiet: this second of her idle is skipped, never her words
                    self.units.extend(units)
                    while len(self.units) > BANK_S * FPS:   # past all the server sends early: this camera fell behind
                        if self.units.popleft()[1] is not None:
                            self.lost += 1


def _mic(s):
    """Where her voice goes for the call: the named output device; else
    VB-Cable on Windows, a PipeWire source of our own on Linux."""
    name = str(s.get("camera_mic") or "").strip()
    if name or sys.platform != "linux":
        return _Speaker(name or CABLE, "low" if s.get("camera_low_latency") else None)
    return _PipeWireMic()


class _Speaker:
    """Her voice to one output device (sounddevice), a tick at a time. The
    camera's tick and the device's clock are two clocks, so her voice waits
    in a buffer held at one level: CUSHION ticks over what the device takes
    at once. A late tick is covered by it instead of heard as a hole, and
    her picture waits as long (`ticks`), so the two stay together. The level
    is put right in the quiet only (a silent tick left out, or given twice),
    never in her words. Unheld, each hiccup left her voice later than her
    face for good, up to 0.4 s, and from there her words lost 40 ms at a
    time (scouts 2026-11-17). Run dry, it waits for the level again: one
    pause, not a hole a tick. 0.4 s over the level her oldest words go (`lost`)."""

    def __init__(self, name, latency=None):     # None: the device's default, its long one
        import sounddevice as sd
        self.buf, self.have, self.lock = deque(), 0, threading.Lock()
        self.block = 0                          # the most the device has taken at once
        self.going = False                      # the level was reached: she plays
        self.fix = 0                            # the level is over (-1) or under (1): put right at the next silent tick
        self.lost = 0                           # ticks of her dropped past the cap
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
        self.latency = self.stream.latency + CUSHION / FPS     # what the device says of itself, and her wait here; a cable adds its own, unseen
        self.stream.start()

    @property
    def need(self):
        """The level before the device takes its block: CUSHION ticks are left after it."""
        return self.block + CUSHION * TICK

    @property
    def ticks(self):
        """How long her voice waits here, in ticks: her picture is held back as long."""
        return round((CUSHION * TICK + self.block / 2) / TICK)

    def _put(self, pcm):
        self.buf.append(pcm)
        self.have += len(pcm)

    def write(self, pcm):
        with self.lock:
            if self.fix and not pcm.any():      # the quiet: the level is put right, a tick at a time
                fix, self.fix = self.fix, 0
                if fix < 0:
                    return
                self._put(pcm)
            self._put(pcm)
            while self.have > self.need + 10 * TICK:
                self.have -= len(self.buf.popleft())
                self.lost += 1

    def _fill(self, out, frames, t, status):
        n = 0
        with self.lock:
            self.block = max(self.block, min(frames, SR // 4))
            self.fix = -1 if self.have > self.need + TICK else 1 if self.have < self.need - TICK else 0
            if not self.going and self.have >= self.need:
                self.going = True
            while self.going and n < frames and self.buf:
                c = self.buf[0]
                k = min(len(c), frames - n)
                out[n:n + k, 0] = c[:k]
                if k == len(c):
                    self.buf.popleft()
                else:
                    self.buf[0] = c[k:]
                n += k
                self.have -= k
            if self.going and n < frames:
                self.going = False              # ran dry: quiet until the level is back
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
