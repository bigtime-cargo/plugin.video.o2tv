# -*- coding: utf-8 -*-
"""Offline testy resources/lib/ads.py (SCTE-35 znacky reklam v MPD).

    cd ~/plugin.video.o2tv && python3 -m unittest tools.test_ads -v

Binarne udalosti su skutocne, z archivneho manifestu Markizy (30. 10. 2026).
"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "resources", "lib"))
import ads   # noqa: E402

# cue-out, break_duration 25,4 s (event id 86573)
OUT_A = "/DAlAAAAAAAAAP/wFAUAAVItf+/+yjAJtf4AIuGwBNMAAAAALyncWA=="
# opakovany cue-out toho isteho bloku, zvysok 15,4 s
OUT_B = "/DAlAAAAAAAAAP/wFAUAAVItf+/+yj23Rf4AFSYQBNMAAAAAz8PtfA=="
# cue-in (event id 86574, immediate)
IN_A = "/DAbAAAAAAAAAP/wCgUAAVIuf18E1wAAAADBaTql"
# cue-out s dlzkou 542,6 s (event id 86575)
OUT_C = "/DAlAAAAAAAAAP/wFAUAAVIvf+/+zgJCtf4C6SXQBNcAAAAAJkF72A=="

T = 10000000    # timescale udalosti


def mpd(events, typ="static", pto=0):
    ev = "".join(
        '<Event presentationTime="%d"%s><Signal xmlns="http://www.scte.org/schemas/35/2016">'
        '<Binary>%s</Binary></Signal></Event>'
        % (pto + int(sec * T), ' duration="%d"' % int(d * T) if d else "", b64)
        for sec, d, b64 in events)
    return ('<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="%s">'
            '<Period id="1"><EventStream schemeIdUri="urn:scte:scte35:2014:xml+bin" '
            'value="2500" timescale="%d" presentationTimeOffset="%d">%s</EventStream>'
            '</Period></MPD>' % (typ, T, pto, ev))


class ParseSplice(unittest.TestCase):
    def test_cue_out(self):
        import base64
        evid, oon, dur = ads.parse_splice_insert(base64.b64decode(OUT_A))
        self.assertEqual((evid, oon), (86573, True))
        self.assertAlmostEqual(dur, 25.4, 1)

    def test_cue_in(self):
        import base64
        self.assertEqual(ads.parse_splice_insert(base64.b64decode(IN_A)),
                         (86574, False, None))

    def test_garbage(self):
        self.assertIsNone(ads.parse_splice_insert(b""))
        self.assertIsNone(ads.parse_splice_insert(b"\x00" * 40))
        self.assertIsNone(ads.parse_splice_insert(b"\xfc" * 5))


class ParseBreaks(unittest.TestCase):
    def test_repeated_cue_out_is_one_break_ended_by_cue_in(self):
        m = mpd([(439.3, 25.4, OUT_A), (449.3, 15.4, OUT_B), (465.8, None, IN_A)],
                pto=17933549961783333)
        self.assertEqual(ads.parse_breaks(m), [(439.3, 465.8)])

    def test_without_cue_in_uses_duration(self):
        m = mpd([(1152.0, 542.6, OUT_C)])
        (s, e), = ads.parse_breaks(m)
        self.assertAlmostEqual(e - s, 542.6, 3)

    def test_short_break_ignored(self):
        m = mpd([(100.0, None, OUT_A)])      # 25,4 s z binarneho SCTE-35
        self.assertEqual(len(ads.parse_breaks(m)), 1)
        from unittest import mock
        with mock.patch.object(ads, "MIN_BREAK", 60.0):
            self.assertEqual(ads.parse_breaks(m), [])

    def test_no_event_stream_or_bad_xml(self):
        self.assertEqual(ads.parse_breaks(
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period/></MPD>'), [])
        self.assertEqual(ads.parse_breaks("<nie xml"), [])
        self.assertEqual(ads.parse_breaks(b""), [])

    def test_bad_event_does_not_break_the_rest(self):
        m = mpd([(10.0, None, "!!!nie base64!!!"), (439.3, 25.4, OUT_A)])
        self.assertEqual(len(ads.parse_breaks(m)), 1)

    def test_dynamic(self):
        self.assertTrue(ads.is_dynamic(mpd([], "dynamic")))
        self.assertFalse(ads.is_dynamic(mpd([], "static")))
        self.assertFalse(ads.is_dynamic(b"nic"))


class BreakAt(unittest.TestCase):
    B = [(100.0, 200.0), (400.0, 500.0)]

    def test_inside_and_outside(self):
        self.assertEqual(ads.break_at(self.B, 150), (100.0, 200.0))
        self.assertEqual(ads.break_at(self.B, 99.8), (100.0, 200.0))
        self.assertIsNone(ads.break_at(self.B, 50))
        self.assertIsNone(ads.break_at(self.B, 200))
        self.assertIsNone(ads.break_at(self.B, 199.5))   # uz sa nevyplati

    def test_skipped_block_is_left_alone(self):
        self.assertIsNone(ads.break_at(self.B, 150, {(100.0, 200.0)}))
        self.assertEqual(ads.break_at(self.B, 450, {(100.0, 200.0)}), (400.0, 500.0))


if __name__ == "__main__":
    unittest.main()
