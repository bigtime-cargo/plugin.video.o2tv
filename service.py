# -*- coding: utf-8 -*-
import json, os, sys, time
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
EXPORT_RETRY = 30 * 60   # po zlyhanom exporte EPG


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


def dead_notice():
    xbmcgui.Dialog().notification(
        "O2 TV", "Relácia vypršala — otvor doplnok a prihlás sa",
        xbmcgui.NOTIFICATION_WARNING, 10000)


def tick(api, auto=None):
    """Jedno kolo údržby. Vracia True ak session drží.
    api.ks() sa samo postará o dennú obnovu relácie.
    `auto` (AutoLogin): pri 500017 si označí, že relácia je mŕtva; toast
    o ručnom prihlásení sa vtedy odloží, kým sa automatické prihlásenie
    nepokúsi (ak by zlyhalo, ukáže ho main())."""
    try:
        api.ks()
        if auto is not None:
            # ks() pri mrtvom refreshi vrati este platnu staru KS, ale
            # 500017 si api zapamata
            auto.dead = api.refresh_dead
        return True
    except O2Error as e:
        log("refresh zlyhal: %s" % e, True)
        if getattr(e, "code", "") == "500017":
            # neplatny refresh token - iny clientTag na tom nic nezmeni
            log("relácia je neobnoviteľná, treba nové prihlásenie "
                "(doplnok O2 TV → Prihlásiť sa nanovo)", True)
            if auto is not None:
                auto.dead = True
            if auto is None or not auto.can_try():
                dead_notice()
            return False
        if e.code in ("NET", "BADJSON"):
            # za vypadok siete clientTag nemoze
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
FOREIGN_FORGET = 20      # tak dlho smie hrat cudzie video, kym zabudneme
                         # adresu O2 (prekrytie pri prepinani na O2 je kratsie)


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
        self.foreign_since = 0

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
        if playing and not self.ours(url):
            # hra nieco ine (Stream Cinema, YouTube...) a adresa ostala po O2,
            # napr. ked sa ine video pustilo bez medzery. restart() by ten
            # film nahradil kanalom O2, preto ju po chvili zabudneme.
            now = time.time()
            if not self.foreign_since:
                self.foreign_since = now
            elif now - self.foreign_since > FOREIGN_FORGET:
                log("hrá iné video — zabúdam adresu O2")
                self.forget()
            return
        self.foreign_since = 0
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
                self.forget()
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

    @staticmethod
    def ours(url):
        """Hra O2? addon.py dava prehravacu manifest z KEEPALIVE_PROP, Kodi
        moze hlasit aj povodnu plugin:// adresu; program z EPG hra ako pvr://
        (IPTV Simple na telke cita len playlist z exportu tohto doplnku)."""
        try:
            f = xbmc.Player().getPlayingFile()
        except RuntimeError:
            return True     # prave nic nehra - o tom rozhoduje zvysok tick()
        return (f == url or f.startswith("pvr://")
                or f.startswith("plugin://plugin.video.o2tv/"))

    def forget(self):
        win = xbmcgui.Window(10000)
        win.clearProperty(KEEPALIVE_PROP)
        win.clearProperty(RESUME_PROP)
        self.seen_video = False
        self.dead = False
        self.paused_since = 0
        self.foreign_since = 0

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
        bid = None
        if xbmc.getCondVisibility("PVR.IsPlayingEpgTag"):
            try:
                bid = rpc("Player.GetItem", playerid=1)["item"].get("id")
            except Exception as e:
                log("obnova: program z EPG som nezistil (%s)" % e, True)
        log("obnovujem prehrávanie od %d s%s" % (pos, " cez EPG" if bid else ""))
        self.seen_video = False
        if not (bid and self.reopen_epg(player, bid)):
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

    def reopen_epg(self, player, bid):
        """Program pusteny z EPG treba obnovit znova cez EPG. S adresou doplnku
        by Kodi zabudol, ze ide o program z EPG, a na konci by nepustil dalsi
        (pvrplayback.autoplaynextprogramme; stalo sa 15. 10. 2026 po pauze
        nad 45 min). Ten isty program, kym este hra, Kodi znova neotvori,
        preto najprv stop.

        Na isPlaying() sa cakat neda: prehravac hlasi koniec hned na zaciatku
        CloseFile, ale PVR program pusti z hlavy az po jeho dobehnuti. Kym ho
        ma za hrajuci, Player.Open vrati OK a neurobi nic (PlayEpgTag len
        prepne na celu obrazovku). 19. 10. 2026 sa zatvaranie pri zamrznutom
        CDN tiahlo 4 s a obnova skoncila v menu. Preto cakame aj na PVR
        a po Open overime, ze sa naozaj nieco otvorilo.

        Ked sa stary stream nezavrie ani do 15 s, Open by nic nespravil
        a kontrola isPlaying() by sa chytila toho stareho - vtedy rovno
        adresa doplnku, ta ho nahradi aj pocas zatvarania."""
        player.stop()
        for _ in range(30):
            if self.monitor.waitForAbort(0.5):
                return True
            if not (player.isPlaying()
                    or xbmc.getCondVisibility("PVR.IsPlayingEpgTag")):
                break
        else:
            log("obnova cez EPG: starý program sa nezavrel do 15 s, "
                "púšťam adresu doplnku", True)
            return False
        for attempt in (1, 2):
            try:
                rpc("Player.Open", item={"broadcastid": bid})
            except Exception as e:
                log("obnova cez EPG zlyhala (%s), púšťam adresu doplnku" % e, True)
                return False
            for _ in range(10):
                if self.monitor.waitForAbort(0.5):
                    return True
                if player.isPlaying():
                    return True
            log("obnova cez EPG: Player.Open nič nepustil (pokus %d)" % attempt, True)
        log("púšťam adresu doplnku", True)
        return False


# ---------- pokracovanie na dalsi program ----------
# Program pusteny z EPG, kym este bezal, dostane od CDN live manifest. Po jeho
# skonceni CDN manifest prepne na static, ale ISA 21 na to nereaguje - dalej
# ho kazde 2 s obnovuje a caka na segmenty, ktore uz nepridu. Prehravanie
# preto nikdy neskonci, Kodi nedostane koniec suboru a jeho "prehrat dalsi
# program" (pvrplayback.autoplaynextprogramme) sa nespusti. Obraz stoji
# 3 s pred koncom (overene 14. 10. 2026: 54:57 z 55:00 este 25 min po konci).
# Zaseknutie na konci preto spoznavame sami a dalsi program pustame cez EPG.
END_MARGIN = 15     # s do konca, odkedy sa stojaci obraz povazuje za koniec
END_STALL = 10      # s bez pohybu pozicie


def rpc(method, **params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        req["params"] = params
    res = json.loads(xbmc.executeJSONRPC(json.dumps(req)))
    if "error" in res:
        raise RuntimeError("%s: %s" % (method, res["error"]))
    return res["result"]


class EndWatch(threading.Thread):

    def __init__(self, monitor):
        threading.Thread.__init__(self, daemon=True)
        self.monitor = monitor
        self.last_pos = -1
        self.still_since = 0
        self.done = None     # broadcastid, pre ktory sme uz zasiahli

    def run(self):
        while not self.monitor.abortRequested():
            if self.monitor.waitForAbort(2):
                break
            try:
                self.tick()
            except Exception as e:
                log("koniec programu: %s" % e, True)
                self.still_since = 0

    def tick(self):
        if not (xbmcgui.Window(10000).getProperty(KEEPALIVE_PROP)
                and xbmc.getCondVisibility("PVR.IsPlayingEpgTag")
                and not xbmc.getCondVisibility("Player.Paused")):
            self.still_since = 0
            return
        player = xbmc.Player()
        pos, total = player.getTime(), player.getTotalTime()
        if total <= 0 or total - pos > END_MARGIN or abs(pos - self.last_pos) > 0.5:
            self.last_pos = pos
            self.still_since = 0
            return
        now = time.time()
        if not self.still_since:
            self.still_since = now
        if now - self.still_since < END_STALL:
            return
        bid = rpc("Player.GetItem", playerid=1)["item"].get("id")
        if not bid or bid == self.done:
            return
        self.done = bid
        self.play_next(bid)

    def play_next(self, bid):
        cur = rpc("PVR.GetBroadcastDetails", broadcastid=bid,
                  properties=["title", "endtime"])["broadcastdetails"]
        utcnow = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        if cur["endtime"] > utcnow:
            # program este bezi a stojime na zivom okraji - to nie je koniec
            self.done = None
            self.still_since = 0
            return
        broadcasts = self.channel_broadcasts(bid)
        if broadcasts is None:
            log("koniec programu: nenašiel som kanál k %s" % cur["title"], True)
            return
        later = [b for b in broadcasts if b["starttime"] >= cur["endtime"]]
        nxt = min(later, key=lambda b: b["starttime"]) if later else None
        if not nxt or nxt["starttime"] > utcnow:
            log("koniec programu %s, ďalší ešte nezačal — zastavujem" % cur["title"])
            xbmc.Player().stop()
            return
        log("koniec programu %s, púšťam %s" % (cur["title"], nxt["label"]))
        rpc("Player.Open", item={"broadcastid": nxt["broadcastid"]})

    @staticmethod
    def channel_broadcasts(bid):
        # vysielanie samo kanal neprezradi, tak ho hladame podla mena
        # a overime, ze program naozaj patri do jeho EPG
        name = xbmc.getInfoLabel("VideoPlayer.ChannelName")
        for ch in rpc("PVR.GetChannels", channelgroupid="alltv")["channels"]:
            if ch["label"] != name:
                continue
            bc = rpc("PVR.GetBroadcasts", channelid=ch["channelid"],
                     properties=["starttime", "endtime"])["broadcasts"]
            if any(b["broadcastid"] == bid for b in bc):
                return bc
        return None


# ---------- automaticke prihlasenie cez SSO cookie ----------
# Refresh token umiera so svojou KS (~7 dni, 500017). Ked je v profile
# sso.json (import cez nastavenia), doplnok si kod zariadenia schvali sam
# (api.auto_login). Poistky proti zahlteniu servera O2:
SSO_FILE = "sso.json"
SSO_PROP = "o2tv.sso_changed"    # nastavi addon.py po importe/zmazani cookie
# addon.py (prehravanie, zoznam kanalov, import) sa prihlasi hned a oznami to
# tymito window property; AUTO_BUSY = prave bezi pokus (cas jeho zaciatku)
AUTO_BUSY = "o2tv.auto_busy"
AUTO_DONE = "o2tv.auto_done"
AUTO_BUSY_MAX = 150              # s, potom sa zamok povazuje za zabudnuty
AUTO_LEFT = 2 * 86400            # skusat, ked do konca prihlasenia ostava <= 2 dni
AUTO_FAIL_WAIT = 3600            # po neuspechu najblizsi pokus o 1 h
AUTO_OK_WAIT = 6 * 3600          # po uspechu nie skor ako o 6 h
AUTO_MAX_FAILS = 3               # tolko neuspechov po sebe, potom pauza do restartu


class AutoLogin:
    def __init__(self, api, path, monitor):
        self.api, self.path, self.monitor = api, path, monitor
        self.next_try = 0
        self.fails = 0
        self.paused = False
        self.dead = False        # refresh vratil 500017 (nastavuje tick)
        self.failed_now = False  # posledne run() sa pokusilo a zlyhalo
        self.seen_prop = xbmcgui.Window(10000).getProperty(SSO_PROP)
        self.seen_done = xbmcgui.Window(10000).getProperty(AUTO_DONE)

    def can_try(self):
        """Smie sa teraz pokusit (subor existuje, poistky to dovoluju)?"""
        self._check_prop()
        return (not self.paused and time.time() >= self.next_try
                and os.path.isfile(self.path) and not self._busy())

    @staticmethod
    def _busy():
        """Iny pokus (addon.py) prave bezi - druhy naraz by len zataazil O2."""
        try:
            v = float(xbmcgui.Window(10000).getProperty(AUTO_BUSY) or 0)
        except ValueError:
            return False
        return time.time() - v < AUTO_BUSY_MAX

    def _check_prop(self):
        # novy import cookie = cisty stol (pauza po 3 neuspechoch by inak
        # trvala az do restartu Kodi)
        cur = xbmcgui.Window(10000).getProperty(SSO_PROP)
        if cur != self.seen_prop:
            self.seen_prop = cur
            self.fails, self.paused, self.next_try = 0, False, 0
            log("automatické prihlásenie: cookie sa zmenilo, poistky vynulované")
        # relaciu medzitym obnovil addon.py (prehravanie/import): mrtvy refresh
        # uz neplati a dalsi pokus nie je potrebny skor nez po AUTO_OK_WAIT
        done = xbmcgui.Window(10000).getProperty(AUTO_DONE)
        if done != self.seen_done:
            self.seen_done = done
            self.dead, self.fails, self.paused = False, 0, False
            self.api.refresh_dead = False
            self.next_try = time.time() + AUTO_OK_WAIT
            log("automatické prihlásenie: relácia už bola obnovená z doplnku")

    def run(self):
        """Skusi prihlasenie, ak je potrebne a smie sa. True = prihlaseny."""
        self.failed_now = False
        if not self.can_try():
            return False
        left, _ = self.api.session_left()
        if not (self.dead or left <= AUTO_LEFT):
            return False
        why = "refresh vrátil 500017" if self.dead else \
            "do konca prihlásenia ostáva %s" % human(left)
        log("automatické prihlásenie: pokus %d/%d (%s)"
            % (self.fails + 1, AUTO_MAX_FAILS, why))
        win = xbmcgui.Window(10000)
        win.setProperty(AUTO_BUSY, str(time.time()))
        try:
            self.api.auto_login(self.path, sleep=self.monitor.waitForAbort,
                                should_stop=self.monitor.abortRequested)
        except O2Error as e:
            if e.code == "ABORT":
                return False
            self._failed("%s (%s)" % (e, e.code or "-"))
            return False
        except Exception as e:
            # sprava vynimky sa nelogue - mohla by obsahovat cokolvek
            self._failed("neočakávaná chyba %s" % type(e).__name__)
            return False
        finally:
            win.clearProperty(AUTO_BUSY)
        self.fails, self.dead = 0, False
        self.next_try = time.time() + AUTO_OK_WAIT
        log("automatické prihlásenie: úspech")
        xbmcgui.Dialog().notification(
            "O2 TV", "Prihlásenie obnovené automaticky",
            xbmcgui.NOTIFICATION_INFO, 5000)
        return True

    def _failed(self, reason):
        self.fails += 1
        self.failed_now = True
        self.next_try = time.time() + AUTO_FAIL_WAIT
        if self.fails >= AUTO_MAX_FAILS:
            self.paused = True
            log("automatické prihlásenie zlyhalo (%s); %d neúspechov po sebe — "
                "pauza do reštartu Kodi, treba ručné prihlásenie"
                % (reason, self.fails), True)
        else:
            log("automatické prihlásenie zlyhalo (%s); ďalší pokus najskôr o 1 h"
                % reason, True)


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
    auto = AutoLogin(api, os.path.join(profile, SSO_FILE), monitor)
    KeepAlive(monitor).start()
    EndWatch(monitor).start()
    last_export = 0
    last_warn = 0
    first = True
    while not monitor.abortRequested():
        # nic nesmie ukoncit tuto slucku - bez nej sa relacia do restartu
        # Kodi neobnovi a po par dnoch prestane hrat vsetko
        try:
            ok = tick(api, auto)
        except Exception as e:
            log("údržba relácie spadla: %r" % e, True)
            ok = False
        # automaticke prihlasenie (pri starte aj v slucke): ked ostavaju
        # <= 2 dni alebo refresh vratil 500017 a existuje sso.json
        try:
            if auto.run():
                ok = tick(api, auto)
            elif auto.dead and auto.failed_now:
                dead_notice()    # pokus zlyhal - zostava rucne prihlasenie
        except Exception as e:
            log("automatické prihlásenie spadlo: %s" % type(e).__name__, True)
        # pri starte odpocet vzdy, potom uz len ked je naliehavy - a aj
        # vtedy nanajvys raz za WARN_EVERY, nech toasty nezavadzaju
        if first or time.time() - last_warn > WARN_EVERY:
            try:
                if session_notice(api, ok, force=first):
                    last_warn = time.time()
            except Exception as e:
                log("odpočet zlyhal: %r" % e, True)
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
                    # dalsi pokus o EXPORT_RETRY, nie o 10 min - export je
                    # stovka volani API a chyba v datach sa sama neopravi
                    log("export zlyhal: %s" % e, True)
                    last_export = time.time() - every + EXPORT_RETRY
        if sleep_abortable(monitor, 600):
            break
    log("service koniec")


if __name__ == "__main__":
    main()
