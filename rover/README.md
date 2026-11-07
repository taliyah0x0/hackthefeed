# Rover Cam: robot side

The Workbench **Rover Cam** page (`/rover`) drives this rover from your phone.

```
phone (Workbench /rover)  ──https──▶  cloudflared tunnel  ──▶  bridge.py :8765  ─┐
                                                                                 │ same process
Arduino (rover_face_follow.ino)  ◀──USB serial "V <l> <r>"──  face_follow.py  ◀─┘
```

## Setup (laptop riding the rover)

1. Upload `rover_face_follow.ino` to the Uno (L298N + HC-SR04, same pins as before).
2. `pip install -r requirements.txt`
3. `brew install cloudflared ffmpeg`. cloudflared gives the phone an https address. ffmpeg is a fallback that converts takes to H.264 if OpenCV can't write it directly.
4. `python face_follow.py --tunnel`. Add `--dry-run` to try it with the wheels off.
5. On your phone, open Workbench → Rover Cam and paste the `https://….trycloudflare.com` address.
   With `--app-url https://your-app.lovable.app` it prints a one-tap link instead.

If you open the app over plain http on the same Wi-Fi (e.g. `npm run dev -- --host`), you can skip
the tunnel and use `http://<laptop-ip>:8765`.

## What the phone can do

- Live view with the tracking box, centring gauge, REC timer and an obstacle warning (from the sonar)
- Follow **face / hands / off**, framing (Wide / Medium / Close, ± zoom), follow speed
- Joystick drive (overrides following while held), quick turns, **Pause wheels**, **STOP** (e-stop)
- Record takes. They're saved in `clips/` as `.mp4` + thumbnail, and sent to BuildRoll automatically
- Missing Beat's shot list: tap a beat to set the framing. The take is titled after the beat
- "Check framing": an AI look at the live frame for the current beat, with one-tap fixes
- Phone voice: "follow my hand", "zoom in", "action", "cut", "stop"…

The rover's own mic (Vosk), hand signs and keyboard still work too. New keyboard key: `r` = record.

## Bridge API (`bridge.py`)

| | |
|---|---|
| `GET /status` | live JSON state |
| `GET /stream.mjpg` | annotated live video |
| `GET /snapshot.jpg` | clean frame |
| `GET /clips`, `GET /clips/<name>.mp4/.jpg` | takes (Range supported) |
| `POST /command` `{"cmd": …}` | `face_on hand_on follow_off zoom_in zoom_out zoom_set{value} turn_left turn_right stop estop pause resume speed{value} record_start{title} record_stop drive{fwd,turn}` |

`drive` must be resent within 0.5 s or the rover falls back to following. The Arduino's own 400 ms watchdog
still stops the motors if the laptop script dies.
