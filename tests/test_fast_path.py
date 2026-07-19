#!/usr/bin/env python3
"""Unit tests for Daisy Cutter experimental fast-path geometry helpers."""
import os
import sys
import unittest

# inkex ships with Inkscape extensions
sys.path.insert(0, "/usr/share/inkscape/extensions")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inkex
from inkex import Path

import daisy_cutter as dc


class SegIntersectTests(unittest.TestCase):
    def test_proper_cross(self):
        self.assertTrue(dc.segments_cross((0, 0), (2, 2), (0, 2), (2, 0)))

    def test_no_cross_parallel(self):
        self.assertFalse(dc.segments_cross((0, 0), (2, 0), (0, 1), (2, 1)))

    def test_shared_endpoint_counts_as_cross(self):
        # Conservative: collinear/endpoint touch forces fallback.
        self.assertTrue(dc.segments_cross((0, 0), (1, 0), (1, 0), (2, 0)))


class PointInFillTests(unittest.TestCase):
    square = [[(0, 0), (10, 0), (10, 10), (0, 10)]]

    def test_inside_evenodd(self):
        self.assertTrue(dc.point_in_fill((5, 5), self.square, "evenodd"))

    def test_outside_evenodd(self):
        self.assertFalse(dc.point_in_fill((15, 5), self.square, "evenodd"))

    def test_inside_nonzero(self):
        self.assertTrue(dc.point_in_fill((5, 5), self.square, "nonzero"))

    def test_hole_evenodd(self):
        # Outer square + inner square → evenodd hole in the middle.
        polys = [
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            [(3, 3), (7, 3), (7, 7), (3, 7)],
        ]
        self.assertFalse(dc.point_in_fill((5, 5), polys, "evenodd"))
        self.assertTrue(dc.point_in_fill((1, 1), polys, "evenodd"))


class SimplePolylineTests(unittest.TestCase):
    def test_simple_square(self):
        self.assertTrue(dc.is_simple_polyline([(0, 0), (1, 0), (1, 1), (0, 1)]))

    def test_bowtie(self):
        # Self-crossing quad (hourglass).
        self.assertFalse(dc.is_simple_polyline([(0, 0), (2, 2), (2, 0), (0, 2)]))


class PolylineCrossTests(unittest.TestCase):
    def test_nested_no_cross(self):
        outer = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        inner = [[(2, 2), (4, 2), (4, 4), (2, 4)]]
        self.assertFalse(dc.polylines_cross(inner, outer))

    def test_overlapping_cross(self):
        a = [[(0, 0), (10, 0), (10, 10), (0, 10)]]
        b = [[(5, 5), (15, 5), (15, 15), (5, 15)]]
        self.assertTrue(dc.polylines_cross(a, b))


class FastCutIntegrationTests(unittest.TestCase):
    """Build tiny SVGs and exercise try_fast_cut without headless Inkscape."""

    def _svg_with(self, target_d, cutter_d, fill_rule=None):
        svg = inkex.SvgDocumentElement()
        tgt = inkex.PathElement()
        tgt.set("id", "tgt")
        tgt.path = Path(target_d)
        if fill_rule:
            tgt.style["fill-rule"] = fill_rule
        cutter = inkex.PathElement()
        cutter.set("id", "cutter")
        cutter.path = Path(cutter_d)
        svg.append(tgt)
        svg.append(cutter)
        return svg, tgt, cutter

    def test_contained_square_is_fast(self):
        svg, tgt, cutter = self._svg_with(
            "M 0 0 L 100 0 L 100 100 L 0 100 Z",
            "M 30 30 L 70 30 L 70 70 L 30 70 Z",
        )
        polys = dc.flatten_doc(cutter)
        ok = dc.try_fast_cut(tgt, cutter, polys, dc.doc_bbox(cutter))
        self.assertTrue(ok)
        d = str(tgt.path)
        # Combined path should contain both outer and hole geometry.
        self.assertIn("M 0 0", d)
        self.assertIn("M 30 30", d)
        self.assertEqual(tgt.style.get("fill-rule"), "evenodd")

    def test_overhang_is_slow(self):
        svg, tgt, cutter = self._svg_with(
            "M 0 0 L 100 0 L 100 100 L 0 100 Z",
            "M 80 80 L 140 80 L 140 140 L 80 140 Z",
        )
        original = str(tgt.path)
        polys = dc.flatten_doc(cutter)
        ok = dc.try_fast_cut(tgt, cutter, polys, dc.doc_bbox(cutter))
        self.assertFalse(ok)
        self.assertEqual(str(tgt.path), original)

    def test_evenodd_target_keeps_rule(self):
        svg, tgt, cutter = self._svg_with(
            "M 0 0 L 100 0 L 100 100 L 0 100 Z",
            "M 30 30 L 70 30 L 70 70 L 30 70 Z",
            fill_rule="evenodd",
        )
        polys = dc.flatten_doc(cutter)
        ok = dc.try_fast_cut(tgt, cutter, polys, dc.doc_bbox(cutter))
        self.assertTrue(ok)
        self.assertEqual(tgt.style.get("fill-rule"), "evenodd")

    def test_rect_target_is_slow(self):
        svg = inkex.SvgDocumentElement()
        tgt = inkex.Rectangle()
        tgt.set("id", "tgt")
        tgt.set("x", "0")
        tgt.set("y", "0")
        tgt.set("width", "100")
        tgt.set("height", "100")
        cutter = inkex.PathElement()
        cutter.set("id", "cutter")
        cutter.path = Path("M 30 30 L 70 30 L 70 70 L 30 70 Z")
        svg.append(tgt)
        svg.append(cutter)
        polys = dc.flatten_doc(cutter)
        self.assertFalse(dc.try_fast_cut(tgt, cutter, polys, dc.doc_bbox(cutter)))


if __name__ == "__main__":
    unittest.main()
