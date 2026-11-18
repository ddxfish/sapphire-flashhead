"""Her screens: FlashHead as the source of her face in core's avatar feed.

A viewer of the FlashHead server that hands every slice it reads to
core.avatar_feed, which plays it out to the boards (frames alone, or her voice
and face as one stream where a board takes it). The playout clock, the pushers
and the one-stream builders that lived here until 1.7 are the hub's now, shared
with every avatar plugin; what stays is FlashHead's own: reading its server.

`screens` setting: off, talking (from the start of a reply until a few seconds
after its last word), always (her idle loop too). The server renders only while
someone reads its stream, so this reads only while the hub has her boards open.
"""
import json
import logging
import struct
import threading
import time
from urllib.parse import quote

from plugins.flashhead import faces

logger = logging.getLogger(__name__)

NAME = "flashhead"

_stop = threading.Event()
_wake = threading.Event()
_connected = threading.Event()   # the server's stream is open and read
_voiced = False                  # ... and it was asked for her voice (a one-stream board was there)
_again = threading.Event()       # the server moved (client mode, a new worker) or a setting changed: reconnect
_thread = None
_ours = False                    # the hub took us as her face
_said = {}


def _feed():
    from core import avatar_feed
    return avatar_feed


def _mode():
    """off | rest | talking | always, as core says (avatar_feed.mode_for): off unless Settings >
    Devices > Settings chose FlashHead as her face, rest while it is and no board answers (the seat
    is kept), else the strongest mode her screens ask for. Each screen's own mode is set on its
    Devices page (2026-11-12; this was the plugin's `screens` setting)."""
    try:
        return _feed().mode_for(NAME)
    except Exception:
        return "off"


def _register():
    """Become her face in the hub (again: the settings it was given may have changed)."""
    global _ours
    try:
        why = _feed().register(NAME, stop=_stop_at_source, screens=_mode(),
                               face_px=faces.settings().get("face_px") or 0)
    except Exception as e:
        why = f"core has no avatar feed ({e})"
    if why != _said.get("why"):                  # once, not at every ask
        _said["why"] = why
        if why:
            logger.warning(f"[flashhead] her screens stay dark: {why}")
    _ours = not why
    return _ours


def start(plugin_loader, settings):
    global _thread, _ours
    _stop.clear()
    _ours = False
    _thread = threading.Thread(target=_run, daemon=True, name="flashhead-screens")
    _thread.start()


def stop():
    _stop.set()
    _wake.set()
    try:
        _feed().release(NAME)        # always: or no other avatar could be her face
    except Exception:
        pass


def on_settings_saved(settings):
    """Core calls this on a save: the hub takes the new settings, a stream to where the server was is dropped."""
    _again.set()
    _wake.set()


def wake():
    """A reply starts: have the boards open and the server's stream read before its first words."""
    if _mode() in ("off", "rest") or not _ours:   # rest: no board to open, her reply waits for none
        return
    _wake.set()
    _feed().wake(NAME, wait=0.3)
    _connected.wait(0.3)


def carries(device_id):
    """A stream with her voice is read now and the hub feeds that board as one stream: a reply
    starting now rides in it (the hook claims it, glass.av_claim)."""
    return _connected.is_set() and _voiced and _feed().carries(device_id)


def claim(device_id, sid):
    """That board's reply rides in the feed's stream (the hub keeps the claim: core lets its own go when the reply is made)."""
    return _feed().claim(NAME, device_id, sid)


def _stop_at_source(sid):
    """The hub says a board's person stopped her: the face server drops the rest of that reply
    (its flush comes back down the stream and reaches the hub through flush). True when
    something was dropped there."""
    try:
        r = faces.http().post(f"{faces.base_url()}/flush?sid={quote(str(sid))}", timeout=2)
        return bool((r.json() if r.ok else {}).get("dropped"))
    except Exception as e:
        logger.info(f"[flashhead] the face server not flushed on a stop ({e})")
        return False


def _run():
    global _ours
    while not _stop.is_set():
        mode = _mode()
        if mode == "off":
            if _ours:                            # off: the seat is free for another avatar
                _ours = False
                try:
                    _feed().release(NAME)
                except Exception:
                    pass
            _wake.wait(5)
            _wake.clear()
            _again.clear()
            continue
        if not _ours or _again.is_set() or _feed().source() != NAME:   # the seat moved away and back behind our back (Devices picker): take it again
            _again.clear()
            if not _register():
                _stop.wait(10)                   # another avatar is her face: ask again when it may have let go
                continue
        if mode == "rest":                       # her face, and no board answers now: the seat is kept, the server left alone
            _wake.wait(5)
            _wake.clear()
            continue
        if mode != "always":
            _wake.wait(5)
            if not _wake.is_set() or _stop.is_set() or _again.is_set():
                continue
        _wake.clear()
        feed = _feed()
        if mode == "always":
            feed.wake(NAME, wait=1)              # in talking the hook woke it already
        w = feed.watching()
        if not w["boards"]:
            _stop.wait(1)                        # no screen to draw for: the server is left alone
            continue
        try:
            _watch(feed, mode, bool(w["av"]))
        except Exception as e:
            logger.warning(f"[flashhead] screens stream ended: {e}")
            faces.forget()
            _stop.wait(2)


def _watch(feed, mode, voiced):
    global _voiced
    try:
        path, _ = faces.current()
        if path:                                 # the server renders nothing without a face: after its restart, her idle
            url, body, headers = faces.face_body(path)   # existed only once she spoke or someone pressed Watch (camera.py does this too)
            faces.http().post(f"{faces.base_url()}{url}", data=body, headers=headers, timeout=5)
    except Exception as e:
        logger.info(f"[flashhead] her face not sent before her stream ({e})")
    try:
        # board=1: the whole packet always, her voice tagged with its device. Asked for only when a
        # board takes her voice in the stream: a frames-only viewer never makes the server take her sound.
        with faces.http().get(f"{faces.base_url()}/stream?who=screens&audio={int(voiced)}&board={int(voiced)}", stream=True, timeout=(5, 30)) as r:
            _voiced = voiced
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

            logger.info(f"[flashhead] her face goes to the feed ({mode}{', with her voice' if voiced else ''})")
            checked = time.monotonic()
            while not _stop.is_set() and not _again.is_set():
                now = time.monotonic()
                if now - checked > 1:
                    checked = now
                    w = feed.watching()
                    if _mode() != mode or feed.source() != NAME:
                        return
                    if mode == "talking" and not w["open"] and not _wake.is_set():
                        return                   # the hub let her boards rest: the server may too
                    if not w["boards"] or bool(w["av"]) != voiced:
                        # The boards that take her as one stream changed (one was rebooting as this
                        # stream opened): the server is asked again, with or without her voice.
                        logger.info("[flashhead] her boards changed: the stream starts over")
                        return
                head = json.loads(read(struct.unpack(">I", read(4))[0]))
                if not head.get("kind"):
                    continue                     # the server's keepalive
                if head.get("kind") == "flush":
                    feed.flush(NAME, head.get("sid") or "")      # she was stopped: the hub clears its boards
                    continue
                body = read(head.get("audio", 0) + sum(head.get("frames", [])))   # always the whole packet: the stream stays in step
                at = head.get("audio", 0)
                frames = []
                for n in head.get("frames", []):
                    frames.append(body[at:at + n])
                    at += n
                if head.get("kind") == "speech":
                    _wake.clear()                # the reply that woke us is here
                feed.packet(NAME, head.get("kind"), frames, head.get("fps") or 25,
                            pcm=body[:head.get("audio", 0)] if head.get("kind") == "speech" else b"",
                            sr=head.get("sr") or 24000, sid=head.get("sid") or "",
                            device=head.get("device") or "", taken=bool(head.get("taken")))
    finally:
        _connected.clear()
        _voiced = False
        logger.info("[flashhead] her screens rest")
