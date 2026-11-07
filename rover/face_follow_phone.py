#!/usr/bin/env python3
"""
face_follow.py - robot camera operator: follow a face (or a hand) by driving the
rover, take voice and hand-sign commands, record the shoot, and hand it off
for automatic clipping. Remote control from the Rover Studio app (Lovable).

Camera is mounted on the rover, facing forward.
  * Target left/right of center -> rover spins left/right (tank turn)
  * Target too small (far)      -> rover drives forward
  * Target too big (close)      -> rover backs up

Talks to rover_face_follow.ino over USB serial with lines "V <left> <right>".

Voice commands (Vosk, fully offline - the small English model downloads itself):
  "face follow on/off"   "hand follow on/off"   "stop following"
  "zoom in" / "zoom out"     - the rover drives a short step closer / farther; while
                               following, it then keeps that new distance
                               (also "come closer" / "move closer", "back up" / "move back")
  "turn left" / "turn right" - short manual spin
  "start recording" / "stop recording"   (also "start/stop filming", "action" / "cut")
  "stop"                     - stop all motion and following
  "emergency stop"           - pause the wheels (always works)
  While RECORDING, start commands with the wake word so normal talking on camera
  can't trigger them: "rover zoom in", "rover stop recording" (--always-listen to turn off).

Hand signs (hold ~1 s; turn/zoom repeat while held):
  index finger up = face follow on       peace sign (V) = hand follow on
  fist            = stop following       open palm      = stop (ignored in hand follow)
  thumbs up / down = drive closer / back up (keeps moving while held)
  point left / right = turn that way
  rock sign (index + pinky up, hold 1.5 s) = start recording
  OK sign (thumb-index circle, hold 1.5 s) = stop recording

Recording: clean video + face track + microphone audio, saved per session under
./sessions/<id>/, converted to H.264 with ffmpeg, optionally clipped (make_clips.py)
and uploaded to the app (Supabase).

Install:  pip install -r requirements.txt     (ffmpeg on PATH recommended)
Run:      python face_follow.py                      (auto-detects the Arduino)
          python face_follow.py --port COM5          (Windows)
          python face_follow.py --dry-run            (camera only, no motors - tune first!)
          python face_follow.py --mode off           (start idle, wait for a command)
          python face_follow.py --auto-clips         (make clips after every recording)
          python face_follow.py --list-mics          (then --mic N to pick a microphone)

Keys in the video window:  q / Esc = quit    space = pause/resume motors
                           r = start/stop recording
                           f / h = toggle face / hand follow   = / - = drive closer / back up
                           a / d = turn left / right            s = stop
                           1 2 3 = framing close / medium / wide
                           [ ]  , .  ; '  = tune turn Kp / Ki / Kd live
"""

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import wave
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

try:
    import mediapipe as mp                   # hand tracking (optional)
    MP_ERROR = None
except Exception as e:                       # not installed, or installed but broken
    mp = None
    MP_ERROR = f"{type(e).__name__}: {e}"

try:
    import sounddevice as sd                 # microphone (optional)
    SD_ERROR = None
except Exception as e:                       # not installed, or PortAudio missing
    sd = None
    SD_ERROR = f"{type(e).__name__}: {e}"

try:
    import vosk                              # offline speech recognition (optional)
except ImportError:
    vosk = None

try:
    import supabase_sync                     # cloud link / uploads (optional)
except ImportError:
    class supabase_sync:                     # no-op fallback: cloud features disabled
        @staticmethod
        def load_dotenv():
            pass

        @staticmethod
        def from_env():
            # Don't fail silently if the user clearly meant to use the app.
            env_file = Path(__file__).resolve().with_name(".env")
            if os.environ.get("SUPABASE_URL") or (env_file.exists() and "SUPABASE_URL" in env_file.read_text()):
                print("WARNING: Supabase is configured but supabase_sync.py is missing - the app link, "
                      "remote control and uploads are OFF. Put supabase_sync.py in the same folder as "
                      "face_follow.py (it's in rover-studio.zip).")
            return None

        @staticmethod
        def ids_from_env():
            return None, None

HERE = Path(__file__).resolve().parent

# ----------------------------- tuning -----------------------------------------
TARGET_SIZE = 0.16     # desired face width as a fraction of frame width ("medium" framing, ~67 cm away)
HAND_TARGET_SIZE = 0.20  # desired hand box size as a fraction of frame width
MIN_TARGET_SIZE, MAX_TARGET_SIZE = 0.06, 0.5   # limits on the follow distance (as a size)

# "zoom in" / "zoom out" drive the rover toward / away from you (it's a real camera
# move, not a crop). Holding thumbs up/down keeps it moving (repeats every second).
MANUAL_DRIVE_EFFORT = 0.45  # forward/back effort while zooming (0..1)
MANUAL_DRIVE_SEC = 0.33     # how long each zoom move lasts (raise for bigger steps)
KEEP_DISTANCE_AFTER_ZOOM = True  # while following, stay at the new distance afterwards
                                 # (otherwise follow would drive straight back to where it was)

MANUAL_TURN_EFFORT = 0.5   # "turn left/right" spin effort (0..1)
MANUAL_TURN_SEC = 0.23     # ...and how long each spin lasts (raise for bigger turns)
SIZE_DEADBAND = 0.03   # +/- this around the target size counts as "good"
X_DEADBAND = 0.08      # |x_err| below this -> "centered", stop turning
X_EXIT = 0.15          # ...and only start turning again once |x_err| exceeds this (hysteresis)

# Turn PID (error = horizontal offset, -1..1). Live-tune in the window:
#   [ / ]  = KP down/up     ; / '  = KD down/up     , / .  = KI down/up
KP_TURN = 0.3          # turn effort per unit of horizontal error
KI_TURN = 0.1          # slowly pushes out small steady offsets the motors won't move for
KD_TURN = 0.9          # damping: brakes the turn as the target swings toward center
I_LIMIT = 0.2          # max effort the integral term may contribute (anti-windup)
D_FILTER = 0.1         # seconds; low-pass on the derivative (boxes are jittery)

KP_FWD = 4.0           # drive effort per unit of size error
MAX_TURN = 0.7         # cap on turn effort (0..1)
MAX_FWD = 0.7          # cap on forward/back effort (0..1)

MIN_PWM = 90           # smallest PWM that actually moves your wheels (motor deadband)
MAX_PWM = 180          # PWM at full effort (Arduino test sketch used 160)
SLEW_PER_TICK = 0.15   # max *increase* in effort per command tick (smooths jerks)
SLEW_DOWN_PER_TICK = 0.5   # max *decrease* per tick - slow down fast so we don't overshoot

SEND_HZ = 20           # command rate; Arduino watchdog is 400 ms
LOST_TIMEOUT = 0.5     # seconds without a target before stopping
SMOOTHING = 0.4        # 0..1, higher = follow detections faster, lower = smoother
DETECT_WIDTH = 320     # frames are downscaled to this width for face detection (speed)

# Framing presets for face follow (face width as fraction of frame width).
FRAMING = {"close": 0.22, "medium": 0.16, "wide": 0.11}   # ~49 cm, ~67 cm, ~97 cm with a typical webcam

REC_FPS = 30           # recorded video frame rate (frames are duplicated/dropped to stay real-time)
PREVIEW_FPS = 12       # MJPEG preview rate
API_PORT = 8765
WAKE_WORD = "rover"    # required before voice commands while recording

YUNET_FILE = HERE / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
YUNET_SCORE = 0.6
HAND_MODEL_FILE = HERE / "models" / "hand_landmarker.task"
HAND_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
                  "hand_landmarker/float16/latest/hand_landmarker.task")
# ------------------------------------------------------------------------------


def download(url, dest, min_bytes, what):
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return dest
    print(f"Downloading {what} (one time) ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    if tmp.stat().st_size < min_bytes:
        tmp.unlink()
        raise RuntimeError(f"{what} download looks wrong (too small)")
    tmp.replace(dest)
    return dest


# ----------------------------- microphone -------------------------------------
class MicHub:
    """One microphone stream shared by voice commands and the session recorder
    (opening the mic twice fails on some systems)."""

    def __init__(self, device=None):
        if sd is None:
            raise RuntimeError(f"sounddevice not available ({SD_ERROR})")
        info = sd.query_devices(device, "input")
        self.rate = int(info["default_samplerate"])
        self.name = info["name"]
        self.subscribers = []
        self.lock = threading.Lock()
        self.stream = sd.RawInputStream(samplerate=self.rate, blocksize=4000, device=device,
                                        dtype="int16", channels=1, callback=self._callback)
        self.stream.start()

    def _callback(self, indata, frames, time_info, status):
        data = bytes(indata)
        with self.lock:
            subs = list(self.subscribers)
        for fn in subs:
            fn(data)

    def subscribe(self, fn):
        with self.lock:
            self.subscribers.append(fn)

    def unsubscribe(self, fn):
        with self.lock:
            if fn in self.subscribers:
                self.subscribers.remove(fn)

    def close(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


# ----------------------------- voice ------------------------------------------
# Spoken phrase -> command. Vosk only listens for these phrases (a fixed grammar),
# which makes recognition fast and keeps random chatter from triggering anything.
VOICE_PHRASES = {
    "face follow on": "face_on",    "follow my face": "face_on",   "follow face": "face_on",
    "face follow off": "face_off",
    "hand follow on": "hand_on",    "follow my hand": "hand_on",   "follow hand": "hand_on",
    "hand follow off": "hand_off",
    "stop following": "follow_off", "follow off": "follow_off",
    "zoom in": "zoom_in",           "come closer": "zoom_in",      "move closer": "zoom_in",
    "zoom out": "zoom_out",         "back up": "zoom_out",         "move back": "zoom_out",
    "turn left": "turn_left",       "turn right": "turn_right",
    "start recording": "record_start", "start filming": "record_start", "action": "record_start",
    "stop recording": "record_stop",   "stop filming": "record_stop",   "cut": "record_stop",
    "stop": "stop",
}
ALWAYS_PHRASES = {"emergency stop": "estop"}       # never need the wake word


def parse_voice(text, wake=WAKE_WORD):
    """Map recognized text to (command, said_wake_word), or (None, False)."""
    text = " ".join(text.lower().split())
    for phrase in ALWAYS_PHRASES:
        if phrase in text:
            return ALWAYS_PHRASES[phrase], True
    woke = bool(wake) and (text == wake or text.startswith(wake + " ") or f" {wake} " in f" {text} ")
    for phrase in sorted(VOICE_PHRASES, key=len, reverse=True):
        if phrase in text:
            return VOICE_PHRASES[phrase], woke
    return None, False


class VoiceListener(threading.Thread):
    """Background thread: mic hub -> Vosk -> (command, heard_text, woke) into a queue."""

    def __init__(self, out_queue, mic, model_path=None, wake=WAKE_WORD):
        super().__init__(daemon=True)
        if vosk is None:
            raise RuntimeError("voice needs: pip install vosk sounddevice")
        vosk.SetLogLevel(-1)
        # With no path, Vosk downloads its small English model (~40 MB) on first run.
        self.model = vosk.Model(model_path) if model_path else vosk.Model(lang="en-us")
        phrases = sorted(VOICE_PHRASES)
        grammar = phrases + ([f"{wake} {p}" for p in phrases] if wake else []) + \
            sorted(ALWAYS_PHRASES) + ["[unk]"]
        self.rec = vosk.KaldiRecognizer(self.model, mic.rate, json.dumps(grammar))
        self.wake = wake
        self.mic = mic
        self.out = out_queue
        self.audio = queue.Queue()
        self.running = True
        mic.subscribe(self.audio.put)

    def run(self):
        while self.running:
            try:
                data = self.audio.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.rec.AcceptWaveform(data):
                text = json.loads(self.rec.Result()).get("text", "")
                cmd, woke = parse_voice(text, self.wake)
                if cmd:
                    self.out.put((cmd, f'heard "{text}"', woke))

    def stop(self):
        self.running = False
        self.mic.unsubscribe(self.audio.put)


# ----------------------------- serial -----------------------------------------
def find_arduino_port():
    if serial is None:
        return None
    ports = list(list_ports.comports())
    keys = ("arduino", "ch340", "ch341", "wch", "usb serial", "usb-serial", "ftdi", "cp210")
    for p in ports:
        text = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in text for k in keys):
            return p.device
    for p in ports:
        if any(k in p.device for k in ("ACM", "usbmodem", "usbserial", "ttyUSB")):
            return p.device
    return None


class Rover:
    def __init__(self, port, baud=115200):
        self.ser = serial.Serial(port, baud, timeout=0)
        time.sleep(2.0)                      # the Uno resets when the port opens
        self.ser.reset_input_buffer()
        self._rx = b""
        self.status = "waiting for rover..."
        self.blocked = False

    def send(self, left, right):
        self.ser.write(f"V {int(left)} {int(right)}\n".encode())

    def poll(self):
        """Read any status lines from the Arduino (non-blocking)."""
        data = self.ser.read(512)
        if not data:
            return
        self._rx += data
        *lines, self._rx = self._rx.split(b"\n")
        for raw in lines:
            line = raw.decode(errors="ignore").strip()
            if line.startswith("S "):
                parts = line.split()
                if len(parts) == 3:
                    cm = int(parts[1]) if parts[1].lstrip("-").isdigit() else -1
                    dist = "no echo" if cm < 0 else f"{cm} cm"
                    self.blocked = parts[2].lower() == "blocked"
                    self.status = f"sonar {dist} - {parts[2].upper()}"
                else:
                    self.status = line[2:]
            elif line:
                print("[arduino]", line)

    def close(self):
        try:
            self.send(0, 0)
            self.ser.write(b"s\n")           # leave streaming mode
            self.ser.flush()
        finally:
            self.ser.close()


# ----------------------------- trackers ---------------------------------------
class BoxTracker:
    """Smooths detections and sticks to the same target. Subclasses implement detect()."""

    def __init__(self):
        self.state = None                    # smoothed (cx, cy, w, h) in full-frame px
        self.last_seen = 0.0
        self.fresh = False                   # True if the last update() had a real detection

    def detect(self, frame):
        """Return a list of (x, y, w, h) boxes in full-frame pixels."""
        raise NotImplementedError

    def reset(self):
        self.state = None
        self.fresh = False

    def update(self, frame):
        boxes = self.detect(frame)
        now = time.time()
        self.fresh = len(boxes) > 0
        if not boxes:
            if now - self.last_seen > LOST_TIMEOUT:
                self.state = None
            return self.state

        if self.state is None:
            x, y, w, h = max(boxes, key=lambda b: b[2] * b[3])          # biggest one
        else:
            pcx, pcy = self.state[0], self.state[1]                      # stick to the same one
            x, y, w, h = min(boxes, key=lambda b: (b[0] + b[2] / 2 - pcx) ** 2 +
                                                  (b[1] + b[3] / 2 - pcy) ** 2)
        meas = (x + w / 2, y + h / 2, w, h)
        if self.state is None:
            self.state = meas
        else:
            a = SMOOTHING
            self.state = tuple(a * m + (1 - a) * s for m, s in zip(meas, self.state))
        self.last_seen = now
        return self.state


class FaceTracker(BoxTracker):
    """YuNet face detector (handles turned heads); falls back to the Haar cascade."""

    def __init__(self, detector="auto"):
        super().__init__()
        self.kind = None
        self.yunet = None
        if detector in ("auto", "yunet") and hasattr(cv2, "FaceDetectorYN"):
            try:
                path = download(YUNET_URL, YUNET_FILE, 100_000, "YuNet face model (230 KB)")
                self.yunet = cv2.FaceDetectorYN.create(str(path), "", (DETECT_WIDTH, 240),
                                                       YUNET_SCORE, 0.3, 50)
                self.kind = "yunet"
            except Exception as e:
                print(f"YuNet unavailable ({e}). Using Haar cascade.")
        if self.kind is None:
            if detector == "yunet":
                sys.exit("YuNet requested but not available (needs opencv-python >= 4.8).")
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            self.cascade = cv2.CascadeClassifier(path)
            if self.cascade.empty():
                sys.exit(f"Could not load face model: {path}")
            self.kind = "haar"
        print(f"Face detector: {self.kind}")

    def detect(self, frame):
        H, W = frame.shape[:2]
        scale = DETECT_WIDTH / W
        small = cv2.resize(frame, (DETECT_WIDTH, int(H * scale)))
        if self.yunet is not None:
            h, w = small.shape[:2]
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(small)
            faces = [] if faces is None else [f[:4] for f in faces]
        else:
            gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            faces = self.cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                                  minSize=(24, 24))
        return [(float(x) / scale, float(y) / scale, float(w) / scale, float(h) / scale)
                for (x, y, w, h) in faces]


class HandTracker(BoxTracker):
    """Hand boxes from MediaPipe. Box is made square (side = longer edge) so its
    width is a stable size measure whether the hand is open, closed, or sideways.

    Works with both MediaPipe APIs: the classic `mp.solutions.hands` and the newer
    Tasks HandLandmarker (current `pip install mediapipe` only has the latter).

    Call process(frame) once per frame; both hand-follow (detect) and the gesture
    recognizer read the cached landmarks, so MediaPipe only runs once."""

    def __init__(self):
        super().__init__()
        if mp is None:
            raise RuntimeError(
                f"mediapipe failed to load in {sys.executable}\n    ({MP_ERROR})\n"
                f"    fix: {sys.executable} -m pip install mediapipe")
        self.api = None
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "hands"):
            self.hands = mp.solutions.hands.Hands(static_image_mode=False, max_num_hands=2,
                                                  model_complexity=0,
                                                  min_detection_confidence=0.6,
                                                  min_tracking_confidence=0.5)
            self.api = "solutions"
        else:
            from mediapipe.tasks.python import BaseOptions, vision
            path = download(HAND_MODEL_URL, HAND_MODEL_FILE, 1_000_000, "MediaPipe hand model (7.5 MB)")
            opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(path)),
                running_mode=vision.RunningMode.VIDEO, num_hands=2,
                min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.5,
                min_tracking_confidence=0.5)
            self.hands = vision.HandLandmarker.create_from_options(opts)
            self.api = "tasks"
            self._last_ts = 0
        self.landmarks = []                  # list of 21 (x, y) pixel points per hand

    def process(self, frame):
        H, W = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if self.api == "solutions":
            result = self.hands.process(rgb)
            hands = [lm.landmark for lm in (result.multi_hand_landmarks or [])]
        else:
            ts = max(self._last_ts + 1, int(time.monotonic() * 1000))   # must increase
            self._last_ts = ts
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            hands = self.hands.detect_for_video(image, ts).hand_landmarks or []
        self.landmarks = [[(p.x * W, p.y * H) for p in pts] for pts in hands]
        return self.landmarks

    def detect(self, frame):
        boxes = []
        for pts in self.landmarks:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            side = max(max(xs) - min(xs), max(ys) - min(ys))
            boxes.append((cx - side / 2, cy - side / 2, side, side))
        return boxes


# ----------------------------- gestures ---------------------------------------
# Hand signs that mirror the voice commands. "left/right" are the rover's left/right,
# i.e. the rover turns toward the side you point to.
GESTURES = {
    "point_up":    ("face_on",    "index finger up"),
    "peace":       ("hand_on",    "peace sign (V)"),
    "fist":        ("follow_off", "fist"),
    "open_palm":   ("stop",       "open palm"),
    "thumbs_up":   ("zoom_in",    "thumbs up"),
    "thumbs_down": ("zoom_out",   "thumbs down"),
    "point_left":  ("turn_left",  "point left"),
    "point_right": ("turn_right", "point right"),
    "rock":        ("record_start", "rock sign (index + pinky)"),
    "ok_sign":     ("record_stop",  "OK sign"),
}
GESTURE_HOLD_SEC = 0.8     # hold a sign this long before it counts
OK_SIGN_REACH = 0.28      # OK sign: thumb tip must be this far (x palm size) from the index knuckle
GESTURE_HOLD_OVERRIDE = {  # signs that need a longer, deliberate hold
    "rock": 1.5, "ok_sign": 1.5,
}
GESTURE_REPEAT_SEC = 1.0   # turn/zoom repeat at this rate while you keep holding
GESTURE_GRACE_SEC = 0.25   # brief detection dropouts don't reset the hold timer
REPEATING = {"turn_left", "turn_right", "zoom_in", "zoom_out"}

# MediaPipe landmark indices: 0 wrist; thumb 1-4; index 5-8; middle 9-12; ring 13-16; pinky 17-20
_FINGERS = {"index": (5, 6, 8), "middle": (9, 10, 12), "ring": (13, 14, 16), "pinky": (17, 18, 20)}


def _angle(a, b, c):
    """Bend angle in degrees at b for the path a->b->c (0 = perfectly straight)."""
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n == 0:
        return 180.0
    cos = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cos))


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _direction(a, b):
    """'up' / 'down' / 'left' / 'right' (image coords) for vector a->b, or None if diagonal."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) > 1.3 * abs(dy):
        return "left" if dx < 0 else "right"
    if abs(dy) > 1.3 * abs(dx):
        return "up" if dy < 0 else "down"
    return None


def classify_gesture(pts):
    """Return a GESTURES key for one hand's 21 landmarks, or None."""
    wrist = pts[0]
    palm = _dist(wrist, pts[9])                      # hand scale
    if palm < 1e-6:
        return None

    def extended(mcp, pip, tip):
        return _angle(pts[mcp], pts[pip], pts[tip]) < 50 and \
               _dist(wrist, pts[tip]) > _dist(wrist, pts[pip]) * 1.1

    ext = {name: extended(*idx) for name, idx in _FINGERS.items()}
    thumb_out = (_angle(pts[2], pts[3], pts[4]) < 40 and
                 _dist(pts[4], pts[5]) > 0.6 * palm and
                 _dist(pts[4], pts[17]) > 1.0 * palm)
    n = sum(ext.values())

    if n == 4:
        return "open_palm"
    if n == 0:
        if thumb_out:
            d = _direction(pts[2], pts[4])
            if d == "up":
                return "thumbs_up"
            if d == "down":
                return "thumbs_down"
            return None
        return "fist"
    if ext["index"] and n == 1:
        d = _direction(pts[5], pts[8])
        if d == "up":
            return "point_up"
        if d in ("left", "right"):
            return "point_" + d
        return None
    if ext["index"] and ext["middle"] and n == 2:
        return "peace"
    if ext["index"] and ext["pinky"] and n == 2:
        return "rock"
    if (n == 3 and not ext["index"]
            and _dist(pts[4], pts[8]) < 0.35 * palm      # thumb tip touches index tip...
            and _dist(pts[4], pts[5]) > OK_SIGN_REACH * palm):  # ...out in front, not tucked in a fist
        return "ok_sign"                             # thumb + index circle, 3 fingers up
    return None


class GestureCommander:
    """Turns a per-frame gesture stream into commands: hold to fire, repeat for turn/zoom."""

    def __init__(self):
        self.current = None
        self.since = 0.0
        self.last_seen = 0.0
        self.fired_at = None

    def progress(self, now):
        """0..1 hold progress of the current gesture (for the overlay)."""
        if self.current is None or self.fired_at is not None:
            return 0.0
        hold = GESTURE_HOLD_OVERRIDE.get(self.current, GESTURE_HOLD_SEC)
        return min(1.0, (now - self.since) / hold)

    def update(self, gesture, now):
        """Feed this frame's gesture (or None). Returns (command, gesture) when one fires."""
        if gesture is None:
            if self.current is not None and now - self.last_seen > GESTURE_GRACE_SEC:
                self.current, self.fired_at = None, None
            return None
        if gesture != self.current:
            self.current, self.since, self.fired_at = gesture, now, None
        self.last_seen = now

        cmd = GESTURES[gesture][0]
        if self.fired_at is None:
            if now - self.since >= GESTURE_HOLD_OVERRIDE.get(gesture, GESTURE_HOLD_SEC):
                self.fired_at = now
                return cmd, gesture
        elif cmd in REPEATING and now - self.fired_at >= GESTURE_REPEAT_SEC:
            self.fired_at = now
            return cmd, gesture
        return None


# ----------------------------- control ----------------------------------------
def deadband(err, band):
    if abs(err) <= band:
        return 0.0
    return err - band if err > 0 else err + band


class PID:
    """PID with a filtered derivative, integral anti-windup, and output clamp."""

    def __init__(self, kp, ki, kd, out_limit, i_limit, d_tau):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.out_limit, self.i_limit, self.d_tau = out_limit, i_limit, d_tau
        self.reset()

    def reset(self):
        self.integral = 0.0
        self.prev_err = None
        self.prev_t = None
        self.d_filt = 0.0
        self.out = 0.0

    def update(self, err, t):
        dt = 0.0 if self.prev_t is None else t - self.prev_t
        if dt > 0:
            raw_d = (err - self.prev_err) / dt
            a = dt / (self.d_tau + dt)                       # first-order low-pass
            self.d_filt += a * (raw_d - self.d_filt)
            if self.ki > 0:
                self.integral += err * dt
                lim = self.i_limit / self.ki                 # cap the I *contribution*
                self.integral = max(-lim, min(lim, self.integral))
        self.prev_err, self.prev_t = err, t

        out = self.kp * err + self.ki * self.integral + self.kd * self.d_filt
        self.out = max(-self.out_limit, min(self.out_limit, out))
        return self.out


class TurnController:
    """Turn PID plus a hysteresis band so it doesn't chatter around center."""

    def __init__(self):
        self.pid = PID(KP_TURN, KI_TURN, KD_TURN, MAX_TURN, I_LIMIT, D_FILTER)
        self.centered = False

    def reset(self):
        self.pid.reset()
        self.centered = False

    def update(self, x_err, fresh, t):
        if self.centered:
            if abs(x_err) > X_EXIT:
                self.centered = False
                self.pid.reset()                             # fresh start, no stale D/I
        elif abs(x_err) < X_DEADBAND:
            self.centered = True
        if self.centered:
            return 0.0
        if fresh:                                            # only differentiate real detections
            return self.pid.update(x_err, t)
        return self.pid.out                                  # hold last output between detections


def compute_efforts(target, W, invert_turn, turn_ctrl, fresh, t, target_size, max_fwd):
    """Return (forward, turn, x_err, size) with efforts in -1..1."""
    cx, _, fw, _ = target
    x_err = (cx - W / 2) / (W / 2)          # -1 (far left) .. +1 (far right)
    size = fw / W
    size_err = target_size - size            # + = too small/far -> drive forward

    turn = turn_ctrl.update(x_err, fresh, t)
    fwd = max(-max_fwd, min(max_fwd, KP_FWD * deadband(size_err, SIZE_DEADBAND)))
    fwd *= max(0.0, 1.0 - abs(x_err) / 0.6)  # mostly turn first when target is off to a side
    if invert_turn:
        turn = -turn
    return fwd, turn, x_err, size


def effort_to_pwm(v):
    if abs(v) < 0.02:
        return 0
    pwm = MIN_PWM + abs(v) * (MAX_PWM - MIN_PWM)
    return int(round(pwm if v > 0 else -pwm))


def slew(current, target, up_step, down_step):
    """Ramp up gently, but allow braking (moving toward zero) much faster."""
    if target * current < 0:                 # reversing: brake to zero first, then ramp up
        return slew(current, 0.0, up_step, down_step)
    step = down_step if abs(target) < abs(current) else up_step
    return current + max(-step, min(step, target - current))


# ----------------------------- follow state -----------------------------------
class FollowState:
    """What the rover should be doing: follow mode, framing, any manual spin or drive,
    and the live-tunable limits. Voice, hand signs, keyboard, the local API and the app
    all feed commands into apply()."""

    MODES = ("off", "face", "hand")
    COMMANDS = ("face_on", "face_off", "hand_on", "hand_off", "follow_off", "zoom_in",
                "zoom_out", "turn_left", "turn_right", "stop")

    def __init__(self, mode, framing="medium"):
        self.mode = mode
        self.framing = framing
        self.face_size = FRAMING[framing]    # follow distance, as face width / frame width
        self.hand_size = HAND_TARGET_SIZE    # ...and as hand box width / frame width
        self.max_fwd = MAX_FWD
        self.max_turn = MAX_TURN
        self.manual_turn = 0.0               # effort while a manual spin is running
        self.manual_until = 0.0
        self.manual_fwd = 0.0                # effort while a zoom drive (closer/farther) runs
        self.manual_fwd_until = 0.0
        self.keep_new_distance = False       # after a zoom drive, follow from the new spot
        self.message = ""                    # last command, shown on screen for a bit
        self.message_until = 0.0

    def target_size(self):
        return self.hand_size if self.mode == "hand" else self.face_size

    def set_target_size(self, v):
        v = max(MIN_TARGET_SIZE, min(MAX_TARGET_SIZE, v))
        if self.mode == "hand":
            self.hand_size = v
        else:
            self.face_size, self.framing = v, "custom"

    def manual_active(self, now):
        return now < self.manual_until

    def drive_active(self, now):
        return now < self.manual_fwd_until

    def set_framing(self, framing):
        self.framing, self.face_size = framing, FRAMING[framing]

    def say(self, text):
        self.message = text
        self.message_until = time.time() + 2.5
        print(f"[command] {text}   (mode={self.mode}, distance={self.target_size():.2f})")

    def apply(self, cmd, source=None):
        now = time.time()
        if cmd == "face_on":
            self.mode = "face"
        elif cmd == "hand_on":
            self.mode = "hand"
        elif cmd == "face_off" and self.mode == "face":
            self.mode = "off"
        elif cmd == "hand_off" and self.mode == "hand":
            self.mode = "off"
        elif cmd == "follow_off":
            self.mode = "off"
        elif cmd in ("zoom_in", "zoom_out"):
            # physically drive toward (in) / away from (out) the creator for a moment
            self.manual_fwd = MANUAL_DRIVE_EFFORT * (1 if cmd == "zoom_in" else -1)
            self.manual_fwd_until = now + MANUAL_DRIVE_SEC
            self.keep_new_distance = KEEP_DISTANCE_AFTER_ZOOM
        elif cmd in ("turn_left", "turn_right"):
            self.manual_turn = MANUAL_TURN_EFFORT * (1 if cmd == "turn_right" else -1)
            self.manual_until = now + MANUAL_TURN_SEC
        elif cmd == "stop":
            self.mode = "off"
            self.manual_until = self.manual_fwd_until = 0.0
            self.keep_new_distance = False
        label = {"zoom_in": "zoom in (driving closer)",
                 "zoom_out": "zoom out (backing up)"}.get(cmd, cmd.replace("_", " "))
        self.say(f"{source} -> {label}" if source else label)
        return {"ok": True, "mode": self.mode, "target_size": round(self.target_size(), 3)}

    def as_dict(self, pid):
        return {"mode": self.mode, "framing": self.framing,
                "target_size": round(self.target_size(), 3),
                "max_fwd": round(self.max_fwd, 3), "max_turn": round(self.max_turn, 3),
                "kp": pid.kp, "ki": pid.ki, "kd": pid.kd}


def apply_settings(payload, follow, pid):
    changed = {}
    if "framing" in payload:
        f = str(payload["framing"]).lower()
        if f not in FRAMING:
            return {"error": f"framing must be one of {list(FRAMING)}"}
        follow.set_framing(f)
        changed["framing"] = f
    if "mode" in payload:
        m = str(payload["mode"]).lower()
        if m not in FollowState.MODES:
            return {"error": f"mode must be one of {list(FollowState.MODES)}"}
        follow.mode = m
        changed["mode"] = m
    limits = {"target_size": (MIN_TARGET_SIZE, MAX_TARGET_SIZE), "max_fwd": (0.0, 1.0),
              "max_turn": (0.0, 1.0), "kp": (0.0, 3.0), "ki": (0.0, 2.0), "kd": (0.0, 5.0)}
    for k, (lo, hi) in limits.items():
        if k not in payload:
            continue
        try:
            v = max(lo, min(hi, float(payload[k])))
        except (TypeError, ValueError):
            return {"error": f"{k} must be a number"}
        if k in ("kp", "ki", "kd"):
            setattr(pid, k, v)
            if k == "ki":
                pid.integral = 0.0
        elif k == "target_size":
            follow.set_target_size(v)
        else:
            setattr(follow, k, v)
        changed[k] = v
    pid.out_limit = follow.max_turn
    return {"ok": True, "changed": changed, "settings": follow.as_dict(pid)}


# ----------------------------- recording --------------------------------------
class WavSink:
    """Receives raw int16 mono chunks from the MicHub and writes a WAV file."""

    def __init__(self, path, rate):
        self.wf = wave.open(str(path), "wb")
        self.wf.setnchannels(1)
        self.wf.setsampwidth(2)
        self.wf.setframerate(rate)
        self.lock = threading.Lock()

    def __call__(self, data):
        with self.lock:
            if self.wf:
                self.wf.writeframes(data)

    def close(self):
        with self.lock:
            self.wf.close()
            self.wf = None


class SessionRecorder:
    def __init__(self, root, fps=REC_FPS, mic=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fps = fps
        self.mic = mic
        self.active = None

    @property
    def recording(self):
        return self.active is not None

    def start(self, frame_shape, title="", shot_idea_id=None, detector=""):
        if self.active:
            return self.active["meta"]
        sid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        d = self.root / sid
        d.mkdir(parents=True)
        H, W = frame_shape[:2]
        writer = cv2.VideoWriter(str(d / "raw.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                                 self.fps, (W, H))
        if not writer.isOpened():
            raise RuntimeError("could not open video writer")
        meta = {"id": sid, "title": title or "", "shot_idea_id": shot_idea_id,
                "started_at": datetime.now(timezone.utc).isoformat(), "state": "recording",
                "width": W, "height": H, "fps": self.fps, "detector": detector, "audio_offset": 0.0}
        sink = None
        if self.mic:
            sink = WavSink(d / "audio.wav", self.mic.rate)
            self.mic.subscribe(sink)
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        self.active = {"meta": meta, "dir": d, "writer": writer, "t0": time.time(), "frames": 0,
                       "track": open(d / "track.jsonl", "w"), "sink": sink}
        print(f"[rec] started {sid}" + ("" if sink else "  (no audio)"))
        return meta

    def write(self, frame, face, fresh):
        a = self.active
        if not a:
            return
        elapsed = time.time() - a["t0"]
        n = min(int(elapsed * self.fps) + 1 - a["frames"], self.fps)   # keep real-time pace
        if n <= 0:
            return
        for _ in range(n):
            a["writer"].write(frame)
        a["frames"] += n
        H, W = frame.shape[:2]
        box = None
        if face is not None:
            cx, cy, fw, fh = face
            box = [round(cx / W, 4), round(cy / H, 4), round(fw / W, 4), round(fh / H, 4)]
        a["track"].write(json.dumps({"t": round(elapsed, 3), "face": box, "fresh": fresh}) + "\n")

    def stop(self):
        a, self.active = self.active, None
        if not a:
            return None
        a["writer"].release()
        a["track"].close()
        if a["sink"]:
            self.mic.unsubscribe(a["sink"])
            a["sink"].close()
        meta = a["meta"]
        meta["duration_s"] = round(a["frames"] / self.fps, 2)
        meta["state"] = "processing"
        meta["dir"] = str(a["dir"])
        (a["dir"] / "meta.json").write_text(json.dumps(meta, indent=2))
        print(f"[rec] stopped {meta['id']} ({meta['duration_s']} s)")
        return meta


def finalize_session(meta, args, app):
    """Runs in a background thread after recording stops."""
    d = Path(meta["dir"])
    sb = app.sb
    owner_id, rover_id = supabase_sync.ids_from_env()

    def set_state(state, **extra):
        meta["state"] = state
        meta.update({k: v for k, v in extra.items() if k == "error"})
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        app.session_states[meta["id"]] = state
        if sb and owner_id:
            try:
                supabase_sync.mark_session(sb, meta, owner_id, rover_id, **extra)
            except Exception as e:
                print("[supabase]", e)

    try:
        set_state("processing")
        raw, wav, out = d / "raw.mp4", d / "audio.wav", d / "video.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw)]
            has_audio = wav.exists() and wav.stat().st_size > 1000
            if has_audio:
                cmd += ["-itsoffset", str(meta.get("audio_offset", 0)), "-i", str(wav)]
            cmd += ["-map", "0:v:0"]
            if has_audio:
                cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "160k", "-shortest"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
            subprocess.run(cmd, check=True)
            raw.unlink()
            if has_audio:
                wav.unlink()
        else:
            print("[rec] ffmpeg not found: keeping mp4v video (may not play in browsers), no audio mux")
            raw.replace(out)

        make_thumb(out, d / "thumb.jpg")      # phone app (BuildRoll) classifies takes from this

        if args.auto_clips:
            print(f"[clips] making clips for {meta['id']} ...")
            subprocess.run([sys.executable, str(HERE / "make_clips.py"), str(d)], check=True)

        if sb and owner_id:
            supabase_sync.sync_session(sb, d, owner_id, rover_id)
        set_state("ready")
        print(f"[rec] {meta['id']} ready: {d}")
    except Exception as e:
        print(f"[rec] processing failed for {meta['id']}: {e}")
        set_state("error", error=str(e)[:500])


# ----------------------------- app state + API --------------------------------
class App:
    """Shared state between the camera/control loop and the API threads."""

    def __init__(self, token=None):
        self.token = token
        self.lock = threading.Lock()
        self.status = {"state": "starting"}
        self.commands = queue.Queue()
        self.frame_cond = threading.Condition()
        self.latest_jpeg = None
        self.preview_clients = 0
        self.session_states = {}
        self.running = True
        self.sb = None
        self.drive = None                    # phone joystick (fwd, turn, time)
        self.public_url = None               # cloudflared tunnel address

    def call(self, name, payload=None, timeout=5.0):
        """Run a command inside the main loop and wait for its result."""
        item = {"name": name, "payload": payload or {}, "done": threading.Event(), "result": None}
        self.commands.put(item)
        if not item["done"].wait(timeout):
            return {"error": "timed out waiting for the control loop"}
        return item["result"]

    def get_status(self):
        with self.lock:
            return dict(self.status)


def make_handler(app, sessions_root):
    sessions_root = Path(sessions_root).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RoverStudio/1.1"

        def log_message(self, fmt, *a):     # keep the console quiet
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self, qs):
            if not app.token:
                return True
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {app.token}" or qs.get("token", [""])[0] == app.token

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._authed(qs):
                return self._json({"error": "unauthorized"}, 401)
            if u.path == "/api/status":
                return self._json(app.get_status())
            # ---- phone app (Workbench Rover Cam / BuildRoll) ----
            if u.path == "/status":
                return self._json(phone_status(app))
            if u.path == "/clips":
                return self._json({"clips": phone_clips(sessions_root, app)})
            if u.path.startswith("/clips/"):
                name = u.path[len("/clips/"):]
                sid, ext = os.path.splitext(name)
                return self._file(f"{sid}/{'video.mp4' if ext == '.mp4' else 'thumb.jpg'}")
            if u.path == "/api/sessions":
                return self._json(list_sessions(sessions_root, app))
            if u.path in ("/stream.mjpg", "/api/stream.mjpg"):
                return self._mjpeg()
            if u.path in ("/snapshot.jpg", "/api/snapshot.jpg"):
                jpg = app.latest_jpeg
                if jpg is None:
                    app.preview_clients += 1          # ask the loop for a frame
                    with app.frame_cond:
                        app.frame_cond.wait(1.0)
                    app.preview_clients -= 1
                    jpg = app.latest_jpeg
                if jpg is None:
                    return self._json({"error": "no frame yet"}, 503)
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)
                return
            if u.path.startswith("/files/"):
                return self._file(u.path[len("/files/"):])
            if u.path == "/":
                return self._json({"name": "rover studio", "endpoints": [
                    "GET /api/status", "GET /api/sessions", "GET /stream.mjpg", "GET /snapshot.jpg",
                    "GET /files/<session>/<file>", "POST /api/record/start", "POST /api/record/stop",
                    "POST /api/pause", "POST /api/estop", "POST /api/settings", "POST /api/follow"]})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authed(parse_qs(u.query)):
                return self._json({"error": "unauthorized"}, 401)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            if u.path == "/command":
                res = phone_command(app, payload)
                return self._json(res, 400 if isinstance(res, dict) and res.get("error") else 200)
            routes = {"/api/record/start": "record_start", "/api/record/stop": "record_stop",
                      "/api/pause": "pause", "/api/estop": "estop", "/api/settings": "settings",
                      "/api/follow": "follow"}
            if u.path not in routes:
                return self._json({"error": "not found"}, 404)
            res = app.call(routes[u.path], payload)
            return self._json(res, 400 if isinstance(res, dict) and res.get("error") else 200)

        def _mjpeg(self):
            self.send_response(200)
            self._cors()
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            app.preview_clients += 1
            try:
                last = None
                while app.running:
                    with app.frame_cond:
                        app.frame_cond.wait(1.0)
                    jpg = app.latest_jpeg
                    if jpg is None or jpg is last:
                        continue
                    last = jpg
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                app.preview_clients -= 1

        def _file(self, rel):
            path = (sessions_root / rel).resolve()
            if sessions_root not in path.parents or not path.is_file():
                return self._json({"error": "not found"}, 404)
            size = path.stat().st_size
            ctype = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".json": "application/json",
                     ".srt": "text/plain"}.get(path.suffix, "application/octet-stream")
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else max(0, size - int(b))
                end = int(b) if (a and b) else size - 1
                end = min(end, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self._cors()
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(1 << 16, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


# ----------------------------- phone app bridge --------------------------------
# The Workbench phone app (Rover Cam page) speaks a slightly different API:
#   GET /status, GET /clips, GET /clips/<id>.mp4|.jpg, POST /command {"cmd": ...}
def make_thumb(video, dest):
    try:
        cap = cv2.VideoCapture(str(video))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, n // 2))
        ok, fr = cap.read()
        cap.release()
        if ok:
            cv2.imwrite(str(dest), fr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    except Exception as e:
        print("[thumb]", e)


def phone_status(app):
    s = app.get_status()
    st = s.get("settings") or {}
    raw = s.get("state", "starting")
    state = {"idle": "off", "manual": "driving", "closer": "driving",
             "farther": "driving"}.get(raw, raw)
    if app.drive and time.time() - app.drive[2] < 0.5:
        state = "driving"
    rover = s.get("rover") or ""
    mode = s.get("mode", "off")
    label = {"tracking": f"{mode} follow - tracking", "searching": f"{mode} follow - searching",
             "off": "follow OFF", "paused": "PAUSED", "driving": "driving"}.get(state, state)
    return {
        "state": state, "label": label, "mode": mode, "paused": raw == "paused",
        "zoom": round(st.get("target_size", FRAMING["medium"]) / FRAMING["medium"], 2),
        "speed": round(st.get("max_fwd", MAX_FWD) / MAX_FWD, 2),
        "target": bool(s.get("target")), "x_err": s.get("x_err"), "size": s.get("size"),
        "target_size": st.get("target_size", FRAMING["medium"]),
        "centered": bool(s.get("centered")), "left_pwm": s.get("left_pwm", 0),
        "right_pwm": s.get("right_pwm", 0), "rover": rover, "blocked": bool(s.get("blocked")),
        "dry_run": "dry run" in rover, "recording": bool(s.get("recording")),
        "recording_s": s.get("recording_s", 0), "recording_title": s.get("recording_title"),
        "voice": bool(s.get("voice")), "gestures": bool(s.get("gestures")),
        "hand_available": bool(s.get("hand_available", True)),
        "message": s.get("last_command"), "fps": s.get("fps", 0), "public_url": app.public_url,
    }


def phone_clips(root, app):
    out = []
    for m in list_sessions(root, app):
        if m.get("state") == "recording":
            continue
        sid = m["id"]
        ready = m.get("state") == "ready" and (Path(root) / sid / "video.mp4").exists()
        out.append({"name": sid, "title": m.get("title") or "", "created": m.get("started_at", ""),
                    "duration": m.get("duration_s", 0), "ready": ready,
                    "video": f"/clips/{sid}.mp4",
                    "thumb": f"/clips/{sid}.jpg" if (Path(root) / sid / "thumb.jpg").exists() else None})
    return out


def phone_command(app, body):
    cmd = body.get("cmd")
    if cmd == "drive":
        clamp = lambda v: max(-1.0, min(1.0, float(v or 0)))
        app.drive = (clamp(body.get("fwd")), clamp(body.get("turn")), time.time())
        return {"ok": True}
    if cmd in ("stop", "estop"):
        app.drive = None
    if cmd == "zoom_set":
        return app.call("settings", {"target_size": FRAMING["medium"] * float(body.get("value", 1))})
    if cmd == "speed":
        return app.call("settings", {"max_fwd": MAX_FWD * float(body.get("value", 1))})
    if cmd in ("pause", "resume"):
        return app.call("pause", {"paused": cmd == "pause"})
    if cmd in ("record_start", "record_stop", "estop"):
        return app.call(cmd, body)
    if cmd in FollowState.COMMANDS:
        return app.call("follow", {"cmd": cmd})
    return {"error": f"unknown command {cmd}"}


def start_tunnel(app, port):
    import re
    exe = shutil.which("cloudflared")
    if not exe:
        print("--tunnel needs cloudflared:  brew install cloudflared")
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def watch():
        for line in proc.stderr:
            m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line)
            if m and not app.public_url:
                app.public_url = m.group(0)
                print("\n" + "=" * 64 + f"\n  Phone app: Rover Cam -> paste  {app.public_url}\n" + "=" * 64 + "\n")
    threading.Thread(target=watch, daemon=True).start()
    return proc


def list_sessions(root, app):
    out = []
    for d in sorted(Path(root).glob("*/meta.json"), reverse=True):
        try:
            meta = json.loads(d.read_text())
        except ValueError:
            continue
        sid = meta["id"]
        meta["state"] = app.session_states.get(sid, meta.get("state"))
        clips = d.parent / "clips.json"
        meta["clips"] = json.loads(clips.read_text()) if clips.exists() else []
        meta.pop("dir", None)
        out.append(meta)
    return out


# ----------------------------- overlay ----------------------------------------
def draw_overlay(frame, target, info_lines, target_size, recording):
    H, W = frame.shape[:2]
    cv2.line(frame, (W // 2, 0), (W // 2, H), (80, 80, 80), 1)
    band = int(X_DEADBAND * W / 2)
    cv2.line(frame, (W // 2 - band, 0), (W // 2 - band, H), (60, 60, 60), 1)
    cv2.line(frame, (W // 2 + band, 0), (W // 2 + band, H), (60, 60, 60), 1)
    tw = int(target_size * W)                # target size guide
    cv2.rectangle(frame, (W // 2 - tw // 2, H // 2 - tw // 2),
                  (W // 2 + tw // 2, H // 2 + tw // 2), (0, 200, 255), 1)
    if target is not None:
        cx, cy, fw, fh = target
        cv2.rectangle(frame, (int(cx - fw / 2), int(cy - fh / 2)),
                      (int(cx + fw / 2), int(cy + fh / 2)), (0, 255, 0), 2)
        cv2.circle(frame, (int(cx), int(cy)), 4, (0, 255, 0), -1)
    if recording and int(time.time() * 2) % 2 == 0:
        cv2.circle(frame, (W - 24, 22), 9, (0, 0, 255), -1)
        cv2.putText(frame, "REC", (W - 72, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
    for i, text in enumerate(info_lines):
        y = 22 + 22 * i
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)


def draw_gesture(frame, at, label, progress, fired):
    """Gesture name under the wrist, with a bar that fills while you hold it."""
    x, y = int(at[0]) - 60, min(frame.shape[0] - 30, int(at[1]) + 20)
    color = (0, 255, 0) if fired else (0, 200, 255)
    cv2.putText(frame, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3)
    cv2.putText(frame, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1)
    cv2.rectangle(frame, (x, y + 8), (x + 120, y + 16), (60, 60, 60), 1)
    fill = 120 if fired else int(120 * progress)
    cv2.rectangle(frame, (x, y + 8), (x + fill, y + 16), color, -1)


# ----------------------------- main -------------------------------------------
def parse_source(s):
    return int(s) if str(s).isdigit() else s


def parse_mic(s):
    if s is None:
        return None
    return int(s) if str(s).isdigit() else s


def main():
    supabase_sync.load_dotenv()
    ap = argparse.ArgumentParser(description="Drive the rover to follow a face or hand, and record.")
    ap.add_argument("--port", help="serial port of the Arduino (auto-detect if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--camera", default="0", help="webcam index, or a video file/URL for testing")
    ap.add_argument("--width", type=int, default=1280, help="camera width (1280+ gives sharper vertical clips)")
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--dry-run", action="store_true", help="don't open serial; just show what would be sent")
    ap.add_argument("--invert-turn", action="store_true", help="use if the rover turns away from the target")
    ap.add_argument("--no-window", action="store_true", help="run headless (Ctrl+C to quit)")
    ap.add_argument("--mode", choices=FollowState.MODES, default="face",
                    help="what to follow at startup (default: face)")
    ap.add_argument("--framing", choices=list(FRAMING), default="medium")
    ap.add_argument("--detector", choices=["auto", "yunet", "haar"], default="auto")
    # voice / gestures / microphone
    ap.add_argument("--no-voice", action="store_true", help="disable voice commands")
    ap.add_argument("--no-gestures", action="store_true", help="disable hand-sign commands")
    ap.add_argument("--always-listen", action="store_true",
                    help="don't require the wake word for voice commands while recording")
    ap.add_argument("--wake-word", default=WAKE_WORD)
    ap.add_argument("--mic", default=None, help="microphone device index or name (default: system mic)")
    ap.add_argument("--vosk-model", help="path to an unpacked Vosk model (default: auto-download small en-us)")
    ap.add_argument("--list-mics", action="store_true", help="print audio devices and exit")
    ap.add_argument("--no-audio", action="store_true", help="don't record microphone audio with the video")
    # recording / cloud / API
    ap.add_argument("--sessions-dir", default=str(HERE / "sessions"))
    ap.add_argument("--auto-clips", action="store_true", help="run make_clips.py after each recording")
    ap.add_argument("--api-host", default="127.0.0.1", help="0.0.0.0 to allow other devices on your network")
    ap.add_argument("--api-port", type=int, default=API_PORT)
    ap.add_argument("--api-token", default=os.environ.get("ROVER_API_TOKEN"),
                    help="require this bearer token on the local API (required with --api-host 0.0.0.0)")
    ap.add_argument("--no-api", action="store_true")
    ap.add_argument("--phone", action="store_true",
                    help="demo mode for the Workbench phone app: listen on the LAN without a token")
    ap.add_argument("--tunnel", action="store_true",
                    help="public https address for the phone app via cloudflared (implies --phone)")
    ap.add_argument("--no-cloud", action="store_true", help="ignore Supabase settings even if present")
    args = ap.parse_args()

    if args.list_mics:
        if sd is None:
            sys.exit(f"sounddevice is not available: {SD_ERROR}")
        print(sd.query_devices())
        return
    if args.tunnel:
        args.phone = True
    if args.phone and args.api_host == "127.0.0.1":
        args.api_host = "0.0.0.0"
    if args.api_host not in ("127.0.0.1", "localhost") and not args.api_token and not args.no_api \
            and not args.phone:
        sys.exit("Refusing to expose the API on the network without --api-token (it can drive the rover).")

    rover = None
    if not args.dry_run:
        if serial is None:
            sys.exit("pyserial is not installed: pip install pyserial  (or use --dry-run)")
        port = args.port or find_arduino_port()
        if not port:
            sys.exit("No Arduino found. Pass --port (e.g. COM5 or /dev/ttyACM0), or use --dry-run.")
        print(f"Opening {port} ...")
        rover = Rover(port, args.baud)

    source = parse_source(args.camera)
    cap = cv2.VideoCapture(source)
    if isinstance(source, int):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if not cap.isOpened():
        if rover:
            rover.close()
        sys.exit(f"Could not open camera {args.camera}")
    is_file = not isinstance(source, int)
    file_period = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30) if is_file else 0

    # ---- trackers ----
    trackers = {"face": FaceTracker(args.detector)}
    try:
        trackers["hand"] = HandTracker()
        print(f"Hand tracking: mediapipe ({trackers['hand'].api} API)")
    except Exception as e:
        print(f"Hand follow disabled: {e}")
    face_tracker = trackers["face"]

    # ---- microphone: shared by voice and recording ----
    mic = None
    if not (args.no_voice and args.no_audio):
        try:
            mic = MicHub(parse_mic(args.mic))
            print(f"Microphone: {mic.name} @ {mic.rate} Hz")
        except Exception as e:
            print(f"Microphone unavailable ({e}) - no voice commands, recordings without audio")

    voice_q = queue.Queue()                  # (command, heard_text, woke) from voice
    voice = None
    wake = None if args.always_listen else args.wake_word.lower().strip()
    if not args.no_voice and mic:
        try:
            voice = VoiceListener(voice_q, mic, args.vosk_model, args.wake_word.lower().strip())
            voice.start()
            print("Voice commands on. Say e.g. 'face follow on', 'zoom in', 'start recording', 'stop'."
                  + (f" While recording, start with '{wake}' (e.g. '{wake} stop recording')." if wake else ""))
        except Exception as e:               # missing package, model download failed
            voice = None
            print(f"Voice commands disabled: {e}")

    hands = trackers.get("hand")
    gestures_on = hands is not None and not args.no_gestures
    if gestures_on:
        print("Hand signs on: " + ", ".join(f"{d} = {c.replace('_', ' ')}"
                                            for c, d in GESTURES.values()))
    commander = GestureCommander()
    gesture_pt = None                        # where to draw the gesture label

    follow = FollowState(args.mode, args.framing)
    turn_ctrl = TurnController()
    turn_ctrl.pid.out_limit = follow.max_turn
    recorder = SessionRecorder(args.sessions_dir, mic=None if args.no_audio else mic)
    app = App(token=args.api_token)

    # ---- local API ----
    server = None
    if not args.no_api:
        server = ThreadingHTTPServer((args.api_host, args.api_port), make_handler(app, args.sessions_dir))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Control API: http://{args.api_host}:{args.api_port}   preview: /stream.mjpg")
    tunnel = start_tunnel(app, args.api_port) if (args.tunnel and server) else None
    if args.phone and not args.tunnel:
        import socket
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sk.connect(("8.8.8.8", 80))
            ip = sk.getsockname()[0]; sk.close()
        except OSError:
            ip = "localhost"
        print(f"Phone app (same Wi-Fi, app opened over http): http://{ip}:{args.api_port}")

    # ---- cloud link (Lovable / Supabase app) ----
    link = None
    if not args.no_cloud:
        app.sb = supabase_sync.from_env()
        owner_id, rover_id = supabase_sync.ids_from_env()
        if app.sb and owner_id and rover_id:
            link = supabase_sync.RoverLink(app.sb, rover_id, lambda c, p: app.call(c, p), app.get_status)
            link.start()
            print(f"Cloud link: rover {rover_id} connected to {os.environ['SUPABASE_URL']}")
        elif app.sb:
            print("Supabase configured but OWNER_ID / ROVER_ID missing - cloud link disabled.")
            app.sb = None

    active = None                            # tracker currently in use
    fwd_cmd = turn_cmd = 0.0
    left_pwm = right_pwm = 0
    paused = False
    last_send = last_preview = 0.0
    send_period = 1.0 / SEND_HZ
    fps_t, fps_n, fps = time.time(), 0, 0.0
    frame = None
    last_gesture = None

    def start_recording(payload, source=None):
        if frame is None:
            return {"error": "no camera frame yet"}
        if recorder.recording:
            return {"ok": True, "session": recorder.active["meta"]["id"], "note": "already recording"}
        if payload.get("framing") in FRAMING:
            follow.set_framing(payload["framing"])
        meta = recorder.start(frame.shape, payload.get("title", ""), payload.get("shot_idea_id"),
                              face_tracker.kind)
        app.session_states[meta["id"]] = "recording"
        follow.say(f"{source} -> recording" if source else "recording")
        if app.sb:
            o, r = supabase_sync.ids_from_env()
            threading.Thread(target=lambda: _safe(supabase_sync.mark_session, app.sb, dict(meta), o, r),
                             daemon=True).start()
        return {"ok": True, "session": meta["id"]}

    def stop_recording(source=None):
        meta = recorder.stop()
        if not meta:
            return {"ok": True, "note": "was not recording"}
        follow.say(f"{source} -> recording stopped" if source else "recording stopped")
        threading.Thread(target=finalize_session, args=(meta, args, app), daemon=True).start()
        return {"ok": True, "session": meta["id"]}

    def run_command(cmd, source=None, payload=None):
        """One entry point for voice, hand signs, keys, the local API and the app."""
        nonlocal paused
        payload = payload or {}
        if cmd == "record_start":
            return start_recording(payload, source)
        if cmd == "record_stop":
            return stop_recording(source)
        if cmd == "estop":
            paused = True
            follow.manual_until = 0.0
            if rover:
                rover.send(0, 0)
            follow.say(f"{source} -> EMERGENCY STOP (space / app to resume)" if source else "EMERGENCY STOP")
            return {"ok": True, "paused": True}
        if cmd == "pause":
            paused = bool(payload.get("paused", not paused))
            follow.say(("paused" if paused else "resumed") + (f" ({source})" if source else ""))
            return {"ok": True, "paused": paused}
        if cmd == "settings":
            return apply_settings(payload, follow, turn_ctrl.pid)
        if cmd == "follow":                      # app/API: {"cmd": "zoom_in"} etc.
            sub = payload.get("cmd")
            if sub not in FollowState.COMMANDS:
                return {"error": f"follow cmd must be one of {list(FollowState.COMMANDS)}"}
            return run_command(sub, source)
        if cmd in FollowState.COMMANDS:
            return follow.apply(cmd, source)
        return {"error": f"unknown command {cmd}"}

    keymap = {ord("="): "zoom_in", ord("+"): "zoom_in", ord("-"): "zoom_out",
              ord("a"): "turn_left", ord("d"): "turn_right", ord("s"): "stop"}

    try:
        while True:
            loop_t = time.time()
            ok, frame = cap.read()
            if not ok:
                print("Camera read failed" if not is_file else "End of video file")
                break
            W = frame.shape[1]

            # ---- commands: app / API ----
            while not app.commands.empty():
                item = app.commands.get_nowait()
                try:
                    item["result"] = run_command(item["name"], "app", item["payload"])
                except Exception as e:
                    item["result"] = {"error": str(e)}
                item["done"].set()

            # ---- commands: voice ----
            while not voice_q.empty():
                cmd, heard, woke = voice_q.get_nowait()
                if recorder.recording and wake and not woke:
                    follow.say(f'{heard} -> ignored while recording (say "{wake} ...")')
                    continue
                run_command(cmd, heard)

            # ---- commands: hand signs (one MediaPipe pass for follow + gestures) ----
            if hands and (gestures_on or follow.mode == "hand"):
                hands.process(frame)
            if gestures_on:
                gesture, gesture_pt = None, None
                for pts in hands.landmarks:
                    g = classify_gesture(pts)
                    if g == "open_palm" and follow.mode == "hand":
                        g = None             # open palm is the normal pose for hand follow
                    if g:
                        gesture, gesture_pt = g, pts[0]
                        break
                fired = commander.update(gesture, time.time())
                if fired:
                    cmd, g = fired
                    last_gesture = GESTURES[g][1]
                    run_command(cmd, f"sign: {GESTURES[g][1]}")
            if follow.mode == "hand" and "hand" not in trackers:
                follow.say("hand follow unavailable (see startup message) - staying off")
                follow.mode = "off"

            # ---- tracking ----
            tracker = trackers.get(follow.mode)
            if tracker is not active:        # mode changed: start clean
                for t in trackers.values():
                    t.reset()
                turn_ctrl.reset()
                active = tracker
            target = tracker.update(frame) if tracker else None

            # the recording's face track drives the vertical-clip crop, so keep it
            # even when following a hand or when follow is off
            if tracker is face_tracker:
                rec_face, rec_fresh = target, face_tracker.fresh
            elif recorder.recording:
                rec_face, rec_fresh = face_tracker.update(frame), face_tracker.fresh
            else:
                rec_face, rec_fresh = None, False
            recorder.write(frame, rec_face, rec_fresh)       # clean frame, before overlay

            # ---- control ----
            now = time.time()
            x_err = size = None
            jd = app.drive if (app.drive and now - app.drive[2] < 0.5) else None
            if paused:
                turn_ctrl.reset()
                fwd_t, turn_t = 0.0, 0.0
            elif jd:                             # phone joystick overrides following
                turn_ctrl.reset()
                fwd_t, turn_t = jd[0] * MAX_FWD, jd[1] * MAX_TURN
            elif follow.manual_active(now):
                turn_ctrl.reset()
                fwd_t = follow.manual_fwd if follow.drive_active(now) else 0.0
                turn_t = -follow.manual_turn if args.invert_turn else follow.manual_turn
            elif follow.drive_active(now):
                # zoom move: drive straight in/out, but keep steering to stay centered on you
                fwd_t = follow.manual_fwd
                if target is not None:
                    _, turn_t, x_err, size = compute_efforts(
                        target, W, args.invert_turn, turn_ctrl, tracker.fresh, now,
                        follow.target_size(), follow.max_fwd)
                else:
                    turn_ctrl.reset()
                    turn_t = 0.0
            elif target is not None:
                fwd_t, turn_t, x_err, size = compute_efforts(
                    target, W, args.invert_turn, turn_ctrl, tracker.fresh, now,
                    follow.target_size(), follow.max_fwd)
            else:
                turn_ctrl.reset()
                fwd_t, turn_t = 0.0, 0.0
            if target is not None and x_err is None:
                x_err, size = (target[0] - W / 2) / (W / 2), target[2] / W

            # zoom move finished: while following, hold the new distance instead of
            # driving back to the old one
            if follow.keep_new_distance and not follow.drive_active(now):
                follow.keep_new_distance = False
                if target is not None and follow.mode in ("face", "hand"):
                    follow.set_target_size(target[2] / W)
                    follow.say(f"holding new distance ({follow.mode} size {follow.target_size():.2f})")

            if now - last_send >= send_period:
                last_send = now
                fwd_cmd = slew(fwd_cmd, fwd_t, SLEW_PER_TICK, SLEW_DOWN_PER_TICK)
                turn_cmd = slew(turn_cmd, turn_t, SLEW_PER_TICK, SLEW_DOWN_PER_TICK)
                left, right = fwd_cmd + turn_cmd, fwd_cmd - turn_cmd
                m = max(1.0, abs(left), abs(right))     # keep within -1..1, preserve ratio
                left_pwm, right_pwm = effort_to_pwm(left / m), effort_to_pwm(right / m)
                if rover:
                    rover.send(left_pwm, right_pwm)

            if rover:
                rover.poll()

            fps_n += 1
            if now - fps_t >= 1.0:
                fps, fps_n, fps_t = fps_n / (now - fps_t), 0, now

            # ---- status for the app / API ----
            if paused:
                state = "paused"
            elif follow.manual_active(now):
                state = "manual"
            elif follow.drive_active(now):
                state = "closer" if follow.manual_fwd > 0 else "farther"
            elif follow.mode == "off":
                state = "idle"
            else:
                state = "tracking" if target is not None else "searching"
            rec = recorder.active["meta"] if recorder.recording else None
            with app.lock:
                app.status = {
                    "state": state, "mode": follow.mode,
                    "face": target is not None and follow.mode == "face",
                    "target": target is not None,
                    "x_err": None if x_err is None else round(x_err, 3),
                    "size": None if size is None else round(size, 3),
                    "centered": turn_ctrl.centered, "left_pwm": left_pwm, "right_pwm": right_pwm,
                    "recording": rec is not None, "session_id": rec["id"] if rec else None,
                    "recording_s": round(now - recorder.active["t0"], 1) if rec else 0,
                    "settings": follow.as_dict(turn_ctrl.pid),
                    "rover": rover.status if rover else "dry run - no serial",
                    "blocked": bool(rover and rover.blocked),
                    "detector": face_tracker.kind, "fps": round(fps, 1),
                    "voice": voice is not None, "gestures": gestures_on,
                    "wake_word": wake, "last_command": follow.message if now < follow.message_until else None,
                    "last_gesture": last_gesture,
                    "cloud": bool(link and not link.last_error),
                    "recording_title": rec.get("title") if rec else None,
                    "hand_available": "hand" in trackers,
                }

            # ---- window / preview ----
            need_preview = app.preview_clients > 0 and now - last_preview >= 1.0 / PREVIEW_FPS
            if not args.no_window or need_preview:
                if paused:
                    status = "PAUSED (space)"
                elif follow.manual_active(now):
                    status = "manual turn " + ("right" if follow.manual_turn > 0 else "left")
                elif follow.drive_active(now):
                    status = "zoom: " + ("driving closer" if follow.manual_fwd > 0 else "backing up")
                elif follow.mode == "off":
                    status = "follow OFF"
                else:
                    status = f"{follow.mode} follow - " + ("tracking" if target is not None
                                                          else f"no {follow.mode} - stopped")
                info = [
                    status + f"   {follow.framing} (target size {follow.target_size():.2f})",
                    f"L {left_pwm:+4d}   R {right_pwm:+4d}   {fps:4.1f} fps   [{face_tracker.kind}]",
                ]
                if x_err is not None:
                    info.append(f"x_err {x_err:+.2f}   size {size:.2f} "
                                f"(target {follow.target_size():.2f})"
                                + ("   CENTERED" if turn_ctrl.centered else ""))
                pid = turn_ctrl.pid
                info.append(f"turn PID  Kp {pid.kp:.2f} [ ]   Ki {pid.ki:.2f} , .   Kd {pid.kd:.2f} ; '")
                info.append(rover.status if rover else "DRY RUN - no serial")
                voice_txt = "voice: off"
                if voice:
                    voice_txt = (f'voice: say "{wake} ..."' if recorder.recording and wake
                                 else "voice: listening")
                info.append(voice_txt +
                            ("   signs: on" if gestures_on else
                             "   signs: off (--no-gestures)" if args.no_gestures else
                             "   signs: off (mediapipe missing)"))
                if now < follow.message_until:
                    info.append(follow.message)
                view = frame.copy()
                draw_overlay(view, target, info, follow.target_size(), recorder.recording)
                if gestures_on and commander.current and gesture_pt:
                    draw_gesture(view, gesture_pt, GESTURES[commander.current][1],
                                 commander.progress(now), commander.fired_at is not None)
                if need_preview:
                    last_preview = now
                    small = cv2.resize(view, (640, int(640 * view.shape[0] / view.shape[1])))
                    ok_j, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok_j:
                        app.latest_jpeg = buf.tobytes()
                        with app.frame_cond:
                            app.frame_cond.notify_all()
                if not args.no_window:
                    cv2.imshow("face follow", view)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), 27):
                        break
                    if key == ord(" "):
                        paused = not paused
                    elif key == ord("r"):
                        run_command("record_stop" if recorder.recording else "record_start", "key")
                    elif key == ord("f"):
                        follow.apply("face_off" if follow.mode == "face" else "face_on")
                    elif key == ord("h"):
                        follow.apply("hand_off" if follow.mode == "hand" else "hand_on")
                    elif key in (ord("1"), ord("2"), ord("3")):
                        apply_settings({"framing": ["close", "medium", "wide"][key - ord("1")]},
                                       follow, pid)
                        follow.say(f"framing {follow.framing}")
                    elif keymap.get(key):
                        follow.apply(keymap[key])
                    tune = {ord("["): ("kp", -0.05), ord("]"): ("kp", 0.05),
                            ord(","): ("ki", -0.02), ord("."): ("ki", 0.02),
                            ord(";"): ("kd", -0.02), ord("'"): ("kd", 0.02)}
                    if key in tune:
                        name, delta = tune[key]
                        setattr(pid, name, max(0.0, round(getattr(pid, name) + delta, 3)))
                        if name == "ki":
                            pid.integral = 0.0
                        print(f"turn PID: Kp={pid.kp:.2f} Ki={pid.ki:.2f} Kd={pid.kd:.2f}")

            if is_file:                                    # play test videos in real time
                time.sleep(max(0.0, file_period - (time.time() - loop_t)))
    except KeyboardInterrupt:
        pass
    finally:
        app.running = False
        if voice:
            voice.stop()
        if rover:
            rover.close()
        meta = recorder.stop()
        if meta:
            print("Finishing the last recording (don't close the window) ...")
            finalize_session(meta, args, app)
        if mic:
            mic.close()
        if link:
            link.stop()
        if server:
            server.shutdown()
        if tunnel:
            tunnel.terminate()
        cap.release()
        cv2.destroyAllWindows()
        while not app.commands.empty():            # answer anything still waiting
            item = app.commands.get_nowait()
            item["result"] = {"error": "shutting down"}
            item["done"].set()


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        print("[supabase]", e)


if __name__ == "__main__":
    main()
