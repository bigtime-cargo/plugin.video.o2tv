# -*- coding: utf-8 -*-
import os, sys, time
import threading, urllib.request

import xbmc, xbmcaddon, xbmcgui, xbmcvfs

ADDON = xbmcaddon.Addon()
sys.path.insert(0, xbmcvfs.translatePath(
    os.path.join(ADDON.getAddonInfo("path"), "resources", "lib")))
from api import O2API, O2Error, UA   # noqa: E402
from export import export_all   # noqa: E402


class Store:
    def get(self, key):
        return ADDON.getSetting(key)

    def set(self, key, value):
        ADDON.setSetting(key, str(value))


def log(msg, err=False):
    xbmc.log("[o2tv.service] %s" % msg, xbmc.LOGERROR if err else xbmc.LOGINFO)


# ks() pri zlyhanej obnove vracia staru KS, takze vypadok obnovy nie je
# navonok vidiet - az kym KS nevyprsi a s nou nezomrie aj refresh token.
# Stav starsi nez dva dni preto znamena, ze obnova neprechadza.
STALE_AFTER = 2 * 86400
WARN_EVERY = 6 * 3600


def check_stale(api, last_warn):
    """Varuje, ked sa stav dlho neobnovil. Vracia novy cas posledneho varovania."""
    age = api.state_age()
    if age < 0 or age <= STALE_AFTER:
        return last_warn
    if time.time() - last_warn < WARN_EVERY:
        return last_warn
    left = api.ks_remaining()
    log("obnova relácie neprechádza už %.1f dňa; relácia platí ešte %.1f dňa, "
        "potom bude treba nové prihlásenie" % (age / 86400.0, max(left, 0) / 86400.0),
        True)
    xbmcgui.Dialog().notification(
        "O2 TV", "Obnova relácie zlyháva %d dní" % int(age / 86400),
        xbmcgui.NOTIFICATION_WARNING, 8000)
    return time.time()


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


# ---------- udrziavanie CDN relacie pocas pauzy ----------
# Archiv sa neprehrava z Kaltury, ale z CDN: manifest sa presmeruje na
# relaciu bpk-token/..., z ktorej idu segmenty. Ta relacia umiera do ~40 s
# necinnosti. Pri pauze si Kodi nepyta segmenty, takze po odpauznuti dohra
# buffer a dalsi segment dostane 403 - obraz zamrzne a prehravanie skonci.
# Pocas pauzy preto manifest pingame my, adresu dava addon.py.
KEEPALIVE_PROP = "o2tv.keepalive_url"
PING_EVERY = 20          # bezpecne pod nameranym prahom (30 s este preslo)
KEEPALIVE_MAX = 45 * 60  # dlhsiu pauzu uz nedrzime, zbytocne by blokovala
                         # jeden z dvoch subeznych streamov


class KeepAlive(threading.Thread):
    """Vlastne vlakno, aby pingy nezavisely od hlavnej slucky - export EPG
    ju blokuje na desiatky sekund, co by pauzu rozbilo."""

    def __init__(self, monitor):
        threading.Thread.__init__(self, daemon=True)
        self.monitor = monitor
        self.last_ping = 0
        self.paused_since = 0
        self.seen_video = False

    def run(self):
        while not self.monitor.abortRequested():
            if self.monitor.waitForAbort(2):
                break
            try:
                self.tick()
            except Exception as e:
                log("keep-alive: %s" % e, True)

    def tick(self):
        win = xbmcgui.Window(10000)
        url = win.getProperty(KEEPALIVE_PROP)
        if not url:
            return
        playing = xbmc.getCondVisibility("Player.HasVideo")
        if playing:
            self.seen_video = True
        if not xbmc.getCondVisibility("Player.Paused"):
            self.paused_since = 0
            # az ked prehravac naozaj bezal - inak by sa adresa zmazala
            # v tej sekunde medzi resolve a startom prehravania
            if self.seen_video and not playing:
                win.clearProperty(KEEPALIVE_PROP)
                self.seen_video = False
            return
        now = time.time()
        if not self.paused_since:
            self.paused_since = now
            log("pauza — držím CDN reláciu nažive")
        if now - self.paused_since > KEEPALIVE_MAX or now - self.last_ping < PING_EVERY:
            return
        self.last_ping = now
        try:
            req = urllib.request.Request(url, headers={"user-agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read(1)
        except Exception as e:
            log("keep-alive zlyhal: %s" % e, True)


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
    KeepAlive(monitor).start()
    last_export = 0
    last_warn = 0
    while not monitor.abortRequested():
        ok = tick(api)
        last_warn = check_stale(api, last_warn)
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
