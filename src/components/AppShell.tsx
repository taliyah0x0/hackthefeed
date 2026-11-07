import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { useRoverAutoSync } from "@/lib/rover-sync";

const nav = [
  { to: "/missing-beat", label: "Missing Beat", n: "01" },
  { to: "/buildroll", label: "BuildRoll", n: "02" },
  { to: "/flop", label: "Why Did It Flop?", n: "03" },
  { to: "/rover", label: "Rover Cam", n: "04" },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  useRoverAutoSync(); // new rover takes land in BuildRoll from any page
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b bg-background/85 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-5 py-3">
          <Link to="/" className="flex items-center gap-2 font-display text-xl font-bold tracking-tight">
            Workbench
          </Link>
          <nav className="ml-auto flex flex-wrap gap-1">
            {nav.map((n) => (
              <Link
                key={n.to}
                to={n.to}
                className="rounded-full px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                activeProps={{ className: "border border-foreground/40 text-foreground" }}
              >
                <span className="tape mr-1.5 opacity-60">{n.n}</span>
                {n.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 py-10">{children}</main>
      <footer className="mx-auto max-w-6xl px-5 pb-10 tape text-muted-foreground">
        Powered by Oriane (viral video search) + GMI Cloud (vision + Kling video) · Rover Cam runs on your own robot
      </footer>
    </div>
  );
}

export function PageHeader({ kicker, title, sub }: { kicker: string; title: string; sub: string }) {
  return (
    <div className="mb-8 max-w-2xl">
      <p className="tape text-accent">{kicker}</p>
      <h1 className="mt-2 text-4xl font-extrabold md:text-6xl">{title}</h1>
      <p className="mt-3 text-lg text-muted-foreground">{sub}</p>
    </div>
  );
}
