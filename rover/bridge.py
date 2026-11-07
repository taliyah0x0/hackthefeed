"""
bridge.py - lets the Workbench phone app (Rover Cam page) control face_follow.py.

Runs a small HTTP server next to the control loop (stdlib only, no extra installs):

  GET  /status         live JSON: mode, tracking, x_err, PWM, sonar, recording...
  GET  /stream.mjpg    live camera with the tracking box drawn on it
  GET  /snapshot.jpg   one clean frame (no overlay) - used for AI framing checks
  GET  /clips          recorded takes, newest first
  GET  /clips/<file>   a take's .mp4 (supports Range, so phones can scrub) or .jpg thumbnail
  POST /command        {"cmd": "...", ...}   see COMMANDS below

The phone app is served over HTTPS, so browsers block calls to a plain http:// LAN
address. Run with --tunnel (needs `brew install cloudflared`) to get a free
https://<random>.trycloudflare.com address that works from any phone, on any network.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, quote

import cv2

# Commands the app may send. Most are the same ones voice and hand signs use.
COMMANDS = {
    "face_on", "face_off", "hand_on", "hand_off", "follow_off",
    "zoom_in", "zoom_out", "zoom_set",          # zoom_set: {"value": 0.5..2.5}
    "turn_left", "turn_right",
    "stop",                                     # stop following + motion
    "estop",                                    # stop everything and pause wheels
    "pause", "resume",                          # wheels off/on, camera keeps tracking
    "speed",                                    # {"value": 0.2..1.0} follow speed
    "record_start", "record_stop",              # record_start: {"title": "..."}
    "drive",                                    # {"fwd": -1..1, "turn": -1..1}, resend < 0.5 s
}
DRIVE_HOLD_SEC = 0.5        # a joystick command is valid this long (phone resends ~8x/s)
STREAM_FPS = 15
STREAM_WIDTH = 640


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ----------------------------- recording ---------------------------------------
class Recorder:
    """Writes takes to <folder>/rover_YYYYmmdd_HHMMSS.mp4 (+ .jpg thumbnail, + .json info).
    Frames are written at a fixed rate (duplicating/skipping as needed) so a take's
    length matches real time even when face detection makes the loop rate wobble."""

    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        os.makedirs(self.folder, exist_ok=True)
        self.writer = None
        self.name = None
        self.title = ""
        self.fps = 20.0
        self.started = 0.0
        self.written = 0
        self.thumb_done = False
        self.tmp_path = None                     # set when we must convert to H.264 afterwards

    @property
    def active(self):
        return self.writer is not None

    def elapsed(self):
        return time.time() - self.started if self.active else 0.0

    def _path(self, ext):
        return os.path.join(self.folder, f"{self.name}.{ext}")

    def start(self, frame, fps, title=""):
        if self.active:
            return
        H, W = frame.shape[:2]
        self.name = time.strftime("rover_%Y%m%d_%H%M%S")
        self.title = (title or "").strip()[:120]
        self.fps = max(8.0, min(30.0, round(fps or 20.0)))
        # H.264 ("avc1") plays in every phone browser. If this OpenCV build can't write it,
        # record MPEG-4 and convert with ffmpeg when the take ends.
        self.writer = cv2.VideoWriter(self._path("mp4"), cv2.VideoWriter_fourcc(*"avc1"), self.fps, (W, H))
        self.tmp_path = None
        if not self.writer.isOpened():
            self.tmp_path = self._path("raw.mp4")
            self.writer = cv2.VideoWriter(self.tmp_path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (W, H))
        if not self.writer.isOpened():
            self.writer = None
            print("[record] could not open a video writer")
            return
        self.started = time.time()
        self.written = 0
        self.thumb_done = False
        self._write_info(ready=False)
        print(f"[record] started {self.name}" + (f'  "{self.title}"' if self.title else ""))

    def write(self, frame):
        if not self.active:
            return
        due = int((time.time() - self.started) * self.fps) + 1
        for _ in range(min(5, due - self.written)):
            self.writer.write(frame)
            self.written += 1
        if not self.thumb_done and time.time() - self.started > 0.8:
            cv2.imwrite(self._path("jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            self.thumb_done = True

    def stop(self, frame=None):
        if not self.active:
            return
        self.writer.release()
        self.writer = None
        if not self.thumb_done and frame is not None:
            cv2.imwrite(self._path("jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        duration = self.written / self.fps
        name, tmp = self.name, self.tmp_path
        print(f"[record] stopped {name} ({duration:.1f}s)")
        if tmp:
            threading.Thread(target=self._convert, args=(name, tmp, duration), daemon=True).start()
        else:
            self._write_info(ready=True, duration=duration)

    def _convert(self, name, tmp, duration):
        out = os.path.join(self.folder, f"{name}.mp4")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", tmp, "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out])
        if os.path.exists(out):
            os.remove(tmp)
        else:
            os.replace(tmp, out)                 # no ffmpeg: keep MPEG-4 (may not play in browsers)
            print("[record] install ffmpeg so takes play on phones (brew install ffmpeg)")
        self._write_info(ready=True, duration=duration, name=name)

    def _write_info(self, ready, duration=0.0, name=None):
        name = name or self.name
        path = os.path.join(self.folder, f"{name}.json")
        info = {}
        if os.path.exists(path):
            with open(path) as f:
                info = json.load(f)
        info.update({"name": name, "title": info.get("title", self.title),
                     "created": info.get("created", time.strftime("%Y-%m-%dT%H:%M:%S")),
                     "duration": round(duration, 1), "ready": ready})
        with open(path, "w") as f:
            json.dump(info, f)

    def list(self):
        out = []
        for fn in os.listdir(self.folder):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.folder, fn)) as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            n = info.get("name")
            if not n or (self.active and n == self.name):
                continue
            info["video"] = f"/clips/{n}.mp4"
            info["thumb"] = f"/clips/{n}.jpg" if os.path.exists(os.path.join(self.folder, f"{n}.jpg")) else None
            out.append(info)
        return sorted(out, key=lambda c: c["name"], reverse=True)


# ----------------------------- server ------------------------------------------
class Bridge:
    def __init__(self, port, clips_dir, commands):
        self.port = port
        self.commands = commands                 # shared with voice: (cmd, source, payload)
        self.recorder = Recorder(clips_dir)
        self.lock = threading.Condition()
        self.view_jpeg = None                    # latest annotated frame for /stream.mjpg
        self.view_id = 0
        self.raw = None                          # latest clean frame for /snapshot.jpg
        self.viewers = 0
        self.status = {}
        self.drive = None                        # (fwd, turn, t)
        self.public_url = None
        self._last_view = 0.0
        self.server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(self))
        self.server.daemon_threads = True

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    # --- called from the control loop ---
    def wants_view(self, now):
        return self.viewers > 0 and now - self._last_view >= 1.0 / STREAM_FPS

    def set_view(self, frame, now):
        self._last_view = now
        H, W = frame.shape[:2]
        if W > STREAM_WIDTH:
            frame = cv2.resize(frame, (STREAM_WIDTH, int(H * STREAM_WIDTH / W)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self.lock:
                self.view_jpeg = buf.tobytes()
                self.view_id += 1
                self.lock.notify_all()

    def set_raw(self, frame):
        self.raw = frame

    def set_status(self, status):
        status["public_url"] = self.public_url
        self.status = status

    def get_drive(self, now):
        d = self.drive
        if d and now - d[2] < DRIVE_HOLD_SEC and (abs(d[0]) > 0.02 or abs(d[1]) > 0.02):
            return d[0], d[1]
        return None

    # --- called from HTTP threads ---
    def handle_command(self, body):
        cmd = body.get("cmd")
        if cmd not in COMMANDS:
            raise ValueError(f"unknown command: {cmd}")
        if cmd == "drive":
            clamp = lambda v: max(-1.0, min(1.0, float(v or 0)))
            self.drive = (clamp(body.get("fwd")), clamp(body.get("turn")), time.time())
            return
        if cmd in ("estop", "stop"):
            self.drive = None                    # takes effect before the loop even sees it
        self.commands.put((cmd, "app", body))


def _make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):               # keep the terminal readable
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length")

        def _json(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path in ("/", "/status"):
                    self._json(bridge.status or {"state": "starting"})
                elif path == "/stream.mjpg":
                    self._stream()
                elif path == "/snapshot.jpg":
                    self._snapshot()
                elif path == "/clips":
                    self._json({"clips": bridge.recorder.list()})
                elif path.startswith("/clips/"):
                    self._file(path[len("/clips/"):])
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if urlparse(self.path).path != "/command":
                return self._json({"error": "not found"}, 404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
                bridge.handle_command(body)
                self._json({"ok": True})
            except (ValueError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)

        def _snapshot(self):
            frame = bridge.raw
            if frame is None:
                return self._json({"error": "no frame yet"}, 503)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            data = buf.tobytes()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream(self):
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            bridge.viewers += 1
            last = -1
            try:
                while True:
                    with bridge.lock:
                        bridge.lock.wait_for(lambda: bridge.view_id != last, timeout=2.0)
                        jpeg, last = bridge.view_jpeg, bridge.view_id
                    if jpeg is None:
                        continue
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg + b"\r\n")
            finally:
                bridge.viewers -= 1

        def _file(self, name):
            m = re.fullmatch(r"([\w\-]+)\.(mp4|jpg)", name)
            path = os.path.join(bridge.recorder.folder, name)
            if not m or not os.path.isfile(path):
                return self._json({"error": "not found"}, 404)
            size = os.path.getsize(path)
            start, end = 0, size - 1
            rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = int(rng.group(2)) if rng.group(2) else size - 1
                else:
                    start = max(0, size - int(rng.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self._cors()
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "video/mp4" if m.group(2) == "mp4" else "image/jpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Disposition", f'inline; filename="{quote(name)}"')
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

    return Handler


# ----------------------------- tunnel ------------------------------------------
def start_tunnel(bridge, app_url=None):
    """Start a free Cloudflare quick tunnel so the HTTPS app can reach this laptop."""
    exe = shutil.which("cloudflared")
    if not exe:
        print("--tunnel needs cloudflared:  brew install cloudflared   "
              "(Windows: winget install Cloudflare.cloudflared)")
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{bridge.port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def watch():
        for line in proc.stderr:
            m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line)
            if m and not bridge.public_url:
                bridge.public_url = m.group(0)
                print_banner(bridge, app_url)
    threading.Thread(target=watch, daemon=True).start()
    return proc


def print_banner(bridge, app_url=None):
    url = bridge.public_url or f"http://{lan_ip()}:{bridge.port}"
    print("\n" + "=" * 64)
    print(f"  Rover bridge:  {url}")
    if not bridge.public_url:
        print("  (plain http: works if you open the app over http on the same Wi-Fi;")
        print("   for the published https app, rerun with --tunnel)")
    if app_url:
        print(f"  Open on your phone:  {app_url.rstrip('/')}/rover?rover={quote(url, safe='')}")
    else:
        print("  On your phone: Workbench -> Rover Cam -> paste the address above")
    print("=" * 64 + "\n")
