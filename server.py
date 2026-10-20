"""FlashHead server — SoulX-FlashHead Lite talking face in its own conda env.

Runs as a Sapphire plugin service with the sapphire-plugin-flashhead env's
python on SAPPHIRE_SERVICE_PORT. A loader: nothing of theirs ships here. On
first start it fetches the engine (Soul-AILab/SoulX-FlashHead, Apache-2.0) at a
pinned commit and the Lite weights from HuggingFace, into user/models/flashhead/
(backups skip user/models/: it all downloads again if lost).

Needs an NVIDIA GPU (compute 8.0+, ~7 GB free): checked before anything is
fetched; without one the service stays up and says why on /health.

Renders only while a viewer is connected. Each chunk is 0.96 s of audio in,
24 frames out; her speech when it's queued, silence otherwise (a calm, living
idle; music under her read as silent singing).

A worker for several Sapphires (setting `share`): other computers come in
through a TLS door on `share_port`, each with its own key (worker_door.py),
and each is its own session: her face, her motion, her words, her viewers.
One model; her words always go first. This computer's Sapphire is "local",
on 127.0.0.1 with no key. The same calls serve both:

  GET  /health          status, this session's viewers
  POST /face?idle=      {"path": image} (local only) or the image itself
                        (Content-Type image/*) - animate this face; idle=loop|live
  POST /speak?take=1    raw audio (any format PyAV reads), mouthed while anyone watches;
                        {"taken": true} only when asked to take it and a viewer plays sound
  POST /flush           she was stopped: drop what is queued
  GET  /stream?audio=0  packets: >I header length, JSON header, s16 PCM, JPEG frames;
                        audio=0 = a viewer that shows frames only (her screens)
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import contextlib
import fnmatch
import io
import json
import logging
import os
import queue
import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np

import worker_door                  # a copy of Sapphire's core/worker_door.py: this server also runs alone

os.environ.setdefault("TORCHINDUCTOR_COMPILE_THREADS", "2")   # not 32 idle compiler workers per service

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("flashhead")

PORT = int(os.environ.get("SAPPHIRE_SERVICE_PORT", 5017))
ROOT = Path(os.environ["SAPPHIRE_ROOT"])
ENGINE_REPO = "Soul-AILab/SoulX-FlashHead"
ENGINE_SHA = "9bc03de06bb0de82cd6bc477804512ae06144bf2"     # 2026-05-28, the commit tested here
_START = json.loads(os.environ.get("SAPPHIRE_PLUGIN_SETTINGS") or "{}")


def _models_dir():
    """Her models' folder: settings `models_dir` (the same rule as faces.models_dir),
    else user/models/flashhead: core's cache floor, never in a backup."""
    raw = str(_START.get("models_dir") or "").strip()
    if not raw:
        return ROOT / "user" / "models" / "flashhead"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else ROOT / p


MODELS = _models_dir()
ENGINE = ROOT / "user" / "models" / "flashhead" / "engine" / ENGINE_SHA[:12]   # fetched code: rebuildable, so cache floor too
CKPT = MODELS / "SoulX-FlashHead-1_3B"
W2V = MODELS / "wav2vec2-base-960h"
PLAY_SR = 24000          # Kokoro's native rate; what the viewer hears
LEAD = 0.5               # seconds rendered ahead of the wall clock
VIEWER_QUEUE = 8

_state = {"status": "starting", "error": None, "progress": None,
          "warning": None,      # ready, but the GPU is slower than real time
          "note": None}         # a few seconds of work under "ready"
_gpu = threading.Lock()
_sessions = {}           # name -> Session: each Sapphire using this worker, "local" being this computer's
_viewers_lock = threading.Lock()
_watching = threading.Event()    # anyone, in any session
_pipe = None
P = {}                   # chunk geometry, filled after the model loads
IDLE_CHUNKS, IDLE_SETTLE, IDLE_KEEP = 14, 3, 5   # loop mode: render 14 silent chunks, skip the 3 that settle, keep the calmest 5
SETTINGS = ROOT / "user" / "webui" / "plugins" / "flashhead.json"
FACES = ROOT / "user" / "models" / "flashhead" / "faces"     # faces other computers sent: copies, rebuildable
DOOR = ROOT / "user" / "worker_door" / "flashhead"           # its cert and the keys of the computers let in
FACES_KEPT = 16          # per session: her face and her moods
_cfg = {"mtime": None, "idle": "loop"}


class Session:
    """One Sapphire using this worker: her face and motion on the GPU, her
    words waiting, her viewers. The model is one; between chunks the render
    loop points it at the session it serves."""

    def __init__(self, name):
        self.name = name
        self.want = None            # the face file wanted
        self.face = None            # file@mtime of the face taken
        self.t = None               # (colour reference, reference latent, motion latent) on the GPU
        self.dq = None              # her last seconds of audio: the model's window
        self.mode = None            # idle "loop" | "live" as her Sapphire asked; None: this computer's setting
        self.speech = queue.Queue()
        self.pending = {"a16": np.zeros(0, np.float32), "a24": np.zeros(0, np.int16)}
        self.lock = threading.Lock()
        self.viewers = {}           # queue -> plays the sound (a browser) or frames only (a board)
        self.idle = {"key": None, "packets": [], "at": 0}
        self.live = False           # her motion runs on from the last chunk; False: speech starts her afresh
        self.clock = None           # [t0, seconds made] while watched
        self.note = None

    def talking(self):
        return not self.speech.empty() or len(self.pending["a16"]) > 0


def _session(name):
    with _viewers_lock:
        return _sessions.get(name) or _sessions.setdefault(name, Session(name))


DONE = ".complete"       # written last: a folder without it is a fetch that did not finish


def _preflight():
    """Why this machine can't run Lite, in words, or None. Before any download."""
    need = "needs an NVIDIA GPU (RTX 30-series or newer) with 7 GB of free video memory"
    import torch
    if torch.version.hip:
        return f"FlashHead {need}: this is an AMD (ROCm) build"
    if not torch.cuda.is_available():
        if shutil.which("nvidia-smi"):
            return ("FlashHead found an NVIDIA GPU but CUDA would not start: "
                    "update the NVIDIA driver to 570 or newer (CUDA 12.8), then Retry")
        return f"FlashHead {need}: no CUDA GPU found"
    major, minor = torch.cuda.get_device_capability(0)
    name = torch.cuda.get_device_name(0)
    if major < 8:
        return f"FlashHead {need}: the {name} is compute {major}.{minor}"
    free = torch.cuda.mem_get_info(0)[0] / 1e9
    if free < 7:
        return f"FlashHead {need}: the {name} has {free:.1f} GB free right now"
    return None


def _fetch_engine():
    """flash_head/ and its LICENSE from the pinned commit, nothing else.
    Unpacked beside the target and renamed into place when whole."""
    import tarfile
    import urllib.request
    if (ENGINE / DONE).exists():
        return
    _state["status"] = "fetching engine"
    part = ENGINE.with_name(ENGINE.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    url = f"https://codeload.github.com/{ENGINE_REPO}/tar.gz/{ENGINE_SHA}"
    with urllib.request.urlopen(url, timeout=120) as r, tarfile.open(fileobj=io.BytesIO(r.read()), mode="r:gz") as t:
        keep = []
        for m in t.getmembers():
            rel = m.name.split("/", 1)[1] if "/" in m.name else ""
            if rel == "LICENSE" or rel.startswith("flash_head/"):
                m.name = rel
                keep.append(m)
        t.extractall(part, members=keep, filter="data")
    (part / DONE).touch()
    shutil.rmtree(ENGINE, ignore_errors=True)
    part.rename(ENGINE)


def _du(path):
    """Bytes under path so far. Files come and go while HuggingFace renames them: those are skipped."""
    total = 0
    for f in Path(path).rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _snapshot(repo, dest, patterns, label):
    """One HuggingFace snapshot, resumable. A first fetch goes to dest.part and
    is renamed when whole; a folder from before the marker is checked in place
    (files already there are skipped). Progress lands on /health."""
    from huggingface_hub import HfApi, snapshot_download
    if (dest / DONE).exists():
        return
    work = dest if dest.exists() else dest.with_name(dest.name + ".part")
    try:
        files = HfApi().model_info(repo, files_metadata=True).siblings
        total = sum(f.size or 0 for f in files if any(fnmatch.fnmatch(f.rfilename, p) for p in patterns))
    except Exception:
        total = 0
    done = threading.Event()

    def watch():
        while not done.wait(2):
            got = _du(work)
            _state["progress"] = {"done": got, "total": total}
            _state["status"] = f"downloading {label}" + (f" {got / 1e9:.1f} / {total / 1e9:.1f} GB" if total else "")
    _state["status"] = f"downloading {label}"
    threading.Thread(target=watch, daemon=True).start()
    try:
        snapshot_download(repo, local_dir=str(work), allow_patterns=patterns)
    finally:
        done.set()
        _state["progress"] = None
    (work / DONE).touch()
    if work != dest:
        work.rename(dest)


def _download():
    _fetch_engine()
    _snapshot("Soul-AILab/SoulX-FlashHead-1_3B", CKPT,
              ["config.json", "model_index.json", "Model_Lite/*", "VAE_LTX/*"], "the Lite model")
    _snapshot("facebook/wav2vec2-base-960h", W2V, ["*.json", "model.safetensors", "*.txt"], "wav2vec2")


def _stubs():
    """The engine imports xfuser (multi-GPU) and mediapipe (face crop) at the top
    of its modules; one GPU and no crop never call them, so they are not in the
    env. Stand-ins that fail loudly if anything ever does."""
    import importlib.util
    import types

    def absent(name):
        def attr(a):
            if a.startswith("__"):
                raise AttributeError(a)
            def call(*_, **__):
                raise RuntimeError(f"{name}.{a}: not installed (FlashHead runs one GPU, no face crop)")
            return call
        m = types.ModuleType(name)
        m.__getattr__ = attr
        return m
    for top, mods in (("xfuser", ("xfuser", "xfuser.core", "xfuser.core.distributed", "xfuser.core.long_ctx_attention")),
                      ("mediapipe", ("mediapipe",))):
        if importlib.util.find_spec(top) is None:
            for name in mods:
                sys.modules.setdefault(name, absent(name))


def _compile_ok():
    """torch.compile needs Triton (on Windows: triton-windows). Without it, plain eager."""
    if os.environ.get("FLASHHEAD_COMPILE") == "0":
        return False
    try:
        import triton  # noqa: F401
        return True
    except Exception:
        return False


def _install_test(why):
    """No GPU to render on, but the install is tried all the same (setting
    install_test): fetch, download, stand-ins, engine import, Triton. For a
    machine (a VM) that can test every step but the last."""
    step = "fetching"
    try:
        _download()
        step = "importing the engine"
        os.chdir(ENGINE)
        if str(ENGINE) not in sys.path:
            sys.path.insert(0, str(ENGINE))
        _stubs()
        from flash_head import inference  # noqa: F401
        from flash_head.src.pipeline import flash_head_pipeline  # noqa: F401
        compiled = "with torch.compile" if _compile_ok() else "without torch.compile (no Triton)"
        msg = (f"Install test passed: engine and models fetched, the engine imports, it would run {compiled}. "
               f"Rendering still {why.split(' ', 1)[1]}")
        logger.info(msg)
        _state.update(status="unsupported", error=msg)
    except Exception as e:
        logger.exception("Install test failed")
        _state.update(status="error", error=f"Install test failed while {step}: {e}")


def _load():
    global _pipe
    try:
        _state.update(status="checking the GPU", error=None)
        why = _preflight()
        if why and _START.get("install_test"):
            return _install_test(why)
        if why:
            logger.warning(why)
            _state.update(status="unsupported", error=why)
            return
        _download()
        _state["status"] = "loading model"
        os.chdir(ENGINE)                      # the engine opens its yaml by relative path
        if str(ENGINE) not in sys.path:
            sys.path.insert(0, str(ENGINE))
        import torch
        _stubs()
        from flash_head import inference as fh
        from flash_head.src.pipeline import flash_head_pipeline as fp
        if not _compile_ok():
            logger.warning("No Triton: running without torch.compile (slower)")
            fp.COMPILE_MODEL = fp.COMPILE_VAE = False
        torch._dynamo.config.suppress_errors = True   # a compile that fails falls back to eager, not to a dead service
        torch.backends.cudnn.benchmark = True        # VAE 3-D convs: ~15% on the 5090
        _pipe = fh.get_pipeline(world_size=1, ckpt_dir=str(CKPT), model_type="lite", wav2vec_dir=str(W2V))
        ip = fh.get_infer_params()
        slice_len = ip["frame_num"] - ip["motion_frames_num"]
        cache = ip["sample_rate"] * ip["cached_audio_duration"]
        end = ip["cached_audio_duration"] * ip["tgt_fps"]
        P.update(fh=fh, ip=ip, fps=ip["tgt_fps"], sr=ip["sample_rate"],
                 motion=ip["motion_frames_num"], n16=slice_len * ip["sample_rate"] // ip["tgt_fps"],
                 chunk_s=slice_len / ip["tgt_fps"], start=end - ip["frame_num"], end=end)
        P["n24"] = P["n16"] * PLAY_SR // P["sr"]
        P["cache"] = cache

        blank = Path(tempfile.gettempdir()) / "flashhead_warmup.png"
        from PIL import Image
        Image.new("RGB", (512, 512), (128, 128, 128)).save(blank)
        warm = Session("warm-up")
        warm.want = str(blank)
        _apply_face(warm)
        _state["status"] = "warming up (torch.compile, ~1 min)"
        _render(warm, np.zeros(P["n16"], np.float32))
        t = time.perf_counter()
        _render(warm, np.zeros(P["n16"], np.float32))          # compiled now: this one is the real speed
        speed = P["chunk_s"] / (time.perf_counter() - t)
        if speed < 1.1:
            _state["warning"] = (f"This GPU renders {speed:.1f}x real time: her face may fall behind her voice. "
                                 "Other work on the GPU (a local LLM, a game) slows her too.")
            logger.warning(_state["warning"])
        _state["status"] = "ready"
        logger.info(f"Ready: {P['n16']} samples -> {P['n16'] * P['fps'] // P['sr']} frames per chunk")
    except Exception as e:
        logger.exception("Load failed")
        _state.update(status="error", error=str(e))


def _apply_face(s):
    """Called by the render loop: give the session the face it wants, if changed."""
    path = s.want
    if not path or not os.path.exists(path):
        return
    key = f"{path}@{os.path.getmtime(path)}"     # a re-upload keeps the path
    if key == s.face:
        return
    with _gpu:
        P["fh"].get_base_data(_pipe, cond_image_path_or_dir=path, base_seed=42, use_face_crop=False)
        s.t = (_pipe.original_color_reference, _pipe.ref_img_latent, _pipe.latent_motion_frames)
        s.dq = np.zeros(P["cache"], np.float32)
    s.face, s.live = key, False
    logger.info(f"Face set ({s.name})")


def _reset_motion(s):
    """Start her from the reference again: speech begins here, and its first
    words hide the ~2 s the model takes to settle."""
    with _gpu:
        s.t = (s.t[0], s.t[1], s.t[1][:, :1].clone())
        s.dq = np.zeros(P["cache"], np.float32)


def _idle_mode(s):
    """'loop' (rendered once, the GPU rests) or 'live' (rendered as she sits,
    the GPU works while anyone watches): as her Sapphire asked, else this
    computer's setting, read again when it changes."""
    if s.mode in ("loop", "live"):
        return s.mode
    try:
        m = SETTINGS.stat().st_mtime
        if m != _cfg["mtime"]:
            _cfg.update(mtime=m, idle=json.loads(SETTINGS.read_text(encoding="utf-8")).get("idle") or "loop")
    except (OSError, ValueError):
        pass
    return _cfg["idle"]


def _drift(s, frames):
    """How far each frame strays from her face as given: small grey middles
    (eyes and mouth), mean difference. Silence slowly lowers her lids and
    tilts her head; this is what sees it."""
    from PIL import Image
    h, w = frames[0].shape[:2]
    box = (int(w * .2), int(h * .2), int(w * .8), int(h * .85))
    small = lambda im: np.asarray(im.crop(box).convert("L").resize((48, 48)), np.float32)
    ref = small(Image.open(s.want).convert("RGB").resize((w, h)))
    return np.array([np.abs(small(Image.fromarray(f)) - ref).mean() for f in frames])


def _idle_packets(s):
    """Loop mode: calm idle for the session's face, rendered once. The stretch
    that stays closest to her face is kept and played forward then back, so
    she never nods off and snaps awake at the turn. The GPU then rests."""
    if s.idle["key"] != s.face:
        s.note = "making her calm loop (a few seconds, once per face)"
        _reset_motion(s)
        frames = []
        for i in range(IDLE_CHUNKS):
            f = _render(s, np.zeros(P["n16"], np.float32))
            if i >= IDLE_SETTLE:
                frames += list(f)
        per, keep = len(f), IDLE_KEEP * len(f)
        d = _drift(s, frames)
        means = np.convolve(d, np.ones(keep) / keep, mode="valid")    # the mean drift of every window
        at = int(means.argmin())
        win = frames[at:at + keep]
        loop = win + win[::-1]
        s.idle.update(key=s.face, at=0,
                      packets=[_pack("idle", loop[i:i + per], None) for i in range(0, len(loop), per)])
        s.note = None
        logger.info(f"Idle loop made ({s.name}): {len(s.idle['packets'])} chunks from frame {at} of {len(frames)}, "
                    f"drift {means[at]:.1f} (whole render {d.mean():.1f}, worst {d.max():.1f})")
    return s.idle["packets"]


def _render(s, a16):
    """0.96 s of 16 kHz audio -> list of RGB uint8 frames, in the session's face and motion."""
    fh = P["fh"]
    with _gpu:
        _pipe.original_color_reference, _pipe.ref_img_latent, _pipe.latent_motion_frames = s.t
        s.dq = np.concatenate((s.dq[len(a16):], a16))
        emb = fh.get_audio_embedding(_pipe, s.dq, P["start"], P["end"])
        with contextlib.redirect_stdout(io.StringIO()):   # per-step timing prints
            video = fh.run_pipeline(_pipe, emb)
        s.t = (s.t[0], s.t[1], _pipe.latent_motion_frames)
        frames = video.float().cpu().numpy()[P["motion"]:]
    return frames.clip(0, 255).astype(np.uint8)


def _decode(data):
    """Any audio bytes -> (16 kHz float32, 24 kHz int16) mono."""
    import av
    outs = {}
    for rate, fmt in ((P["sr"], "flt"), (PLAY_SR, "s16")):
        res = av.AudioResampler(format=fmt, layout="mono", rate=rate)
        parts = []
        with av.open(io.BytesIO(data)) as c:
            for frame in c.decode(audio=0):
                for f in res.resample(frame):
                    parts.append(f.to_ndarray().reshape(-1))
            for f in res.resample(None):
                parts.append(f.to_ndarray().reshape(-1))
        outs[rate] = np.concatenate(parts) if parts else np.zeros(0)
    return outs[P["sr"]].astype(np.float32), outs[PLAY_SR].astype(np.int16)


def _queue_speech(s, data):
    a16, a24 = _decode(data)
    with s.lock:
        p16 = np.concatenate((s.pending["a16"], a16))
        p24 = np.concatenate((s.pending["a24"], a24))
        while len(p16) >= P["n16"]:
            s.speech.put((p16[:P["n16"]], p24[:P["n24"]]))
            p16, p24 = p16[P["n16"]:], p24[P["n24"]:]
        s.pending.update(a16=p16, a24=p24)


def _next_chunk(s):
    """Speech if any is waiting (flushing a partial tail), else silence."""
    try:
        a16, a24 = s.speech.get_nowait()
        return "speech", a16, a24
    except queue.Empty:
        pass
    with s.lock:
        if len(s.pending["a16"]):
            a16 = np.pad(s.pending["a16"], (0, P["n16"] - len(s.pending["a16"])))
            a24 = np.pad(s.pending["a24"], (0, P["n24"] - len(s.pending["a24"])))
            s.pending.update(a16=np.zeros(0, np.float32), a24=np.zeros(0, np.int16))
            return "speech", a16, a24
    return "idle", np.zeros(P["n16"], np.float32), None


def _jpeg(f):
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(f).save(b, "JPEG", quality=85)
    return b.getvalue()


def _pack(kind, frames, a24):
    jpegs = [_jpeg(f) for f in frames]
    audio = a24.tobytes() if a24 is not None else b""
    head = json.dumps({"kind": kind, "fps": P["fps"], "sr": PLAY_SR, "audio": len(audio),
                       "frames": [len(j) for j in jpegs]}).encode()
    return struct.pack(">I", len(head)) + head + audio + b"".join(jpegs)


def _drain(s):
    """Every queued speech chunk, taken off the queue (the render loop may be taking them too)."""
    out = []
    while True:
        try:
            out.append(s.speech.get_nowait())
        except queue.Empty:
            return out


def _flush(s):
    """Stop: drop queued speech here and tell viewers to drop what they scheduled."""
    _drain(s)
    with s.lock:
        s.pending.update(a16=np.zeros(0, np.float32), a24=np.zeros(0, np.int16))
    head = json.dumps({"kind": "flush"}).encode()
    _broadcast(s, struct.pack(">I", len(head)) + head)


def _broadcast(s, pkt):
    with _viewers_lock:
        for q in s.viewers:
            try:
                q.put_nowait(pkt)
            except queue.Full:
                pass


def _pick():
    """The session to serve next, of those watched and due (less than LEAD
    ahead of their own clock): her words first, then a calm loop (no GPU),
    then a live idle render. A live idle never holds up anyone's words."""
    now, best = time.monotonic(), None
    with _viewers_lock:
        watched = [s for s in _sessions.values() if s.viewers and s.want]
    for s in watched:
        if s.clock is None:
            s.clock = [now, 0.0]
        lead = s.clock[1] - (now - s.clock[0])
        if lead < -2.0:                         # fell behind (GPU busy): rebase
            s.clock, lead = [now, 0.0], 0.0
        if lead > LEAD:
            continue
        rank = 0 if s.talking() else 2 if _idle_mode(s) == "live" else 1
        if best is None or (rank, lead) < best[0]:
            best = ((rank, lead), s)
    return best and best[1]


def _loop():
    fails = 0
    while True:
        _watching.wait()
        if _state["status"] != "ready":         # a viewer may come while she downloads: wait, never touch the model
            time.sleep(0.5)
            continue
        s = _pick()
        if s is None:
            time.sleep(0.02)
            continue
        try:
            _apply_face(s)
        except Exception:
            logger.exception(f"Face not taken ({s.name})")
            s.want = None                       # never retried in a loop; her next reply sends it again
            continue
        if not s.face:                          # never animate the grey warm-up card
            time.sleep(0.05)
            continue
        kind, a16, a24 = _next_chunk(s)
        t = time.perf_counter()
        try:
            if kind == "idle" and _idle_mode(s) == "live":
                pkt = _pack(kind, _render(s, a16), None)      # she sits as the model has her; her next words go on from here
                s.live = True
            elif kind == "idle":
                loop = _idle_packets(s)
                pkt = loop[s.idle["at"] % len(loop)]
                s.idle["at"] += 1
                s.live = False
            else:
                if not s.live:
                    _reset_motion(s)
                    s.live = True
                pkt = _pack(kind, _render(s, a16), a24)
                logger.info(f"speech chunk rendered in {time.perf_counter() - t:.3f}s ({s.name})")
        except Exception as e:
            logger.exception("Render failed")
            if a24 is not None:
                _broadcast(s, _pack(kind, [], a24))            # her words still reach whoever took them, without a face
            fails += 1
            if fails >= 3:                                      # not a hiccup: stop taking her voice, say so
                _state.update(status="error", error=f"Rendering keeps failing ({e}). Her voice plays as usual; Retry starts the face again.")
                with _viewers_lock:
                    every = list(_sessions.values())
                for x in every:
                    for _, rest in _drain(x):
                        _broadcast(x, _pack("speech", [], rest))
            time.sleep(1)
            continue
        fails = 0
        _broadcast(s, pkt)
        if s.clock:
            s.clock[1] += P["chunk_s"]


def _inside(path):
    """A face only from inside Sapphire's folder (avatars, uploads): never any picture on the disk."""
    try:
        p = Path(path).resolve()
        return any(p.is_relative_to(r.resolve()) for r in (ROOT, ROOT / "user"))   # user/ may be a link elsewhere
    except (OSError, ValueError, TypeError):
        return False


def _face_bytes(s, data):
    """A face sent as an image (another computer's avatar): checked, re-saved
    as PNG under FACES/<session>/ by its hash, the oldest dropped past FACES_KEPT."""
    import hashlib
    from PIL import Image
    folder = FACES / s.name
    dest = folder / f"{hashlib.sha256(data).hexdigest()[:16]}.png"
    if not dest.exists():
        img = Image.open(io.BytesIO(data)).convert("RGB")
        folder.mkdir(parents=True, exist_ok=True)
        img.save(dest)
        for old in sorted(folder.glob("*.png"), key=lambda f: f.stat().st_mtime)[:-FACES_KEPT]:
            old.unlink(missing_ok=True)
    dest.touch()                                # used now: kept longest
    return str(dest)


class Handler(BaseHTTPRequestHandler):
    def _caller(self, post=False):
        """The session a request speaks for, or None (refused, and answered).
        Through the shared door: the computer its key names. Here on
        127.0.0.1: "local", for Sapphire's own calls only. The Host check
        stops a web page that renames itself to 127.0.0.1 (DNS rebinding)
        from reading her face; the header on writes stops a page's blind
        cross-site POST, which cannot set one without a preflight this server
        never answers."""
        if getattr(self.server, "door", None):
            name = worker_door.client_of(self.server.door, self.headers.get("Authorization"))
            if not name:
                self._json({"error": "unauthorized"}, 401)
                return None
            return _session(name)
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host in ("127.0.0.1", "localhost") and (not post or self.headers.get("X-FlashHead") == "1"):
            return _session("local")
        self._json({"error": "forbidden"}, 403)
        return None

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        s = self._caller()
        if not s:
            return
        url = urlsplit(self.path)
        if url.path == "/health":
            with _viewers_lock:
                viewers, listeners = len(s.viewers), sum(s.viewers.values())
            return self._json({**_state, "note": s.note or _state["note"], "face": bool(s.face),
                               "viewers": viewers, "listeners": listeners})
        if url.path != "/stream":
            return self._json({"error": "not found"}, 404)
        q = queue.Queue(maxsize=VIEWER_QUEUE)
        with _viewers_lock:
            s.viewers[q] = parse_qs(url.query).get("audio", ["1"])[0] != "0"
            _watching.set()
        logger.info(f"Viewer joined ({s.name}: {len(s.viewers)})")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                try:
                    pkt = q.get(timeout=5)
                except queue.Empty:
                    pkt = struct.pack(">I", 2) + b"{}"       # keepalive
                self.wfile.write(pkt)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _viewers_lock:
                s.viewers.pop(q, None)
                if not s.viewers:
                    s.clock = None
                    _drain(s)
                if not any(x.viewers for x in _sessions.values()):
                    _watching.clear()
            logger.info(f"Viewer left ({s.name}: {len(s.viewers)})")

    def do_POST(self):
        s = self._caller(post=True)
        if not s:
            return
        data = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 16 * 2**20))
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path == "/face":
            try:
                if (self.headers.get("Content-Type") or "").startswith("image/"):
                    path = _face_bytes(s, data)
                elif getattr(self.server, "door", None):
                    return self._json({"error": "send the face as an image"}, 400)
                else:
                    path = json.loads(data).get("path")
                    if not _inside(path):
                        return self._json({"error": "a face must be inside Sapphire's folder"}, 400)
                s.want = path
                if query.get("idle", [""])[0] in ("loop", "live"):
                    s.mode = query["idle"][0]
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if url.path == "/flush":
            _flush(s)
            return self._json({"ok": True})
        if url.path == "/speak":
            if _state["status"] != "ready" or not s.viewers or not s.face:
                return self._json({"taken": False})
            try:
                _queue_speech(s, data)
                take = query.get("take", ["0"])[0] == "1"
                return self._json({"taken": take and any(s.viewers.values())})
            except Exception as e:
                logger.warning(f"Speak decode failed ({s.name}): {e}")
                return self._json({"taken": False, "error": str(e)})
        return self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


def _open_door():
    """Setting `share`: other computers' Sapphires use this GPU through a TLS
    door, each with its own key (worker_door.py, a copy of core's), each its
    own session: her face, her motion, her viewers."""
    port = int(_START.get("share_port") or 5018)
    try:
        srv = worker_door.serve(ThreadingHTTPServer, Handler, "0.0.0.0", port, DOOR)
    except Exception as e:
        logger.error(f"Shared door not opened on port {port}: {e}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True, name="flashhead-door").start()
    logger.info(f"Shared with other computers on port {port} (TLS, {len(worker_door.clients(DOOR))} let in)")


if __name__ == "__main__":
    threading.Thread(target=_load, daemon=True).start()
    threading.Thread(target=_loop, daemon=True).start()
    if _START.get("share"):
        _open_door()
    logger.info(f"FlashHead server on 127.0.0.1:{PORT}, models in {MODELS}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
