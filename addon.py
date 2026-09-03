# -*- coding: utf-8 -*-
import sys, os
import urllib.parse

import xbmc, xbmcgui, xbmcplugin, xbmcaddon, xbmcvfs

ADDON = xbmcaddon.Addon()
HANDLE = int(sys.argv[1]) if len(sys.argv) > 1 else -1
BASE = sys.argv[0] if len(sys.argv) > 0 else "plugin://plugin.video.o2tv/"

sys.path.insert(0, xbmcvfs.translatePath(
    os.path.join(ADDON.getAddonInfo("path"), "resources", "lib")))
from api import O2API, O2Error, gen_udid   # noqa: E402


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


def list_channels(api):
    try:
        chans = api.channels()
    except O2Error as e:
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
    xbmcplugin.endOfDirectory(HANDLE)


def play(api, cid, start_ts=None, end_ts=None):
    try:
        manifest, lic = api.resolve(cid, start_ts, end_ts)
        log("resolve: %s" % getattr(api, "last_mode", "?"))
    except O2Error as e:
        notify("Nedá sa prehrať: %s" % e, True)
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())
        return

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
    else:
        list_channels(api)


if __name__ == "__main__":
    main()
