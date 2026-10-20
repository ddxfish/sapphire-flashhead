"""Her screens: her face on every board whose picture door reads JPEG.

A frames-only viewer of the FlashHead server (?audio=0: it never makes the
server take her sound). `screens` setting: off, talking (from the start of a
reply until a few seconds after its last word), always (her idle loop too, at
a gentle rate). One pusher per board keeps only the newest frame, so a slow
board drops frames instead of falling behind. A board stays a speaker first:
nothing is sent to one that does not say `screen.jpeg` in its /health.
"""
import io
import logging
import threading
import time

import requests

from plugins.flashhead import faces

logger = logging.getLogger(__name__)

TALK_FPS, IDLE_FPS = 8, 2       # of the server's 25
HOLD_S = 4                      # talking: stay on this long after her last word
AHEAD_S = 0.15                  # the browser starts a packet's sound this far after it lands

_stop = threading.Event()
_wake = threading.Event()
_connected = threading.Event()
_thread = None


def start(plugin_loader, settings):
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_run, daemon=True, name="flashhead-screens")
    _thread.start()


def stop():
    _stop.set()
    _wake.set()


def wake():
    """A reply starts: be connected before its first words reach the server."""
    if _mode() == "off":
        return
    _wake.set()
    _connected.wait(0.3)


def _mode():
    return faces.settings().get("screens") or "talking"


class _Pusher:
    """One board: the newest frame wins, sent one at a time."""

    def __init__(self, board):
        self.id, self.side = board
        self.frame, self.lock, self.ready = None, threading.Lock(), threading.Event()
        self.fails = 0
        threading.Thread(target=self._run, daemon=True, name=f"flashhead-{self.id}").start()

    def give(self, jpeg):
        with self.lock:
            self.frame = jpeg
        self.ready.set()

    def close(self):
        self.give(None)

    def _run(self):
        from PIL import Image
        from core.devices import glass
        while True:
            self.ready.wait()
            self.ready.clear()
            with self.lock:
                jpeg, self.frame = self.frame, None
            if jpeg is None:
                return
            try:
                b = io.BytesIO()
                Image.open(io.BytesIO(jpeg)).resize((self.side, self.side)).save(b, "JPEG", quality=80)
                glass.face_frame(self.id, b.getvalue(), seconds=3)
                self.fails = 0
            except Exception as e:
                self.fails += 1
                if self.fails in (1, 20):
                    logger.info(f"[flashhead] {self.id}: frame not taken ({e})")


def _run():
    while not _stop.is_set():
        mode = _mode()
        if mode != "always":
            _wake.wait(5)
            if not _wake.is_set() or _stop.is_set() or _mode() == "off":
                continue
        _wake.clear()
        try:
            from core.devices import glass
            boards = glass.face_boards()
        except Exception as e:
            logger.warning(f"[flashhead] boards unknown: {e}")
            boards = []
        if not boards:
            _connected.set()                     # nothing to wait for
            time.sleep(1)
            _connected.clear()
            continue
        try:
            _watch(boards, mode)
        except Exception as e:
            logger.warning(f"[flashhead] screens stream ended: {e}")
            time.sleep(2)


def _watch(boards, mode):
    import json
    import struct
    pushers = [_Pusher(b) for b in boards]
    last_word, checked = time.monotonic(), time.monotonic()
    try:
        with requests.get(f"{faces.base_url()}/stream?audio=0", stream=True, timeout=(5, 30)) as r:
            _connected.set()
            raw = r.raw

            def read(n):
                b = b""
                while len(b) < n:
                    got = raw.read(n - len(b))
                    if not got:
                        raise EOFError("the face server closed the stream")
                    b += got
                return b

            logger.info(f"[flashhead] her face on {', '.join(b[0] for b in boards)} ({mode})")
            while not _stop.is_set():
                now = time.monotonic()
                if mode == "talking" and now - last_word > HOLD_S and not _wake.is_set():
                    return
                if now - checked > 5:
                    checked = now
                    if _mode() != mode:
                        return
                head = json.loads(read(struct.unpack(">I", read(4))[0]))
                if head.get("kind") == "flush":
                    last_word = now - HOLD_S + 1
                    continue
                if not head.get("frames"):
                    continue
                body = read(head["audio"] + sum(head["frames"]))
                if head["kind"] == "speech":
                    last_word = now
                    _wake.clear()                    # the reply that woke us is here
                elif mode == "talking":
                    continue                     # between her lines the last frame holds
                fps = TALK_FPS if head["kind"] == "speech" else IDLE_FPS
                step = max(1, round(head["fps"] / fps))
                at = head["audio"]
                frames = []
                for n in head["frames"]:
                    frames.append(body[at:at + n])
                    at += n
                for i in range(0, len(frames), step):
                    due = now + AHEAD_S + i / head["fps"]
                    time.sleep(max(0, due - time.monotonic()))
                    for p in pushers:
                        p.give(frames[i])
    finally:
        _connected.clear()
        for p in pushers:
            p.close()
        logger.info("[flashhead] her screens rest")
