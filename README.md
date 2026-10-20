# FlashHead

Her persona avatar, lip-synced live to her voice by SoulX-FlashHead Lite on your GPU, in the chat sidebar, a full page, and on her boards' screens.

A loader: this plugin ships only its own code. The engine and the weights are fetched on first start (below).

## Requirements

- **NVIDIA GPU, compute 8.0+** with **~7 GB of free VRAM** and a **driver 570 or newer** (CUDA 12.8). Tested on an RTX 5090 (4x real time); slower cards may not keep up live, and the panel says so after warm-up. CPU, AMD (ROCm) and macOS are not supported.
- **~13 GB of disk**: the conda env (~5 GB) and the models (~7.7 GB).
- **Streaming TTS** (Kokoro or Piper).
- **Sapphire 2.15.1 or newer** (`min_core_version`).
- **conda**, as Sapphire's own install has: Sapphire builds this plugin's env itself when you enable it, after asking.
- **Linux and Windows** (`platforms`). Linux: tested. Windows: untested; the env adds `triton-windows` for `torch.compile`, and without Triton it runs uncompiled (slower). On Windows keep Sapphire in a short folder path (or enable long paths): the download's work files sit deep.

The GPU is checked when the service starts, before the models download. The env itself is built before that check can run, so on a machine without a suitable GPU the ~5 GB env is still downloaded once.

## Install

1. **Settings › Plugins › Install from URL**: this repository's URL.
2. Enable **FlashHead** and say yes to **Build environment** (a few minutes, ~5 GB).
3. The service starts, checks the GPU, then downloads the engine and models (~7.7 GB). The sidebar panel shows progress; **↻ Retry** resumes a download that stopped.
4. Open the **FlashHead** sidebar panel and press **▶ Watch**. Talk to her.

Logs: `user/logs/flashhead-server.log`.

**Testing without a GPU** (a VM): Settings › FlashHead › Advanced › *Test the install without a GPU*. It fetches and imports everything, never renders, and reports in the panel whether every step worked.

**Uninstall** removes the plugin's code. What it fetched stays until you remove it: the env (`conda env remove -n sapphire-plugin-flashhead`), `user/models/flashhead/` (the models), and `user/flashhead/` (your uploaded faces and moods).

## How it flows

```
her streamed voice ─tts_chunk_audio─► server.py (own conda env, GPU)
                                        0.96 s of audio -> 24 frames
   sidebar / app page ◄── /api/plugin/flashhead/stream (sound + frames)
   her boards        ◄── screens.py (frames only) ── /screen/picture?format=jpeg
```

- **Watch** in the sidebar or the Chat flyout's FlashHead page. Collapsing or leaving stops it. **⚙** opens her settings.
- **Settings › FlashHead**: what she does between her words, her screens, whose face (empty = the persona of the chat she speaks in), a **Faces** tab (upload a face, back to her avatar, moods) and **Advanced** (port, the models folder and where it is now, the no-GPU install test).
- **Between her words**: *Calm loop* (default) renders a few seconds of silence once per face, keeps the stretch closest to her real face, and replays it forward then back: the GPU works only while she talks. *Live* renders her as she sits, never repeating, and her next words flow on from there; the GPU works whenever someone watches.
- Her voice is taken from this computer only on a lane that plays here and while a viewer plays sound. A board's voice is never taken; its face mouths along. If rendering keeps failing, the panel says so and her voice plays as usual.
- **Her screens** (setting): Sapphire ESP32 satellites with a screen (the Waveshare board on satellite firmware 0.7.7+) show her face while she talks, or always. A board stays a speaker first.
- **Moods**: `user/flashhead/moods/<emotion>.png`, matched to her active emotion pieces at the start of each reply. Add them with **+ Mood** on the Faces tab.
- **Stop** flushes the face with her voice.
- The service listens on 127.0.0.1 only, answers Sapphire's own calls only, and animates faces only from inside Sapphire's folder.

## What gets fetched, and their licenses

Nothing below is in this plugin; each comes from its source the first time it is needed.

| What | From | License |
|---|---|---|
| Engine `flash_head/` at commit `9bc03de0` | github.com/Soul-AILab/SoulX-FlashHead | Apache-2.0 |
| FlashHead 1.3B Lite DiT weights | huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B | Apache-2.0 |
| LTX-Video 0.9 VAE (`VAE_LTX`, used by Lite) | same repo | **OpenRAIL-M**: use-based restrictions apply to anyone running Lite |
| wav2vec2-base-960h | huggingface.co/facebook/wav2vec2-base-960h | Apache-2.0 |
| Python env: transformers, diffusers, accelerate, einops, pyloudnorm, loguru, pyyaml, pillow, huggingface_hub | PyPI | Apache-2.0 / MIT / BSD / HPND |
| torch, torchvision (cu128) | download.pytorch.org | BSD-3; they pull NVIDIA's CUDA runtime wheels (NVIDIA license) |
| av | PyPI | BSD-3; its wheels bundle FFmpeg (LGPL) |
| triton-windows (Windows only) | PyPI | MIT |

The engine imports xfuser (multi-GPU) and mediapipe (face crop) at module top; one GPU and no crop never call them, so they are not installed and the server stands in for them (loudly, if anything ever calls one).

The engine and models land in `user/models/flashhead/`, which Sapphire's backups always skip (rebuildable: they download again). Her faces and moods live in `user/flashhead/` and are backed up. The models folder can be moved in Settings › FlashHead › Advanced.

## Credits

SoulX-FlashHead by Soul-AILab. The streaming approach was mapped from MovLab2's teri-realtime-avatar-5060ti.

## License

This plugin's own code: MIT (`LICENSE`). What it fetches keeps its own licenses (table above).
