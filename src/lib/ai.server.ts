// Server-only helpers for Oriane (viral video search) and GMI (vision LLM + Kling video).
const ORIANE = "https://connect.oriane.xyz/rest";
const GMI_CHAT = "https://api.gmi-serving.com/v1/chat/completions";
const GMI_QUEUE = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey/requests";
export const VISION_MODEL = "google/gemini-3.7-flash";
export const FILLER_MODEL = "kling-3.0-turbo-i2v";

function key(name: "ORIANE_API_KEY" | "GMI_API_KEY") {
  const v = process.env[name];
  if (!v) throw new Error(`${name} is not configured`);
  return v;
}

export type OrianeContent = {
  id: string;
  platform: string;
  platformId: string;
  profileHandle: string;
  caption: string | null;
  thumbnailMediaUrl: string | null;
  viewsCount: number | null;
  likesCount: number | null;
  media?: Array<{
    duration?: number;
    onScreenText?: string[];
    transcript?: string | null;
    transcriptChunks?: Array<{ startSeconds: number; text: string }>;
    frames?: Array<{ timestampSeconds: number; url: string; onScreenTextRegions?: unknown[] }>;
  }>;
};

export function contentUrl(c: OrianeContent) {
  if (c.platform === "tiktok") return `https://www.tiktok.com/@${c.profileHandle}/video/${c.platformId}`;
  if (c.platform === "instagram") return `https://www.instagram.com/reel/${c.platformId}/`;
  if (c.platform === "youtube") return `https://www.youtube.com/shorts/${c.platformId}`;
  return null;
}

async function oriane<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${ORIANE}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${key("ORIANE_API_KEY")}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = (await res.json().catch(() => ({}))) as { data?: T; error?: { message?: string } };
  if (!res.ok || !json.data) {
    console.error("Oriane error", res.status, JSON.stringify(json).slice(0, 500));
    throw new Error(`Viral video search failed (${res.status})`);
  }
  return json.data;
}

export async function searchViral(topic: string, limit = 8) {
  const words = topic.trim().slice(0, 120).split(/\s+/);
  // Try the full phrase, then progressively shorter tails ("LED desk lamp" -> "desk lamp").
  const phrases = [...new Set([words.join(" "), words.slice(-3).join(" "), words.slice(-2).join(" ")])];
  const seen = new Map<string, OrianeContent>();
  for (const phrase of phrases) {
    const data = await oriane<{ results: OrianeContent[] }>(
      `/contents/search?limit=${limit}&sort=viewsCount:desc&projection=full`,
      {
        operator: "and",
        filters: {
          format: { includes: ["video"] },
          caption: { includesFuzzy: { values: [phrase] } },
        },
      },
    );
    for (const r of data.results) if (!seen.has(r.id)) seen.set(r.id, r);
    if (seen.size >= 4) break;
  }
  return [...seen.values()].sort((a, b) => (b.viewsCount ?? 0) - (a.viewsCount ?? 0)).slice(0, limit);
}

export async function visualTrend(base64Jpeg: string) {
  const asset = await oriane<{ id: string }>("/assets", {
    type: "image",
    image: { type: "base64", mediaType: "image/jpeg", base64: base64Jpeg },
  });
  const data = await oriane<{ results: OrianeContent[]; aggregations?: { averageViewsCount: number | null } }>(
    `/contents/search?limit=10&sort=visualSimilarity&projection=basic`,
    { operator: "and", filters: { visualSimilarity: { includes: { values: [{ assetId: asset.id, minScore: 0.45 }] } } } },
  );
  const views = data.results.map((r) => r.viewsCount ?? 0).sort((a, b) => b - a);
  const top = views.slice(0, 5);
  const avg = top.length ? top.reduce((a, b) => a + b, 0) / top.length : 0;
  // log scale: ~1k views -> 0, ~10M views -> 100
  const score = avg > 0 ? Math.max(0, Math.min(100, Math.round(((Math.log10(avg) - 3) / 4) * 100))) : 0;
  const best = data.results[0];
  return {
    score,
    lookalike: best
      ? { caption: (best.caption ?? "").slice(0, 80), views: best.viewsCount ?? 0, thumb: best.thumbnailMediaUrl, url: contentUrl(best) }
      : null,
  };
}

type Part = { type: "text"; text: string } | { type: "image_url"; image_url: { url: string } };

async function inlineImage(url: string): Promise<string | null> {
  if (url.startsWith("data:")) return url;
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    const header = res.headers.get("content-type")?.split(";")[0] ?? "";
    const ext = url.split("?")[0]!.split(".").pop()?.toLowerCase();
    const type = header.startsWith("image/")
      ? header
      : ext === "png" ? "image/png" : ext === "jpg" || ext === "jpeg" ? "image/jpeg" : "image/webp";
    const bytes = new Uint8Array(await res.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return `data:${type};base64,${btoa(bin)}`;
  } catch {
    return null;
  }
}

export async function gmiJson<T>(system: string, rawParts: Part[]): Promise<T> {
  // GMI's Gemini endpoint rejects remote image links, so embed them.
  const parts = (
    await Promise.all(
      rawParts.map(async (p) => {
        if (p.type !== "image_url") return p;
        const url = await inlineImage(p.image_url.url);
        return url ? { type: "image_url" as const, image_url: { url } } : null;
      }),
    )
  ).filter((p): p is Part => p !== null);
  const res = await fetch(GMI_CHAT, {
    method: "POST",
    headers: { Authorization: `Bearer ${key("GMI_API_KEY")}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: VISION_MODEL,
      response_format: { type: "json_object" },
      messages: [
        { role: "system", content: system + "\nRespond with JSON only." },
        { role: "user", content: parts },
      ],
    }),
  });
  const json = (await res.json().catch(() => ({}))) as { choices?: Array<{ message?: { content?: string } }> };
  if (!res.ok) {
    console.error("GMI chat error", res.status, JSON.stringify(json).slice(0, 500));
    if (res.status === 429) throw new Error("GMI is busy right now. Wait a few seconds and try again.");
    throw new Error(`The AI request failed (${res.status})`);
  }
  const text = json.choices?.[0]?.message?.content ?? "";
  const cleaned = text.replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
  try {
    return JSON.parse(cleaned) as T;
  } catch {
    console.error("GMI returned non-JSON", text.slice(0, 300));
    throw new Error("The AI gave an unreadable answer. Try again.");
  }
}

export async function startVideo(image: string, prompt: string) {
  const res = await fetch(GMI_QUEUE, {
    method: "POST",
    headers: { Authorization: `Bearer ${key("GMI_API_KEY")}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: FILLER_MODEL, payload: { first_frame: image, prompt, resolution: "720p", duration: "4" } }),
  });
  const json = (await res.json().catch(() => ({}))) as { request_id?: string };
  if (!res.ok || !json.request_id) {
    console.error("GMI video error", res.status, JSON.stringify(json).slice(0, 500));
    throw new Error(`Couldn't start the filler clip (${res.status})`);
  }
  return json.request_id;
}

export async function videoStatus(id: string) {
  const res = await fetch(`${GMI_QUEUE}/${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${key("GMI_API_KEY")}` },
  });
  const json = (await res.json().catch(() => ({}))) as {
    status?: string;
    outcome?: { video_url?: string; media_urls?: Array<string | { url?: string }> } | null;
  };
  if (!res.ok) throw new Error(`Couldn't check the filler clip (${res.status})`);
  const o = json.outcome;
  const first = o?.media_urls?.[0];
  const url = o?.video_url ?? (typeof first === "string" ? first : first?.url) ?? null;
  return { status: json.status ?? "unknown", url };
}

export function parseVideoUrl(raw: string): { platform: "instagram" | "youtube" | "tiktok"; platformId: string } | null {
  let u: URL;
  try { u = new URL(raw.trim()); } catch { return null; }
  const h = u.hostname.replace(/^www\.|^m\./, "");
  const seg = u.pathname.split("/").filter(Boolean);
  if (h.endsWith("instagram.com")) {
    const i = seg.findIndex((s) => ["reel", "reels", "p", "tv"].includes(s));
    return i >= 0 && seg[i + 1] ? { platform: "instagram", platformId: seg[i + 1]! } : null;
  }
  if (h === "youtu.be" && seg[0]) return { platform: "youtube", platformId: seg[0] };
  if (h.endsWith("youtube.com")) {
    const v = u.searchParams.get("v");
    if (v) return { platform: "youtube", platformId: v };
    const i = seg.findIndex((s) => ["shorts", "embed", "live"].includes(s));
    return i >= 0 && seg[i + 1] ? { platform: "youtube", platformId: seg[i + 1]! } : null;
  }
  if (h.endsWith("tiktok.com")) {
    const i = seg.indexOf("video");
    return i >= 0 && seg[i + 1] ? { platform: "tiktok", platformId: seg[i + 1]! } : null;
  }
  return null;
}

export async function lookupVideo(platform: string, platformId: string) {
  const data = await oriane<{ results: Array<OrianeContent & { commentsCount?: number | null; publishedAt?: string | null }> }>(
    `/contents/search?limit=1&projection=full`,
    { operator: "and", filters: { platformId: { includes: [{ platform, platformId }] } } },
  );
  return data.results[0] ?? null;
}

/** Fallback for YouTube videos Oriane hasn't indexed: title + thumbnail via public oEmbed. */
export async function youtubeOembed(id: string) {
  const r = await fetch(`https://www.youtube.com/oembed?format=json&url=${encodeURIComponent(`https://www.youtube.com/watch?v=${id}`)}`);
  if (!r.ok) return null;
  const j = (await r.json()) as { title?: string; author_name?: string };
  return { title: j.title ?? "", author: j.author_name ?? "", thumb: `https://i.ytimg.com/vi/${id}/hqdefault.jpg` };
}
