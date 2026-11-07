import { createFileRoute, Link } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import { AppShell, PageHeader } from "@/components/AppShell";
import { checkFraming, indexClip, type Beat, type FramingCheck } from "@/lib/ai.functions";
import { imageToDataUrl } from "@/lib/frames";
import { useRoll } from "@/lib/roll-store";
import { importTake as importRoverTake, takeId, useImporting } from "@/lib/rover-sync";
import {
  isBlockedByHttps, listenOnce, normalizeRoverUrl, parseVoice, savedRoverUrl, saveRoverUrl, speechSupported, useRover,
  type RoverClip, type RoverCommand, type RoverStatus,
} from "@/lib/rover";

export const Route = createFileRoute("/rover")({
  validateSearch: (s: Record<string, unknown>) => ({ rover: typeof s["rover"] === "string" ? s["rover"] : undefined }),
  head: () => ({
    meta: [
      { title: "Rover Cam — your robot camera operator | Workbench" },
      { name: "description", content: "Drive your camera rover from your phone: face and hand follow, framing, recording, and a shot list from Missing Beat." },
      { property: "og:title", content: "Rover Cam — a camera crew of one" },
      { property: "og:description", content: "Live view, follow modes, joystick, voice and one-tap recording for your camera rover." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: RoverCam,
});

type Plan = { idea: string; beats: Beat[] };
const SHOT_ZOOM: Record<string, number> = { Wide: 0.6, Medium: 1.0, "Close-up": 1.6 };
const FRAMINGS = [["Wide", 0.6], ["Medium", 1.0], ["Close", 1.6]] as const;

function RoverCam() {
  const search = Route.useSearch();
  const [url, setUrl] = useState("");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const fromLink = search.rover ? normalizeRoverUrl(search.rover) : "";
    if (fromLink) saveRoverUrl(fromLink);
    setUrl(fromLink || savedRoverUrl());
    setReady(true);
  }, [search.rover]);

  const connect = (u: string) => { saveRoverUrl(u); setUrl(u); };

  return (
    <AppShell>
      <PageHeader kicker="04 · Rover Cam" title="A camera crew of one." sub="Your rover follows your face or hands while you build. Frame, drive and record from your phone, then every take lands in BuildRoll." />
      {!ready ? null : url ? <Console url={url} onDisconnect={() => connect("")} /> : <ConnectCard onConnect={connect} />}
    </AppShell>
  );
}

// ---------------------------------------------------------------------------
function ConnectCard({ onConnect }: { onConnect: (url: string) => void }) {
  const [raw, setRaw] = useState("");
  const url = normalizeRoverUrl(raw);
  const blocked = url && isBlockedByHttps(url);
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <form onSubmit={(e) => { e.preventDefault(); if (url && !blocked) onConnect(url); }} className="rounded-2xl border bg-card p-6">
        <h2 className="text-2xl font-bold">Connect your rover</h2>
        <label htmlFor="rover-url" className="tape mt-4 block text-muted-foreground">Rover address</label>
        <input id="rover-url" value={raw} onChange={(e) => setRaw(e.target.value)} inputMode="url" autoCapitalize="off" autoCorrect="off" spellCheck={false}
          placeholder="https://something.trycloudflare.com"
          className="mt-2 w-full rounded-lg border bg-background px-3 py-3 font-mono text-base outline-none focus:ring-2 focus:ring-ring" />
        {blocked && (
          <p className="mt-3 text-sm text-destructive">
            This page is served over https, so your browser won't talk to a plain http address. Start the rover with <code className="font-mono">--tunnel</code> and use the https address it prints.
          </p>
        )}
        <button disabled={!url || !!blocked} className="mt-4 w-full rounded-lg bg-primary px-4 py-3 font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-50">Connect</button>
      </form>
      <div className="rounded-2xl border bg-card p-6">
        <p className="tape text-accent">On the laptop riding the rover</p>
        <ol className="mt-3 list-decimal space-y-3 pl-5 text-sm text-muted-foreground">
          <li>Upload <code className="font-mono text-foreground">rover/rover_face_follow.ino</code> to the Arduino and plug it in over USB.</li>
          <li><code className="font-mono text-foreground">pip install -r rover/requirements.txt</code> and <code className="font-mono text-foreground">brew install cloudflared ffmpeg</code></li>
          <li><code className="font-mono text-foreground">python rover/face_follow.py --tunnel</code> <span className="block">Add <code className="font-mono">--dry-run</code> to try it with the wheels off.</span></li>
          <li>Paste the <span className="text-foreground">https://…trycloudflare.com</span> address it prints.</li>
        </ol>
        <p className="mt-4 text-xs text-muted-foreground">Your phone talks straight to the rover. Video and controls never pass through Workbench's servers.</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
function Console({ url, onDisconnect }: { url: string; onDisconnect: () => void }) {
  const { status: s, online, error, clips, send } = useRover(url);
  const roll = useRoll();
  const index = useServerFn(indexClip);
  const framing = useServerFn(checkFraming);
  const [note, setNote] = useState<string | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [beatId, setBeatId] = useState<string | null>(null);
  const [check, setCheck] = useState<FramingCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const importing = useImporting();

  useEffect(() => {
    try {
      const p = JSON.parse(localStorage.getItem("workbench-plan") ?? "null") as Plan | null;
      if (p?.beats?.length) setPlan(p);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { if (!note) return; const t = setTimeout(() => setNote(null), 3500); return () => clearTimeout(t); }, [note]);

  const run = useCallback((cmd: RoverCommand, payload?: Record<string, unknown>) => {
    send(cmd, payload).catch((e: Error) => setNote(e.name === "AbortError" ? "Rover didn't answer." : e.message));
  }, [send]);

  const beat = plan?.beats.find((b) => b.id === beatId) ?? null;
  const filmed = useMemo(() => new Set(clips.map((c) => c.title)), [clips]);

  const record = () => {
    if (s?.recording) return run("record_stop");
    run("record_start", { title: beat?.label ?? "" });
  };
  const filmBeat = (b: Beat) => {
    setBeatId(b.id); setCheck(null);
    run("zoom_set", { value: SHOT_ZOOM[b.shot] ?? 1 });
    setNote(`Framing set for a ${b.shot.toLowerCase()} shot. Tap record when you're ready.`);
  };

  const runCheck = async () => {
    setChecking(true); setCheck(null);
    try {
      const frame = await imageToDataUrl(`${url}/snapshot.jpg?t=${Date.now()}`);
      setCheck(await framing({ data: { frame, beat: beat ? { label: beat.label, shot: beat.shot, why: beat.why } : null, mode: s?.mode ?? "off" } }));
    } catch (e) { setNote((e as Error).message); }
    finally { setChecking(false); }
  };
  const applyCheck = (c: FramingCheck) => {
    if (c.zoom !== "hold") run(c.zoom === "in" ? "zoom_in" : "zoom_out");
    if (c.follow !== "keep" && c.follow !== s?.mode) run(c.follow === "hand" ? "hand_on" : "face_on");
    setCheck(null);
  };

  // Send takes to BuildRoll: classified by the same AI as the camera roll, so Missing Beat sees them too.
  const inRoll = useMemo(() => new Map(roll.items.filter((i) => i.source === "rover").map((i) => [i.id, i])), [roll.items]);
  const importTake = useCallback((c: RoverClip) => importRoverTake(url, c, index), [url, index]);

  // Every finished rover take goes to BuildRoll automatically (each tried once per visit).
  const known = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!online) return;
    for (const c of clips) {
      if (!c.ready || known.current.has(c.name)) continue;
      known.current.add(c.name);
      if (!inRoll.has(takeId(c))) importTake(c);
    }
  }, [clips, online, inRoll, importTake]);

  // Floating STOP on phones, shown whenever the main STOP button is scrolled out of view.
  const mainStop = useRef<HTMLButtonElement>(null);
  const [stopVisible, setStopVisible] = useState(true);
  useEffect(() => {
    const el = mainStop.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setStopVisible(!!e?.isIntersecting));
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <>
      {!online && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-destructive bg-card px-4 py-3 text-sm">
          <span className="size-2.5 animate-pulse rounded-full bg-destructive" />
          <span className="flex-1">{error ?? "Connecting to the rover…"}</span>
          <button onClick={onDisconnect} className="rounded-lg border px-3 py-1.5 hover:bg-muted">Change address</button>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1.3fr_1fr]">
        <div className="space-y-4">
          <LiveView url={url} s={s} online={online} />

          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3">
            <button onClick={() => run(s?.paused ? "resume" : "pause")} disabled={!online}
              className={`rounded-xl border px-3 py-3 text-sm font-semibold disabled:opacity-40 ${s?.paused ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted"}`}>
              {s?.paused ? "Resume wheels" : "Pause wheels"}
            </button>
            <button onClick={record} disabled={!online} aria-label={s?.recording ? "Stop recording" : "Start recording"}
              className={`grid size-20 place-items-center rounded-full border-4 transition disabled:opacity-40 ${s?.recording ? "animate-pulse border-destructive" : "border-foreground"}`}>
              <span className={`block bg-destructive transition-all ${s?.recording ? "size-7 rounded-md" : "size-14 rounded-full"}`} />
            </button>
            <button ref={mainStop} onClick={() => run("estop")} disabled={!online}
              className="rounded-xl bg-destructive px-3 py-3 text-sm font-extrabold tracking-wide text-destructive-foreground disabled:opacity-40">STOP</button>
          </div>
          {beat && !s?.recording && <p className="text-center text-sm text-muted-foreground">Next take: <span className="text-foreground">{beat.label}</span> · {beat.shot}</p>}
          {note && <p className="rounded-lg bg-muted px-3 py-2 text-center text-sm">{note}</p>}

          <section className="rounded-2xl border bg-card p-5">
            <p className="tape text-muted-foreground">Follow</p>
            <div className="mt-2 grid grid-cols-3 gap-1 rounded-xl bg-muted p-1">
              {([["off", "Off", "follow_off"], ["face", "Face", "face_on"], ["hand", "Hands", "hand_on"]] as const).map(([m, label, cmd]) => (
                <button key={m} onClick={() => run(cmd)} disabled={!online || (m === "hand" && s?.hand_available === false)}
                  className={`rounded-lg py-2.5 text-sm font-semibold disabled:opacity-40 ${s?.mode === m ? "bg-secondary text-secondary-foreground" : "text-muted-foreground"}`}>{label}</button>
              ))}
            </div>

            <p className="tape mt-5 text-muted-foreground">Framing · zoom ×{(s?.zoom ?? 1).toFixed(2)}</p>
            <div className="mt-2 flex gap-2">
              <button onClick={() => run("zoom_out")} disabled={!online} aria-label="Zoom out" className="w-12 rounded-lg border py-2.5 text-lg font-bold hover:bg-muted disabled:opacity-40">−</button>
              {FRAMINGS.map(([label, z]) => (
                <button key={label} onClick={() => run("zoom_set", { value: z })} disabled={!online}
                  className={`flex-1 rounded-lg border py-2.5 text-sm font-medium disabled:opacity-40 ${s && Math.abs(s.zoom - z) < 0.05 ? "border-primary text-primary" : "hover:bg-muted"}`}>{label}</button>
              ))}
              <button onClick={() => run("zoom_in")} disabled={!online} aria-label="Zoom in" className="w-12 rounded-lg border py-2.5 text-lg font-bold hover:bg-muted disabled:opacity-40">+</button>
            </div>

            <SpeedSlider value={s?.speed ?? 1} disabled={!online} onChange={(v) => run("speed", { value: v })} />
          </section>

          <section className="rounded-2xl border bg-card p-5">
            <div className="flex items-center justify-between">
              <p className="tape text-muted-foreground">Drive</p>
              <span className="font-mono text-xs text-muted-foreground">L {s?.left_pwm ?? 0} · R {s?.right_pwm ?? 0}</span>
            </div>
            <div className="mt-3 flex items-center justify-around gap-4">
              <button onClick={() => run("turn_left")} disabled={!online} className="rounded-xl border px-4 py-3 text-sm hover:bg-muted disabled:opacity-40">↺ Turn</button>
              <Joystick disabled={!online || !!s?.paused} onMove={(fwd, turn) => { send("drive", { fwd, turn }).catch(() => {}); }} />
              <button onClick={() => run("turn_right")} disabled={!online} className="rounded-xl border px-4 py-3 text-sm hover:bg-muted disabled:opacity-40">Turn ↻</button>
            </div>
            <p className="mt-3 text-center text-xs text-muted-foreground">{s?.paused ? "Wheels are paused. Resume to drive." : "Hold and drag. Following picks up again when you let go."}</p>
            <VoiceButton disabled={!online} onCommand={(cmd, heard) => {
              setNote(`Heard “${heard}”`);
              run(cmd, cmd === "record_start" ? { title: beat?.label ?? "" } : undefined);
            }} onNote={setNote} />
          </section>
        </div>

        <div className="space-y-4">
          <section className="rounded-2xl border bg-card p-5">
            <div className="flex items-baseline justify-between gap-2">
              <h2 className="text-xl font-bold">Shot list</h2>
              {plan && <span className="truncate text-sm text-muted-foreground">{plan.idea}</span>}
            </div>
            {!plan ? (
              <p className="mt-2 text-sm text-muted-foreground">
                Make a shot list in <Link to="/missing-beat" className="text-primary underline">Missing Beat</Link> and it shows up here, so the rover frames each beat for you.
              </p>
            ) : (
              <ol className="mt-3 space-y-2">
                {plan.beats.map((b, i) => {
                  const done = filmed.has(b.label);
                  const sel = b.id === beatId;
                  return (
                    <li key={b.id}>
                      <button onClick={() => filmBeat(b)} disabled={!online}
                        className={`flex w-full items-center gap-3 rounded-xl border p-3 text-left transition-colors disabled:opacity-60 ${sel ? "border-primary bg-muted" : "hover:bg-muted"}`}>
                        <span className={`grid size-6 shrink-0 place-items-center rounded-full text-xs font-bold ${done ? "bg-success text-success-foreground" : "bg-muted text-muted-foreground"}`}>{done ? "✓" : i + 1}</span>
                        <span className="min-w-0 flex-1">
                          <span className="block font-medium">{b.label}</span>
                          {sel && <span className="block text-xs text-muted-foreground">{b.why}</span>}
                        </span>
                        <span className="tape shrink-0 rounded bg-accent px-2 py-1 text-accent-foreground">{b.shot}</span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            )}
            <button onClick={runCheck} disabled={!online || checking}
              className="mt-4 w-full rounded-lg bg-secondary px-4 py-2.5 font-semibold text-secondary-foreground hover:opacity-90 disabled:opacity-50">
              {checking ? "Looking through the rover's eyes…" : beat ? `Check framing for “${beat.label}”` : "Check my framing"}
            </button>
            {check && (
              <div className={`mt-3 rounded-xl border p-3 text-sm ${check.verdict === "good" ? "border-success" : "border-warning"}`}>
                <p className={`font-semibold ${check.verdict === "good" ? "text-success" : "text-warning"}`}>{check.verdict === "good" ? "Looks ready. Roll it." : "Almost there"}</p>
                <p className="mt-1">{check.tip}</p>
                {(check.zoom !== "hold" || (check.follow !== "keep" && check.follow !== s?.mode)) && (
                  <button onClick={() => applyCheck(check)} className="mt-2 rounded-lg bg-primary px-3 py-1.5 font-semibold text-primary-foreground">
                    Apply: {[check.zoom !== "hold" && `zoom ${check.zoom}`, check.follow !== "keep" && check.follow !== s?.mode && `follow ${check.follow === "hand" ? "hands" : "face"}`].filter(Boolean).join(" + ")}
                  </button>
                )}
              </div>
            )}
          </section>

          <section className="rounded-2xl border bg-card p-5">
            <div className="flex items-baseline justify-between">
              <h2 className="text-xl font-bold">Takes</h2>
              <Link to="/buildroll" className="text-sm text-primary underline">Open BuildRoll</Link>
            </div>
            {clips.length === 0 ? (
              <p className="mt-2 text-sm text-muted-foreground">Nothing recorded yet. Takes are saved on the rover laptop and sorted into BuildRoll.</p>
            ) : (
              <ul className="mt-3 space-y-2">
                {clips.slice(0, 12).map((c) => <TakeRow key={c.name} c={c} url={url} roll={inRoll.get(`ROVER_${c.name}`)?.project ?? (inRoll.has(`ROVER_${c.name}`) ? "Everyday" : null)} state={importing[`ROVER_${c.name}`]} onImport={() => importTake(c)} />)}
              </ul>
            )}
          </section>

          <section className="rounded-2xl border bg-card p-5 text-sm">
            <div className="flex items-center gap-2">
              <span className={`size-2.5 rounded-full ${online ? "bg-success" : "bg-destructive"}`} />
              <span className="font-semibold">{online ? "Rover online" : "Rover offline"}</span>
              {s?.dry_run && <span className="tape rounded bg-warning px-2 py-0.5 text-foreground">Dry run</span>}
              <button onClick={onDisconnect} className="ml-auto text-muted-foreground underline">Disconnect</button>
            </div>
            <p className="mt-2 break-all font-mono text-xs text-muted-foreground">{url}</p>
            {s && <p className="mt-1 text-xs text-muted-foreground">{s.rover} · {s.fps} fps · rover mic {s.voice ? "on" : "off"} · hand signs {s.gestures ? "on" : "off"}</p>}
          </section>
        </div>
      </div>

      {online && !stopVisible && (
        <button onClick={() => run("estop")} aria-label="Emergency stop"
          className="fixed bottom-5 left-1/2 -translate-x-1/2 z-30 grid size-16 place-items-center rounded-full bg-destructive text-sm font-extrabold text-destructive-foreground shadow-xl lg:hidden">STOP</button>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
function LiveView({ url, s, online }: { url: string; s: RoverStatus | null; online: boolean }) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!failed || !online) return;
    const t = setTimeout(() => { setFailed(false); setAttempt((a) => a + 1); }, 2000);
    return () => clearTimeout(t);
  }, [failed, online]);

  const label = !s ? "Connecting" : s.paused ? "Wheels paused" : s.state === "driving" ? "Manual drive"
    : s.mode === "off" ? "Follow off" : s.target ? (s.centered ? "Locked on" : "Tracking") : `Looking for your ${s.mode === "hand" ? "hand" : "face"}`;
  const tone = !s ? "bg-muted" : s.paused ? "bg-warning text-foreground" : s.target ? "bg-success text-success-foreground" : "bg-background/80";

  return (
    <div className="relative aspect-[4/3] overflow-hidden rounded-2xl border bg-black">
      {online && !failed ? (
        <img key={attempt} src={`${url}/stream.mjpg?a=${attempt}`} alt="Live rover camera" className="size-full object-contain" onError={() => setFailed(true)} />
      ) : (
        <div className="grid size-full place-items-center text-sm text-muted-foreground">{online ? "Reconnecting video…" : "No video"}</div>
      )}
      <span className={`tape absolute left-3 top-3 rounded px-2 py-1 ${tone}`}>{label}</span>
      {s?.recording && (
        <span className="tape absolute right-3 top-3 flex items-center gap-1.5 rounded bg-destructive px-2 py-1 text-destructive-foreground">
          <span className="size-2 animate-pulse rounded-full bg-destructive-foreground" />REC {fmtTime(s.recording_s)}
        </span>
      )}
      {s?.blocked && <span className="absolute inset-x-3 top-12 rounded-lg bg-destructive px-3 py-2 text-center text-sm font-semibold text-destructive-foreground">Sonar says the way is blocked. Wheels held.</span>}
      {s?.message && <span className="absolute inset-x-0 bottom-10 mx-auto w-fit max-w-[90%] truncate rounded bg-background/80 px-2 py-1 text-xs">{s.message}</span>}
      {s && s.x_err != null && (
        <div className="absolute inset-x-6 bottom-3 h-1.5 rounded-full bg-background/60" aria-label="How centered you are">
          <span className="absolute inset-y-0 left-1/2 w-px bg-foreground/60" />
          <span className={`absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full ${s.centered ? "bg-success" : "bg-primary"}`}
            style={{ left: `${((Math.max(-1, Math.min(1, s.x_err)) + 1) / 2) * 100}%` }} />
        </div>
      )}
    </div>
  );
}

function SpeedSlider({ value, disabled, onChange }: { value: number; disabled: boolean; onChange: (v: number) => void }) {
  const [v, setV] = useState(value);
  const dragging = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => { if (!dragging.current) setV(value); }, [value]);
  return (
    <label className="mt-5 block">
      <span className="tape flex justify-between text-muted-foreground"><span>Follow speed</span><span>{Math.round(v * 100)}%</span></span>
      <input type="range" min={0.2} max={1} step={0.05} value={v} disabled={disabled}
        onPointerDown={() => { dragging.current = true; }} onPointerUp={() => { dragging.current = false; }}
        onChange={(e) => {
          const n = Number(e.target.value);
          setV(n);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => onChange(n), 250);
        }}
        className="mt-2 w-full accent-[var(--primary)] disabled:opacity-40" />
    </label>
  );
}

function Joystick({ onMove, disabled }: { onMove: (fwd: number, turn: number) => void; disabled?: boolean }) {
  const pad = useRef<HTMLDivElement>(null);
  const [knob, setKnob] = useState({ x: 0, y: 0 });
  const vec = useRef({ fwd: 0, turn: 0 });
  const active = useRef(false);
  const move = useRef(onMove);
  move.current = onMove;

  useEffect(() => {
    const t = setInterval(() => { if (active.current) move.current(vec.current.fwd, vec.current.turn); }, 120);
    return () => clearInterval(t);
  }, []);

  const update = (e: RPointerEvent) => {
    const r = pad.current!.getBoundingClientRect();
    const R = r.width / 2, max = R - 26;
    let dx = e.clientX - (r.left + R), dy = e.clientY - (r.top + R);
    const d = Math.hypot(dx, dy);
    if (d > max) { dx *= max / d; dy *= max / d; }
    setKnob({ x: dx, y: dy });
    const curve = (n: number) => Math.sign(n) * n * n; // fine control near the centre
    vec.current = { fwd: curve(-dy / max), turn: curve(dx / max) };
  };
  const release = () => {
    if (!active.current) return;
    active.current = false;
    vec.current = { fwd: 0, turn: 0 };
    setKnob({ x: 0, y: 0 });
    move.current(0, 0);
  };

  return (
    <div ref={pad} role="slider" aria-label="Drive joystick" aria-valuenow={0}
      className={`relative size-40 shrink-0 touch-none select-none rounded-full border-2 bg-muted ${disabled ? "opacity-40" : ""}`}
      onPointerDown={(e) => { if (disabled) return; e.currentTarget.setPointerCapture(e.pointerId); active.current = true; update(e); move.current(vec.current.fwd, vec.current.turn); }}
      onPointerMove={(e) => { if (active.current) update(e); }}
      onPointerUp={release} onPointerCancel={release} onLostPointerCapture={release}>
      <span className="tape pointer-events-none absolute left-1/2 top-2 -translate-x-1/2 text-muted-foreground">fwd</span>
      <span className="tape pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 text-muted-foreground">back</span>
      <span className="pointer-events-none absolute left-1/2 top-1/2 size-12 rounded-full bg-secondary shadow-lg"
        style={{ transform: `translate(calc(-50% + ${knob.x}px), calc(-50% + ${knob.y}px))` }} />
    </div>
  );
}

function VoiceButton({ disabled, onCommand, onNote }: { disabled: boolean; onCommand: (cmd: RoverCommand, heard: string) => void; onNote: (n: string) => void }) {
  const [listening, setListening] = useState(false);
  const [supported, setSupported] = useState(false);
  const stopRef = useRef<() => void>(() => {});
  useEffect(() => setSupported(speechSupported()), []);
  if (!supported) return null;
  const go = async () => {
    if (listening) { stopRef.current(); return; }
    setListening(true);
    try {
      const l = listenOnce();
      stopRef.current = l.stop;
      const heard = await l.result;
      const cmd = parseVoice(heard);
      if (cmd) onCommand(cmd, heard);
      else onNote(heard ? `“${heard}” isn't a rover command. Try “follow my hand” or “zoom in”.` : "Didn't catch that.");
    } catch (e) { onNote((e as Error).message); }
    finally { setListening(false); }
  };
  return (
    <button onClick={go} disabled={disabled}
      className={`mt-4 w-full rounded-xl border px-4 py-3 text-sm font-semibold disabled:opacity-40 ${listening ? "animate-pulse border-primary text-primary" : "hover:bg-muted"}`}>
      {listening ? "Listening… say “follow my hand”, “zoom in”, “action”, “cut”" : "🎙 Tap to talk to the rover"}
    </button>
  );
}

function TakeRow({ c, url, roll, state, onImport }: { c: RoverClip; url: string; roll: string | null; state: "busy" | "failed" | undefined; onImport: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="overflow-hidden rounded-xl border">
      <div className="flex items-center gap-2 p-2">
        <button onClick={() => c.ready && setOpen(!open)} className="flex min-w-0 flex-1 items-center gap-3 rounded-lg text-left hover:bg-muted" aria-expanded={open}>
          {c.thumb ? <img src={`${url}${c.thumb}`} alt="" className="h-12 w-16 shrink-0 rounded object-cover" loading="lazy" /> : <span className="h-12 w-16 shrink-0 rounded bg-muted" />}
          <span className="min-w-0 flex-1 text-sm">
            <span className="block truncate font-medium">{c.title || "Untitled take"}</span>
            <span className="block text-xs text-muted-foreground">{c.ready ? fmtTime(c.duration) : "Saving…"} · {new Date(c.created).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span>
          </span>
        </button>
        <span className="max-w-[40%] shrink-0 truncate text-xs">
          {state === "busy" ? <span className="animate-pulse text-muted-foreground">Sorting…</span>
            : roll ? <span className="text-success">✓ {roll === "Everyday" ? "Saved" : roll}</span>
            : c.ready && (
              <button onClick={onImport} className={`rounded-md border px-2 py-1 ${state === "failed" ? "border-destructive text-destructive" : "hover:bg-muted"}`}>
                {state === "failed" ? "Retry" : "Add to BuildRoll"}
              </button>
            )}
        </span>
      </div>
      {open && <video src={`${url}${c.video}`} controls playsInline autoPlay className="w-full bg-black" />}
    </li>
  );
}

function fmtTime(sec: number) {
  const s = Math.max(0, Math.round(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
