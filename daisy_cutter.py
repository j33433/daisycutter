#!/usr/bin/env python3
"""
Daisy Cutter: use the selected object as a cutter and subtract its shape
from every object rendered below it, leaving transparent holes.
"""
import copy
import math
import os
import tempfile

import inkex
from inkex import bezier, command, load_svg
from inkex.elements import ShapeElement, Group, Image, Use
from lxml import etree

SKIP_CONTEXTS = {"defs", "clipPath", "mask", "symbol", "marker", "pattern", "metadata"}

# Experimental fast path: flatten tolerance and the safety gap (both in px,
# document units) used by the containment test. Anything closer than the gap
# is treated as "too close to be sure" and falls back to headless Inkscape.
FLATTEN_TOL = 0.05
TANGENCY_MARGIN = 0.5

MADE_WITH_NOTE = (
    "Made with Daisy Cutter (https://github.com/j33433/daisycutter)"
)

# Appearance attrs path-difference often drops or overwrites (→ default black).
STYLE_ATTRS = (
    "style", "class",
    "fill", "fill-opacity", "fill-rule",
    "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray",
    "stroke-dashoffset", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
    "opacity", "filter", "mask", "clip-path", "paint-order",
    "marker", "marker-start", "marker-mid", "marker-end",
)


def localname(el):
    try:
        return etree.QName(el).localname
    except ValueError:
        return ""


def in_skipped_context(el):
    """True if element lives inside defs/clipPath/mask/etc."""
    p = el.getparent()
    while p is not None:
        if localname(p) in SKIP_CONTEXTS:
            return True
        p = p.getparent()
    return False


def is_locked_or_hidden(el):
    """True if el or any ancestor is locked or display:none (Inkscape layer hide/lock)."""
    node = el
    while node is not None:
        if node.get("sodipodi:insensitive", None) == "true":
            return True
        try:
            if node.style.get("display", "") == "none":
                return True
        except (AttributeError, TypeError):
            pass
        node = node.getparent()
    return False


def bboxes_overlap(a, b):
    if a is None or b is None:
        return False
    return (a.left < b.right and b.left < a.right and
            a.top < b.bottom and b.top < a.bottom)


def doc_bbox(el):
    """Bounding box in document coordinates."""
    parent = el.getparent()
    tr = parent.composed_transform() if isinstance(parent, ShapeElement) else None
    return el.bounding_box(tr)


def fill_rule_of(el):
    """Return 'evenodd' or 'nonzero' for el's fill-rule."""
    try:
        rule = (el.style.get("fill-rule") or "").strip().lower()
    except (AttributeError, TypeError):
        rule = ""
    if not rule:
        rule = (el.get("fill-rule") or "").strip().lower()
    if rule == "evenodd":
        return "evenodd"
    return "nonzero"


def path_to_polylines(path, transform=None, flat=FLATTEN_TOL):
    """Flatten a Path into closed polylines of (x, y) knots in the given space."""
    if path is None:
        return []
    p = path.to_absolute()
    if transform is not None:
        p = p.transform(transform)
    csp = p.to_superpath()
    if not csp:
        return []
    bezier.cspsubdiv(csp, flat)
    out = []
    for sp in csp:
        if not sp:
            continue
        pts = [(float(pt[1][0]), float(pt[1][1])) for pt in sp]
        if len(pts) >= 2:
            out.append(pts)
    return out


def flatten_doc(el, flat=FLATTEN_TOL):
    """Flatten el's outline to polylines in document coordinates."""
    try:
        return path_to_polylines(el.path, el.composed_transform(), flat)
    except (AttributeError, TypeError, ValueError):
        return []


def _orient(ax, ay, bx, by, cx, cy):
    """Cross product (b-a) x (c-a). Positive = CCW."""
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _on_segment(ax, ay, bx, by, cx, cy, eps=1e-9):
    """True if c lies on segment ab (inclusive), assuming collinear."""
    return (min(ax, bx) - eps <= cx <= max(ax, bx) + eps and
            min(ay, by) - eps <= cy <= max(ay, by) + eps)


def segments_cross(a, b, c, d, eps=1e-9):
    """True if segments ab and cd properly intersect (shared endpoints ignored)."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and \
       ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
        return True
    # Collinear touch counts as a crossing for safety (forces fallback).
    if abs(o1) <= eps and _on_segment(ax, ay, bx, by, cx, cy):
        return True
    if abs(o2) <= eps and _on_segment(ax, ay, bx, by, dx, dy):
        return True
    if abs(o3) <= eps and _on_segment(cx, cy, dx, dy, ax, ay):
        return True
    if abs(o4) <= eps and _on_segment(cx, cy, dx, dy, bx, by):
        return True
    return False


def _poly_segments(poly):
    """Yield consecutive segment pairs; closes the loop."""
    n = len(poly)
    if n < 2:
        return
    for i in range(n - 1):
        yield poly[i], poly[i + 1]
    a, b = poly[-1], poly[0]
    if abs(a[0] - b[0]) > 1e-12 or abs(a[1] - b[1]) > 1e-12:
        yield a, b


def polylines_cross(a_polys, b_polys):
    """True if any segment of a crosses any segment of b."""
    for pa in a_polys:
        for pb in b_polys:
            for sa in _poly_segments(pa):
                for sb in _poly_segments(pb):
                    if segments_cross(sa[0], sa[1], sb[0], sb[1]):
                        return True
    return False


def _point_seg_dist2(px, py, ax, ay, bx, by):
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab2 = abx * abx + aby * aby
    if ab2 <= 1e-18:
        dx, dy = px - ax, py - ay
        return dx * dx + dy * dy
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    dx, dy = px - (ax + t * abx), py - (ay + t * aby)
    return dx * dx + dy * dy


def min_polyline_dist(a_polys, b_polys):
    """Minimum distance between any point of a and any segment of b (and vice versa)."""
    best = float("inf")
    for pa in a_polys:
        for pb in b_polys:
            for p in pa:
                for s0, s1 in _poly_segments(pb):
                    best = min(best, _point_seg_dist2(p[0], p[1], s0[0], s0[1], s1[0], s1[1]))
            for p in pb:
                for s0, s1 in _poly_segments(pa):
                    best = min(best, _point_seg_dist2(p[0], p[1], s0[0], s0[1], s1[0], s1[1]))
    if best == float("inf"):
        return 0.0
    return math.sqrt(best)


def point_in_fill(pt, polys, rule):
    """Point-in-fill for flattened closed polylines under evenodd or nonzero."""
    x, y = pt
    if rule == "evenodd":
        inside = False
        for poly in polys:
            n = len(poly)
            if n < 2:
                continue
            j = n - 1
            for i in range(n):
                xi, yi = poly[i]
                xj, yj = poly[j]
                if ((yi > y) != (yj > y)) and \
                        (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi):
                    inside = not inside
                j = i
        return inside
    # nonzero winding
    wn = 0
    for poly in polys:
        n = len(poly)
        if n < 2:
            continue
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if y1 <= y:
                if y2 > y and _orient(x1, y1, x2, y2, x, y) > 0:
                    wn += 1
            else:
                if y2 <= y and _orient(x1, y1, x2, y2, x, y) < 0:
                    wn -= 1
    return wn != 0


def is_simple_polyline(poly):
    """True if a single closed polyline has no non-adjacent self-intersections."""
    segs = list(_poly_segments(poly))
    n = len(segs)
    if n < 4:
        return True
    for i in range(n):
        for j in range(i + 1, n):
            # Skip adjacent segments (including first/last which share the close vertex).
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if segments_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                return False
    return True


def bbox_strictly_inside(inner, outer, margin=TANGENCY_MARGIN):
    if inner is None or outer is None:
        return False
    return (inner.left >= outer.left + margin and
            inner.right <= outer.right - margin and
            inner.top >= outer.top + margin and
            inner.bottom <= outer.bottom - margin)


def try_fast_cut(tgt, cutter, cutter_polys, cutter_bbox):
    """
    If the cutter is provably inside tgt, append it as a compound-path hole
    and return True. Otherwise leave tgt alone and return False (caller falls back).
    Conservative: only real <path> targets; evenodd any-subpath, or nonzero
    single simple subpath (then force evenodd).
    """
    if not isinstance(tgt, inkex.PathElement):
        return False
    if not cutter_polys:
        return False

    tgt_bbox = doc_bbox(tgt)
    if not bbox_strictly_inside(cutter_bbox, tgt_bbox):
        return False

    tgt_polys = flatten_doc(tgt)
    if not tgt_polys:
        return False

    if polylines_cross(cutter_polys, tgt_polys):
        return False

    rule = fill_rule_of(tgt)
    for cpoly in cutter_polys:
        if not cpoly:
            return False
        if not point_in_fill(cpoly[0], tgt_polys, rule):
            return False

    if min_polyline_dist(cutter_polys, tgt_polys) < TANGENCY_MARGIN:
        return False

    force_evenodd = False
    if rule != "evenodd":
        if len(tgt_polys) != 1 or not is_simple_polyline(tgt_polys[0]):
            return False
        force_evenodd = True

    try:
        tgt_ct = tgt.composed_transform()
        cutter_ct = cutter.composed_transform()
        local_tr = (-tgt_ct) @ cutter_ct
        hole = cutter.path.to_absolute().transform(local_tr)
        combined = tgt.path.to_absolute() + hole
    except (AttributeError, TypeError, ValueError):
        return False
    # Mutate only after geometry is fully computed so a failure never
    # leaves a half-cut target that the slow path would cut again.
    tgt.path = combined
    if force_evenodd:
        tgt.style["fill-rule"] = "evenodd"
    return True


def snapshot_style(el):
    """Capture paint/style so we can restore after boolean ops."""
    snap = {}
    for attr in STYLE_ATTRS:
        val = el.get(attr)
        if val is not None:
            snap[attr] = val
    # inkex Style object covers computed inline style even if split across attrs
    try:
        style_str = str(el.style)
        if style_str:
            snap["style"] = style_str
    except (AttributeError, TypeError):
        pass
    return snap


def restore_style(el, snap):
    """Re-apply snapshotted appearance onto the boolean result."""
    if el is None or not snap:
        return
    # Drop paint attrs that booleans may have injected (e.g. fill:#000000)
    for attr in STYLE_ATTRS:
        if attr in el.attrib:
            del el.attrib[attr]
    for attr, val in snap.items():
        el.set(attr, val)


def neutralize_cutter_paint(el):
    """Cutter dups only need geometry; strip paint so it can't leak into results."""
    for attr in STYLE_ATTRS:
        if attr in el.attrib:
            del el.attrib[attr]
    el.set("style", "fill:#000000;stroke:none;opacity:1")


def _ensure_child(parent, tag):
    """Return existing child with clark-notation tag, or create one."""
    child = parent.find(tag)
    if child is None:
        child = etree.SubElement(parent, tag)
    return child


def add_made_with_metadata(svg):
    """Leave a Dublin Core description credit in svg metadata (idempotent)."""
    meta = svg.metadata
    rdf = _ensure_child(meta, inkex.addNS("RDF", "rdf"))
    work = _ensure_child(rdf, inkex.addNS("Work", "cc"))
    if work.get(inkex.addNS("about", "rdf")) is None:
        work.set(inkex.addNS("about", "rdf"), "")

    fmt = _ensure_child(work, inkex.addNS("format", "dc"))
    if not (fmt.text and fmt.text.strip()):
        fmt.text = "image/svg+xml"

    dtype = _ensure_child(work, inkex.addNS("type", "dc"))
    if dtype.get(inkex.addNS("resource", "rdf")) is None:
        dtype.set(
            inkex.addNS("resource", "rdf"),
            "http://purl.org/dc/dcmitype/StillImage",
        )

    desc = _ensure_child(work, inkex.addNS("description", "dc"))
    existing = (desc.text or "").strip()
    if MADE_WITH_NOTE in existing:
        return
    if existing:
        desc.text = existing + "\n" + MADE_WITH_NOTE
    else:
        desc.text = MADE_WITH_NOTE


class DaisyCutter(inkex.EffectExtension):

    def add_arguments(self, pars):
        pars.add_argument("--keep_cutter", type=inkex.Boolean, default=False)
        pars.add_argument("--only_overlapping", type=inkex.Boolean, default=True)
        pars.add_argument("--experimental_fast", type=inkex.Boolean, default=True)

    def effect(self):
        selection = [e for e in self.svg.selection]
        if len(selection) != 1:
            raise inkex.AbortExtension(
                "Select exactly one object to use as the cutter.")
        cutter = selection[0]
        if isinstance(cutter, Group):
            raise inkex.AbortExtension(
                "The cutter must be a single shape/path, not a group. "
                "(Combine it into one path first: Path > Union / Combine)")

        cutter_id = cutter.get_id()
        cutter_bbox = doc_bbox(cutter)
        cutter_ct = cutter.composed_transform()

        # --- 1. Find all shapes rendered below the cutter -----------------
        # Document order == paint order, so anything iterated before the
        # cutter is below it.
        targets = []
        for el in self.svg.iter():
            if el is cutter:
                break
            if not isinstance(el, ShapeElement):
                continue
            if isinstance(el, (Group, Image, Use)):
                continue  # groups paint via children; images/clones can't be cut
            if in_skipped_context(el):
                continue
            if is_locked_or_hidden(el):
                continue
            if self.options.only_overlapping and \
                    not bboxes_overlap(doc_bbox(el), cutter_bbox):
                continue
            targets.append(el)

        if not targets:
            raise inkex.AbortExtension("No objects found below the cutter.")

        # --- 2. Optional experimental fast path (in-process compound path) -
        slow_targets = list(targets)
        if self.options.experimental_fast:
            cutter_polys = flatten_doc(cutter)
            remaining = []
            for tgt in targets:
                if try_fast_cut(tgt, cutter, cutter_polys, cutter_bbox):
                    continue
                remaining.append(tgt)
            slow_targets = remaining

        # --- 3. Headless path-difference for anything the fast path skipped
        if slow_targets:
            pairs = []
            saved_styles = {}
            for i, tgt in enumerate(slow_targets):
                tgt_id = tgt.get_id()  # ensures an id exists
                saved_styles[tgt_id] = snapshot_style(tgt)
                parent = tgt.getparent()

                dup = copy.deepcopy(cutter)
                dup_id = "{}_punch_{}".format(cutter_id, i)
                dup.set("id", dup_id)
                neutralize_cutter_paint(dup)

                # Compensate transforms so the duplicate lands at the exact
                # same visual spot even inside a different (transformed) group.
                parent_tr = parent.composed_transform() \
                    if isinstance(parent, ShapeElement) else inkex.Transform()
                dup.transform = (-parent_tr) @ cutter_ct

                parent.insert(parent.index(tgt) + 1, dup)
                pairs.append((tgt_id, dup_id))

            if not self.options.keep_cutter:
                cutter.getparent().remove(cutter)

            tmp_in = tempfile.NamedTemporaryFile(suffix=".svg", delete=False).name
            tmp_out = tempfile.NamedTemporaryFile(suffix=".svg", delete=False).name
            try:
                self.document.write(tmp_in)

                # Convert all targets + cutter dups to paths once, then difference.
                all_ids = []
                for tgt_id, dup_id in pairs:
                    all_ids.append(tgt_id)
                    all_ids.append(dup_id)
                actions = [
                    "select-clear",
                    "select-by-id:{}".format(",".join(all_ids)),
                    "object-to-path",
                ]
                for tgt_id, dup_id in pairs:
                    actions.append("select-clear")
                    actions.append("select-by-id:{},{}".format(tgt_id, dup_id))
                    actions.append("path-difference")  # bottom minus top
                actions.append("export-filename:{}".format(tmp_out))
                actions.append("export-do")

                command.inkscape(tmp_in, actions=";".join(actions),
                                 batch_process=True)

                self.document = load_svg(tmp_out)
                self.svg = self.document.getroot()
                for tgt_id, snap in saved_styles.items():
                    result = self.svg.getElementById(tgt_id)
                    restore_style(result, snap)
                add_made_with_metadata(self.svg)
            finally:
                for f in (tmp_in, tmp_out):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
        else:
            # All targets handled in-process; no Inkscape launch.
            if not self.options.keep_cutter:
                cutter.getparent().remove(cutter)
            add_made_with_metadata(self.svg)


if __name__ == "__main__":
    DaisyCutter().run()
