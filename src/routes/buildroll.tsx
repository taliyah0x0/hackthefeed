import { createFileRoute, Link } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useMemo, useState } from "react";
import { AppShell, PageHeader } from "@/components/AppShell";
import { indexClip, searchClips } from "@/lib/ai.functions";
import { fmtViews } from "@/lib/frames";
import { mainPlace, removeItems, resetRoll, useRoll, type Indexed } from "@/lib/roll-store";
import { connectRoll } from "@/lib/rover-sync";

export const Route = createFileRoute("/buildroll")({
  head: () => ({
    meta: [
      { title: "BuildRoll — your camera roll, sorted into builds | Workbench" },
      { name: "description", content: "Every take your rover camera films, sorted into build projects by what's in the frame." },
      { property: "og:title", content: "BuildRoll — find any take in seconds" },
      { property: "og:description", content: "Automatic project albums from GPS + scene detection, plain-language search and a trend score." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: BuildRoll,
});

function BuildRoll() {
  const index = useServerFn(indexClip);
  const search = useServerFn(searchClips);
  const roll = useRoll();
  const [asking, setAsking] = useState(false);
  const [project, setProject] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [matches, setMatches] = useState<Array<{ id: string; why: string }> | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const builds = roll.items.filter((c) => c.project);
  const projects = useMemo(() => {
    const m = new Map<string, Indexed[]>();
    for (const c of builds) m.set(c.project!, [...(m.get(c.project!) ?? []), c]);
    return [...m.entries()];
  }, [builds]);
  const everyday = roll.items.length - builds.length;

  useEffect(() => {
    if (!project && projects[0]) setProject(projects[0][0]);
  }, [projects, project]);

  const allow = async () => {
    setAsking(false); setError(null); setProject(null);
    try { await connectRoll(index); } catch (e) { setError((e as Error).message); }
  };

  const runSearch = async (query: string) => {
    setQ(query);
    if (!query.trim()) { setMatches(null); return; }
    setSearching(true); setError(null);
    try { setMatches(await search({ data: { query, clips: builds.map((c) => ({ id: c.id, description: c.description, tags: c.tags })) } })); }
    catch (e) { setError((e as Error).message); }
    finally { setSearching(false); }
  };

  const current = projects.find(([p]) => p === project)?.[1] ?? [];
  const shown = matches
    ? matches.map((m) => ({ c: builds.find((x) => x.id === m.id), why: m.why })).filter((x): x is { c: Indexed; why: string } => !!x.c)
    : current.map((c) => ({ c, why: "" }));
  const lowScore = current.filter((c) => c.kind === "video" && c.trend != null && c.trend < 40);
  const freeable = lowScore.reduce((a, c) => a + c.sizeMb, 0);
  const place = mainPlace(current);
  // Search suggestions from what's actually in your takes.
  const suggestions = useMemo(() => {
    const count = new Map<string, number>();
    for (const c of builds) for (const t of c.tags) count.set(t, (count.get(t) ?? 0) + 1);
    return [...count.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([t]) => t);
  }, [builds]);
  const total = roll.total ?? 0;

  return (
    <AppShell>
      <PageHeader kicker="02 · BuildRoll" title="Your builds, not your brunch." sub="Everything your rover camera films lands here, sorted into a project per build by what's in the frame. Search any take in plain words." />
      {error && <p className="mb-4 rounded-lg border border-destructive bg-card px-4 py-3 text-sm text-destructive">{error}</p>}

      {roll.status === "none" && (
        <div className="grid place-items-center rounded-3xl border bg-card px-6 py-16 text-center">
          <span className="text-5xl" aria-hidden>🎥</span>
          <h2 className="mt-6 text-3xl font-bold">Connect your camera roll</h2>
          <p className="mt-2 max-w-md text-muted-foreground">Your camera roll is your rover. We pull in every take it has filmed and sort them by what's in each shot. New takes show up on their own.</p>
          <button onClick={() => setAsking(true)} className="mt-6 rounded-full bg-primary px-6 py-3 font-semibold text-primary-foreground hover:opacity-90">Connect Photos</button>
        </div>
      )}

      {roll.status === "scanning" && (
        <div className="rounded-3xl border bg-card p-6">
          <div className="flex items-baseline justify-between gap-3">
            <h2 className="text-2xl font-bold">Reading your rover's takes…</h2>
            <span className="font-mono text-primary">{total ? `${roll.done} / ${total}` : "…"}</span>
          </div>
          <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full bg-primary transition-all" style={{ width: `${total ? (roll.done / total) * 100 : 5}%` }} />
          </div>
          <p className="tape mt-3 text-muted-foreground">scene detection · project grouping · trend matching</p>
          <div className="mt-5 grid grid-cols-4 gap-1 sm:grid-cols-7">
            {roll.items.map((r) => (
              <div key={r.id} className="relative aspect-square overflow-hidden rounded-md">
                <img src={r.img} alt={r.name} className="size-full object-cover" />
                <span className="tape absolute inset-x-0 bottom-0 truncate bg-primary px-1 py-0.5 text-[0.55rem] text-primary-foreground">{r.project ?? "Everyday"}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {roll.status === "done" && roll.items.length === 0 && (
        <div className="grid place-items-center rounded-3xl border bg-card px-6 py-16 text-center">
          <span className="text-5xl" aria-hidden>🤖</span>
          <h2 className="mt-6 text-2xl font-bold">No rover takes yet</h2>
          <p className="mt-2 max-w-md text-muted-foreground">Record something with the rover and it shows up here automatically, sorted into its build.</p>
          <Link to="/rover" search={{ rover: undefined }} className="mt-6 rounded-full bg-primary px-6 py-3 font-semibold text-primary-foreground hover:opacity-90">Open Rover Cam</Link>
        </div>
      )}

      {roll.status === "done" && roll.items.length > 0 && (
        <div className="grid gap-6 lg:grid-cols-[250px_1fr]">
          <aside className="space-y-2">
            <p className="tape text-accent">Projects found</p>
            {projects.map(([p, items]) => (
              <button key={p} onClick={() => { setProject(p); setMatches(null); setQ(""); }}
                className={`w-full rounded-xl border px-3 py-2.5 text-left transition-colors ${p === project && !matches ? "border-primary bg-card" : "border-transparent hover:bg-muted"}`}>
                <span className="flex items-center justify-between"><span className="truncate font-semibold">{p}</span><span className="font-mono text-xs opacity-70">{items.length}</span></span>
                <span className="block truncate text-xs text-muted-foreground">📍 {mainPlace(items)}</span>
              </button>
            ))}
            {everyday > 0 && <div className="rounded-xl border border-dashed px-3 py-2.5 text-sm text-muted-foreground">Everyday shots · {everyday} hidden</div>}
            {lowScore.length > 0 && (
              <div className="mt-4 rounded-xl border border-pink bg-card p-4">
                <p className="tape text-pink">Storage</p>
                <p className="mt-1 text-2xl font-bold">{(freeable / 1000).toFixed(1)} GB</p>
                <p className="text-sm text-muted-foreground">in {lowScore.length} low-trend takes</p>
                <button onClick={() => removeItems(lowScore.map((c) => c.id))} className="mt-3 w-full rounded-lg bg-pink px-3 py-2 text-sm font-semibold text-primary-foreground hover:opacity-90">Clear them</button>
                <p className="mt-2 text-xs text-muted-foreground">Takes scoring 40+ are kept.</p>
              </div>
            )}
            <button onClick={() => { resetRoll(); setProject(null); }} className="mt-2 text-xs text-muted-foreground underline">Re-scan rover takes</button>
          </aside>

          <section>
            <form onSubmit={(e) => { e.preventDefault(); runSearch(q); }} className="flex gap-2">
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Describe the take, e.g. “the part where I glue the frame”"
                className="w-full rounded-xl border bg-card px-4 py-3.5 text-lg outline-none focus:ring-2 focus:ring-ring" />
              <button disabled={searching} className="rounded-xl bg-primary px-5 font-semibold text-primary-foreground disabled:opacity-60">{searching ? "…" : "Search"}</button>
            </form>
            <div className="mt-2 flex flex-wrap gap-2">
              {suggestions.map((s) => (
                <button key={s} onClick={() => runSearch(s)} className="rounded-full border px-3 py-1 text-sm text-muted-foreground hover:bg-muted">{s}</button>
              ))}
              {matches && <button onClick={() => { setMatches(null); setQ(""); }} className="rounded-full px-3 py-1 text-sm text-primary underline">Clear search</button>}
            </div>

            {!matches && project && (
              <div className="mt-5 flex flex-wrap items-center gap-2 text-sm">
                <h2 className="mr-2 text-2xl font-bold">{project}</h2>
                <span className="rounded-full border border-blue px-3 py-1 text-blue">📍 {place}</span>
                <span className="rounded-full border border-success px-3 py-1 text-success">🧠 grouped by what's in frame</span>
              </div>
            )}

            <div className="mt-5 grid grid-cols-2 gap-4 md:grid-cols-3">
              {shown.map(({ c, why }) => (
                <div key={c.id} className="overflow-hidden rounded-xl border bg-card">
                  <div className="relative aspect-[4/3]">
                    {c.video
                      ? <video src={c.video} poster={c.img} controls playsInline preload="none" className="size-full bg-black object-cover" />
                      : <img src={c.img} alt={c.description} loading="lazy" className="size-full object-cover" />}
                    {c.trend != null && (
                      <span className={`tape absolute left-2 top-2 rounded px-2 py-1 ${c.trend >= 70 ? "bg-primary text-primary-foreground" : c.trend >= 40 ? "bg-accent text-accent-foreground" : "bg-muted text-muted-foreground"}`}>Trend {c.trend}</span>
                    )}
                    {c.source === "rover" && <span className="tape absolute right-2 top-2 rounded bg-accent px-2 py-1 text-accent-foreground">Rover cam</span>}
                    {c.duration && !c.video && <span className="tape absolute bottom-2 right-2 rounded bg-background/80 px-1.5 py-0.5">{Math.floor(c.duration / 60)}:{String(c.duration % 60).padStart(2, "0")}</span>}
                  </div>
                  <div className="p-3 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono">{c.name}</span>
                      <span className="shrink-0 text-muted-foreground">{c.sizeMb} MB</span>
                    </div>
                    <p className="mt-0.5 text-xs text-muted-foreground">{new Date(c.takenAt).toLocaleDateString(undefined, { month: "short", day: "numeric" })} · {c.groupedBy.join(" · ")}</p>
                    {why && <p className="mt-1 text-primary">{why}</p>}
                    {c.lookalike && (
                      <a href={c.lookalike.url ?? undefined} target="_blank" rel="noreferrer" className="mt-1 block truncate text-xs text-muted-foreground hover:text-foreground">
                        Looks like: “{c.lookalike.caption}” · {fmtViews(c.lookalike.views)} views
                      </a>
                    )}
                  </div>
                </div>
              ))}
              {shown.length === 0 && <p className="col-span-full text-muted-foreground">{matches ? "No clips match that. Try different words." : "No clips here yet."}</p>}
            </div>
            <p className="mt-4 text-xs text-muted-foreground">Trend score: how popular videos that look like this clip are right now.</p>
          </section>
        </div>
      )}

      {asking && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-background/70 p-6 backdrop-blur-sm" role="dialog" aria-label="Photo access">
          <div className="w-full max-w-xs overflow-hidden rounded-2xl bg-popover text-center">
            <div className="p-5">
              <p className="font-semibold">“Workbench” Would Like Access to Your Rover Camera Roll</p>
              <p className="mt-2 text-sm text-muted-foreground">Workbench reads what's in each take to sort your build clips into projects.</p>
            </div>
            {["Allow Full Access", "Limit Access…", "Don't Allow"].map((l, i) => (
              <button key={l} onClick={i === 2 ? () => setAsking(false) : allow}
                className={`block w-full border-t py-3 text-blue ${i === 0 ? "font-semibold" : ""} hover:bg-muted`}>{l}</button>
            ))}
          </div>
        </div>
      )}
    </AppShell>
  );
}
