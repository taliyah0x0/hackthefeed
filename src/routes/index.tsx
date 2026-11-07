import { createFileRoute, Link } from "@tanstack/react-router";
import { AppShell } from "@/components/AppShell";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Workbench — tools for build creators" },
      { name: "description", content: "Plan every beat, find any clip, and learn why a video flopped. Built for maker content creators." },
      { property: "og:title", content: "Workbench — tools for build creators" },
      { property: "og:description", content: "Shot lists, clip search and a kind performance coach for maker videos." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Index,
});

const tools = [
  { to: "/missing-beat", n: "01", title: "Missing Beat", line: "A shot list from viral builds before you film. A gap-check after.", icon: "🎬", color: "border-primary", quote: "“You forgot the assembly shot.”" },
  { to: "/buildroll", n: "02", title: "BuildRoll", line: "Every rover take, sorted into projects. Search them in plain words.", icon: "🗂️", color: "border-pink", quote: "“the clip where I’m soldering the LED”" },
  { to: "/flop", n: "03", title: "Why Did It Flop?", line: "Compare with similar videos and get kind, specific next steps.", icon: "📈", color: "border-blue", quote: "“Show the lamp by 0:01.”" },
  { to: "/rover", n: "04", title: "Rover Cam", line: "A robot camera that follows your face or hands. Drive and record from your phone.", icon: "🤖", color: "border-accent", quote: "“follow my hand… action!”" },
] as const;

function Index() {
  return (
    <AppShell>
      <section className="mb-12 max-w-3xl">
        <p className="tape text-accent">For people who make things on camera</p>
        <h1 className="mt-3 text-5xl font-extrabold leading-[1.02] md:text-7xl">
          Film the whole story of the build.<span className="block text-primary">Then make it spread.</span>
        </h1>
        <p className="mt-5 text-lg text-muted-foreground">
          Viewers love how a build comes together. Workbench helps you catch every step, find every take, and keep your head up when a video underperforms.
        </p>
      </section>
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {tools.map((t) => (
          <Link key={t.to} to={t.to} className={`group overflow-hidden rounded-2xl border-[1.5px] ${t.color} bg-card transition-all hover:-translate-y-1 hover:shadow-xl`}>
            <div className="relative grid aspect-[4/3] place-items-center overflow-hidden bg-muted">
              <span className="text-6xl transition-transform duration-500 group-hover:scale-110" aria-hidden>{t.icon}</span>
              <span className="tape absolute left-3 top-3 rounded bg-background/80 px-2 py-1">{t.n}</span>
            </div>
            <div className="p-5">
              <h2 className="text-2xl font-bold">{t.title}</h2>
              <p className="mt-1 text-muted-foreground">{t.line}</p>
              <p className="mt-4 font-mono text-sm text-primary">{t.quote}</p>
            </div>
          </Link>
        ))}
      </div>
    </AppShell>
  );
}
