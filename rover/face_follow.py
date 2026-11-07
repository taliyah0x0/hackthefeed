#!/usr/bin/env python3
"""
face_follow.py - keep a face centered and at a good size by driving the rover.

Camera is assumed to be mounted on the rover, facing forward.
  * Face left/right of center  -> rover spins left/right (tank turn)
  * Face too small (far)       -> rover drives forward
  * Face too big (close)       -> rover backs up

Talks to rover_face_follow.ino over USB serial with lines "V <left> <right>".

Can also follow a hand (MediaPipe) instead of a face, and takes voice commands
(Vosk, fully offline - the small English model downloads itself on first run):
  "face follow on/off"   "hand follow on/off"   "stop following"
  "zoom in" / "zoom out"   - get closer / farther (changes the target size)
  "turn left" / "turn right" - short manual spin
  "stop"                   - stop all motion and following

Hand signs do the same thing (hold the sign ~1 s; turn/zoom repeat while held):
  index finger up = face follow on       peace sign (V) = hand follow on
  fist            = stop following       open palm      = stop (ignored in hand follow)
  thumbs up / down = zoom in / out       point left / right = turn that way

Phone control (Workbench app -> Rover Cam): a small HTTP bridge (bridge.py) starts on
port 8765 with a live view, follow/zoom/drive/record controls and a list of recorded takes.
  python face_follow.py --tunnel     (https address for the published app; needs cloudflared)
  python face_follow.py --no-server  (no phone control)

Install:  pip install -r requirements.txt     (ffmpeg recommended so takes play on phones)
Run:      python face_follow.py                      (auto-detects the Arduino)
          python face_follow.py --port COM5          (Windows)
          python face_follow.py --port /dev/ttyACM0  (Linux)
          python face_follow.py --dry-run            (camera only, no motors - tune first!)
          python face_follow.py --mode off           (start idle, wait for a voice command)
          python face_follow.py --list-mics          (then --mic N to pick a microphone)

Keys in the video window:  q / Esc = quit    space = pause/resume motors
                           [ ]  , .  ; '  = tune turn Kp / Ki / Kd live
                           f / h = toggle face / hand follow   = / - = zoom in / out
                           a / d = turn left / right            s = stop
"""

import argparse
import json
import math
import os
import queue
import sys
import threading
import time

import cv2

from bridge import Bridge, Recorder, print_banner, start_tunnel

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

try:
    import mediapipe as mp                   # hand tracking (optional)
    _ = mp.solutions.hands                   # very new mediapipe versions dropped this API
    MP_ERROR = None
except Exception as e:                       # not installed, or installed but broken
    mp = None
    MP_ERROR = f"{type(e).__name__}: {e}"

try:
    import sounddevice as sd                 # microphone (optional)
    import vosk                              # offline speech recognition (optional)
except ImportError:
    sd = vosk = None


# ----------------------------- tuning -----------------------------------------
TARGET_SIZE = 0.22     # desired face width as a fraction of frame width
HAND_TARGET_SIZE = 0.20  # desired hand box size as a fraction of frame width
ZOOM_STEP = 1.2        # "zoom in/out" multiplies/divides the target size by this
ZOOM_MIN, ZOOM_MAX = 0.5, 2.5   # limits on the zoom factor

MANUAL_TURN_EFFORT = 0.5   # "turn left/right" spin effort (0..1)
MANUAL_TURN_SEC = 0.6      # ...and how long each spin lasts
MANUAL_DRIVE_MAX = 0.8     # phone joystick at full tilt (0..1)
SIZE_DEADBAND = 0.03   # +/- this around TARGET_SIZE counts as "good"
X_DEADBAND = 0.08      # |x_err| below this -> "centered", stop turning
X_EXIT = 0.15          # ...and only start turning again once |x_err| exceeds this (hysteresis)

# Turn PID (error = horizontal offset, -1..1). Live-tune in the window:
#   [ / ]  = KP down/up     ; / '  = KD down/up     , / .  = KI down/up
KP_TURN = 0.3          # turn effort per unit of horizontal error
KI_TURN = 0.1          # slowly pushes out small steady offsets the motors won't move for
KD_TURN = 0.9         # damping: brakes the turn as the face swings toward center
I_LIMIT = 0.2          # max effort the integral term may contribute (anti-windup)
D_FILTER = 0.1         # seconds; low-pass on the derivative (face boxes are jittery)

KP_FWD = 4.0           # drive effort per unit of size error
MAX_TURN = 0.7         # cap on turn effort (0..1)
MAX_FWD = 0.7          # cap on forward/back effort (0..1)

MIN_PWM = 90           # smallest PWM that actually moves your wheels (motor deadband)
MAX_PWM = 180          # PWM at full effort (Arduino test sketch used 160)
SLEW_PER_TICK = 0.15   # max *increase* in effort per command tick (smooths jerks)
SLEW_DOWN_PER_TICK = 0.5   # max *decrease* per tick - slow down fast so we don't overshoot

SEND_HZ = 20           # command rate; Arduino watchdog is 400 ms
LOST_TIMEOUT = 0.5     # seconds without a face before stopping
SMOOTHING = 0.4        # 0..1, higher = follow detections faster, lower = smoother
DETECT_WIDTH = 320     # frames are downscaled to this width for detection (speed)
# ------------------------------------------------------------------------------


# ----------------------------- voice ------------------------------------------
# Spoken phrase -> command. Vosk only listens for these phrases (a fixed grammar),
# which makes recognition fast and keeps random chatter from triggering anything.
VOICE_PHRASES = {
    "face follow on": "face_on",    "follow my face": "face_on",   "follow face": "face_on",
    "face follow off": "face_off",
    "hand follow on": "hand_on",    "follow my hand": "hand_on",   "follow hand": "hand_on",
    "hand follow off": "hand_off",
    "stop following": "follow_off", "follow off": "follow_off",
    "zoom in": "zoom_in",           "come closer": "zoom_in",
    "zoom out": "zoom_out",         "back up": "zoom_out",
    "turn left": "turn_left",       "turn right": "turn_right",
    "stop": "stop",
    "start recording": "record_start", "action": "record_start",
    "stop recording": "record_stop",   "cut": "record_stop",
}


def parse_voice(text):
    """Map recognized text to a command (longest matching phrase wins), or None."""
    text = " ".join(text.lower().split())
    for phrase in sorted(VOICE_PHRASES, key=len, reverse=True):
        if phrase in text:
            return VOICE_PHRASES[phrase]
    return None


class VoiceListener(threading.Thread):
    """Background thread: microphone -> Vosk -> (command, heard_text) into a queue."""

    def __init__(self, out_queue, model_path=None, device=None):
        super().__init__(daemon=True)
        if vosk is None:
            raise RuntimeError("voice needs: pip install vosk sounddevice")
        vosk.SetLogLevel(-1)
        # With no path, Vosk downloads its small English model (~40 MB) on first run.
        self.model = vosk.Model(model_path) if model_path else vosk.Model(lang="en-us")
        self.device = device
        info = sd.query_devices(device, "input")
        self.rate = int(info["default_samplerate"])
        grammar = json.dumps(sorted(VOICE_PHRASES) + ["[unk]"])
        self.rec = vosk.KaldiRecognizer(self.model, self.rate, grammar)
        self.out = out_queue
        self.audio = queue.Queue()
        self.running = True

    def _callback(self, indata, frames, time_info, status):
        self.audio.put(bytes(indata))

    def run(self):
        with sd.RawInputStream(samplerate=self.rate, blocksize=4000, device=self.device,
                               dtype="int16", channels=1, callback=self._callback):
            while self.running:
                try:
                    data = self.audio.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self.rec.AcceptWaveform(data):
                    text = json.loads(self.rec.Result()).get("text", "")
                    cmd = parse_voice(text)
                    if cmd:
                        self.out.put((cmd, f'heard "{text}"'))

    def stop(self):
        self.running = False


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


class BoxTracker:
    """Smooths detections and sticks to the same target. Subclasses implement detect()."""

    def __init__(self):
        self.state = None                    # smoothed (cx, cy, size, size) in full-frame px
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
    def __init__(self):
        super().__init__()
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self.cascade = cv2.CascadeClassifier(path)
        if self.cascade.empty():
            sys.exit(f"Could not load face model: {path}")

    def detect(self, frame):
        H, W = frame.shape[:2]
        scale = DETECT_WIDTH / W
        small = cv2.resize(frame, (DETECT_WIDTH, int(H * scale)))
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        faces = self.cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                              minSize=(24, 24))
        return [(x / scale, y / scale, w / scale, h / scale) for (x, y, w, h) in faces]


class HandTracker(BoxTracker):
    """Hand boxes from MediaPipe Hands. Box is made square (side = longer edge) so its
    width is a stable size measure whether the hand is open, closed, or sideways.

    Call process(frame) once per frame; both hand-follow (detect) and the gesture
    recognizer read the cached landmarks, so MediaPipe only runs once."""

    def __init__(self):
        super().__init__()
        if mp is None:
            raise RuntimeError(
                f"mediapipe failed to load in {sys.executable}\n    ({MP_ERROR})\n"
                f"    fix: {sys.executable} -m pip install mediapipe")
        self.hands = mp.solutions.hands.Hands(static_image_mode=False, max_num_hands=2,
                                              model_complexity=0,
                                              min_detection_confidence=0.6,
                                              min_tracking_confidence=0.5)
        self.landmarks = []                  # list of 21 (x, y) pixel points per hand

    def process(self, frame):
        H, W = frame.shape[:2]
        result = self.hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        self.landmarks = [[(p.x * W, p.y * H) for p in lm.landmark]
                          for lm in (result.multi_hand_landmarks or [])]
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
}
GESTURE_HOLD_SEC = 0.8     # hold a sign this long before it counts
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
        return min(1.0, (now - self.since) / GESTURE_HOLD_SEC)

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
            if now - self.since >= GESTURE_HOLD_SEC:
                self.fired_at = now
                return cmd, gesture
        elif cmd in REPEATING and now - self.fired_at >= GESTURE_REPEAT_SEC:
            self.fired_at = now
            return cmd, gesture
        return None


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
        if self.prev_t is None:
            dt = 0.0
        else:
            dt = t - self.prev_t
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


def compute_efforts(face, W, invert_turn, turn_ctrl, fresh, t, target_size):
    """Return (forward, turn, x_err, size) with efforts in -1..1."""
    cx, _, fw, _ = face
    x_err = (cx - W / 2) / (W / 2)          # -1 (far left) .. +1 (far right)
    size = fw / W
    size_err = target_size - size            # + = too small/far -> drive forward

    turn = turn_ctrl.update(x_err, fresh, t)
    fwd = max(-MAX_FWD, min(MAX_FWD, KP_FWD * deadband(size_err, SIZE_DEADBAND)))
    fwd *= max(0.0, 1.0 - abs(x_err) / 0.6)  # mostly turn first when face is off to a side
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


def draw_overlay(frame, face, info_lines, target_size):
    H, W = frame.shape[:2]
    cv2.line(frame, (W // 2, 0), (W // 2, H), (80, 80, 80), 1)
    band = int(X_DEADBAND * W / 2)
    cv2.line(frame, (W // 2 - band, 0), (W // 2 - band, H), (60, 60, 60), 1)
    cv2.line(frame, (W // 2 + band, 0), (W // 2 + band, H), (60, 60, 60), 1)
    tw = int(target_size * W)                # target size guide
    cv2.rectangle(frame, (W // 2 - tw // 2, H // 2 - tw // 2),
                  (W // 2 + tw // 2, H // 2 + tw // 2), (0, 200, 255), 1)
    if face is not None:
        cx, cy, fw, fh = face
        cv2.rectangle(frame, (int(cx - fw / 2), int(cy - fh / 2)),
                      (int(cx + fw / 2), int(cy + fh / 2)), (0, 255, 0), 2)
        cv2.circle(frame, (int(cx), int(cy)), 4, (0, 255, 0), -1)
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


class FollowState:
    """What the rover should be doing: follow mode, zoom, and any manual spin.
    Voice, hand gestures and keyboard all feed commands into apply()."""

    MODES = ("off", "face", "hand")

    def __init__(self, mode):
        self.mode = mode
        self.zoom = 1.0
        self.paused = False                  # wheels off; camera keeps tracking
        self.speed = 1.0                     # follow speed multiplier (phone slider)
        self.manual_turn = 0.0               # effort while a manual spin is running
        self.manual_until = 0.0
        self.message = ""                    # last command, shown on screen for a bit
        self.message_until = 0.0

    def target_size(self):
        base = HAND_TARGET_SIZE if self.mode == "hand" else TARGET_SIZE
        return base * self.zoom

    def manual_active(self, now):
        return now < self.manual_until

    def apply(self, cmd, source=None, payload=None):
        now = time.time()
        payload = payload or {}

        def num(lo, hi, default):
            try:
                return max(lo, min(hi, float(payload.get("value", default))))
            except (TypeError, ValueError):
                return default

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
        elif cmd == "zoom_in":
            self.zoom = min(ZOOM_MAX, self.zoom * ZOOM_STEP)
        elif cmd == "zoom_out":
            self.zoom = max(ZOOM_MIN, self.zoom / ZOOM_STEP)
        elif cmd in ("turn_left", "turn_right"):
            self.manual_turn = MANUAL_TURN_EFFORT * (1 if cmd == "turn_right" else -1)
            self.manual_until = now + MANUAL_TURN_SEC
        elif cmd == "zoom_set":
            self.zoom = num(ZOOM_MIN, ZOOM_MAX, self.zoom)
        elif cmd == "speed":
            self.speed = num(0.2, 1.0, self.speed)
        elif cmd == "stop":
            self.mode = "off"
            self.manual_until = 0.0
        elif cmd == "estop":
            self.mode = "off"
            self.manual_until = 0.0
            self.paused = True
        elif cmd == "pause":
            self.paused = True
        elif cmd == "resume":
            self.paused = False
        label = cmd.replace("_", " ")
        self.message = f"{source} -> {label}" if source else label
        self.message_until = now + 2.5
        print(f"[command] {self.message}   (mode={self.mode}, zoom={self.zoom:.2f})")


def main():
    ap = argparse.ArgumentParser(description="Drive the rover to keep a face centered.")
    ap.add_argument("--port", help="serial port of the Arduino (auto-detect if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--camera", default="0", help="webcam index, or a video file to test with")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--dry-run", action="store_true", help="don't open serial; just show what would be sent")
    ap.add_argument("--invert-turn", action="store_true", help="use if the rover turns away from the face")
    ap.add_argument("--no-window", action="store_true", help="run headless (Ctrl+C to quit)")
    ap.add_argument("--mode", choices=FollowState.MODES, default="face",
                    help="what to follow at startup (default: face)")
    ap.add_argument("--no-voice", action="store_true", help="disable voice commands")
    ap.add_argument("--no-gestures", action="store_true", help="disable hand-sign commands")
    ap.add_argument("--mic", type=int, default=None, help="microphone device index (default: system mic)")
    ap.add_argument("--vosk-model", help="path to an unpacked Vosk model (default: auto-download small en-us)")
    ap.add_argument("--list-mics", action="store_true", help="print audio devices and exit")
    ap.add_argument("--no-server", action="store_true", help="don't start the phone-app bridge")
    ap.add_argument("--http-port", type=int, default=8765, help="phone-app bridge port (default 8765)")
    ap.add_argument("--tunnel", action="store_true",
                    help="expose the bridge at a public https address (needs cloudflared)")
    ap.add_argument("--app-url", help="your Workbench app URL, to print a one-tap phone link")
    ap.add_argument("--clips-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "clips"),
                    help="where recorded takes are saved")
    args = ap.parse_args()

    if args.list_mics:
        if sd is None:
            sys.exit("sounddevice is not installed: pip install sounddevice")
        print(sd.query_devices())
        return

    rover = None
    if not args.dry_run:
        if serial is None:
            sys.exit("pyserial is not installed: pip install pyserial  (or use --dry-run)")
        port = args.port or find_arduino_port()
        if not port:
            sys.exit("No Arduino found. Pass --port (e.g. COM5 or /dev/ttyACM0), or use --dry-run.")
        print(f"Opening {port} ...")
        rover = Rover(port, args.baud)

    cam_is_file = not args.camera.isdigit()
    cap = cv2.VideoCapture(args.camera if cam_is_file else int(args.camera))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    if not cap.isOpened():
        if rover:
            rover.close()
        sys.exit(f"Could not open camera {args.camera}")

    trackers = {"face": FaceTracker()}
    try:
        trackers["hand"] = HandTracker()
    except RuntimeError as e:
        print(f"Hand follow disabled: {e}")

    commands = queue.Queue()                 # (command, source[, payload]) from voice + phone
    voice = None
    if not args.no_voice:
        try:
            voice = VoiceListener(commands, args.vosk_model, args.mic)
            voice.start()
            print("Voice commands on. Say e.g. 'face follow on', 'hand follow on', "
                  "'zoom in', 'turn left', 'start recording', 'stop'.")
        except Exception as e:               # missing package, no mic, model download failed
            voice = None
            print(f"Voice commands disabled: {e}")

    bridge = tunnel = None
    if not args.no_server:
        try:
            bridge = Bridge(args.http_port, args.clips_dir, commands)
            bridge.start()
            if args.tunnel:
                tunnel = start_tunnel(bridge, args.app_url)
            print_banner(bridge, args.app_url)
        except OSError as e:
            bridge = None
            print(f"Phone bridge disabled (port {args.http_port}: {e})")
    recorder = bridge.recorder if bridge else Recorder(args.clips_dir)

    hands = trackers.get("hand")
    gestures_on = hands is not None and not args.no_gestures
    if gestures_on:
        print("Hand signs on: " + ", ".join(f"{d} = {c.replace('_', ' ')}"
                                            for c, d in GESTURES.values()))
    commander = GestureCommander()
    gesture_pt = None                        # where to draw the gesture label

    follow = FollowState(args.mode)
    turn_ctrl = TurnController()
    active = None                            # tracker currently in use
    fwd_cmd = turn_cmd = 0.0
    left_pwm = right_pwm = 0
    last_send = 0.0
    send_period = 1.0 / SEND_HZ
    fps, last_frame_t = 20.0, time.time()
    last_status = 0.0

    keymap = {ord("="): "zoom_in", ord("+"): "zoom_in", ord("-"): "zoom_out",
              ord("a"): "turn_left", ord("d"): "turn_right", ord("s"): "stop",
              ord("r"): "record_toggle"}

    try:
        while True:
            ok, frame = cap.read()
            if not ok and cam_is_file:       # test video: loop it
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok:
                print("Camera read failed")
                break
            W = frame.shape[1]
            t = time.time()
            fps = 0.9 * fps + 0.1 / max(1e-3, t - last_frame_t)
            last_frame_t = t
            if cam_is_file:
                time.sleep(max(0.0, 1 / 30 - (time.time() - t)))

            recorder.write(frame)            # clean frame, before any overlay
            if bridge:
                bridge.set_raw(frame.copy())

            while not commands.empty():
                item = commands.get_nowait()
                cmd, src = item[0], item[1]
                payload = item[2] if len(item) > 2 else {}
                if cmd == "record_toggle":
                    cmd = "record_stop" if recorder.active else "record_start"
                if cmd == "record_start":
                    recorder.start(frame, fps, payload.get("title", ""))
                elif cmd == "record_stop":
                    recorder.stop(frame)
                follow.apply(cmd, src, payload)

            if hands and (gestures_on or follow.mode == "hand"):
                hands.process(frame)         # one MediaPipe pass for follow + gestures
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
                    follow.apply(cmd, f"sign: {GESTURES[g][1]}")
            if follow.mode == "hand" and "hand" not in trackers:
                print("Hand follow unavailable (install mediapipe) - staying off")
                follow.mode = "off"

            tracker = trackers.get(follow.mode)
            if tracker is not active:        # mode changed: start clean
                for tr in trackers.values():
                    tr.reset()
                turn_ctrl.reset()
                active = tracker
            target = tracker.update(frame) if tracker else None

            now = time.time()
            drive = bridge.get_drive(now) if bridge else None
            x_err = size = None
            if follow.paused:
                turn_ctrl.reset()
                fwd_t, turn_t = 0.0, 0.0
            elif drive:                      # phone joystick overrides following
                turn_ctrl.reset()
                fwd_t = drive[0] * MANUAL_DRIVE_MAX
                turn_t = drive[1] * MANUAL_DRIVE_MAX
                if args.invert_turn:
                    turn_t = -turn_t
            elif follow.manual_active(now):
                turn_ctrl.reset()
                fwd_t = 0.0
                turn_t = -follow.manual_turn if args.invert_turn else follow.manual_turn
            elif target is not None:
                fwd_t, turn_t, x_err, size = compute_efforts(
                    target, W, args.invert_turn, turn_ctrl, tracker.fresh, now,
                    follow.target_size())
                fwd_t *= follow.speed
                turn_t *= follow.speed
            else:
                turn_ctrl.reset()
                fwd_t, turn_t = 0.0, 0.0

            if now - last_send >= send_period:
                last_send = now
                fwd_cmd = slew(fwd_cmd, fwd_t, SLEW_PER_TICK, SLEW_DOWN_PER_TICK)
                turn_cmd = slew(turn_cmd, turn_t, SLEW_PER_TICK, SLEW_DOWN_PER_TICK)
                if follow.paused:            # pause / e-stop: no ramp-down, stop now
                    fwd_cmd = turn_cmd = 0.0
                left, right = fwd_cmd + turn_cmd, fwd_cmd - turn_cmd
                m = max(1.0, abs(left), abs(right))     # keep within -1..1, preserve ratio
                left_pwm, right_pwm = effort_to_pwm(left / m), effort_to_pwm(right / m)
                if rover:
                    rover.send(left_pwm, right_pwm)

            if rover:
                rover.poll()

            if follow.paused:
                status = "PAUSED"
            elif drive:
                status = "joystick"
            elif follow.manual_active(now):
                status = "manual turn " + ("right" if follow.manual_turn > 0 else "left")
            elif follow.mode == "off":
                status = "follow OFF"
            else:
                status = f"{follow.mode} follow - " + ("tracking" if target is not None
                                                      else f"no {follow.mode} - stopped")

            if bridge and now - last_status >= 0.1:
                last_status = now
                rover_text = rover.status if rover else "dry run - no motors"
                bridge.set_status({
                    "state": ("paused" if follow.paused else "driving" if drive or follow.manual_active(now)
                              else "off" if follow.mode == "off"
                              else "tracking" if target is not None else "searching"),
                    "label": status,
                    "mode": follow.mode,
                    "paused": follow.paused,
                    "zoom": round(follow.zoom, 2),
                    "speed": round(follow.speed, 2),
                    "target": target is not None,
                    "x_err": None if x_err is None else round(x_err, 3),
                    "size": None if size is None else round(size, 3),
                    "target_size": round(follow.target_size(), 3),
                    "centered": turn_ctrl.centered,
                    "left_pwm": left_pwm, "right_pwm": right_pwm,
                    "rover": rover_text,
                    "blocked": "BLOCKED" in rover_text.upper(),
                    "dry_run": rover is None,
                    "recording": recorder.active,
                    "recording_s": round(recorder.elapsed(), 1),
                    "recording_title": recorder.title if recorder.active else None,
                    "voice": voice is not None,
                    "gestures": gestures_on,
                    "hand_available": "hand" in trackers,
                    "message": follow.message if now < follow.message_until else None,
                    "fps": round(fps, 1),
                })
            if bridge and bridge.wants_view(now):
                view = frame.copy()
                draw_overlay(view, target, [], follow.target_size())
                if recorder.active:
                    cv2.circle(view, (view.shape[1] - 22, 22), 9, (0, 0, 255), -1)
                bridge.set_view(view, now)

            if not args.no_window:
                info = [
                    status + f"   zoom x{follow.zoom:.2f}   speed {follow.speed:.1f}",
                    f"L {left_pwm:+4d}   R {right_pwm:+4d}" +
                    (f"   REC {recorder.elapsed():.0f}s" if recorder.active else ""),
                ]
                if x_err is not None:
                    info.append(f"x_err {x_err:+.2f}   size {size:.2f} "
                                f"(target {follow.target_size():.2f})"
                                + ("   CENTERED" if turn_ctrl.centered else ""))
                pid = turn_ctrl.pid
                info.append(f"turn PID  Kp {pid.kp:.2f} [ ]   Ki {pid.ki:.2f} , .   Kd {pid.kd:.2f} ; '")
                info.append(rover.status if rover else "DRY RUN - no serial")
                info.append(("voice: listening" if voice else "voice: off") +
                            ("   signs: on" if gestures_on else
                             "   signs: off (--no-gestures)" if args.no_gestures else
                             "   signs: off (mediapipe missing)") +
                            (f"   phone: :{args.http_port}" if bridge else ""))
                if now < follow.message_until:
                    info.append(follow.message)
                draw_overlay(frame, target, info, follow.target_size())
                if gestures_on and commander.current and gesture_pt:
                    draw_gesture(frame, gesture_pt, GESTURES[commander.current][1],
                                 commander.progress(now), commander.fired_at is not None)
                cv2.imshow("face follow", frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord(" "):
                    follow.apply("resume" if follow.paused else "pause", "key")
                elif key == ord("f"):
                    follow.apply("face_off" if follow.mode == "face" else "face_on")
                elif key == ord("h"):
                    follow.apply("hand_off" if follow.mode == "hand" else "hand_on")
                elif keymap.get(key):
                    commands.put((keymap[key], "key"))
                tune = {ord("["): ("kp", -0.05), ord("]"): ("kp", 0.05),
                        ord(","): ("ki", -0.02), ord("."): ("ki", 0.02),
                        ord(";"): ("kd", -0.02), ord("'"): ("kd", 0.02)}
                if key in tune:
                    name, delta = tune[key]
                    setattr(pid, name, max(0.0, round(getattr(pid, name) + delta, 3)))
                    if name == "ki":
                        pid.integral = 0.0
                    print(f"turn PID: Kp={pid.kp:.2f} Ki={pid.ki:.2f} Kd={pid.kd:.2f}")
    except KeyboardInterrupt:
        pass
    finally:
        recorder.stop()
        if voice:
            voice.stop()
        if bridge:
            bridge.close()
        if tunnel:
            tunnel.terminate()
        if rover:
            rover.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
