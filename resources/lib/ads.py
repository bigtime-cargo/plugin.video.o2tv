# -*- coding: utf-8 -*-
"""Reklamne bloky z manifestu: SCTE-35 EventStream v MPD od Broadpeak CDN.

JOJ: koniec bloku nie je prvy cue-in, ale posledny z serie cue-in za nim,
a zaciatok je uz prvy z cue-in tesne pred cue-out (overene 30. 10. 2026:
cue-in 15:30,4 a 15:36,5, cue-out 16:11-19:24, cue-in 19:25 ... 24:39,6;
reklama bezala 15:31 - 24:40).

Hotovy (static) archivny program ma v Period <EventStream
schemeIdUri="urn:scte:scte35:2014:xml+bin"> s udalostami splice_insert:
cue-out (out_of_network=1, break_duration) a cue-in (out_of_network=0,
immediate). Cue-out sa opakuje kazdych ~10-20 s s klesajucim zvyskom, vsetky
maju rovnake event id. Cas udalosti (presentationTime - presentationTimeOffset)
je sekundy od zaciatku programu, teda rovnaka poloha, akú hlási prehravac.

Program, ktory este bezi (dynamic), ma EventStream tiez (overene 30. 10. 2026),
ale bez presentationTimeOffset: casy udalosti su absolutne (sekundy od roku
1970), rovnako ako casova os segmentov. Poloha prehravaca 0 je zaciatok
casovej osi, teda cas prveho segmentu (timeline_origin) - ten sa odpocita.
Cisto zivy okraj (timeshift ~1 min) sa preskocit nedá."""
import base64
import re
import xml.etree.ElementTree as ET

NS = {"m": "urn:mpeg:dash:schema:mpd:2011",
      "s": "http://www.scte.org/schemas/35/2016"}
SCHEME = "urn:scte:scte35:2014:xml+bin"
MIN_BREAK = 5.0     # kratsie bloky nestoja za seek
EXT_GAP = 45.0      # najvacsi rozostup cue-in v serii za blokom (JOJ: 40 s)
EXT_MAX = 600.0     # a najviac tolko za prvym cue-in (JOJ: 315 s)


def parse_splice_insert(data):
    """(event_id, out_of_network, break_duration_s | None) zo splice_info_section,
    alebo None, ked to nie je splice_insert. Nic z toho nesmie zhodit sluzbu."""
    if len(data) < 20 or data[0] != 0xFC or data[13] != 0x05:
        return None
    evid = int.from_bytes(data[14:18], "big")
    if data[18] & 0x80:                 # splice_event_cancel_indicator
        return None
    flags = data[19]
    oon = bool(flags & 0x80)
    program = bool(flags & 0x40)
    has_dur = bool(flags & 0x20)
    immediate = bool(flags & 0x10)
    p = 20
    if program and not immediate:       # splice_time()
        p += 5 if data[p] & 0x80 else 1
    elif not program:
        return None                     # component splice - nepouzivane
    dur = None
    if has_dur:
        if p + 5 > len(data):
            return None
        dur = (int.from_bytes(data[p:p + 5], "big") & 0x1FFFFFFFF) / 90000.0
    return evid, oon, dur


def timeline_origin(mpd_text):
    """Cas (s) prveho segmentu casovej osi bezieho programu - tam je poloha 0
    prehravaca s play_timeshift_buffer. None, ked sa neda urcit."""
    try:
        root = ET.fromstring(mpd_text.encode("utf-8") if isinstance(mpd_text, str)
                             else mpd_text)
    except ET.ParseError:
        return None
    sets = root.findall(".//m:AdaptationSet", NS)
    sets.sort(key=lambda a: a.get("contentType") != "video")   # video ako prve
    for a in sets:
        st = a.find("m:SegmentTemplate", NS)
        s = st.find("m:SegmentTimeline/m:S", NS) if st is not None else None
        if s is None or s.get("t") is None:
            continue
        try:
            ts = int(st.get("timescale") or 1)
            pto = int(st.get("presentationTimeOffset") or 0)
            return (int(s.get("t")) - pto) / float(ts)
        except ValueError:
            continue
    return None


def parse_breaks(mpd_text, origin=None):
    """Zoznam (start_s, end_s) reklamnych blokov podla zaciatku. Prazdny, ked
    manifest EventStream nema alebo sa neda precitat. `origin` = timeline_origin
    bezieho programu; casy udalosti, ktore su absolutne, sa o neho posunu."""
    try:
        root = ET.fromstring(mpd_text.encode("utf-8") if isinstance(mpd_text, str)
                             else mpd_text)
    except ET.ParseError:
        return []
    events = []     # (cas_s, evid, oon, trvanie)
    for es in root.iterfind(".//m:Period/m:EventStream", NS):
        if es.get("schemeIdUri") != SCHEME:
            continue
        try:
            ts = int(es.get("timescale") or 1)
            pto = int(es.get("presentationTimeOffset") or 0)
        except ValueError:
            continue
        for ev in es.iterfind("m:Event", NS):
            try:
                t = (int(ev.get("presentationTime") or 0) - pto) / float(ts)
                if origin is not None and t > origin - 3600:    # absolutny cas
                    t -= origin
                b = ev.find(".//s:Binary", NS)
                parsed = parse_splice_insert(base64.b64decode(b.text.strip()))
            except (ValueError, AttributeError, TypeError):
                continue
            if parsed:
                evid, oon, dur = parsed
                if dur is None and ev.get("duration"):
                    dur = int(ev.get("duration")) / float(ts)
                events.append((t, evid, oon, dur))
    events.sort()
    outs = [e for e in events if e[2]]
    ins = [e[0] for e in events if not e[2]]
    # cue-out sa opakuje s klesajucim zvyskom, u JOJ aj s inym event id -
    # zhluk sa preto ratat podla casu: opakovanie lezi pred koncom bloku
    clusters = []       # [start, odhad konca]
    for t, _evid, _oon, dur in outs:
        if not dur:
            continue
        if clusters and t < clusters[-1][1]:
            clusters[-1][1] = max(clusters[-1][1], t + dur)
        else:
            clusters.append([t, t + dur])
    out = []
    for start, est in clusters:
        # skutocny koniec je cue-in; trvanie z cue-out byva o ~1 s kratsie
        back = [t for t in ins if start < t <= est + 10]
        end = min(back) if back else est
        if back:
            # JOJ: po cue-in pokracuje seria dalsich cue-in (rozostupy 4-40 s)
            # a reklama trva az do jej posledneho clena (24:40 pri cue-in 19:25)
            nxt = min([t for t, _e, o, _d in events if o and t > end] or [1e12])
            last = end
            for t in ins:
                if t <= last or t >= nxt:
                    continue
                if t - last > EXT_GAP or t - end > EXT_MAX:
                    break
                last = t
            end = last
        if end - start >= MIN_BREAK:
            out.append((start, end))
    out.sort()
    # JOJ: reklama zacina uz pri cue-in tesne pred cue-out (15:30,4 a 15:36,5
    # pred cue-out 16:11,5; reklama sa v obraze zacala v 15:31) - posun zaciatok
    # na prvy z nich, ale nie pred koniec predosleho bloku
    for i, (start, end) in enumerate(out):
        limit = out[i - 1][1] if i else -1.0
        first = start
        for t in reversed([t for t in ins if limit < t < start]):
            if first - t > EXT_GAP or start - t > EXT_MAX:
                break
            first = t
        out[i] = (first, end)
    return out


def is_dynamic(mpd_text):
    """Program este bezi (live manifest) - reklamy v nom pribudaju."""
    if isinstance(mpd_text, bytes):
        mpd_text = mpd_text.decode("utf-8", "replace")
    m = re.search(r"<MPD\b[^>]*\btype=\"(\w+)\"", mpd_text)
    return bool(m and m.group(1) == "dynamic")


def break_at(breaks, pos, skipped=()):
    """Blok, v ktorom je poloha `pos` a ktory sme este nepreskocili."""
    for b in breaks:
        if b[0] - 0.3 <= pos < b[1] - 1.0 and b not in skipped:
            return b
    return None
