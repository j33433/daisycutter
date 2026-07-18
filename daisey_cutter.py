#!/usr/bin/env python3
"""
Punch Holes: use the selected object as a cutter and subtract its shape
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


class PunchHoles(inkex.EffectExtension):

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
        for i, tgt in enumerate(targets):
            tgt_id = tgt.get_id()  # ensures an id exists
            parent = tgt.getparent()

            dup = copy.deepcopy(cutter)
            dup_id = "{}_punch_{}".format(cutter_id, i)
            dup.set("id", dup_id)

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

            # --- 5. Replace our document with the processed result --------
            self.document = load_svg(tmp_out)
            self.svg = self.document.getroot()
        finally:
            for f in (tmp_in, tmp_out):
                try:
                    os.remove(f)
                except OSError:
                    pass


if __name__ == "__main__":
    PunchHoles().run()
