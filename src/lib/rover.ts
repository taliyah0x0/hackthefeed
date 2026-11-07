// Browser client for the rover bridge (rover/bridge.py, started by rover/face_follow.py).
// The phone talks straight to the rover laptop; nothing goes through our server.
import { useCallback, useEffect, useRef, useState } from "react";

export type RoverState = "tracking" | "searching" | "off" | "paused" | "driving" | "starting";

export type RoverStatus = {
  state: RoverState;
  label: string;
  mode: "off" | "face" | "hand";
  paused: boolean;
  zoom: number;
  speed: number;
  target: boolean;
  x_err: number | null;
  size: number | null;
  target_size: number;
  centered: boolean;
  left_pwm: number;
  right_pwm: number;
  rover: string;
  blocked: boolean;
  dry_run: boolean;
  recording: boolean;
  recording_s: number;
  recording_title: string | null;
  voice: boolean;
  gestures: boolean;
  hand_available: boolean;
  message: string | null;
  fps: number;
  public_url: string | null;
};

export type RoverClip = {
  name: string;
  title: string;
  created: string;
  duration: number;
  ready: boolean;
  video: string;
  thumb: string | null;
};

export type RoverCommand =
  | "face_on" | "face_off" | "hand_on" | "hand_off" | "follow_off"
  | "zoom_in" | "zoom_out" | "zoom_set" | "turn_left" | "turn_right"
  | "stop" | "estop" | "pause" | "resume" | "speed"
  | "record_start" | "record_stop" | "drive";

const KEY = "workbench-rover-url";

export function savedRoverUrl() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem(KEY) ?? "";
}
export function saveRoverUrl(url: string) {
  if (url) localStorage.setItem(KEY, url);
  else localStorage.removeItem(KEY);
}

/** "abc.trycloudflare.com" -> "https://abc.trycloudflare.com", "192.168.1.5:8765" -> "http://…" */
export function normalizeRoverUrl(raw: string) {
  let u = raw.trim().replace(/\/+$/, "").replace(/\/(status|stream\.mjpg)$/, "");
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) {
    const local = /^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|[\w-]+\.local\b)/i.test(u);
    if (local && !/:\d+$/.test(u)) u += ":8765";
    u = (local ? "http://" : "https://") + u;
  }
  return u;
}

/** An https page can't call a plain-http LAN address (except localhost). */
export function isBlockedByHttps(url: string) {
  if (typeof window === "undefined") return false;
  return window.location.protocol === "https:" && /^http:\/\//i.test(url) && !/^http:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/i.test(url);
}

async function fetchJson<T>(url: string, init?: RequestInit, ms = 2500): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try {
    const res = await fetch(url, { ...init, signal: ctrl.signal, cache: "no-store" });
    const json = (await res.json().catch(() => ({}))) as T & { error?: string };
    if (!res.ok) throw new Error(json.error ?? `Rover answered ${res.status}`);
    return json;
  } finally {
    clearTimeout(t);
  }
}

/** Live connection to a rover: status polling, commands and the take list. */
export function useRover(url: string) {
  const [status, setStatus] = useState<RoverStatus | null>(null);
  const [online, setOnline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clips, setClips] = useState<RoverClip[]>([]);
  const [clipsLoaded, setClipsLoaded] = useState(false);
  const fails = useRef(0);

  const refreshClips = useCallback(async () => {
    if (!url) return;
    try {
      const r = await fetchJson<{ clips: RoverClip[] }>(`${url}/clips`);
      setClips(r.clips);
      setClipsLoaded(true);
    } catch { /* status polling reports connection problems */ }
  }, [url]);

  useEffect(() => {
    setStatus(null); setOnline(false); setError(null); setClips([]); setClipsLoaded(false);
    fails.current = 0;
    if (!url) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (document.visibilityState === "visible") {
        try {
          const s = await fetchJson<RoverStatus>(`${url}/status`);
          if (stop) return;
          fails.current = 0;
          setStatus(s); setOnline(true); setError(null);
        } catch (e) {
          if (stop) return;
          if (++fails.current >= 3) {
            setOnline(false);
            setError(e instanceof TypeError || (e as Error).name === "AbortError"
              ? "Can't reach the rover. Is face_follow.py running, and is the address right?"
              : (e as Error).message);
          }
        }
      }
      if (!stop) timer = setTimeout(tick, 350);
    };
    tick();
    refreshClips();
    return () => { stop = true; clearTimeout(timer); };
  }, [url, refreshClips]);

  // Re-read the take list when a recording ends, then until every take is ready.
  const wasRecording = useRef(false);
  useEffect(() => {
    const rec = !!status?.recording;
    if (wasRecording.current && !rec) refreshClips();
    wasRecording.current = rec;
  }, [status?.recording, refreshClips]);
  useEffect(() => {
    if (!clips.some((c) => !c.ready)) return;
    const t = setInterval(refreshClips, 2000);
    return () => clearInterval(t);
  }, [clips, refreshClips]);

  const send = useCallback(
    async (cmd: RoverCommand, payload: Record<string, unknown> = {}) => {
      if (!url) return;
      // text/plain body = "simple" CORS request, so no preflight round trip per joystick tick.
      await fetchJson(`${url}/command`, { method: "POST", body: JSON.stringify({ cmd, ...payload }) }, 2000);
    },
    [url],
  );

  return { status, online, error, clips, clipsLoaded, refreshClips, send };
}

// ---------------------------------------------------------------------------
// Phone voice commands (Web Speech API). Same phrases as the rover's own mic.
const PHRASES: Record<string, RoverCommand> = {
  "face follow on": "face_on", "follow my face": "face_on", "follow face": "face_on",
  "face follow off": "face_off",
  "hand follow on": "hand_on", "follow my hand": "hand_on", "follow hand": "hand_on",
  "hand follow off": "hand_off",
  "stop following": "follow_off", "follow off": "follow_off",
  "zoom in": "zoom_in", "come closer": "zoom_in", "closer": "zoom_in",
  "zoom out": "zoom_out", "back up": "zoom_out",
  "turn left": "turn_left", "turn right": "turn_right",
  "start recording": "record_start", "record": "record_start", "action": "record_start",
  "stop recording": "record_stop", "cut": "record_stop",
  "pause": "pause", "resume": "resume",
  "emergency": "estop", "stop": "stop",
};

export function parseVoice(text: string): RoverCommand | null {
  const t = " " + text.toLowerCase().replace(/[^a-z ]/g, " ").replace(/\s+/g, " ").trim() + " ";
  const hit = Object.keys(PHRASES).sort((a, b) => b.length - a.length).find((p) => t.includes(` ${p} `));
  return hit ? PHRASES[hit]! : null;
}

type SpeechRec = {
  lang: string; interimResults: boolean; maxAlternatives: number;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void; stop(): void;
};

export function speechSupported() {
  if (typeof window === "undefined") return false;
  const w = window as unknown as Record<string, unknown>;
  return !!(w["SpeechRecognition"] || w["webkitSpeechRecognition"]);
}

/** Listen for one phrase. Resolves with what was heard ("" if nothing). */
export function listenOnce(): { result: Promise<string>; stop: () => void } {
  const w = window as unknown as Record<string, new () => SpeechRec>;
  const Rec = w["SpeechRecognition"] ?? w["webkitSpeechRecognition"];
  const rec = new Rec!();
  rec.lang = "en-US";
  rec.interimResults = false;
  rec.maxAlternatives = 3;
  const result = new Promise<string>((resolve, reject) => {
    let heard = "";
    rec.onresult = (e) => {
      const alts = Array.from(e.results[0] ?? []).map((a) => a.transcript);
      heard = alts.find((a) => parseVoice(a)) ?? alts[0] ?? "";
    };
    rec.onerror = (e) => (e.error === "no-speech" || e.error === "aborted" ? resolve("") : reject(new Error(`Mic error: ${e.error}`)));
    rec.onend = () => resolve(heard);
  });
  rec.start();
  return { result, stop: () => rec.stop() };
}
