# -*- coding: utf-8 -*-
import os, time, datetime, tempfile

PLUGIN = "plugin://plugin.video.o2tv/"


class Aborted(Exception):
    """Kodi sa vypina - export sa prerušil a nic sa nezapisalo."""


class Incomplete(Exception):
    """Cast EPG sa nestiahla a na disku je este cerstve EPG - nic sa nezapisalo."""


# Neuplne EPG je horsie nez to stare, ale len kym je stare cerstve: EPG drzi
# 3 dni dopredu, takze trvalo zlyhavajuca davka by inak cez par dni vyprazdnila
# program vsetkym kanalom.
KEEP_OLD_EPG = 24 * 3600


def xesc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("'", "&apos;").replace('"', "&quot;"))


def fmt(ts):
    return datetime.datetime.fromtimestamp(
        int(ts), datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        # inak by pri plnom disku ostaval tmpXXXX aj vo verejnom export_dir
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def build_m3u(chans, epg_url=None):
    out = ['#EXTM3U' + (' url-tvg="%s"' % epg_url if epg_url else '')]
    for c in chans:
        cid = c["id"]
        attrs = 'tvg-id="%s" tvg-name="%s"' % (cid, xesc(c["name"]))
        if c.get("logo"):
            attrs += ' tvg-logo="%s"' % c["logo"]
        if c.get("number"):
            attrs += ' tvg-chno="%s"' % c["number"]
        attrs += ' catchup="default" catchup-days="7"'
        attrs += (' catchup-source="%s?action=play&cid=%s'
                  '&start_ts={utc}&end_ts={utcend}"' % (PLUGIN, cid))
        out.append('#EXTINF:-1 %s,%s' % (attrs, c["name"]))
        out.append('%s?action=play&cid=%s' % (PLUGIN, cid))
    return "\n".join(out) + "\n"


def build_xmltv(api, chans, days_back=7, days_fwd=3, log=None, should_stop=None):
    """Vracia (xml, pocet zlyhanych davok)."""
    now = int(time.time())
    failed = 0
    start_ts, end_ts = now - days_back * 86400, now + days_fwd * 86400
    id2name = {c["id"]: c["name"] for c in chans}

    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="plugin.video.o2tv">']
    for c in chans:
        xml.append('<channel id="%s"><display-name>%s</display-name>%s</channel>' % (
            xesc(c["id"]), xesc(c["name"]),
            '<icon src="%s"/>' % xesc(c["logo"]) if c.get("logo") else ""))

    ids = [c["id"] for c in chans]
    for i in range(0, len(ids), 5):
        # Export trva desiatky sekund a Kodi pri vypinani caka na vlakno
        # skriptu. Medzi davkami sa preto pozerame, ci sa nekonci - inak
        # by vypnutie Kodi drzal az do poslednej davky.
        if should_stop and should_stop():
            raise Aborted()
        try:
            progs = api.epg_batch(ids[i:i + 5], start_ts, end_ts)
        except Exception as e:
            if log:
                log("EPG dávka %d zlyhala: %s" % (i // 5, e))
            failed += 1
            continue
        for p in progs:
            if p.get("objectType") not in ("KalturaProgramAsset", "KalturaRecordingAsset"):
                continue
            ch = str(p.get("linearAssetId"))
            if ch not in id2name:
                continue
            xml.append('<programme start="%s" stop="%s" channel="%s">' % (
                fmt(p["startDate"]), fmt(p["endDate"]), xesc(ch)))
            xml.append('<title>%s</title>' % xesc(p.get("name", "")))
            if p.get("description"):
                xml.append('<desc>%s</desc>' % xesc(p["description"]))
            xml.append('</programme>')
    xml.append('</tv>')
    return "\n".join(xml), failed


def export_all(api, profile_dir, days_back=7, days_fwd=3, log=None, extra_dir=None,
               should_stop=None):
    """Vygeneruje playlist a EPG. Vracia (počet kanálov, počet programov).
    Pri ukončovaní Kodi vyhodí Aborted a nezapíše nič — neúplné EPG je
    horšie než to staré, ktoré na disku už je. Z rovnakého dôvodu vyhodí
    Incomplete, keď časť EPG chýba a staré EPG je ešte čerstvé."""
    chans = api.channels()
    ok = api.entitled_cached([c["id"] for c in chans], should_stop=should_stop)
    if should_stop and should_stop():
        raise Aborted()
    chans = [c for c in chans if c["id"] in ok]

    xml, failed = build_xmltv(api, chans, days_back, days_fwd, log, should_stop)

    epg_main = os.path.join(profile_dir, "epg.xml")
    if failed:
        try:
            age = time.time() - os.path.getmtime(epg_main)
        except OSError:
            age = None
        if age is not None and age < KEEP_OLD_EPG:
            raise Incomplete("%d dávok EPG zlyhalo, ponechávam EPG staré %.1f h"
                             % (failed, age / 3600.0))
        if log:
            log("%d dávok EPG zlyhalo, staré EPG je staršie než %d h — "
                "zapisujem neúplné" % (failed, KEEP_OLD_EPG // 3600))

    targets = [(os.path.join(profile_dir, "playlist.m3u"), epg_main)]
    if extra_dir:
        targets.append((os.path.join(extra_dir, "playlist.m3u"),
                        os.path.join(extra_dir, "o2tv_epg.xml")))

    for m3u_path, epg_path in targets:
        try:
            _write(m3u_path, build_m3u(chans, epg_url=epg_path))
            _write(epg_path, xml)
        except Exception as e:
            if log:
                log("zápis do %s zlyhal: %s" % (os.path.dirname(m3u_path), e))
    return len(chans), xml.count("<programme")
