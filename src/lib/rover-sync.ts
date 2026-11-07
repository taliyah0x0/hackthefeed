// Pulls takes filmed by the rover camera into the shared BuildRoll library, where Missing Beat
// and the rest of the app pick them up. Used by BuildRoll's "Connect", Rover Cam, and a
// background sync on every page once the camera roll is connected.
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useRef, useSyncExternalStore } from "react";
import { indexClip } from "./ai.functions";
import { imageToDataUrl } from "./frames";
import { addItems, finishScan, getRoll, setScanProgress, useRoll, type IndexFn } from "./roll-store";
import { savedRoverUrl, type RoverClip } from "./rover";

export const takeId = (c: RoverClip) => `ROVER_${c.name}`;

// ---- per-take import state (shared so every page shows the same thing) ----
type ImportState = "busy" | "failed";
let importing: Record<string, ImportState> = {};
const subs = new Set<() => void>();
function mark(id: string, st: ImportState | null) {
  importing = { ...importing };
  if (st) importing[id] = st;
  else delete importing[id];
  subs.forEach((f) => f());
}
export function useImporting() {
  return useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f); }, () => importing, () => importing);
}

function planIdea(): string {
  try { return (JSON.parse(localStorage.getItem("workbench-plan") ?? "null") as { idea?: string } | null)?.idea ?? ""; }
  catch { return ""; }
}

/** Classify one rover take with the same AI as the rest of the library and add it to BuildRoll. */
export async function importTake(url: string, c: RoverClip, index: IndexFn) {
  const id = takeId(c);
  if (importing[id] === "busy") return;
  mark(id, "busy");
  try {
    if (!c.thumb) throw new Error("no thumbnail");
    const frame = await imageToDataUrl(`${url}${c.thumb}`);
    const idea = planIdea();
    const known = getRoll().items.map((i) => i.aiProject).filter((p) => p !== "Everyday");
    const r = await index({ data: { frame, projects: [...new Set([...(idea ? [idea] : []), ...known])] } });
    // Everything the rover films is deliberate build footage, so never hide a take as "Everyday".
    const aiProject = r.project === "Everyday" ? idea || "Rover takes" : r.project;
    addItems([{
      id, name: `${c.name}.mp4`, img: frame, kind: "video", duration: Math.max(1, Math.round(c.duration)),
      sizeMb: Math.max(1, Math.round(c.duration * 0.6)), takenAt: c.created, lat: 0, lng: 0, place: "Rover cam",
      source: "rover", video: `${url}${c.video}`,
      aiProject, project: null, groupedBy: [], trend: r.trend, lookalike: r.lookalike,
      tags: c.title ? [...r.tags, c.title.toLowerCase()] : r.tags,
      description: c.title ? `${c.title}: ${r.description}` : r.description,
    }]);
    mark(id, null);
  } catch (e) {
    console.error(e);
    mark(id, "failed");
  }
}

export async function fetchTakes(url: string): Promise<RoverClip[]> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 4000);
  try {
    const res = await fetch(`${url}/clips`, { signal: ctrl.signal, cache: "no-store" });
    if (!res.ok) throw new Error(`Rover answered ${res.status}`);
    return ((await res.json()) as { clips: RoverClip[] }).clips;
  } finally {
    clearTimeout(t);
  }
}

/** Import every finished rover take that isn't in BuildRoll yet. */
export async function syncRoverTakes(url: string, index: IndexFn, opts: { retryFailed?: boolean; onProgress?: (done: number, total: number) => void } = {}) {
  const have = new Set(getRoll().items.map((i) => i.id));
  const todo = (await fetchTakes(url)).filter((c) => c.ready && !have.has(takeId(c))
    && importing[takeId(c)] !== "busy" && (opts.retryFailed || importing[takeId(c)] !== "failed"));
  let done = 0;
  opts.onProgress?.(0, todo.length);
  const queue = [...todo];
  const worker = async () => {
    while (queue.length) {
      await importTake(url, queue.shift()!, index);
      opts.onProgress?.(++done, todo.length);
    }
  };
  await Promise.all([worker(), worker(), worker()]);
  return todo.length;
}

/** BuildRoll "Connect": the camera roll is the rover, so read every take it has filmed. */
export async function connectRoll(index: IndexFn) {
  setScanProgress(0, 0);
  const url = savedRoverUrl();
  try {
    if (url) await syncRoverTakes(url, index, { retryFailed: true, onProgress: setScanProgress });
  } finally {
    finishScan();
  }
  if (!url) throw new Error("No rover connected yet. Connect it in Rover Cam and its takes will show up here.");
}

/** Once the camera roll is connected, keep pulling new rover takes in on every page. */
export function useRoverAutoSync() {
  const roll = useRoll();
  const index = useServerFn(indexClip);
  const indexRef = useRef(index);
  indexRef.current = index;
  const connected = roll.status === "done";
  useEffect(() => {
    if (!connected) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const url = savedRoverUrl();
      if (url && document.visibilityState === "visible") {
        try { await syncRoverTakes(url, (a) => indexRef.current(a)); } catch { /* rover offline: try again later */ }
      }
      if (!stop) timer = setTimeout(tick, 5000);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, [connected]);
}
