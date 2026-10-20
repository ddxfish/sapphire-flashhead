"""Which face to animate: a mood face matching one of her active emotions,
else the uploaded face, else the persona's avatar (the one named in settings,
or with none named, the persona of the chat she speaks in). Shared by hooks
and routes. Also where her models live (models_dir), and how to reach her
FlashHead server: this computer's, or with `client` on, the worker on another
computer named by the pairing string in `worker` (core/worker_door.py)."""
import json
import re
from pathlib import Path

import requests

from core import worker_door
from core.paths import PROJECT_ROOT
from plugins.flashhead.worker_door import DEVICES     # its own copy: core 2.15.3 has no such list

DIR = PROJECT_ROOT / "user" / "flashhead"
FACE = DIR / "face.png"
MOODS = DIR / "moods"
_SLUG = re.compile(r"[^a-z0-9_-]")


def slug(name):
    s = _SLUG.sub("", (name or "").strip().lower().replace(" ", "_"))[:40]
    return s + "_" if s in DEVICES else s


def settings():
    from core.plugin_loader import plugin_loader
    return plugin_loader.get_plugin_settings("flashhead")


HEADERS = {"X-FlashHead": "1"}       # the service takes writes only with this (server.py _ours)


def worker(s=None):
    """The pairing string of the FlashHead this Sapphire uses on another
    computer, else '' (this computer's own service)."""
    s = s or settings()
    return str(s.get("worker") or "").strip() if s.get("client") is True else ""


def base_url(s=None):
    w = worker(s)
    if w:
        from core import worker_door
        return worker_door.parse(w)[0]
    return f"http://127.0.0.1:{int((s or settings()).get('port') or 5017)}"


_http = {}


def http(s=None):
    """A requests session to her FlashHead server: this computer's (plain, our
    header) or the worker's (pinned TLS, its key). Raises if the worker is
    unreachable or no longer the one paired."""
    w = worker(s)
    sess = _http.get(w)
    if sess is None:
        if w:
            from core import worker_door
            sess, _ = worker_door.session(w, timeout=2)
        else:
            sess = requests.Session()
            sess.trust_env = False
            sess.headers.update(HEADERS)
        _http[w] = sess
    return sess


def forget(s=None):
    """A call failed: the next one connects afresh (and re-checks a worker's cert)."""
    w = worker(s)
    _http.pop(w, None)
    if w:
        from core import worker_door
        base, _, fp = worker_door.parse(w)
        worker_door.forget(base, fp)


def httpx_kwargs(s=None):
    """The same for an httpx client (the routes)."""
    w = worker(s)
    if not w:
        return {"headers": HEADERS}
    from core import worker_door
    base, key, fp = worker_door.parse(w)
    return {"verify": worker_door.context(base, fp, 2), "trust_env": False,
            "headers": {"Authorization": f"Bearer {key}"}}


def face_body(path, s=None):
    """(url, body, headers) that show this face: its path to this computer's
    server, the image itself to a worker elsewhere. Asks for her idle mode too."""
    s = s or settings()
    url = f"/face?idle={s.get('idle') or 'loop'}"
    if worker(s):
        kind = Path(path).suffix.lower().lstrip(".").replace("jpg", "jpeg") or "png"
        return url, Path(path).read_bytes(), {"Content-Type": f"image/{kind}"}
    return url, json.dumps({"path": str(path)}).encode(), {"Content-Type": "application/json"}


def models_dir(s=None):
    """The folder her models download to and load from. server.py works it out
    the same way from the settings it starts with. The default sits under
    user/models/, which core's backup never takes (rebuildable by contract);
    her faces and moods stay in user/flashhead/ and are backed up."""
    raw = str((s or settings()).get("models_dir") or "").strip().strip('"')     # Explorer's "Copy as path" adds quotes
    if not raw:
        return PROJECT_ROOT / "user" / "models" / "flashhead"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def _chat_persona(chat=None):
    """The persona of that chat (the active one when none is named), '' when unknown."""
    try:
        from core.api_fastapi import get_system
        sm = get_system().llm_chat.session_manager
        return (sm.get_settings_for(chat or sm.get_active_chat_name()) or {}).get("persona") or ""
    except Exception:
        return ""


def base_face(s=None, chat=None):
    if FACE.exists():
        return FACE
    from core.personas import persona_manager
    name = str((s or settings()).get("persona") or "").strip() or _chat_persona(chat)
    return persona_manager.get_avatar_path(name) if name else None


def moods():
    return sorted(p.stem for p in MOODS.glob("*.png")) if MOODS.exists() else []


def emotions():
    """Her active emotion pieces, persistent then transient."""
    from core import prompt_state
    keys = prompt_state.get_current_state().get("emotions") or []
    keys = [keys] if isinstance(keys, str) else list(keys)
    keys += [k for k, _ in prompt_state.get_transients().get("emotions", [])]
    return keys


def current(s=None, chat=None):
    """(path, mood name or None) for right now; `chat`: the one she speaks in."""
    try:
        for e in emotions():
            p = MOODS / f"{slug(e)}.png"
            if p.exists():
                return p, p.stem
    except Exception:
        pass
    return base_face(s, chat), None
