# -*- coding: utf-8 -*-
import os, sys, time
import threading, urllib.request, urllib.error

import xbmc, xbmcaddon, xbmcgui, xbmcvfs

ADDON = xbmcaddon.Addon()
sys.path.insert(0, xbmcvfs.translatePath(
    os.path.join(ADDON.getAddonInfo("path"), "resources", "lib")))
from api import O2API, O2Error, UA   # noqa: E402
from export import export_all, Aborted   # noqa: E402


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
LOW_LEFT = 2 * 86400     # odkedy je odpocet naliehavy


def human(sec):
    """Zostavajuci cas po slovensky: 1 den, 2-4 dni, 5+ dni."""
    if sec < 86400:
        h = int(max(sec, 0) / 3600)
        if h < 1:
            return "menej než hodinu"
        return "%d hodinu" % h if h == 1 else (
            "%d hodiny" % h if h <= 4 else "%d hodín" % h)
    d = int(sec / 86400)
    return "%d deň" % d if d == 1 else (
        "%d dni" % d if d <= 4 else "%d dní" % d)


def session_notice(api, healthy, force=False):
    """Toast s odpoctom do noveho prihlasenia. Vracia True, ked ho ukazal.

    Bez `force` sa ozve len vtedy, ked je co riesit - pokojny odpocet patri
    na start Kodi, nie kazdych par hodin do beziaceho filmu.

    Kym obnova prechadza, odpocet sa rata od prihlasenia - ks_expiry sa
    kazdou obnovou posuva, takze z neho by vzdy vyslo 7 dni. Ked obnova
    prestane prechadzat, KS zamrzne a jej vyprsanie je uz tvrdy koniec
    prehravania, cize vtedy je zmysluplnejsie ukazovat ten.
    """
    age = api.state_age()
    if healthy and (age < 0 or age <= STALE_AFTER):
        left, est = api.session_left()
        if left < 0:
            return False
        low = left <= LOW_LEFT
        if not (force or low):
            return False
        msg = ("Prihlás sa nanovo, platí ešte %s" if low
               else "Prihlásenie platí ešte %s") % human(left)
        if est:
            msg += " (odhad)"
        log("odpočet: %s" % msg, low)
        xbmcgui.Dialog().notification(
            "O2 TV", msg,
            xbmcgui.NOTIFICATION_WARNING if low else xbmcgui.NOTIFICATION_INFO,
            8000 if low else 5000)
        return True

    left = api.ks_remaining()
    if left > 0:
        msg = "Prihlás sa nanovo — vysielanie funguje ešte %s" % human(left)
    else:
        msg = "Relácia vypršala — otvor doplnok a prihlás sa"
    log("obnova relácie neprechádza (stav starý %.1f dňa); %s"
        % (max(age, 0) / 86400.0, msg), True)
    xbmcgui.Dialog().notification(
        "O2 TV", msg, xbmcgui.NOTIFICATION_WARNING, 10000)
    return True


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
            log("relácia je neobnoviteľná, treba nové prihlásenie "
                "(doplnok O2 TV → Prihlásiť sa nanovo)", True)
            xbmcgui.Dialog().notification(
                "O2 TV", "Relácia vypršala — otvor doplnok a prihlás sa",
                xbmcgui.NOTIFICATION_WARNING, 10000)
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
RESUME_PROP = "o2tv.play_url"
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
        self.dead = False

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
            was_paused = bool(self.paused_since)
            self.paused_since = 0
            # relacia umrela uz pocas pauzy: ISA ma v sebe mrtvu adresu,
            # po odpauznuti dohra buffer a na dalsi segment dostane 403.
            # Vymenit sa mu neda, tak to pustime znova od toho isteho miesta.
            if was_paused and self.dead and playing:
                self.dead = False
                self.restart()
                return
            # az ked prehravac naozaj bezal - inak by sa adresa zmazala
            # v tej sekunde medzi resolve a startom prehravania
            if self.seen_video and not playing:
                win.clearProperty(KEEPALIVE_PROP)
                win.clearProperty(RESUME_PROP)
                self.seen_video = False
                self.dead = False
            return
        now = time.time()
        if not self.paused_since:
            self.paused_since = now
            log("pauza — držím CDN reláciu nažive")
        if self.dead:
            return          # mrtvu relaciu uz neoziveme, netreba na nu klopat
        if now - self.paused_since > KEEPALIVE_MAX:
            # prestali sme drzat, takze relacia je tiez prec - nech to vie
            # aj odpauznutie a nekonci ciernou obrazovkou
            self.dead = True
            log("pauza nad %d min — po odpauznutí prehrám znova" % (KEEPALIVE_MAX // 60))
            return
        if now - self.last_ping < PING_EVERY:
            return
        self.last_ping = now
        try:
            req = urllib.request.Request(url, headers={"user-agent": UA})
            with urllib.request.urlopen(req, timeout=5) as r:
                r.read(1)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                # 403 znamena, ze relacia je definitivne prec - dalsie pingy
                # by uz len plnili log, ako sa to 18. 9. 2026 aj stalo
                self.dead = True
                log("CDN relácia vypršala — po odpauznutí prehrám znova", True)
                return
            log("keep-alive zlyhal: %s" % e, True)
        except Exception as e:
            log("keep-alive zlyhal: %s" % e, True)

    def restart(self):
        """Pusti to iste od tej istej sekundy. Nove volanie si vypyta cerstvy
        playback context, takze aj cerstvu CDN relaciu."""
        play_url = xbmcgui.Window(10000).getProperty(RESUME_PROP)
        if not play_url:
            log("obnova nemá čo pustiť - chýba adresa", True)
            return
        player = xbmc.Player()
        try:
            pos = int(player.getTime())
            old = player.getPlayingFile()
        except Exception:
            pos, old = 0, ""
        log("obnovujem prehrávanie od %d s" % pos)
        self.seen_video = False
        player.play(play_url)
        # cakat treba na naozaj novy stream: stary este chvilu bezi, takze
        # "hra a ma dlzku" plati hned a seek by sa stratil v tom, co sa
        # o sekundu zavrie (overene 18. 9. 2026). Novy stream spozname
        # po zmene adresy, a ak by ostala rovnaka, tak po navrate na zaciatok.
        for _ in range(30):
            if self.monitor.waitForAbort(1):
                return
            try:
                if not (player.isPlayingVideo() and player.getTotalTime() > 0):
                    continue
                if player.getPlayingFile() != old or player.getTime() < pos - 5:
                    break
            except Exception:
                continue
        else:
            log("obnova sa nerozbehla do 30 s", True)
            return
        if pos <= 0:
            return
        self.monitor.waitForAbort(1)
        try:
            player.seekTime(pos)
        except Exception as e:
            log("seek po obnove zlyhal: %s" % e, True)


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
    first = True
    while not monitor.abortRequested():
        ok = tick(api)
        # pri starte odpocet vzdy, potom uz len ked je naliehavy - a aj
        # vtedy nanajvys raz za WARN_EVERY, nech toasty nezavadzaju
        if first or time.time() - last_warn > WARN_EVERY:
            if session_notice(api, ok, force=first):
                last_warn = time.time()
        first = False
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
                                      ADDON.getSetting("export_dir") or None,
                                      should_stop=monitor.abortRequested)
                    last_export = time.time()
                    log("export hotový: %d kanálov, %d programov" % (n, p))
                except Aborted:
                    # Kodi sa vypina - export dobehne pri dalsom starte.
                    log("export prerušený, Kodi sa vypína")
                    break
                except Exception as e:
                    log("export zlyhal: %s" % e, True)
        if sleep_abortable(monitor, 600):
            break
    log("service koniec")


if __name__ == "__main__":
    main()
