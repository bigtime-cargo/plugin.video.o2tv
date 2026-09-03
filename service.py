# -*- coding: utf-8 -*-
import os, sys, time

import xbmc, xbmcaddon, xbmcvfs

ADDON = xbmcaddon.Addon()
sys.path.insert(0, xbmcvfs.translatePath(
    os.path.join(ADDON.getAddonInfo("path"), "resources", "lib")))
from api import O2API, O2Error   # noqa: E402
from export import export_all   # noqa: E402


class Store:
    def get(self, key):
        return ADDON.getSetting(key)

    def set(self, key, value):
        ADDON.setSetting(key, str(value))


def log(msg, err=False):
    xbmc.log("[o2tv.service] %s" % msg, xbmc.LOGERROR if err else xbmc.LOGINFO)


def tick(api):
    """Jedno kolo údržby. Vracia True ak session drží.
    api.ks() sa samo postará o dennú obnovu relácie."""
    try:
        api.ks()
        return True
    except O2Error as e:
        log("refresh zlyhal: %s" % e, True)
        if getattr(e, "code", "") == "500017":
            # neplatny refresh token - iny clientTag na tom nic nezmeni
            log("relácia je neobnoviteľná, treba nové prihlásenie", True)
            return False
        if ADDON.getSetting("auto_clienttag") == "true":
            try:
                tag = api.update_client_tag()
                if tag:
                    log("clientTag aktualizovaný na %s, skúšam znova" % tag)
                    api.ks()
                    return True
            except Exception as e2:
                log("update clientTag zlyhal: %s" % e2, True)
        return False


def sleep_abortable(monitor, seconds, chunk=2):
    """Caka po malych kuskoch, aby Python stihol prijat ukoncenie skriptu."""
    waited = 0
    while waited < seconds:
        step = min(chunk, seconds - waited)
        if monitor.waitForAbort(step):
            return True
        waited += step
    return False

def main():
    monitor = xbmc.Monitor()
    api = O2API(Store(), xbmcvfs.translatePath(ADDON.getAddonInfo("profile")),
                log=lambda m: log(m, True))
    log("service štart")

    # ked sa stav neda zapisat, relacia sa neobnovi a o par dni vyprsi -
    # nech je to vidiet hned, nie az ked prestane fungovat
    ok, detail = api.state_writable()
    if ok:
        log("zápis stavu overený")
    else:
        log("POZOR: stav sa nedá zapísať (%s) — obnova relácie sa neuloží "
            "a doplnok o pár dní prestane fungovať" % detail, True)

    # počkaj, kým Kodi dobehne
    if sleep_abortable(monitor, 20):
        return

    profile = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    last_export = 0
    while not monitor.abortRequested():
        ok = tick(api)
        if ok:
            try:
                every = int(ADDON.getSetting("epg_refresh_h") or 12) * 3600
            except ValueError:
                every = 43200
            if time.time() - last_export > every:
                try:
                    db = int(ADDON.getSetting("epg_days_back") or 7)
                    df = int(ADDON.getSetting("epg_days_fwd") or 3)
                    n, p = export_all(api, profile, db, df,
                                      lambda m: log(m, True),
                                      ADDON.getSetting("export_dir") or None)
                    last_export = time.time()
                    log("export hotový: %d kanálov, %d programov" % (n, p))
                except Exception as e:
                    log("export zlyhal: %s" % e, True)
        if sleep_abortable(monitor, 600):
            break
    log("service koniec")


if __name__ == "__main__":
    main()
