#!/usr/bin/env python3
"""
Daisy Cutter: use the selected object as a cutter and subtract its shape
from every object rendered below it, leaving transparent holes.
"""
import copy
import os
import tempfile

import inkex
from inkex import command, load_svg
from inkex.elements import ShapeElement, Group, Image, Use
from lxml import etree

SKIP_CONTEXTS = {"defs", "clipPath", "mask", "symbol", "marker", "pattern", "metadata"}

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
            if self.options.only_overlapping and \
                    not bboxes_overlap(doc_bbox(el), cutter_bbox):
                continue
            targets.append(el)

        if not targets:
            raise inkex.AbortExtension("No objects found below the cutter.")

        # --- 2. Place one cutter duplicate directly above each target -----
        pairs = []
        saved_styles = {}
        for i, tgt in enumerate(targets):
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

        # --- 3. Remove the original cutter (optional) ----------------------
        if not self.options.keep_cutter:
            cutter.getparent().remove(cutter)

        # --- 4. Run the boolean differences via headless Inkscape ---------
        tmp_in = tempfile.NamedTemporaryFile(suffix=".svg", delete=False).name
        tmp_out = tempfile.NamedTemporaryFile(suffix=".svg", delete=False).name
        try:
            self.document.write(tmp_in)

            actions = []
            for tgt_id, dup_id in pairs:
                actions.append("select-clear")
                actions.append("select-by-id:{},{}".format(tgt_id, dup_id))
                actions.append("object-to-path")   # rects/ellipses/text -> path
                actions.append("path-difference")  # bottom minus top
            actions.append("export-filename:{}".format(tmp_out))
            actions.append("export-do")

            command.inkscape(tmp_in, actions=";".join(actions),
                             batch_process=True)

            # --- 5. Replace document and restore each target's style ------
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


if __name__ == "__main__":
    DaisyCutter().run()
