"""Her voice drives the face.

stream start: pick the face for her current mood and wake her screens. Each
chunk: handed to the server so the face mouths it; the audio is taken (not
played here) only on a lane that plays here and while a viewer plays sound,
so a closed panel never mutes her and a board's voice is never stolen.
Stop: flush what's queued so the face stops when she does.
Only while the server was last seen ready: a stopped or loading service costs
her voice nothing (looked at again at most every 10 s). The server is this
computer's, or in client mode a worker elsewhere (faces.http()).
"""
import logging
import time

from plugins.flashhead import faces

logger = logging.getLogger(__name__)


_ready = {"ok": False, "at": 0.0}
RECHECK_S = 10


def _post(path, timeout=2, **kw):
    return faces.http().post(f"{faces.base_url()}{path}", timeout=timeout, **kw)


def _server_ready():
    now = time.monotonic()
    if _ready["ok"] or now - _ready["at"] > RECHECK_S:
        try:
            ok = faces.http().get(f"{faces.base_url()}/health", timeout=0.5).json().get("status") == "ready"
        except Exception:
            ok = False
            faces.forget()
        _ready.update(ok=ok, at=now)
    return _ready["ok"]


def tts_stream_start(event):
    if not _server_ready():
        return
    try:
        from plugins.flashhead import screens
        screens.wake()
    except Exception as e:
        logger.debug(f"[flashhead] screens not woken: {e}")
    try:
        path, _ = faces.current(chat=getattr(event, "chat_name", None))
        if path:
            url, body, headers = faces.face_body(path)
            _post(url, timeout=3, data=body, headers=headers)
    except Exception as e:
        logger.debug(f"[flashhead] face pick skipped: {e}")


def tts_chunk_audio(event):
    audio = (event.metadata or {}).get("audio") or {}
    data = audio.get("audio_bytes")
    if not data or not _ready["ok"]:
        return
    here = (event.metadata or {}).get("plays_here", True)      # False: it is a device's voice, never ours to take
    try:
        r = _post(f"/speak?take={int(here)}", data=data, headers={"Content-Type": audio.get("content_type", "audio/ogg")})
        event.skip_tts = bool(here and r.ok and r.json().get("taken"))
    except Exception as e:
        _ready["ok"] = False                     # gone mid-reply: the next reply looks again
        faces.forget()
        logger.debug(f"[flashhead] server not taking audio: {e}")


def tts_stream_end(event):
    if not (event.metadata or {}).get("interrupted") or not _ready["ok"]:
        return
    try:
        _post("/flush", timeout=1)
    except Exception:
        pass
