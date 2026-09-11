# -*- coding: utf-8 -*-
import sys, os
import urllib.parse, urllib.request

import xbmc, xbmcgui, xbmcplugin, xbmcaddon, xbmcvfs

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE = sys.argv[0] if len(sys.argv) > 0 else "plugin://plugin.video.o2tv/"

sys.path.insert(0, xbmcvfs.translatePath(
    os.path.join(ADDON.getAddonInfo("path"), "resources", "lib")))
from api import O2API, O2Error, gen_udid, UA   # noqa: E402

# adresa CDN relacie pre keep-alive v service.py (window property, vidia ju
# oba procesy)
KEEPALIVE_PROP = "o2tv.keepalive_url"


class Store:
    def get(self, key):
        return ADDON.getSetting(key)

    def set(self, key, value):
        ADDON.setSetting(key, str(value))


def log(msg):
    xbmc.log("[o2tv] %s" % msg, xbmc.LOGINFO)


def notify(msg, err=False):
    xbmcgui.Dialog().notification(
        "O2 TV", msg,
        xbmcgui.NOTIFICATION_ERROR if err else xbmcgui.NOTIFICATION_INFO, 5000)


def url_for(**kwargs):
    return BASE + "?" + urllib.parse.urlencode(kwargs)


def do_login(api):
    """Prihlasenie kodom zariadenia. Uzivatel potvrdi kod v prehliadaci na
    mobile - na telke je to jediny znesitelny sposob a na PC odpada
    vytahovanie tokenu z DevTools. Vracia True pri uspechu."""
    import time
    try:
        dev = api.device_start()
    except O2Error as e:
        notify("Prihlásenie nezačalo: %s" % e, True)
        return False

    # DialogProgress od Kodi 19 berie jediny text, riadky sa daju len \n
    head = ("Otvor [B]o2.sk/zariadenie[/B] a zadaj kód:  [B]%s[/B]\n"
            "Prihlás sa [B]číslom služby[/B] a SMS kódom — e-mail a heslo "
            "O2 pre doplnok neuzná.\n" % dev["user_code"])
    dlg = xbmcgui.DialogProgress()
    dlg.create("O2 TV — prihlásenie", head)
    step = max(int(dev.get("interval", 5)), 1)
    total = max(int(dev.get("expires_in", 600)), step)
    waited, token = 0, None
    while waited < total:
        if dlg.iscanceled():
            dlg.close()
            return False
        xbmc.sleep(step * 1000)
        waited += step
        dlg.update(int(100 * waited / total),
                   head + "Čakám na potvrdenie… (zostáva %d s)" % (total - waited))
        try:
            token = api.device_poll(dev["device_code"])
        except O2Error as e:
            dlg.close()
            notify("%s" % e, True)
            return False
        if token:
            break
    dlg.close()

    if not token:
        notify("Kód vypršal, skús to znova.", True)
        return False
    try:
        ls = api.login_with_token(token)
    except O2Error as e:
        notify("%s" % e, True)
        log("prihlásenie zlyhalo: %s" % e)
        return False
    log("prihlásenie kódom zariadenia OK, KS do %s"
        % time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ls["expiry"]))))
    notify("Prihlásené. Relácia platí do %s."
           % time.strftime("%d.%m. %H:%M", time.localtime(int(ls["expiry"]))))
    return True


def list_channels(api):
    try:
        chans = api.channels()
    except O2Error as e:
        # 500017 = relacia sa uz neda obnovit; ponukni prihlasenie rovno tu,
        # inak by sa uzivatel k polozke "Prihlásiť sa" nedostal
        if getattr(e, "code", "") == "500017" and xbmcgui.Dialog().yesno(
                "O2 TV", "Relácia vypršala a nedá sa obnoviť.\n"
                         "Prihlásiť sa teraz kódom zariadenia?"):
            if do_login(api):
                try:
                    chans = api.channels()
                except O2Error as e2:
                    e = e2
                    chans = None
            else:
                chans = None
        else:
            chans = None
        if chans is None:
            notify("Chyba: %s" % e, True)
            xbmcplugin.endOfDirectory(HANDLE, succeeded=False)
            return

    ids = [c["id"] for c in chans]
    ok = api.entitled_cached(ids)
    chans = [c for c in chans if c["id"] in ok]

    xbmcplugin.setContent(HANDLE, "videos")
    for c in chans:
        label = "%s. %s" % (c["number"], c["name"]) if c["number"] else c["name"]
        li = xbmcgui.ListItem(label=label)
        art = {"icon": c["logo"], "thumb": c["logo"]} if c["logo"] else {}
        if art:
            li.setArt(art)
        li.setProperty("IsPlayable", "true")
        tag = li.getVideoInfoTag()
        tag.setMediaType("video")
        tag.setTitle(label)
        xbmcplugin.addDirectoryItem(
            HANDLE, url_for(action="play", cid=c["id"]), li, isFolder=False)

    li = xbmcgui.ListItem(label="[ Prihlásiť sa nanovo (kód zariadenia) ]")
    xbmcplugin.addDirectoryItem(HANDLE, url_for(action="login"), li, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE)


def cdn_session(url):
    """Manifest od Kaltury sa 307-presmeruje na reláciu CDN (bpk-token/...),
    z ktorej sa ťahajú segmenty. Rozbalíme si ju sami, aby ju service.py
    vedel počas pauzy udržiavať nažive - inak umrie do ~40 s nečinnosti
    a po odpauznutí prídu na segmenty 403."""
    try:
        req = urllib.request.Request(url, headers={"user-agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except Exception as e:
        log("presmerovanie manifestu zlyhalo: %s" % e)
        return url


def play(api, cid, start_ts=None, end_ts=None):
    try:
        manifest, lic = api.resolve(cid, start_ts, end_ts)
        log("resolve: %s" % getattr(api, "last_mode", "?"))
    except O2Error as e:
        notify("Nedá sa prehrať: %s" % e, True)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

    manifest = cdn_session(manifest)
    xbmcgui.Window(10000).setProperty(KEEPALIVE_PROP, manifest)

    li = xbmcgui.ListItem(path=manifest)
    li.setMimeType("application/dash+xml")
    li.setContentLookup(False)
    li.setProperty("inputstream", "inputstream.adaptive")
    li.setProperty("inputstream.adaptive.manifest_type", "mpd")
    if lic:
        li.setProperty("inputstream.adaptive.license_type", "com.widevine.alpha")
        li.setProperty("inputstream.adaptive.license_key",
                       "%s|Content-Type=application/octet-stream|R{SSM}|" % lic)

    maxb = ADDON.getSetting("max_bitrate")
    caps = {"1080p": "1920", "720p": "1280", "576p": "1024"}
    if maxb in caps:
        li.setProperty("inputstream.adaptive.max_resolution", caps[maxb])

    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def main():
    raw = urllib.parse.parse_qs(sys.argv[2][1:]) if len(sys.argv) > 2 else {}
    args = {k[4:] if k.startswith("amp;") else k: v for k, v in raw.items()}
    action = args.get("action", [None])[0]

    api = O2API(Store(), xbmcvfs.translatePath(ADDON.getAddonInfo("profile")),
                log=log)
    if not api.st_get("udid") and not ADDON.getSetting("udid"):
        ADDON.setSetting("udid", gen_udid())

    if action == "play":
        cid = args.get("cid", [None])[0]
        st = args.get("start_ts", [None])[0]
        et = args.get("end_ts", [None])[0]
        play(api, cid, int(st) if st else None, int(et) if et else None)
    elif action == "login":
        do_login(api)
        xbmc.executebuiltin("Container.Refresh")
    else:
        list_channels(api)


if __name__ == "__main__":
    main()
