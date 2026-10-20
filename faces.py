"""Which face to animate: a mood face matching one of her active emotions,
else the uploaded face, else the persona's avatar (the one named in settings,
or with none named, the persona of the chat she speaks in). Shared by hooks
and routes. Also where her models live (models_dir)."""
import re

from core.paths import PROJECT_ROOT

DIR = PROJECT_ROOT / "user" / "flashhead"
FACE = DIR / "face.png"
MOODS = DIR / "moods"
_SLUG = re.compile(r"[^a-z0-9_-]")


def slug(name):
    return _SLUG.sub("", (name or "").strip().lower().replace(" ", "_"))[:40]


def settings():
    from core.plugin_loader import plugin_loader
    return plugin_loader.get_plugin_settings("flashhead")


HEADERS = {"X-FlashHead": "1"}       # the service takes writes only with this (server.py _ours)


def base_url(s=None):
    return f"http://127.0.0.1:{int((s or settings()).get('port') or 5017)}"


def models_dir(s=None):
    """The folder her models download to and load from. server.py works it out
    the same way from the settings it starts with. The default sits under
    user/models/, which core's backup never takes (rebuildable by contract);
    her faces and moods stay in user/flashhead/ and are backed up."""
    from pathlib import Path
    raw = str((s or settings()).get("models_dir") or "").strip()
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
