# Workbench + Rover Cam

🥈 2nd place at [Hack the Feed](https://luma.com/92iq2a12?tk=uoFqyE).

[Watch the Project video](https://www.instagram.com/reel/Dfcxudyh_Jo/?xtok=MXdhenl4ZzNwMzZ0eg==).

## The problem
I asked 25 creators what they hate about filming their projects. Three answers kept coming up:

1. **Stopping the build to move the camera**
2. **Too much footage to sort through**
3. **Forgetting to film key steps**

## The solution
| Feature | What it does |
|---|---|
| **Rover Cam** | A robot cameraman that follows your face, takes voice commands, and watches for hand signs to zoom in and out, pan left and right, and start and stop recording, so you never have to adjust a tripod. Built on a modular Framework computer. |
| **BuildRoll** | Every take syncs to your phone automatically. GMI Cloud vision tags each clip, groups it into a project, and lets you search in plain English. |
| **Missing Beat** | Oriane's viral video search builds a shot list for your project, then checks your footage, and tells you which shots you still need. |

## How it works
- **Rover:** a Python script (`rover/face_follow_phone.py`) and an Arduino sketch (`rover/rover_face_follow.ino`).
  - The Python script handles tracking with OpenCV YuNet, hand signs with MediaPipe, and offline voice commands with Vosk.
  - It records takes and serves them over a small HTTP API.
- **App:** a TanStack Start + React app built with Lovable.
  - Once connected, it polls the rover every 5s and imports new takes.
  - Clip state lives in the browser (localStorage). There's no database yet.
- **AI:** calls go through server functions (`src/lib/ai.functions.ts`), so API keys never reach the browser.

## Run it
**1. Rover laptop**, with the Arduino plugged in:

```bash
pip install -r rover/requirements.txt
```

```bash
python rover/face_follow_phone.py --tunnel
```

The script prints a public `https://….trycloudflare.com` address. Use `--phone` instead of `--tunnel` if the phone is on the same Wi-Fi. Add `--dry-run` to test without the Arduino.

**2. App server.** Set `GMI_API_KEY` and `ORIANE_API_KEY` in the environment, then:

```bash
npm install
```

```bash
npm run dev -- --host
```

**3. Phone:**
1. Open the app.
2. Go to **Rover Cam** and paste the rover address.
3. Tap **Connect** in BuildRoll. Takes from then on show up automatically.

## Project layout

```
rover/          rover script, Arduino sketch, requirements
src/routes/     pages: rover, buildroll, missing-beat, flop
src/lib/        rover sync, roll store, AI server functions
demo-video/     scripts that render the demo and reel animations
```
