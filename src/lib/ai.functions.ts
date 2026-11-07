import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";
import { contentUrl, gmiJson, lookupVideo, parseVideoUrl, searchViral, startVideo, videoStatus, visualTrend, youtubeOembed } from "./ai.server";

const img = z.string().startsWith("data:image/").max(2_000_000);

export type Beat = { id: string; label: string; shot: string; why: string };
export type RefVideo = { title: string; views: number; creator: string; thumb: string | null; url: string | null };

export const planShots = createServerFn({ method: "POST" })
  .inputValidator((d) => z.object({ idea: z.string().min(2).max(200) }).parse(d))
  .handler(async ({ data }) => {
    let refs: Awaited<ReturnType<typeof searchViral>> = [];
    try {
      refs = await searchViral(data.idea, 8);
    } catch (e) {
      console.error(e);
    }
    const top = refs.slice(0, 4);
    const parts: Parameters<typeof gmiJson>[1] = [
      {
        type: "text",
        text:
          `Build idea: "${data.idea}".\nTop performing similar videos:\n` +
          top
            .map((r, i) => {
              const m = r.media?.[0];
              return `#${i + 1} ${r.viewsCount ?? "?"} views, ${m?.duration ?? "?"}s. Caption: ${(r.caption ?? "").slice(0, 200)}. On-screen text: ${(m?.onScreenText ?? []).slice(0, 12).join(" | ")}. Frames shown at: ${(m?.frames ?? []).map((f) => f.timestampSeconds.toFixed(1) + "s").join(", ")}`;
            })
            .join("\n") +
          `\nKeyframes from the top videos follow.`,
      },
    ];
    for (const r of top.slice(0, 3)) {
      for (const f of (r.media?.[0]?.frames ?? []).slice(0, 3)) parts.push({ type: "image_url", image_url: { url: f.url } });
    }
    const out = await gmiJson<{ beats: Beat[] }>(
      `You help maker/DIY content creators plan what to film. From the viral reference videos, extract the story beats of a build video (e.g. sketch, model, print, wiring, assembly, reveal) as a 5-8 item shot checklist in story order. Each beat: id (short kebab-case), label (short, plain words), shot ("Wide"|"Medium"|"Close-up"), why (one short sentence grounded in what the reference videos do, mention timing if useful). JSON shape: {"beats":[...]}`,
      parts,
    );
    const references: RefVideo[] = refs.slice(0, 4).map((r) => ({
      title: (r.caption ?? "Untitled").split("\n")[0]!.slice(0, 70),
      views: r.viewsCount ?? 0,
      creator: "@" + r.profileHandle,
      thumb: r.thumbnailMediaUrl,
      url: contentUrl(r),
    }));
    return { beats: (out.beats ?? []).slice(0, 8), references };
  });

export const checkClips = createServerFn({ method: "POST" })
  .inputValidator((d) =>
    z
      .object({
        beats: z.array(z.object({ id: z.string(), label: z.string(), shot: z.string(), why: z.string() })).min(1).max(10),
        clips: z.array(z.object({ id: z.string(), name: z.string(), frame: img })).min(1).max(12),
      })
      .parse(d),
  )
  .handler(async ({ data }) => {
    const parts: Parameters<typeof gmiJson>[1] = [
      { type: "text", text: `Checklist beats:\n${data.beats.map((b) => `- ${b.id}: ${b.label} (${b.shot})`).join("\n")}\n\nClips follow, each labeled by id.` },
    ];
    for (const c of data.clips) {
      parts.push({ type: "text", text: `Clip id=${c.id} (${c.name}):` });
      parts.push({ type: "image_url", image_url: { url: c.frame } });
    }
    const out = await gmiJson<{
      clips: Array<{ id: string; beatId: string | null; seen: string }>;
      missing: Array<{ beatId: string; tip: string; fillerFromClipId: string | null; motionPrompt: string }>;
    }>(
      `You check a maker's filmed clips against their shot checklist. For each clip, match it to the one beat it best covers (or null) and describe what is seen in under 8 words. Then list beats with no matching clip. For each missing beat give a friendly one-sentence reshoot tip, pick the clip id whose frame would make the best starting image for an AI filler clip of that beat (or null), and write a short camera/motion prompt for animating that frame into the missing beat (e.g. "hands pick up the parts and screw the shade onto the base, steady medium shot"). JSON: {"clips":[{"id","beatId","seen"}],"missing":[{"beatId","tip","fillerFromClipId","motionPrompt"}]}`,
      parts,
    );
    return { clips: out.clips ?? [], missing: out.missing ?? [] };
  });

export const startFiller = createServerFn({ method: "POST" })
  .inputValidator((d) => z.object({ frame: img, prompt: z.string().min(3).max(1000) }).parse(d))
  .handler(async ({ data }) => ({
    requestId: await startVideo(data.frame, `${data.prompt}. Realistic handheld phone footage, natural workshop light, no text.`),
  }));

export const getFiller = createServerFn({ method: "POST" })
  .inputValidator((d) => z.object({ requestId: z.string().min(4).max(100) }).parse(d))
  .handler(async ({ data }) => videoStatus(data.requestId));

export type ClipInfo = { description: string; tags: string[]; project: string };

export const indexClip = createServerFn({ method: "POST" })
  .inputValidator((d) => z.object({ frame: img, projects: z.array(z.string().max(60)).max(30) }).parse(d))
  .handler(async ({ data }) => {
    const [info, trend] = await Promise.all([
      gmiJson<ClipInfo>(
        `You sort a maker's camera-roll clips into build projects. Describe the frame in one sentence, give 5-8 lowercase search tags (objects, actions, shot type), and pick the project it belongs to from the existing list, or invent a short new project name (2-3 words) if none fit. If it's an everyday non-build photo use project "Everyday". JSON: {"description","tags","project"}`,
        [
          { type: "text", text: `Existing projects: ${data.projects.join(", ") || "(none)"}` },
          { type: "image_url", image_url: { url: data.frame } },
        ],
      ),
      Promise.race([
        visualTrend(data.frame.split(",")[1] ?? ""),
        new Promise<never>((_, rej) => setTimeout(() => rej(new Error("Trend lookup timed out")), 15000)),
      ]).catch((e) => {
        console.error(e);
        return { score: null as number | null, lookalike: null };
      }),
    ]);
    return { ...info, trend: trend.score, lookalike: trend.lookalike };
  });

export const searchClips = createServerFn({ method: "POST" })
  .inputValidator((d) =>
    z
      .object({
        query: z.string().min(1).max(200),
        clips: z.array(z.object({ id: z.string(), description: z.string(), tags: z.array(z.string()) })).max(200),
      })
      .parse(d),
  )
  .handler(async ({ data }) => {
    const out = await gmiJson<{ matches: Array<{ id: string; why: string }> }>(
      `You search a creator's clip library. Return clips that match the plain-language query, best first, at most 6. Only include real matches. JSON: {"matches":[{"id","why"}]} where why is under 10 words.`,
      [{ type: "text", text: `Query: "${data.query}"\nClips:\n${data.clips.map((c) => `${c.id}: ${c.description} [${c.tags.join(", ")}]`).join("\n")}` }],
    );
    return out.matches ?? [];
  });

export const coachVideo = createServerFn({ method: "POST" })
  .inputValidator((d) =>
    z
      .object({
        topic: z.string().min(2).max(200),
        views: z.number().int().min(0).max(1e10).nullable(),
        durationSec: z.number().min(0).max(3600),
        frames: z.array(z.object({ t: z.number(), frame: z.union([img, z.string().url().startsWith("https://")]) })).min(1).max(8),
        remote: z.boolean().optional(),
      })
      .parse(d),
  )
  .handler(async ({ data }) => {
    const refs = await searchViral(data.topic, 6);
    const top = refs.slice(0, 3);
    const parts: Parameters<typeof gmiJson>[1] = [
      {
        type: "text",
        text: `Creator's video about "${data.topic}", ${Math.round(data.durationSec)}s long${data.views != null ? `, ${data.views} views` : ""}. Their frames at the given seconds follow.`,
      },
    ];
    for (const f of data.frames) {
      parts.push({ type: "text", text: `Creator frame at ${f.t.toFixed(1)}s:` });
      parts.push({ type: "image_url", image_url: { url: f.frame } });
    }
    top.forEach((r, i) => {
      const m = r.media?.[0];
      const firstSpeech = m?.transcriptChunks?.[0]?.startSeconds;
      parts.push({
        type: "text",
        text: `Top video #${i + 1}: ${r.viewsCount} views, ${m?.duration ?? "?"}s, caption "${(r.caption ?? "").slice(0, 120)}", on-screen text: ${(m?.onScreenText ?? []).slice(0, 8).join(" | ")}, first speech at ${firstSpeech ?? "none"}s. Its frames:`,
      });
      for (const f of (m?.frames ?? []).slice(0, 3)) {
        parts.push({ type: "text", text: `#${i + 1} at ${f.timestampSeconds.toFixed(1)}s:` });
        parts.push({ type: "image_url", image_url: { url: f.url } });
      }
    });
    const out = await gmiJson<{
      goodNews: string;
      rows: Array<{ label: string; you: string; top: string; gap: boolean }>;
      steps: Array<{ title: string; detail: string }>;
    }>(
      `You are a warm, honest performance coach for maker content creators who feel discouraged when a video underperforms. Compare the creator's opening with the top videos. Be concrete with timestamps (e.g. "your finished product first appears at 0:04, top videos show it by 0:01"). Never shame, never focus on view counts. JSON: {"goodNews": one genuine specific compliment sentence, "rows": 4-6 comparisons {label, you, top, gap:boolean} using short values like "0:04", "Yes", "2", "steps": exactly 3 {title: short imperative, detail: one specific sentence}}`,
      parts,
    );
    return {
      ...out,
      references: refs.slice(0, 3).map((r) => ({
        title: (r.caption ?? "Untitled").split("\n")[0]!.slice(0, 60),
        views: r.viewsCount ?? 0,
        creator: "@" + r.profileHandle,
        thumb: r.thumbnailMediaUrl,
        url: contentUrl(r),
      })),
    };
  });

export const coachLink = createServerFn({ method: "POST" })
  .inputValidator((d) => z.object({ url: z.string().url().max(500), topic: z.string().max(200) }).parse(d))
  .handler(async ({ data }) => {
    const parsed = parseVideoUrl(data.url);
    if (!parsed) throw new Error("That doesn't look like an Instagram, YouTube or TikTok video link.");
    const v = await lookupVideo(parsed.platform, parsed.platformId);
    let frames: { t: number; frame: string }[] = [];
    let caption = "";
    let video: {
      platform: string; creator: string; caption: string; thumb: string | null; url: string;
      views: number | null; likes: number | null; comments: number | null; duration: number | null; found: boolean;
    };
    if (v) {
      const m = v.media?.[0];
      caption = v.caption ?? "";
      frames = (m?.frames ?? []).slice(0, 6).map((f) => ({ t: f.timestampSeconds, frame: f.url }));
      if (!frames.length && v.thumbnailMediaUrl) frames = [{ t: 0, frame: v.thumbnailMediaUrl }];
      video = {
        platform: v.platform, creator: "@" + v.profileHandle, caption: caption.split("\n")[0]!.slice(0, 140),
        thumb: v.thumbnailMediaUrl, url: data.url, views: v.viewsCount, likes: v.likesCount,
        comments: v.commentsCount ?? null, duration: m?.duration ?? null, found: true,
      };
    } else if (parsed.platform === "youtube") {
      const o = await youtubeOembed(parsed.platformId);
      if (!o) throw new Error("We couldn't find that YouTube video. Is it public?");
      caption = o.title;
      frames = [{ t: 0, frame: o.thumb }];
      video = { platform: "youtube", creator: o.author, caption: o.title, thumb: o.thumb, url: data.url, views: null, likes: null, comments: null, duration: null, found: false };
    } else {
      throw new Error("We couldn't find stats for that video yet. Try another public reel or a YouTube link.");
    }
    const topic = (data.topic.trim() || caption.replace(/#\S+/g, "").trim().split(/\s+/).slice(0, 5).join(" ") || "maker build").slice(0, 200);
    const coaching = await coachVideo({ data: { topic, views: video.views, durationSec: video.duration ?? 30, frames, remote: true } });
    return { ...coaching, video, topic };
  });

export type FramingCheck = { verdict: "good" | "adjust"; tip: string; zoom: "in" | "out" | "hold"; follow: "face" | "hand" | "keep" };

/** Rover Cam: look at the rover's live frame and say whether it fits the beat being filmed. */
export const checkFraming = createServerFn({ method: "POST" })
  .inputValidator((d) =>
    z
      .object({
        frame: img,
        beat: z.object({ label: z.string().max(120), shot: z.string().max(30), why: z.string().max(300) }).nullable(),
        mode: z.enum(["off", "face", "hand"]),
      })
      .parse(d),
  )
  .handler(async ({ data }) => {
    const goal = data.beat
      ? `They are about to film the beat "${data.beat.label}" as a ${data.beat.shot} shot (${data.beat.why}).`
      : "They are filming a maker build video.";
    const out = await gmiJson<FramingCheck>(
      `You are a camera operator for a solo maker filming with a robot camera rover that follows their face or hands. Judge the live frame for the shot they want. ${goal} The rover is currently in "${data.mode}" follow mode. Decide: verdict "good" if it's ready to record, else "adjust"; tip = one short friendly sentence (max 18 words) on what to change (framing, light, clutter, hands in view); zoom = "in" if the subject should fill more of the frame, "out" if it's too tight, else "hold"; follow = "hand" if the shot is about what the hands are doing and the rover should follow hands, "face" if it's a talking/reaction shot, else "keep". JSON: {"verdict","tip","zoom","follow"}`,
      [
        { type: "text", text: "Live frame from the rover camera:" },
        { type: "image_url", image_url: { url: data.frame } },
      ],
    );
    return {
      verdict: out.verdict === "good" ? "good" : "adjust",
      tip: String(out.tip ?? "").slice(0, 200),
      zoom: out.zoom === "in" || out.zoom === "out" ? out.zoom : "hold",
      follow: out.follow === "face" || out.follow === "hand" ? out.follow : "keep",
    } satisfies FramingCheck;
  });
