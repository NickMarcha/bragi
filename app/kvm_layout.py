"""The KVM layout drawn as monitors (step 2 of docs/kvm-layout.md).

Each computer is a group: its monitors, arranged as its own display settings
have them, at a position on a shared canvas and at a scale (canvas units per
desktop unit, the same for the whole group). The dashboard moves and sizes
the groups; this module finds where monitors of different computers touch
and turns each shared stretch into Deskflow partial edges:

    sage:
        down(0,64.18) = sagedeck(0,100)

Deskflow counts a range in percent of the computer's whole bounding box, the
same space the group is drawn in, so with one scale per group the mapping is
linear and a split lands exactly on a seam between monitors.

Plain Deskflow only crosses at a computer's outer edge, so an edge of an
inner monitor (an L-shape, offset monitors) is still written but gets a
warning. Bragi's Deskflow fork knows each computer's real monitors
(monitor_shapes(), written as the config's monitors section): it keeps the
cursor on them and crosses at the edge of the monitor it's on, so those
links work as drawn and there's no warning (step 3).
"""

from __future__ import annotations

import math

SIZES = ("os", "physical", "manual")
EPS = 0.5          # canvas units: edges closer than this touch
# A shared stretch shorter than this share of the shorter monitor edge is a
# corner, not an edge (sage's BenQ ends 4 px above its Samsung).
MIN_SHARED = 0.02
# A monitor edge this close to the outline (share of the outline's size)
# counts as the outline: Deskflow's cursor crosses the dead strip unnoticed.
OUTER_SLACK = 0.01
MAX_MONITORS = 16
SIDES_ORDER = ("left", "right", "up", "down")


class LayoutError(ValueError):
    """Something the dashboard can show as it is."""


def _num(value, low: float, high: float, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not low <= value <= high:
        raise LayoutError(f"{what} is out of range")
    return float(value)


def check_groups(groups, names: set[str]) -> dict[str, dict]:
    """The groups as the dashboard sent them, checked."""
    if not isinstance(groups, dict):
        raise LayoutError("groups must be an object")
    out = {}
    for name, g in groups.items():
        if name not in names:
            raise LayoutError(f"Unknown computer: {name}")
        if not isinstance(g, dict):
            raise LayoutError(f"Bad layout for {name}")
        monitors = g.get("monitors")
        if not isinstance(monitors, list) or not 1 <= len(monitors) <= MAX_MONITORS:
            raise LayoutError(f"{name} needs its monitors")
        clean = []
        for m in monitors:
            if not isinstance(m, dict):
                raise LayoutError(f"Bad monitor for {name}")
            clean.append({
                "id": str(m.get("id", ""))[:64],
                "name": str(m.get("name", ""))[:64],
                "x": _num(m.get("x"), -100_000, 100_000, "A monitor position"),
                "y": _num(m.get("y"), -100_000, 100_000, "A monitor position"),
                "width": _num(m.get("width"), 1, 100_000, "A monitor size"),
                "height": _num(m.get("height"), 1, 100_000, "A monitor size"),
            })
        size = g.get("size", "os")
        if size not in SIZES:
            raise LayoutError(f"Unknown size setting for {name}")
        out[name] = {
            "x": _num(g.get("x"), -1_000_000, 1_000_000, f"{name}'s position"),
            "y": _num(g.get("y"), -1_000_000, 1_000_000, f"{name}'s position"),
            "scale": _num(g.get("scale"), 0.01, 100, f"{name}'s size"),
            "size": size,
            "monitors": clean,
        }
    return out


MM_UNITS = 96 / 25.4  # canvas units per millimetre at "real size", as the editor draws it


def physical_scale(monitors: list[dict]) -> float | None:
    """Real size: the monitors' millimetres per desktop unit, averaged."""
    per = [m["width_mm"] / m["width"] for m in monitors if m.get("width_mm") and m.get("width")]
    return sum(per) / len(per) * MM_UNITS if per else None


def with_monitors(groups: dict[str, dict], current: dict[str, list[dict]]) -> dict[str, dict]:
    """The groups with each computer's monitors as reported now, kept where
    and as large as the layout has them. Computers without a report keep
    the monitors the layout was saved with."""
    out = {}
    for name, g in groups.items():
        g = dict(g)
        now = current.get(name)
        if now:
            g["monitors"] = [{"id": m["id"], "name": m["name"], "x": float(m["x"]), "y": float(m["y"]),
                              "width": float(m["width"]), "height": float(m["height"])} for m in now]
            if g["size"] == "os":
                g["scale"] = 1.0
            elif g["size"] == "physical":
                g["scale"] = physical_scale(now) or g["scale"]
        out[name] = g
    return out


def bounds(group: dict) -> tuple[float, float, float, float]:
    """The group's bounding box in its computer's desktop coordinates:
    (x, y, width, height). This is the box Deskflow's percentages count in."""
    ms = group["monitors"]
    x0 = min(m["x"] for m in ms)
    y0 = min(m["y"] for m in ms)
    x1 = max(m["x"] + m["width"] for m in ms)
    y1 = max(m["y"] + m["height"] for m in ms)
    return x0, y0, x1 - x0, y1 - y0


def canvas_rects(group: dict) -> list[tuple[float, float, float, float]]:
    """Each monitor on the canvas: (left, top, right, bottom)."""
    x0, y0, _, _ = bounds(group)
    s = group["scale"]
    out = []
    for m in group["monitors"]:
        left = group["x"] + (m["x"] - x0) * s
        top = group["y"] + (m["y"] - y0) * s
        out.append((left, top, left + m["width"] * s, top + m["height"] * s))
    return out


def _box(group: dict) -> tuple[float, float, float, float]:
    _, _, w, h = bounds(group)
    s = group["scale"]
    return group["x"], group["y"], group["x"] + w * s, group["y"] + h * s


def _slack(box: tuple[float, float, float, float], edge: int) -> float:
    """How far inside the outline (left, top, right, bottom index `edge`) a
    monitor edge may be and still count as the outline."""
    extent = box[2] - box[0] if edge in (0, 2) else box[3] - box[1]
    return max(EPS, OUTER_SLACK * extent)


def _percent(c: float, start: float, length: float) -> float:
    return min(100.0, max(0.0, round((c - start) / length * 100, 2)))


def _range(start: float, end: float) -> str:
    return "" if (start, end) == (0, 100) else f"({start:g},{end:g})"


def monitor_shapes(groups: dict[str, dict]) -> dict[str, str]:
    """The config's monitors section: {computer: "x,y,w,h ..."}, each
    monitor in percent of the computer's outline, for computers whose
    monitors don't fill it. An edge within OUTER_SLACK of the outline is
    put on it, as analyse() counts it, so there's no thin dead strip."""
    out = {}
    for name, g in sorted(groups.items()):
        x0, y0, w, h = bounds(g)
        if w <= 0 or h <= 0:
            continue
        rects = []
        for m in g["monitors"]:
            left, top = (m["x"] - x0) / w, (m["y"] - y0) / h
            right, bottom = left + m["width"] / w, top + m["height"] / h
            left, top = (0.0 if left <= OUTER_SLACK else left), (0.0 if top <= OUTER_SLACK else top)
            right, bottom = (1.0 if right >= 1 - OUTER_SLACK else right), (1.0 if bottom >= 1 - OUTER_SLACK else bottom)
            rects.append((left, top, right - left, bottom - top))
        if len(rects) == 1 and rects[0] == (0.0, 0.0, 1.0, 1.0):
            continue
        out[name] = " ".join(",".join(f"{v * 100:.4f}".rstrip("0").rstrip(".") for v in r) for r in rects)
    return out


def analyse(groups: dict[str, dict], shapes: bool = False) -> dict:
    """Where the groups touch, as Deskflow links, plus what's wrong with
    the layout. errors stop a save; warnings don't.

    edges: one per shared stretch and direction, with its canvas segment
    (for the editor to draw) and both computers' ranges.
    links: {computer: ["right(0,50) = other(25,75)", ...]}.
    shapes: the server knows the computers' real monitors, so a link on an
    inner monitor edge works as drawn."""
    errors: list[str] = []
    warnings: list[str] = []
    rects = {n: canvas_rects(g) for n, g in groups.items()}
    boxes = {n: _box(g) for n, g in groups.items()}
    names = list(groups)

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if any(min(ra[2], rb[2]) - max(ra[0], rb[0]) > EPS and min(ra[3], rb[3]) - max(ra[1], rb[1]) > EPS
                   for ra in rects[a] for rb in rects[b]):
                errors.append(f"{a} and {b} overlap. Move one so they only touch.")

    # Shared stretches: (a, side, b) -> [[start, end, a_outer, b_outer, position]];
    # start and end run along the edge, position is where the edge is.
    raw: dict[tuple[str, str, str], list[list]] = {}
    for a in names:
        for b in names:
            if a == b:
                continue
            ba, bb = boxes[a], boxes[b]
            for ra in rects[a]:
                for rb in rects[b]:
                    for side, (i0, i1), ea, eb in (("right", (1, 3), 2, 0), ("left", (1, 3), 0, 2),
                                                   ("down", (0, 2), 3, 1), ("up", (0, 2), 1, 3)):
                        # A monitor edge just short of the outline counts as the outline.
                        a_outer = abs(ra[ea] - ba[ea]) <= _slack(ba, ea)
                        b_outer = abs(rb[eb] - bb[eb]) <= _slack(bb, eb)
                        pa = ba[ea] if a_outer else ra[ea]
                        pb = bb[eb] if b_outer else rb[eb]
                        if abs(pa - pb) > EPS:
                            continue
                        lo, hi = max(ra[i0], rb[i0]), min(ra[i1], rb[i1])
                        if hi - lo < max(2 * EPS, MIN_SHARED * min(ra[i1] - ra[i0], rb[i1] - rb[i0])):
                            continue
                        raw.setdefault((a, side, b), []).append([lo, hi, a_outer, b_outer, pa])

    edges: list[dict] = []
    taken: dict[tuple[str, str], list[tuple[float, float]]] = {}
    links: dict[str, list[tuple]] = {n: [] for n in names}
    for (a, side, b), parts in sorted(raw.items()):
        parts.sort()
        merged: list[list] = []
        for part in parts:
            last = merged[-1] if merged else None
            # Two monitors side by side along the same line make one stretch.
            if last and part[0] <= last[1] + EPS and abs(part[4] - last[4]) <= EPS:
                last[1] = max(last[1], part[1])
                last[2] = last[2] and part[2]
                last[3] = last[3] and part[3]
            else:
                merged.append(list(part))
        ba, bb = boxes[a], boxes[b]
        vertical = side in ("left", "right")
        a0, a_len = (ba[1], ba[3] - ba[1]) if vertical else (ba[0], ba[2] - ba[0])
        b0, b_len = (bb[1], bb[3] - bb[1]) if vertical else (bb[0], bb[2] - bb[0])
        for lo, hi, a_outer, b_outer, pos in merged:
            ar = (_percent(lo, a0, a_len), _percent(hi, a0, a_len))
            br = (_percent(lo, b0, b_len), _percent(hi, b0, b_len))
            if ar[0] >= ar[1] or br[0] >= br[1]:
                continue
            # Deskflow refuses two links over the same part of one side.
            if any(ar[0] < t1 and t0 < ar[1] for t0, t1 in taken.get((a, side), [])):
                warnings.append(f"{a}'s {side} side reaches two computers at the same height; "
                                f"the mouse goes to the first one only, not {b}.")
                continue
            taken.setdefault((a, side), []).append(ar)
            links[a].append((SIDES_ORDER.index(side), ar[0], f"{side}{_range(*ar)} = {b}{_range(*br)}"))
            seg = [[pos, lo], [pos, hi]] if vertical else [[lo, pos], [hi, pos]]
            edges.append({"from": a, "side": side, "to": b, "segment": seg,
                          "from_range": list(ar), "to_range": list(br),
                          "outer": a_outer and b_outer})
            if not a_outer and not shapes:
                warnings.append(f"{a} meets {b} on a monitor edge inside {a}'s outline (an L-shape or offset "
                                f"monitor). Deskflow only crosses at the outline, so the mouse goes to {b} "
                                f"from {a}'s outer {side} edge there.")

    if len(names) > 1:
        reached = {e["from"] for e in edges} | {e["to"] for e in edges}
        for n in names:
            if n not in reached:
                warnings.append(f"Nothing touches {n}: the mouse can't reach it, only hotkeys can.")

    return {"edges": edges, "links": {n: [line for *_, line in sorted(v)] for n, v in links.items() if v},
            "errors": errors, "warnings": _unique(warnings)}


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    return [i for i in items if not (i in seen or seen.add(i))]
