// A clip in the BuildRoll library. The camera roll is the rover camera: every item is a take it filmed.
export type RollItem = {
  id: string;
  name: string;
  img: string; // thumbnail (data URL)
  kind: "video" | "photo";
  duration: number | null;
  sizeMb: number;
  takenAt: string;
  lat: number; // 0,0 = no GPS (the rover has none)
  lng: number;
  place: string;
  source?: "rover"; // filmed by the rover camera (Rover Cam)
  video?: string; // playable take on the rover laptop (only while face_follow.py is running)
};

export const hasGps = (a: { lat: number; lng: number }) => a.lat !== 0 || a.lng !== 0;

/** Distance in metres between two GPS points. */
export function metres(a: { lat: number; lng: number }, b: { lat: number; lng: number }) {
  const R = 6371e3, r = Math.PI / 180;
  const dLat = (b.lat - a.lat) * r, dLng = (b.lng - a.lng) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
