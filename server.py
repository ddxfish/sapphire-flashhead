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

  GET  /health          status, viewers
  POST /face            {"path": image} - animate this face
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

_state = {"status": "starting", "error": None, "face": None, "progress": None,
          "warning": None,      # ready, but the GPU is slower than real time
          "note": None}         # a few seconds of work under "ready" (her idle loop being made)
_gpu = threading.Lock()
_viewers = {}            # queue -> plays the sound (a browser) or frames only (a board)
_viewers_lock = threading.Lock()
_watching = threading.Event()
_speech = queue.Queue()
_pending = {"a16": np.zeros(0, np.float32), "a24": np.zeros(0, np.int16)}
_pending_lock = threading.Lock()
_pipe = None
P = {}                   # chunk geometry, filled after the model loads
IDLE_CHUNKS, IDLE_SETTLE, IDLE_KEEP = 14, 3, 5   # loop mode: render 14 silent chunks, skip the 3 that settle, keep the calmest 5
SETTINGS = ROOT / "user" / "webui" / "plugins" / "flashhead.json"
_cfg = {"mtime": None, "idle": "loop"}
_idle = {"key": None, "packets": [], "at": 0}


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
        P["dq"] = np.zeros(cache, np.float32)

        blank = Path(tempfile.gettempdir()) / "flashhead_warmup.png"
        from PIL import Image
        Image.new("RGB", (512, 512), (128, 128, 128)).save(blank)
        fh.get_base_data(_pipe, cond_image_path_or_dir=str(blank), base_seed=42, use_face_crop=False)
        _state["status"] = "warming up (torch.compile, ~1 min)"
        _render(np.zeros(P["n16"], np.float32))
        t = time.perf_counter()
        _render(np.zeros(P["n16"], np.float32))          # compiled now: this one is the real speed
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


def _apply_face():
    """Called by the render loop: switch to the wanted face if it changed."""
    path = _state.get("want")
    if not path or not os.path.exists(path):
        return
    key = f"{path}@{os.path.getmtime(path)}"     # a re-upload keeps the path
    if key == _state["face"]:
        return
    with _gpu:
        P["fh"].get_base_data(_pipe, cond_image_path_or_dir=path, base_seed=42, use_face_crop=False)
    _state["face"] = key
    logger.info(f"Face set: {path}")


def _reset_motion():
    """Start her from the reference again: speech begins here, and its first
    words hide the ~2 s the model takes to settle."""
    with _gpu:
        _pipe.reset_person_name()
        P["dq"] = np.zeros_like(P["dq"])


def _idle_mode():
    """'loop' (rendered once, the GPU rests) or 'live' (rendered as she sits,
    the GPU works while anyone watches). Read again when the settings change."""
    try:
        m = SETTINGS.stat().st_mtime
        if m != _cfg["mtime"]:
            _cfg.update(mtime=m, idle=json.loads(SETTINGS.read_text(encoding="utf-8")).get("idle") or "loop")
    except (OSError, ValueError):
        pass
    return _cfg["idle"]


def _drift(frames):
    """How far each frame strays from her face as given: small grey middles
    (eyes and mouth), mean difference. Silence slowly lowers her lids and
    tilts her head; this is what sees it."""
    from PIL import Image
    h, w = frames[0].shape[:2]
    box = (int(w * .2), int(h * .2), int(w * .8), int(h * .85))
    small = lambda im: np.asarray(im.crop(box).convert("L").resize((48, 48)), np.float32)
    ref = small(Image.open(_state["want"]).convert("RGB").resize((w, h)))
    return np.array([np.abs(small(Image.fromarray(f)) - ref).mean() for f in frames])


def _idle_packets():
    """Loop mode: calm idle for the current face, rendered once. The stretch
    that stays closest to her face is kept and played forward then back, so
    she never nods off and snaps awake at the turn. The GPU then rests."""
    if _idle["key"] != _state["face"]:
        _state["note"] = "making her calm loop (a few seconds, once per face)"
        _reset_motion()
        frames = []
        for i in range(IDLE_CHUNKS):
            f = _render(np.zeros(P["n16"], np.float32))
            if i >= IDLE_SETTLE:
                frames += list(f)
        per, keep = len(f), IDLE_KEEP * len(f)
        d = _drift(frames)
        means = np.convolve(d, np.ones(keep) / keep, mode="valid")    # the mean drift of every window
        at = int(means.argmin())
        win = frames[at:at + keep]
        loop = win + win[::-1]
        _idle.update(key=_state["face"], at=0,
                     packets=[_pack("idle", loop[i:i + per], None) for i in range(0, len(loop), per)])
        _state["note"] = None
        logger.info(f"Idle loop made: {len(_idle['packets'])} chunks from frame {at} of {len(frames)}, "
                    f"drift {means[at]:.1f} (whole render {d.mean():.1f}, worst {d.max():.1f})")
    return _idle["packets"]


def _render(a16):
    """0.96 s of 16 kHz audio -> list of RGB uint8 frames."""
    fh = P["fh"]
    with _gpu:
        P["dq"] = np.concatenate((P["dq"][len(a16):], a16))
        emb = fh.get_audio_embedding(_pipe, P["dq"], P["start"], P["end"])
        with contextlib.redirect_stdout(io.StringIO()):   # per-step timing prints
            video = fh.run_pipeline(_pipe, emb)
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


def _queue_speech(data):
    a16, a24 = _decode(data)
    with _pending_lock:
        p16 = np.concatenate((_pending["a16"], a16))
        p24 = np.concatenate((_pending["a24"], a24))
        while len(p16) >= P["n16"]:
            _speech.put((p16[:P["n16"]], p24[:P["n24"]]))
            p16, p24 = p16[P["n16"]:], p24[P["n24"]:]
        _pending.update(a16=p16, a24=p24)


def _next_chunk():
    """Speech if any is waiting (flushing a partial tail), else silence."""
    try:
        a16, a24 = _speech.get_nowait()
        return "speech", a16, a24
    except queue.Empty:
        pass
    with _pending_lock:
        if len(_pending["a16"]):
            a16 = np.pad(_pending["a16"], (0, P["n16"] - len(_pending["a16"])))
            a24 = np.pad(_pending["a24"], (0, P["n24"] - len(_pending["a24"])))
            _pending.update(a16=np.zeros(0, np.float32), a24=np.zeros(0, np.int16))
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


def _drain():
    """Every queued speech chunk, taken off the queue (the render loop may be taking them too)."""
    out = []
    while True:
        try:
            out.append(_speech.get_nowait())
        except queue.Empty:
            return out


def _flush():
    """Stop: drop queued speech here and tell viewers to drop what they scheduled."""
    _drain()
    with _pending_lock:
        _pending.update(a16=np.zeros(0, np.float32), a24=np.zeros(0, np.int16))
    head = json.dumps({"kind": "flush"}).encode()
    _broadcast(struct.pack(">I", len(head)) + head)


def _broadcast(pkt):
    with _viewers_lock:
        for q in _viewers:
            try:
                q.put_nowait(pkt)
            except queue.Full:
                pass


def _loop():
    while True:
        _watching.wait()
        if _state["status"] != "ready":         # a viewer may come while she downloads: wait, never touch the model
            time.sleep(0.5)
            continue
        try:
            _apply_face()
        except Exception:
            logger.exception("Face not taken")
            time.sleep(1)
            continue
        if not _state["face"]:                  # never animate the grey warm-up card
            time.sleep(0.5)
            continue
        t0, made, live, fails = time.monotonic(), 0.0, False, 0
        while _watching.is_set() and _state["status"] == "ready":
            lead = made - (time.monotonic() - t0)
            if lead > LEAD:
                time.sleep(min(lead - LEAD, 0.05))
                continue
            if lead < -2.0:                     # fell behind (GPU busy): rebase
                t0, made = time.monotonic(), 0.0
            kind, a16, a24 = _next_chunk()
            t = time.perf_counter()
            try:
                _apply_face()
                if kind == "idle" and _idle_mode() == "live":
                    pkt = _pack(kind, _render(a16), None)      # she sits as the model has her; her next words go on from here
                    live = True
                elif kind == "idle":
                    loop = _idle_packets()
                    pkt = loop[_idle["at"] % len(loop)]
                    _idle["at"] += 1
                    live = False
                else:
                    if not live:
                        _reset_motion()
                        live = True
                    pkt = _pack(kind, _render(a16), a24)
                    logger.info(f"speech chunk rendered in {time.perf_counter() - t:.3f}s")
            except Exception as e:
                logger.exception("Render failed")
                if a24 is not None:
                    _broadcast(_pack(kind, [], a24))           # her words still reach whoever took them, without a face
                fails += 1
                if fails >= 3:                                  # not a hiccup: stop taking her voice, say so
                    _state.update(status="error", error=f"Rendering keeps failing ({e}). Her voice plays as usual; Retry starts the face again.")
                    for _, rest in _drain():
                        _broadcast(_pack("speech", [], rest))
                time.sleep(1)
                continue
            fails = 0
            _broadcast(pkt)
            made += P["chunk_s"]


def _inside(path):
    """A face only from inside Sapphire's folder (avatars, uploads): never any picture on the disk."""
    try:
        p = Path(path).resolve()
        return any(p.is_relative_to(r.resolve()) for r in (ROOT, ROOT / "user"))   # user/ may be a link elsewhere
    except (OSError, ValueError, TypeError):
        return False


class Handler(BaseHTTPRequestHandler):
    def _ours(self, post=False):
        """Sapphire's own calls only. The Host check stops a web page that
        renames itself to 127.0.0.1 (DNS rebinding) from reading her face; the
        header on writes stops a page's blind cross-site POST, which cannot
        set one without a preflight this server never answers."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        ok = host in ("127.0.0.1", "localhost") and (not post or self.headers.get("X-FlashHead") == "1")
        if not ok:
            self._json({"error": "forbidden"}, 403)
        return ok

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._ours():
            return
        url = urlsplit(self.path)
        if url.path == "/health":
            with _viewers_lock:
                viewers, listeners = len(_viewers), sum(_viewers.values())
            return self._json({**_state, "viewers": viewers, "listeners": listeners})
        if url.path != "/stream":
            return self._json({"error": "not found"}, 404)
        q = queue.Queue(maxsize=VIEWER_QUEUE)
        with _viewers_lock:
            _viewers[q] = parse_qs(url.query).get("audio", ["1"])[0] != "0"
            _watching.set()
        logger.info(f"Viewer joined ({len(_viewers)})")
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
                _viewers.pop(q, None)
                if not _viewers:
                    _watching.clear()
                    _drain()
            logger.info(f"Viewer left ({len(_viewers)})")

    def do_POST(self):
        if not self._ours(post=True):
            return
        data = self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 16 * 2**20))
        url = urlsplit(self.path)
        if url.path == "/face":
            try:
                path = json.loads(data).get("path")
                if not _inside(path):
                    return self._json({"error": "a face must be inside Sapphire's folder"}, 400)
                _state["want"] = path
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if url.path == "/flush":
            _flush()
            return self._json({"ok": True})
        if url.path == "/speak":
            if _state["status"] != "ready" or not _viewers or not _state["face"]:
                return self._json({"taken": False})
            try:
                _queue_speech(data)
                take = parse_qs(url.query).get("take", ["0"])[0] == "1"
                return self._json({"taken": take and any(_viewers.values())})
            except Exception as e:
                logger.warning(f"Speak decode failed: {e}")
                return self._json({"taken": False, "error": str(e)})
        return self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=_load, daemon=True).start()
    threading.Thread(target=_loop, daemon=True).start()
    logger.info(f"FlashHead server on 127.0.0.1:{PORT}, models in {MODELS}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
