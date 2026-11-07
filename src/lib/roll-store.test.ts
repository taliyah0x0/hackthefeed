import { describe, expect, it } from "vitest";
import { groupProjects, type Indexed } from "./roll-store";

const base = { name: "x", img: "", kind: "video" as const, duration: 5, sizeMb: 1, place: "", description: "", trend: 0, lookalike: null, project: null, groupedBy: [] };
const HOME = { lat: 37.7612, lng: -122.4352 };
const SHOP = { lat: 37.8044, lng: -122.2712 };
const item = (id: string, aiProject: string, tags: string[], at: string, gps = HOME): Indexed => ({ ...base, id, aiProject, tags, takenAt: at, ...gps });

describe("BuildRoll project grouping", () => {
  const out = groupProjects([
    item("sketch", "LED Desk Lamp", ["sketch"], "2026-10-28T19:00:00"),
    item("print", "3D Printed Funnel", ["3d printer"], "2026-10-29T18:00:00"),
    item("dog", "Everyday", ["dog"], "2026-10-28T20:00:00"),
    item("saw", "Woodworking Project", ["walnut", "table saw"], "2026-11-03T14:00:00", SHOP),
    item("shelf", "Walnut Shelf", ["shelf", "plants"], "2026-11-05T19:00:00"),
  ]);
  const p = (id: string) => out.find((i) => i.id === id)!.project;
  it("joins clips filmed at the same place within 2 days, even if named differently", () => expect(p("print")).toBe(p("sketch")));
  it("joins clips at different places when the same build is in frame", () => expect(p("shelf")).toBe(p("saw")));
  it("keeps separate builds apart", () => expect(p("saw")).not.toBe(p("sketch")));
  it("hides everyday photos", () => expect(p("dog")).toBeNull());

  it("doesn't group rover takes (no GPS) just because they were filmed close together", () => {
    const rover = groupProjects([
      item("r1", "Walnut Shelf", ["shelf"], "2026-11-06T10:00:00", { lat: 0, lng: 0 }),
      item("r2", "LED Desk Lamp", ["lamp"], "2026-11-06T10:05:00", { lat: 0, lng: 0 }),
    ]);
    expect(rover[0]!.project).not.toBe(rover[1]!.project);
  });
});
