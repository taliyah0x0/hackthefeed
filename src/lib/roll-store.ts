// Shared BuildRoll library (client-side, persisted to localStorage) used by BuildRoll and Missing Beat.
import { useSyncExternalStore } from "react";
import { hasGps, metres, type RollItem } from "./camera-roll";

export type Indexed = RollItem & {
  aiProject: string;
  project: string | null; // null = everyday, hidden
  tags: string[];
  description: string;
  trend: number | null;
  lookalike: { caption: string; views: number; url: string | null } | null;
  groupedBy: string[];
};

export type RollState = { status: "none" | "scanning" | "done"; done: number; total?: number; items: Indexed[] };

const KEY = "workbench-roll-v3"; // v2 held the old bundled sample library
const ROVER_KEY = "workbench-roll-rover"; // rover takes, kept even before/without a camera-roll scan
let state: RollState = { status: "none", done: 0, items: [] };
let loaded = false;
const subs = new Set<() => void>();

function load() {
  if (loaded || typeof window === "undefined") return;
  loaded = true;
  try {
    const s = JSON.parse(localStorage.getItem(KEY) ?? "null") as RollState | null;
    if (s?.status === "done") state = s;
    const rover = JSON.parse(localStorage.getItem(ROVER_KEY) ?? "[]") as Indexed[];
    const have = new Set(state.items.map((i) => i.id));
    const missing = rover.filter((r) => !have.has(r.id));
    if (missing.length) state = { ...state, items: groupProjects([...state.items, ...missing]) };
  } catch { /* ignore */ }
}
function set(next: RollState) {
  state = next;
  if (next.status === "done") localStorage.setItem(KEY, JSON.stringify(next));
  try {
    localStorage.setItem(ROVER_KEY, JSON.stringify(next.items.filter((i) => i.source === "rover")));
  } catch { /* storage full: rover takes stay for this visit */ }
  subs.forEach((f) => f());
}
const roverItems = () => state.items.filter((i) => i.source === "rover");
const EMPTY: RollState = { status: "none", done: 0, items: [] };

export function useRoll() {
  return useSyncExternalStore(
    (f) => { load(); subs.add(f); return () => subs.delete(f); },
    () => { load(); return state; },
    () => EMPTY,
  );
}
export function getRoll() { load(); return state; }
export function resetRoll() { localStorage.removeItem(KEY); set({ ...EMPTY, items: roverItems() }); }
export function setScanProgress(done: number, total: number) { set({ ...state, status: "scanning", done, total }); }
export function finishScan() { set({ ...state, status: "done" }); }
/** Add (or replace) already-indexed clips, e.g. takes filmed by the rover, and regroup projects. */
export function addItems(items: Indexed[]) {
  load();
  const ids = new Set(items.map((i) => i.id));
  set({ ...state, items: groupProjects([...state.items.filter((i) => !ids.has(i.id)), ...items]) });
}
export function removeItems(ids: string[]) { set({ ...state, items: state.items.filter((i) => !ids.includes(i.id)) }); }

const GENERIC = new Set(["close", "shot", "medium", "wide", "overhead", "top", "down", "workbench", "workshop", "hands", "project", "custom", "build", "diy", "indoor", "table", "tabletop", "home", "living", "room", "power", "tool", "finished", "design"]);
const words = (s: string) => new Set(s.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !GENERIC.has(w)));
const overlap = (a: Set<string>, b: Set<string>) => [...a].some((w) => b.has(w));

/** Group build clips into projects: link clips filmed at the same place within 2 days, or showing the same build. */
export function groupProjects(items: Indexed[]): Indexed[] {
  const builds = items.filter((i) => i.aiProject.toLowerCase() !== "everyday");
  const parent = builds.map((_, i) => i);
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i]!)));
  const why = builds.map(() => new Set<string>());
  const names = builds.map((b) => words(b.aiProject));
  const tagw = builds.map((b) => new Set(b.tags.flatMap((t) => [...words(t)])));
  for (let i = 0; i < builds.length; i++)
    for (let j = i + 1; j < builds.length; j++) {
      const a = builds[i]!, b = builds[j]!;
      const near = hasGps(a) && hasGps(b) && metres(a, b) < 300 && Math.abs(+new Date(a.takenAt) - +new Date(b.takenAt)) < 2 * 864e5;
      const sharedTags = [...tagw[i]!].filter((w) => tagw[j]!.has(w)).length;
      const scene = overlap(names[i]!, names[j]!) || overlap(names[i]!, tagw[j]!) || overlap(names[j]!, tagw[i]!) || sharedTags >= 2;
      if (near || scene) {
        parent[find(i)] = find(j);
        for (const k of [i, j]) why[k]!.add(near ? "Same location" : "Same build in frame");
      }
    }
  const groups = new Map<number, number[]>();
  builds.forEach((_, i) => groups.set(find(i), [...(groups.get(find(i)) ?? []), i]));
  const titles = new Map<number, string>();
  for (const [root, idx] of groups) {
    const count = new Map<string, number>();
    for (const i of idx) count.set(builds[i]!.aiProject, (count.get(builds[i]!.aiProject) ?? 0) + 1);
    titles.set(root, [...count.entries()].sort((x, y) => y[1] - x[1] || y[0].length - x[0].length)[0]![0]);
  }
  const out: Indexed[] = items.map((it) => {
    const i = builds.indexOf(it);
    if (i < 0) return { ...it, project: null, groupedBy: ["Not a build"] };
    return { ...it, project: titles.get(find(i))!, groupedBy: why[i]!.size ? [...why[i]!] : ["New build detected"] };
  });
  return out.sort((a, b) => b.takenAt.localeCompare(a.takenAt));
}

export type IndexFn = (a: { data: { frame: string; projects: string[] } }) => Promise<{
  project: string; tags: string[]; description: string; trend: number | null; lookalike: Indexed["lookalike"];
}>;

/** Most common capture place among a project's clips. */
export function mainPlace(items: Indexed[]) {
  const c = new Map<string, number>();
  for (const i of items) c.set(i.place, (c.get(i.place) ?? 0) + 1);
  return [...c.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";
}
