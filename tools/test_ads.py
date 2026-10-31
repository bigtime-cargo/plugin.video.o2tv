# -*- coding: utf-8 -*-
"""Offline testy resources/lib/ads.py (SCTE-35 znacky reklam v MPD).

    cd ~/plugin.video.o2tv && python3 -m unittest tools.test_ads -v

Binarne udalosti su skutocne, z archivneho manifestu Markizy (30. 10. 2026).
"""
import base64, os, sys, unittest

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


def splice(evid, oon, dur=None):
    """Zostavi splice_insert (CRC sa nekontroluje). Rovnaka kostra ako v O2."""
    flags = (0x80 if oon else 0) | 0x40 | (0x20 if dur else 0) | (0 if dur else 0x10) | 0x0F
    body = evid.to_bytes(4, "big") + b"\x00" + bytes([flags])
    if dur:
        body += (0xFE << 32 | 1).to_bytes(5, "big")                 # splice_time
        body += ((1 << 39) | (0x3F << 33) | int(dur * 90000)).to_bytes(5, "big")
    body += b"\x00\x00\x00\x00\x00"
    d = b"\xfc\x30\x00\x00" + b"\x00" * 5 + b"\x00" + b"\xff\xf0\x00" + b"\x05" + body
    return base64.b64encode(d + b"\x00\x00\x00\x00").decode()


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


class JojSeries(unittest.TestCase):
    """JOJ: kazde opakovanie cue-out ma ine event id a za prvym cue-in
    pokracuje seria dalsich cue-in - reklama trva do jej posledneho clena."""

    def joj(self):
        ev = [(500.0, None, splice(40, False)),                   # dlho pred blokom
              (930.4, None, splice(41, False)),
              (936.5, None, splice(42, False)),
              (971.5, 194.5, splice(1, True, 194.5)),
              (981.4, 184.6, splice(2, True, 184.6)),
              (1136.1, 29.9, splice(3, True, 29.9))]
        for i, t in enumerate((1164.9, 1184.8, 1214.8, 1244.8, 1274.8, 1299.6, 1339.4,
                            1374.1, 1404.1, 1434.5, 1462.6, 1479.6)):
            ev.append((t, None, splice(10 + i, False)))
        ev += [(2436.6, None, splice(30, False)),            # po 957 s ticha
               (2442.6, None, splice(33, False)),
               (2462.6, 197.0, splice(31, True, 197.0)),
               (2640.0, None, splice(32, False))]
        return mpd(ev)

    def test_block_ends_with_last_cue_in_of_series(self):
        got = ads.parse_breaks(self.joj())
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0], (930.4, 1479.6))      # od prveho cue-in pred cue-out

    def test_series_stops_at_next_cue_out(self):
        self.assertEqual(ads.parse_breaks(self.joj())[1][0], 2436.6)
        self.assertEqual(ads.parse_breaks(self.joj())[1][1], 2640.0)

    def test_series_is_capped(self):
        ev = [(100.0, 30.0, splice(1, True, 30.0))]
        ev += [(130.0 + 30 * i, None, splice(2 + i, False)) for i in range(60)]
        (s, e), = ads.parse_breaks(mpd(ev))
        self.assertLessEqual(e - 130.0, ads.EXT_MAX)

    def test_start_does_not_cross_previous_block(self):
        # cue-in 8:22 konci prvy blok a je len 5 s pred druhym cue-out
        ev = [(473.0, 29.0, splice(1, True, 29.0)), (502.0, None, splice(2, False)),
              (507.7, 29.0, splice(3, True, 29.0)), (536.4, None, splice(4, False))]
        self.assertEqual(ads.parse_breaks(mpd(ev)), [(473.0, 536.4)])    # 5,7 s medzera = jedna reklama

    def test_joj_short_block_before_big_one_is_merged(self):
        # skutocne casy z JOJ 31. 10. 2026 (46:31, 46:40, 46:46, 47:07)
        ev = [(2791.44, 9.92, splice(1, True, 9.92)), (2800.24, None, splice(2, False)),
              (2806.2, None, splice(3, False)),
              (2827.72, 471.4, splice(4, True, 471.4)), (3299.12, None, splice(6, False))]
        self.assertEqual(ads.parse_breaks(mpd(ev)), [(2791.44, 3299.12)])

    def test_far_blocks_stay_separate(self):
        ev = [(100.0, 30.0, splice(1, True, 30.0)), (131.0, None, splice(2, False)),
              (200.0, 30.0, splice(3, True, 30.0)), (231.0, None, splice(4, False))]
        self.assertEqual(ads.parse_breaks(mpd(ev)), [(100.0, 131.0), (200.0, 231.0)])

    def test_series_gap_of_48_s_does_not_cut_the_series(self):
        # JOJ 31. 10. 2026: za blokom 54:59 ide cue-in 55:46,8 (rozostup 47,7 s),
        # 56:16,7 a 56:32,4 - reklama (Noviny TV JOJ) trvala do posledneho
        ev = [(100.0, 30.0, splice(1, True, 30.0)), (130.0, None, splice(2, False)),
              (177.7, None, splice(3, False)), (207.6, None, splice(4, False)),
              (223.2, None, splice(5, False)), (1000.0, None, splice(6, False))]
        self.assertEqual(ads.parse_breaks(mpd(ev)), [(100.0, 223.2)])

    def test_single_cue_in_does_not_extend(self):
        ev = [(100.0, 30.0, splice(1, True, 30.0)), (131.0, None, splice(2, False)),
              (400.0, None, splice(3, False))]
        self.assertEqual(ads.parse_breaks(mpd(ev)), [(100.0, 131.0)])


class RunningProgram(unittest.TestCase):
    """Program, ktory este bezi (dynamic): EventStream bez presentationTimeOffset,
    casy udalosti su absolutne ako casova os segmentov (overene 30. 10. 2026,
    Markiza: prvy segment 1793395796,2, cue-out o 12:31 od zaciatku programu)."""

    T0 = 1793395796.2

    def dyn(self, events, seg_t=None):
        m = mpd([(self.T0 + sec, d, b64) for sec, d, b64 in events], "dynamic")
        seg_t = int(self.T0 * 48000) if seg_t is None else seg_t
        tpl = ('<AdaptationSet contentType="%s"><SegmentTemplate timescale="48000">'
               '<SegmentTimeline><S t="%d" d="92160" r="9"/></SegmentTimeline>'
               '</SegmentTemplate></AdaptationSet>')
        return m.replace("</Period>", (tpl % ("audio", seg_t + 7)) + (tpl % ("video", seg_t))
                         + "</Period>")

    def test_origin_is_first_segment_of_video(self):
        self.assertAlmostEqual(ads.timeline_origin(self.dyn([])), self.T0, 3)

    def test_origin_missing(self):
        self.assertIsNone(ads.timeline_origin(mpd([])))
        self.assertIsNone(ads.timeline_origin("<nie xml"))

    def test_absolute_times_become_positions(self):
        m = self.dyn([(750.8, 25.4, OUT_A), (760.8, 15.4, OUT_B), (781.0, None, IN_A)])
        o = ads.timeline_origin(m)
        (s, e), = ads.parse_breaks(m, o)
        self.assertAlmostEqual(s, 750.8, 1)
        self.assertAlmostEqual(e, 781.0, 1)

    def test_without_origin_times_stay_absolute(self):
        m = self.dyn([(750.8, 25.4, OUT_A)])
        self.assertGreater(ads.parse_breaks(m)[0][0], 1e9)

    def test_break_started_before_the_buffer(self):
        m = self.dyn([(-168.0, 25.4, OUT_A), (-100.0, None, IN_A)])
        (s, e), = ads.parse_breaks(m, ads.timeline_origin(m))
        self.assertAlmostEqual(s, -168.0, 1)

    def test_static_ignores_origin_argument_when_relative(self):
        m = mpd([(439.3, 25.4, OUT_A), (465.8, None, IN_A)])
        self.assertEqual(ads.parse_breaks(m, self.T0), [(439.3, 465.8)])


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
