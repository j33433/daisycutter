#!/usr/bin/env python3
"""
Daisy Cutter: use the selected object as a cutter and subtract its shape
from every object rendered below it, leaving transparent holes.
"""
import copy
import os
import subprocess
import sys
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


def _popen_kwargs():
    """Platform flags for headless child processes (no console flash on Windows)."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kwargs


def action_path(path):
    """
    Absolute path safe to embed in Inkscape action args (file-open, export-filename).
    Actions are parsed as name:arg, so Windows backslashes and bare drive colons
    are hazardous; forward slashes are accepted by Inkscape on all platforms.
    """
    return os.path.abspath(path).replace("\\", "/")


def inkscape_executable():
    """Same binary inkex would call (respects INKSCAPE_COMMAND / Windows .exe)."""
    try:
        return command.which(command.INKSCAPE_EXECUTABLE_NAME)
    except Exception:
        return command.INKSCAPE_EXECUTABLE_NAME


def start_inkscape_shell():
    """
    Launch `inkscape --shell` early so startup overlaps with Python prep.
    Returns a Popen, or None if the binary can't be started.
    """
    try:
        return subprocess.Popen(
            [inkscape_executable(), "--shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_kwargs()
        )
    except OSError:
        return None


def run_actions_in_shell(proc, svg_in, actions, svg_out):
    """
    Send one action line to a warm --shell process and wait for it to finish.
    actions: list of action strings (no file-open/quit); paths already action-safe.
    Raises OSError/subprocess.SubprocessError/RuntimeError on failure.
    """
    if proc is None or proc.stdin is None:
        raise RuntimeError("no inkscape shell")
    # Shell mode: open the prepared SVG, run booleans, export, exit.
    line = "file-open:{}; {}; quit-immediate\n".format(
        action_path(svg_in), ";".join(actions))
    try:
        stdout, stderr = proc.communicate(line.encode("utf-8"), timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError("inkscape shell timed out")
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            "inkscape shell failed (rc={}): {}".format(proc.returncode, detail))
    if not os.path.isfile(svg_out) or os.path.getsize(svg_out) == 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise RuntimeError("inkscape shell produced no output: {}".format(detail))


def stop_inkscape_shell(proc):
    """Best-effort cleanup if we abandon a shell process."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    except OSError:
        pass


def run_path_differences(svg_in, actions, svg_out, shell_proc=None):
    """
    Run boolean actions on svg_in → svg_out.
    Prefer a pre-started --shell (startup overlapped with prep); fall back to
    a classic one-shot inkscape invocation (no new deps either way).
    """
    if shell_proc is not None:
        try:
            run_actions_in_shell(shell_proc, svg_in, actions, svg_out)
            return
        except (OSError, RuntimeError, subprocess.SubprocessError):
            stop_inkscape_shell(shell_proc)
            # fall through to classic path
    command.inkscape(svg_in, actions=";".join(actions), batch_process=True)


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
            if is_locked_or_hidden(el):
                continue
            if self.options.only_overlapping and \
                    not bboxes_overlap(doc_bbox(el), cutter_bbox):
                continue
            targets.append(el)

        if not targets:
            raise inkex.AbortExtension("No objects found below the cutter.")

        # Start headless Inkscape now so its ~0.3s startup overlaps prep work
        # (dups, style snapshots, temp SVG write). No extra deps: stdlib only.
        shell_proc = start_inkscape_shell()

        # --- 2. Place one cutter duplicate directly above each target -----
        pairs = []
        saved_styles = {}
        try:
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

            # --- 3. Remove the original cutter (optional) ------------------
            if not self.options.keep_cutter:
                cutter.getparent().remove(cutter)

            # --- 4. Boolean differences via warm shell (or classic fallback)
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
                actions.append("export-filename:{}".format(action_path(tmp_out)))
                actions.append("export-do")

                run_path_differences(tmp_in, actions, tmp_out, shell_proc)
                shell_proc = None  # consumed (exited) or abandoned in fallback

                # --- 5. Replace document and restore each target's style --
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
        finally:
            stop_inkscape_shell(shell_proc)


if __name__ == "__main__":
    DaisyCutter().run()
