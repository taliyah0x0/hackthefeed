// Browser-only helpers: turn images and video files into small JPEG data URLs.
const MAX = 512;

function drawToJpeg(src: CanvasImageSource, w: number, h: number) {
  const s = Math.min(1, MAX / Math.max(w, h));
  const c = document.createElement("canvas");
  c.width = Math.round(w * s);
  c.height = Math.round(h * s);
  c.getContext("2d")!.drawImage(src, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.8);
}

export function imageToDataUrl(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.crossOrigin = "anonymous";
    im.onload = () => resolve(drawToJpeg(im, im.naturalWidth, im.naturalHeight));
    im.onerror = () => reject(new Error("Couldn't read image"));
    im.src = url;
  });
}

export async function videoFrames(file: File, times: number[]): Promise<{ frames: { t: number; frame: string }[]; duration: number }> {
  const url = URL.createObjectURL(file);
  const v = document.createElement("video");
  v.muted = true;
  v.playsInline = true;
  v.preload = "auto";
  v.src = url;
  await new Promise<void>((res, rej) => {
    v.onloadeddata = () => res();
    v.onerror = () => rej(new Error("Couldn't read that video. Try an MP4 or MOV."));
  });
  const duration = v.duration || 0;
  const frames: { t: number; frame: string }[] = [];
  for (const t0 of times) {
    const t = Math.min(Math.max(0, t0 < 0 ? duration / 2 : t0), Math.max(0, duration - 0.05));
    await new Promise<void>((res) => {
      v.onseeked = () => res();
      v.currentTime = t;
    });
    frames.push({ t, frame: drawToJpeg(v, v.videoWidth, v.videoHeight) });
  }
  URL.revokeObjectURL(url);
  return { frames, duration };
}

export async function fileToFrame(file: File): Promise<string> {
  if (file.type.startsWith("image/")) {
    const url = URL.createObjectURL(file);
    try {
      return await imageToDataUrl(url);
    } finally {
      URL.revokeObjectURL(url);
    }
  }
  const { frames } = await videoFrames(file, [-1]);
  return frames[0]!.frame;
}

export function fmtViews(n: number) {
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "K";
  return String(n);
}
