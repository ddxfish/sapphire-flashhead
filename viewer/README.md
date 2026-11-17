# FlashHead Viewer

Her live talking face in its own window, on any computer that can reach your Sapphire: a Raspberry Pi on a screen, a spare laptop. It plays her voice in time with her lips, or just her face with `--mute`.

Standalone: it needs none of Sapphire or FlashHead installed, only this folder.

**Want her as a webcam on a Windows computer and don't want to read the rest?** Go to [Easy mode](#easy-mode-her-face-as-a-webcam-on-windows) at the bottom and follow it top to bottom.

## Setup

Linux (Raspberry Pi OS, Ubuntu):

```
sudo apt install libportaudio2          # sound, for sounddevice
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Windows: `python -m venv venv` then `venv\Scripts\pip install -r requirements.txt`.

PySide6 needs a 64-bit OS (a Pi 4 or 5 on 64-bit Raspberry Pi OS).

## The key

On your Sapphire: **Settings › System › API Keys**, make one for this viewer.

## Run

```
venv/bin/python flashhead-viewer.py --host 192.168.1.50 --key <API key> --fullscreen
```

| Option | |
|---|---|
| `--host` | Sapphire's address, or a full URL (`https://host:8073`, or `http://` if her SSL is off) |
| `--port` | her web port, default 8073 |
| `--key` | the API key |
| `--fullscreen` | start fullscreen |
| `--mute` | her face only: her voice stays where it plays now |
| `--device` | sound output, a number or part of a name |
| `--list-devices` | list sound outputs |
| `--camera` | start with her face as a webcam (see below) |
| `--camera-delay` | hold the webcam's picture back this many ms, for a voice that reaches the call late (see below) |

`FLASHHEAD_HOST`, `FLASHHEAD_KEY` and `FLASHHEAD_DEVICE` work in place of the options. The key in an environment variable stays out of `ps`.

Keys: **F** or double-click toggles fullscreen, **Esc** or **Q** quits, **C** (or a right click) turns the webcam on and off, **[** and **]** hold the webcam's picture back less or more. A click on her face (or **Space**) while she speaks stops her: here, at the server and at its source.

## Her face as a webcam

The viewer can be the camera a call picks (Zoom, Meet, Discord), on this computer, with no Sapphire here. Start with `--camera`, or press **C** in the window. A red dot in the corner of her face says the camera is on.

**Windows.** Install **OBS Studio** (obsproject.com). Her camera is OBS's virtual camera, and OBS stays closed: nothing of it runs, so it takes no video memory. Keep OBS's own *Start Virtual Camera* off, since one program at a time can send to it. In the call, pick **OBS Virtual Camera**.

**Linux.** Load v4l2loopback (`sudo apt install v4l2loopback-dkms`, then `sudo modprobe v4l2loopback exclusive_caps=1 card_label="Sapphire Cam"`). `--camera` takes the first free device; `--camera /dev/video3` names one. In the call, pick **Sapphire Cam**.

**Her voice in the call.** A camera carries no sound. Send her voice to a virtual cable with `--device`: on Windows install VB-Cable (free, vb-audio.com/Cable), run the viewer with `--device "CABLE Input"`, and pick **CABLE Output** as the mic in the call.

**Her lips move before her voice.** The viewer times her face to the moment the sound device says her voice comes out. A virtual cable and the call's own mic buffer add more that no device reports, so in the call her voice can come late. Press **]** to hold the webcam's picture back 40 ms more, **[** for 40 ms less, while you watch the call; her window shows the number (0 to 2000). Only the webcam's picture waits, the window stays as it is. To start with that number next time, add `--camera-delay 240` (your number) to the line.

The window has to stay open (minimized is fine): the camera shows what the window shows.

## Her voice

Without `--mute`, this viewer takes her voice: she speaks from here instead of the computer she runs on, with her lips in time. The newest viewer playing her voice takes it, so opening the browser's FlashHead panel later takes it back.

With `--mute`, her voice plays where it always does and this shows her face. The face runs a little behind her voice (her render time plus your network), so `--mute` suits a face across the room more than one beside the speaker.

## Her certificate

Sapphire's HTTPS certificate is her own, so nothing vouches for it. The viewer trusts the one it sees the first time and refuses any other after that, so your key never goes to a stranger. The pins live in `~/.config/flashhead-viewer/pins.json`. If you reinstall Sapphire, remove her line there.

## Start on boot (Pi)

`~/.config/systemd/user/flashhead-viewer.service`:

```
[Unit]
Description=FlashHead viewer

[Service]
Environment=FLASHHEAD_HOST=192.168.1.50
Environment=FLASHHEAD_KEY=<API key>
ExecStart=%h/flashhead/viewer/venv/bin/python %h/flashhead/viewer/flashhead-viewer.py --fullscreen
Restart=on-failure

[Install]
WantedBy=graphical-session.target
```

`systemctl --user enable --now flashhead-viewer`. With no desktop (Pi OS Lite), add `Environment=QT_QPA_PLATFORM=eglfs` to draw straight to the screen.

## Easy mode: her face as a webcam on Windows

For the Windows computer that will be in the call. Sapphire runs on your other computer; nothing of Sapphire gets installed here. Do the steps in order. About 15 minutes, most of it downloads.

**Before you start**, on this Windows computer, open your browser and go to your Sapphire the way you normally would from another computer (something like `https://192.168.1.50:8073`). If she opens, keep that address: it is your **address** below. If she doesn't open, fix that first; nothing below will work until she does.

### 1. Install two programs

1. **Python 3.14** from python.org/downloads. Pick 3.14 exactly, the "Windows installer (64-bit)". Not 3.15 or newer: the webcam part has no build for those yet. Click through the installer with its defaults.
2. **OBS Studio** from obsproject.com. Install it, then leave it alone. You never have to open it. Her camera borrows OBS's camera, and a closed OBS uses none of your video memory.

### 2. Get the viewer folder

On the Sapphire computer, find the folder `user/plugins/flashhead/viewer` inside Sapphire's folder. Copy the whole `viewer` folder to this computer's Desktop (a USB stick or a shared folder is fine).

### 3. Get a key

On your Sapphire, in the browser: **Settings › System › API Keys**, make a new key, and copy it. This is your **key** below. Treat it like a password.

### 4. Set it up (once)

1. Open the `viewer` folder on your Desktop.
2. Click the address bar at the top of the folder window, type `cmd` and press Enter. A black window opens.
3. Paste this line into it and press Enter:

   ```
   py -3.14 -m venv venv
   ```

4. Then this one. It downloads for a few minutes; wait until the black window lets you type again:

   ```
   venv\Scripts\pip install -r requirements.txt
   ```

### 5. Start her

In the same black window, paste this line, with your own address and key in place of the two examples, and press Enter:

```
venv\Scripts\python flashhead-viewer.py --host https://192.168.1.50:8073 --key PASTE-YOUR-KEY-HERE --camera
```

A window opens with her face. A **red dot** in the corner of her face means the camera is on. The first time, the black window says it now trusts Sapphire's certificate: that is normal.

### 6. Pick her in your call

In Zoom, Discord, Meet or whatever you use, open the camera setting and pick **OBS Virtual Camera**. That's her. If the program was already open before step 5, close it and open it again so it sees the camera.

Leave her window open while you're in the call (minimized is fine). Closing her window turns the camera off.

### Next time

Open the `viewer` folder, type `cmd` in the address bar, and paste the line from step 5 again. Steps 1 to 4 are done for good.

To skip the pasting: in Notepad, paste the line from step 5, and save it in the `viewer` folder as `start.bat` (in Notepad's Save box, set "Save as type" to "All files"). After that, double-click `start.bat`. Your key is inside that file, so don't share it.

### Her voice in the call (optional)

A camera carries no sound. As set up above, her voice plays from this computer's speakers. To make her voice the microphone of your call:

1. Install **VB-Cable** (free, vb-audio.com/Cable): unzip it, right-click `VBCABLE_Setup_x64.exe`, **Run as administrator**, then restart the computer.
2. Add this to the end of the line from step 5: `--device "CABLE Input"`
3. In your call, pick **CABLE Output** as the microphone.

You will no longer hear her from this computer's speakers, only the people in the call will.

If the people in the call see her lips move before they hear her, the cable is making her voice late. Click her window and press the **]** key a few times: each press holds her picture back a little more (the number shows on her window), **[** takes it back. When her lips and voice meet, note the number, and add it to the end of the line from step 5 so she starts that way next time, like this: `--camera-delay 240`

### If something goes wrong

Her window says what is wrong in plain words along the bottom. The usual ones:

| It says | Do this |
|---|---|
| `No camera: install OBS Studio` | OBS isn't installed. Step 1. |
| `The camera would not start` | OBS is open with its own camera running, or a second viewer is running. Close OBS, close the other viewer, then press **C** in her window. |
| `Sapphire refused the API key` | The key was pasted wrong. Make a new one (step 3) and paste the whole thing. |
| `FlashHead isn't enabled on that Sapphire` | Turn the FlashHead plugin on in Sapphire. |
| `can't reach Sapphire` | The address is wrong, or Sapphire is off. Try the address in your browser on this computer. |
| `The camera needs the pyvirtualcam package` | Python is the wrong version. Uninstall it, install 3.14 (step 1), delete the `venv` folder, and do step 4 again. |
| The black window says `py` is not recognized, or no 3.14 was found | Python 3.14 isn't installed. Step 1. |
| No red dot | Press **C** in her window. |
| Her face is there but she never talks | Check that FlashHead shows her talking in Sapphire's own browser page first. |

**C** in her window turns the camera off and on. **Esc** closes her.
