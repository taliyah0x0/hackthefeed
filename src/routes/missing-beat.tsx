import { createFileRoute } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { AppShell, PageHeader } from "@/components/AppShell";
import { useRoll } from "@/lib/roll-store";
import { checkClips, getFiller, planShots, startFiller, type Beat, type RefVideo } from "@/lib/ai.functions";
import { fmtViews, imageToDataUrl } from "@/lib/frames";

export const Route = createFileRoute("/missing-beat")({
  head: () => ({
    meta: [
      { title: "Missing Beat — shot list and gap-check | Workbench" },
      { name: "description", content: "Turn viral builds into a shot checklist, then check your clips for missing beats." },
      { property: "og:title", content: "Missing Beat — never forget the assembly shot" },
      { property: "og:description", content: "Shot list before filming, gap-check after, optional filler clips." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: MissingBeat,
});

type Stage = "plan" | "check";
type Clip = { id: string; name: string; preview: string; frame: string | null; isVideo: boolean };
type Result = Awaited<ReturnType<typeof checkClips>>;
type Filler = { state: "making" | "ready" | "failed"; requestId?: string; url?: string | null; error?: string };

const STORE = "workbench-plan";

function MissingBeat() {
  const plan = useServerFn(planShots);
  const check = useServerFn(checkClips);
  const start = useServerFn(startFiller);
  const poll = useServerFn(getFiller);

  const [idea, setIdea] = useState("");
  const [beats, setBeats] = useState<Beat[]>([]);
  const [refs, setRefs] = useState<RefVideo[]>([]);
  const [planning, setPlanning] = useState(false);
  const [stage, setStage] = useState<Stage>("plan");
  const [done, setDone] = useState<Record<string, boolean>>({});
  const [clips, setClips] = useState<Clip[]>([]);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [fillers, setFillers] = useState<Record<string, Filler>>({});
  const [skipped, setSkipped] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const roll = useRoll();
  const rollProjects = useMemo(() => [...new Set(roll.items.map((i) => i.project).filter(Boolean) as string[])], [roll.items]);
  const [rollProject, setRollProject] = useState<string | null>(null);
  useEffect(() => {
    if (!rollProjects.length) return;
    const p = rollProject && rollProjects.includes(rollProject) ? rollProject : rollProjects[0]!;
    setRollProject(p);
    setClips(roll.items.filter((i) => i.project === p && i.kind === "video").slice(0, 12).reverse()
      .map((i) => ({ id: i.id, name: i.name, preview: i.img, frame: null, isVideo: true })));
    setResult(null); setFillers({});
  }, [rollProjects, rollProject, roll.items]);

  useEffect(() => {
    try {
      const s = JSON.parse(localStorage.getItem(STORE) ?? "null");
      if (s?.beats?.length) { setBeats(s.beats); setRefs(s.refs ?? []); setIdea(s.idea ?? idea); }
    } catch { /* ignore */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runPlan = async () => {
    setPlanning(true); setError(null);
    try {
      const r = await plan({ data: { idea } });
      setBeats(r.beats); setRefs(r.references); setDone({}); setResult(null);
      localStorage.setItem(STORE, JSON.stringify({ idea, beats: r.beats, refs: r.references }));
    } catch (e) { setError((e as Error).message); }
    finally { setPlanning(false); }
  };

  const runCheck = async () => {
    if (!beats.length) { setError("Make a shot list first (Before filming)."); return; }
    setChecking(true); setError(null); setFillers({});
    try {
      const withFrames = await Promise.all(clips.map(async (c) => ({ ...c, frame: c.frame ?? (await imageToDataUrl(c.preview)) })));
      setClips(withFrames);
      setResult(await check({ data: { beats, clips: withFrames.map((c) => ({ id: c.id, name: c.name, frame: c.frame! })) } }));
    } catch (e) { setError((e as Error).message); }
    finally { setChecking(false); }
  };

  const makeFiller = async (m: Result["missing"][number]) => {
    const src = clips.find((c) => c.id === m.fillerFromClipId) ?? clips[0];
    if (!src?.frame) return;
    setFillers((f) => ({ ...f, [m.beatId]: { state: "making" } }));
    try {
      const { requestId } = await start({ data: { frame: src.frame, prompt: m.motionPrompt } });
      setFillers((f) => ({ ...f, [m.beatId]: { state: "making", requestId } }));
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 5000));
        const s = await poll({ data: { requestId } });
        if (s.status === "success" && s.url) { setFillers((f) => ({ ...f, [m.beatId]: { state: "ready", url: s.url } })); return; }
        if (s.status === "failed" || s.status === "cancelled") throw new Error("The video model couldn't make this clip.");
      }
      throw new Error("Still rendering after 5 minutes. Try again later.");
    } catch (e) {
      setFillers((f) => ({ ...f, [m.beatId]: { state: "failed", error: (e as Error).message } }));
    }
  };

  const beatById = (id: string | null) => beats.find((b) => b.id === id);
  const covered = new Set((result?.clips ?? []).map((c) => c.beatId).filter(Boolean) as string[]);
  const missing = (result?.missing ?? []).filter((m) => beatById(m.beatId));
  const headline = missing[0] ? `You forgot the ${beatById(missing[0].beatId)!.label.toLowerCase()} shot.` : "Every beat is covered. Nice.";

  return (
    <AppShell>
      <PageHeader kicker="01 · Missing Beat" title="Every step, on camera." sub="Plan from what's working on TikTok, Reels and Shorts, film freely, then catch the gaps before you edit." />

      <div className="mb-6 inline-flex rounded-full border bg-card p-1">
        {(["plan", "check"] as const).map((s) => (
          <button key={s} onClick={() => setStage(s)}
            className={`rounded-full px-4 py-1.5 text-sm font-medium transition-colors ${stage === s ? "bg-secondary text-secondary-foreground" : "text-muted-foreground hover:text-foreground"}`}>
            {s === "plan" ? "Before filming" : "After filming"}
          </button>
        ))}
      </div>

      {error && <p className="mb-4 rounded-lg border border-destructive bg-card px-4 py-3 text-sm text-destructive">{error}</p>}

      {stage === "plan" && (
        <section className="grid gap-6 lg:grid-cols-[1fr_1.3fr]">
          <div className="rounded-2xl border bg-card p-6">
            <label className="tape text-muted-foreground" htmlFor="idea">What are you building?</label>
            <input id="idea" value={idea} placeholder="e.g. 3D printed LED desk lamp" onChange={(e) => setIdea(e.target.value)}
              className="mt-2 w-full rounded-lg border bg-background px-3 py-2.5 text-lg outline-none focus:ring-2 focus:ring-ring" />
            <button onClick={runPlan} disabled={planning || idea.trim().length < 2}
              className="mt-4 w-full rounded-lg bg-primary px-4 py-3 font-semibold text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-60">
              {planning ? "Studying viral builds like yours…" : beats.length ? "Rebuild my shot list" : "Build my shot list"}
            </button>
            {refs.length > 0 && (
              <div className="mt-6">
                <p className="tape mb-2 text-muted-foreground">Based on these top videos</p>
                <ul className="space-y-2">
                  {refs.map((v) => (
                    <li key={v.title + v.creator}>
                      <a href={v.url ?? undefined} target="_blank" rel="noreferrer" className="flex items-center gap-3 rounded-lg bg-muted p-2 text-sm hover:bg-accent">
                        {v.thumb && <img src={v.thumb} alt="" className="size-12 shrink-0 rounded object-cover" loading="lazy" />}
                        <span className="min-w-0 flex-1"><span className="block truncate font-medium">{v.title}</span><span className="text-muted-foreground">{v.creator}</span></span>
                        <span className="font-mono text-primary">{fmtViews(v.views)}</span>
                      </a>
                    </li>
                  ))}
                </ul>
                <p className="mt-4 text-sm text-muted-foreground">You can only plan so much. Treat this as a nudge, not a script.</p>
              </div>
            )}
          </div>

          <div className="rounded-2xl border bg-card p-6">
            <h2 className="text-2xl font-bold">Shot list</h2>
            {!beats.length ? (
              <p className="mt-3 text-muted-foreground">Your beats will show up here.</p>
            ) : (
              <>
                <ol className="mt-4 space-y-2">
                  {beats.map((b, i) => (
                    <li key={b.id}>
                      <label className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted">
                        <input type="checkbox" checked={!!done[b.id]} onChange={() => setDone({ ...done, [b.id]: !done[b.id] })} className="mt-1 size-4 accent-primary" />
                        <span className="flex-1">
                          <span className={`font-medium ${done[b.id] ? "line-through opacity-50" : ""}`}>{i + 1}. {b.label}</span>
                          <span className="block text-sm text-muted-foreground">{b.why}</span>
                        </span>
                        <span className="tape rounded bg-accent px-2 py-1 text-accent-foreground">{b.shot}</span>
                      </label>
                    </li>
                  ))}
                </ol>
                <button onClick={() => setStage("check")} className="mt-4 rounded-lg bg-secondary px-4 py-2.5 font-semibold text-secondary-foreground hover:opacity-90">Done filming → check my clips</button>
              </>
            )}
          </div>
        </section>
      )}

      {stage === "check" && (
        <section className="space-y-6">
          <div className="rounded-2xl border bg-card p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-2xl font-bold">Your clips from BuildRoll</h2>
                <p className="text-muted-foreground">{clips.length ? `${clips.length} clips in ${rollProject}` : "Connect your camera roll in BuildRoll and your build clips show up here."}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {rollProjects.length > 1 && (
                  <select value={rollProject ?? ""} onChange={(e) => setRollProject(e.target.value)} aria-label="Project"
                    className="rounded-lg border bg-card px-3 py-2.5 font-medium">
                    {rollProjects.map((p) => <option key={p}>{p}</option>)}
                  </select>
                )}
                {!rollProjects.length && <Link to="/buildroll" className="rounded-lg border px-4 py-2.5 font-medium hover:bg-muted">Open BuildRoll</Link>}
                <button onClick={runCheck} disabled={checking || !clips.length}
                  className="rounded-lg bg-primary px-5 py-2.5 font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-60">
                  {checking ? "Watching your clips…" : result ? "Check again" : "Check for missing beats"}
                </button>
              </div>
            </div>
            {!beats.length && <p className="mt-3 text-sm text-muted-foreground">Tip: make a shot list under “Before filming” first.</p>}
            {clips.length > 0 && (
              <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
                {clips.map((c) => {
                  const r = result?.clips.find((x) => x.id === c.id);
                  const b = beatById(r?.beatId ?? null);
                  return (
                    <div key={c.id} className="overflow-hidden rounded-xl border">
                      <div className="aspect-square"><img src={c.preview} alt={c.name} className="size-full object-cover" /></div>
                      <div className="p-2 text-xs">
                        <p className="truncate font-mono">{c.name}</p>
                        {r && <p className={b ? "text-success" : "text-muted-foreground"}>{b ? `✓ ${b.label}` : r.seen}</p>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {result && (
            <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
              <div className="rounded-2xl border bg-card p-6">
                <h3 className="text-xl font-bold">Checklist</h3>
                <ul className="mt-3 space-y-1.5">
                  {beats.map((b) => {
                    const ok = covered.has(b.id);
                    return (
                      <li key={b.id} className="flex items-center gap-3 text-sm">
                        <span className={`grid size-6 place-items-center rounded-full text-xs font-bold ${ok ? "bg-success text-success-foreground" : "bg-destructive text-destructive-foreground"}`}>{ok ? "✓" : "!"}</span>
                        <span className={ok ? "" : "font-semibold"}>{b.label}</span>
                        <span className="tape ml-auto text-muted-foreground">{b.shot}</span>
                      </li>
                    );
                  })}
                </ul>
              </div>

              <div className="rounded-2xl border-2 border-primary bg-card p-6">
                <p className="tape text-primary">{missing.length} missing</p>
                <h3 className="mt-1 text-3xl font-bold">{headline}</h3>
                <p className="mt-2 text-muted-foreground">Best fix is a quick real take on your phone. If you can't, an AI filler clip can bridge the gap. It's optional, and worth labeling if you post it.</p>
                <div className="mt-5 space-y-4">
                  {missing.map((m) => {
                    const b = beatById(m.beatId)!;
                    const f = fillers[m.beatId];
                    const src = clips.find((c) => c.id === m.fillerFromClipId) ?? clips[0];
                    return (
                      <div key={m.beatId} className="rounded-xl border p-4">
                        <div className="flex items-center justify-between gap-2">
                          <p className="font-semibold">{b.label}</p>
                          <span className="tape text-muted-foreground">{b.shot}</span>
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">{m.tip}</p>
                        {skipped[m.beatId] ? (
                          <p className="mt-3 text-sm text-success">Added to your next filming list.</p>
                        ) : !f ? (
                          <div className="mt-3 flex flex-wrap gap-2">
                            <button onClick={() => makeFiller(m)} className="rounded-lg bg-secondary px-3 py-2 text-sm font-semibold text-secondary-foreground hover:opacity-90">
                              Make optional filler from {src?.name}
                            </button>
                            <button onClick={() => setSkipped({ ...skipped, [m.beatId]: true })} className="rounded-lg border px-3 py-2 text-sm hover:bg-muted">I'll reshoot it</button>
                          </div>
                        ) : f.state === "making" ? (
                          <div className="mt-3 flex items-center gap-3">
                            <img src={src?.preview} alt="" className="kenburns size-14 rounded object-cover" />
                            <p className="animate-pulse font-mono text-sm">Kling is animating your photo… about a minute or two</p>
                          </div>
                        ) : f.state === "failed" ? (
                          <p className="mt-3 text-sm text-destructive">{f.error} <button className="underline" onClick={() => makeFiller(m)}>Try again</button></p>
                        ) : (
                          <div className="mt-3">
                            <div className="relative overflow-hidden rounded-xl bg-secondary">
                              <video src={f.url ?? undefined} autoPlay loop muted playsInline controls className="w-full" />
                              <span className="tape absolute left-3 top-3 rounded bg-warning px-2 py-1 text-foreground">AI filler · optional</span>
                            </div>
                            <div className="mt-2 flex gap-2">
                              <a href={f.url ?? undefined} target="_blank" rel="noreferrer" download className="rounded-lg bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground">Download</a>
                              <button onClick={() => setFillers((x) => { const n = { ...x }; delete n[m.beatId]; return n; })} className="rounded-lg border px-3 py-2 text-sm hover:bg-muted">Discard</button>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </section>
      )}
    </AppShell>
  );
}
