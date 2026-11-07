import { createFileRoute } from "@tanstack/react-router";
import { useServerFn } from "@tanstack/react-start";
import { useState } from "react";
import { AppShell, PageHeader } from "@/components/AppShell";
import { coachLink } from "@/lib/ai.functions";
import { fmtViews } from "@/lib/frames";

export const Route = createFileRoute("/flop")({
  head: () => ({
    meta: [
      { title: "Why Did It Flop? — a kind performance coach | Workbench" },
      { name: "description", content: "Compare your video with similar top videos and get specific, encouraging next steps." },
      { property: "og:title", content: "Why Did It Flop? — your video, explained kindly" },
      { property: "og:description", content: "Side-by-side timing comparison and next steps, not just a view count." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Flop,
});

type Out = Awaited<ReturnType<typeof coachLink>>;

function Flop() {
  const coach = useServerFn(coachLink);
  const [url, setUrl] = useState("");
  const [topic, setTopic] = useState("");
  const [loading, setLoading] = useState(false);
  const [out, setOut] = useState<Out | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (link = url) => {
    if (!link.trim()) return;
    setLoading(true); setError(null); setOut(null);
    try { setOut(await coach({ data: { url: link.trim(), topic } })); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  };
  const v = out?.video;

  return (
    <AppShell>
      <PageHeader kicker="03 · Why Did It Flop?" title="One video doesn't define you." sub="Paste a link to your Instagram, YouTube or TikTok video. We pull its real stats, compare it with similar videos that took off, and turn the difference into a few kind, doable changes." />

      {error && <p className="mb-4 rounded-lg border border-destructive bg-card px-4 py-3 text-sm text-destructive">{error}</p>}

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        <div className="rounded-2xl border bg-card p-5">
          <label className="tape block text-muted-foreground" htmlFor="url">Video link</label>
          <input id="url" type="url" value={url} placeholder="https://www.instagram.com/reel/…" onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") run(); }}
            className="mt-1 w-full rounded-lg border bg-background px-3 py-2 outline-none focus:ring-2 focus:ring-ring" />
          <label className="tape mt-4 block text-muted-foreground" htmlFor="topic">What's it about? (optional)</label>
          <input id="topic" value={topic} placeholder="We'll guess from the caption" onChange={(e) => setTopic(e.target.value)} className="mt-1 w-full rounded-lg border bg-background px-3 py-2 outline-none focus:ring-2 focus:ring-ring" />
          <button onClick={() => run()} disabled={loading || !url.trim()}
            className="mt-4 w-full rounded-lg bg-primary px-4 py-3 font-semibold text-primary-foreground hover:opacity-90 disabled:opacity-60">
            {loading ? "Pulling stats & comparing…" : out ? "Run again" : "What could help?"}
          </button>

          {v && (
            <a href={v.url} target="_blank" rel="noreferrer" className="mt-5 block overflow-hidden rounded-xl border hover:bg-muted">
              {v.thumb && <img src={v.thumb} alt="Your video" className="aspect-[9/12] w-full object-cover" />}
              <div className="p-3">
                <p className="tape text-muted-foreground">{v.platform} · {v.creator}</p>
                <p className="mt-1 line-clamp-2 text-sm">{v.caption}</p>
              </div>
            </a>
          )}
          {v && (
            <div className="mt-3 grid grid-cols-2 gap-2 text-center">
              {[["Views", v.views], ["Likes", v.likes], ["Comments", v.comments], ["Length", v.duration != null ? `${Math.round(v.duration)}s` : null]].map(([k, val]) => (
                <div key={k as string} className="rounded-lg bg-muted p-2">
                  <p className="font-mono text-lg font-semibold">{val == null ? "—" : typeof val === "number" ? fmtViews(val) : val}</p>
                  <p className="tape text-muted-foreground">{k}</p>
                </div>
              ))}
            </div>
          )}
          {v && !v.found && <p className="mt-2 text-xs text-muted-foreground">Full stats aren't available for this video yet, so we compared its title and cover.</p>}
        </div>

        {!out ? (
          <div className="grid place-items-center rounded-2xl border border-dashed p-10 text-center text-muted-foreground">
            {loading ? <p className="animate-pulse">Pulling your stats and watching the top videos like it…</p> : "Paste a link and we'll compare it with similar builds that did well."}
          </div>
        ) : (
          <div className="space-y-6">
            <div className="rounded-2xl bg-secondary p-6 text-secondary-foreground">
              <p className="tape opacity-70">First, the good news</p>
              <p className="mt-2 font-display text-xl">{out.goodNews}</p>
            </div>

            <div className="overflow-hidden rounded-2xl border bg-card">
              <div className="tape grid grid-cols-[1fr_90px_90px] gap-2 border-b px-5 py-3 text-muted-foreground">
                <span>Moment</span><span>You</span><span>Top videos</span>
              </div>
              {out.rows.map((r) => (
                <div key={r.label} className="grid grid-cols-[1fr_90px_90px] items-center gap-2 border-b px-5 py-3 last:border-0">
                  <span>{r.label}</span>
                  <span className={`font-mono ${r.gap ? "text-destructive" : "text-success"}`}>{r.you}</span>
                  <span className="font-mono">{r.top}</span>
                </div>
              ))}
            </div>

            <div>
              <h2 className="text-2xl font-bold">Try this next time</h2>
              <ol className="mt-3 space-y-3">
                {out.steps.map((s, i) => (
                  <li key={s.title} className="flex gap-4 rounded-xl border bg-card p-4">
                    <span className="grid size-8 shrink-0 place-items-center rounded-full bg-primary font-bold text-primary-foreground">{i + 1}</span>
                    <div><p className="font-semibold">{s.title}</p><p className="text-muted-foreground">{s.detail}</p></div>
                  </li>
                ))}
              </ol>
            </div>

            {out.references.length > 0 && (
              <div>
                <p className="tape mb-2 text-muted-foreground">Compared with</p>
                <div className="grid gap-3 sm:grid-cols-3">
                  {out.references.map((r) => (
                    <a key={r.title + r.creator} href={r.url ?? undefined} target="_blank" rel="noreferrer" className="flex items-center gap-3 rounded-xl border bg-card p-2 text-sm hover:bg-muted">
                      {r.thumb && <img src={r.thumb} alt="" className="size-12 shrink-0 rounded object-cover" loading="lazy" />}
                      <span className="min-w-0"><span className="block truncate font-medium">{r.title}</span><span className="text-muted-foreground">{r.creator} · {fmtViews(r.views)}</span></span>
                    </a>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </AppShell>
  );
}
