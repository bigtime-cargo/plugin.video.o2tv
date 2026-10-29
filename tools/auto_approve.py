#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Skuska: schvali kod zariadenia O2 sam, cez SSO cookie z prehliadaca.

Robi presne to, co pri prihlaseni klikas rukou na o2.sk/zariadenie:
kod -> "Ste prihlaseny ako ..." (Pokracovat) -> suhlas (Povolit) -> hotovo.
Schvaluje len kod, ktory si sam vyziadal (device flow klienta o2-xtv-kaltura),
nie lubovolny. Access token na konci len overi (vypise, ci ma claim
authenticated_via_subscriber_id, bez ktoreho ho Kaltura odmieta) a zahodi.

Bezpecnost:
  - cookie su kluc k O2 uctu. Ulozia sa len do suboru s pravami 600
    (O2TV_SSO, predvolene ~/.config/kontrola-TCL/o2-sso.json), nikdy do gitu.
  - klikne len na tlacidla "Pokračovať" a "Povoliť". Ak stranka chce nieco
    ine (heslo, cislo sluzby, Turnstile/CAPTCHA), skript sa ZASTAVI. Nic sa
    neobchadza.
  - hodnoty cookie sa nikdy nevypisuju.

    python3 tools/auto_approve.py --uloz      # skryte zada 2 cookie (getpass)
    python3 tools/auto_approve.py             # skusi schvalit novy kod

Cookie: DevTools > Application > Cookies > https://identity.o2.sk
        KEYCLOAK_IDENTITY a KEYCLOAK_SESSION (stlpec Value).
"""
import base64, getpass, html.parser, http.cookiejar, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

HOST = "identity.o2.sk"
KC = "https://%s/realms/o2/protocol/openid-connect" % HOST
CLIENT_ID = "o2-xtv-kaltura"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
COOKIES = ("KEYCLOAK_IDENTITY", "KEYCLOAK_SESSION")
FILE = os.environ.get("O2TV_SSO") or os.path.expanduser(
    "~/.config/kontrola-TCL/o2-sso.json")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
OK_BUTTONS = ("pokračovať", "povoliť")   # presne tieto, nikdy "iným účtom"
USER_CODE_FIELDS = ("device_user_code", "user_code")
MAX_STEPS = 8
WIDGET = re.compile(r'cf-turnstile|data-sitekey|turnstile\.render|g-recaptcha|h-captcha'
                    r'|<o2-turnstile|<o2-captcha', re.I)


class Stop(Exception):
    pass


class Page(html.parser.HTMLParser):
    def __init__(self, url, raw):
        super().__init__(convert_charrefs=True)
        self.url, self.raw = url, raw
        self.forms, self.cur, self.btn, self.skip, self.chunks = [], None, None, 0, []
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("form", "o2-form"):   # O2 pouziva vlastny komponent o2-form
            self.cur = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(),
                        "hidden": {}, "fields": [], "buttons": []}
            self.forms.append(self.cur)
        elif tag == "input" and self.cur is not None:
            t, n = (a.get("type") or "text").lower(), a.get("name")
            if t == "hidden" and n:
                self.cur["hidden"][n] = a.get("value") or ""
            elif t == "submit":
                self.cur["buttons"].append({"name": n, "value": a.get("value") or "",
                                            "text": a.get("value") or ""})
            elif t not in ("checkbox", "radio", "button", "image", "reset"):
                self.cur["fields"].append((n or "", t))
        elif tag in ("button", "o2-button") and self.cur is not None:
            # o2-button typu "button" (suhlas) odosiela JS stranky natívnym POST
            # na action o2-form; tlacidlo sa potom vyberie len podla popisku
            if tag == "o2-button" or (a.get("type") or "submit").lower() == "submit":
                self.btn = {"name": a.get("name"), "value": a.get("value") or "", "text": ""}
                self.cur["buttons"].append(self.btn)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("button", "o2-button"):
            self.btn = None
        elif tag in ("form", "o2-form"):
            self.cur = None

    def handle_data(self, d):
        if self.skip:
            return
        if self.btn is not None:
            self.btn["text"] += d
        self.chunks.append(d)

    @property
    def text(self):
        return " ".join(" ".join(self.chunks).split())


def jwt_claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))


def uloz():
    vals = {}
    for n in COOKIES:
        vals[n] = getpass.getpass("%s (hodnota sa nezobrazuje): " % n).strip()
        if not vals[n]:
            sys.exit("Prázdna hodnota, nič sa neuložilo.")
    os.makedirs(os.path.dirname(FILE), mode=0o700, exist_ok=True)
    fd = os.open(FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(vals, f)
    print("Uložené do %s (práva 600)." % FILE)


def nacitaj():
    try:
        if os.stat(FILE).st_mode & 0o077:
            sys.exit("Súbor %s je čitateľný pre iných, oprav: chmod 600" % FILE)
        vals = json.load(open(FILE))
    except FileNotFoundError:
        sys.exit("Chýba %s — najprv: python3 tools/auto_approve.py --uloz" % FILE)
    jar = http.cookiejar.CookieJar()
    for n in COOKIES:
        jar.set_cookie(http.cookiejar.Cookie(
            0, n, vals[n], None, False, HOST, True, False, "/realms/o2/", True,
            True, None, False, None, None, {}))
    return jar, vals


def fetch(op, url, data=None):
    host = urllib.parse.urlparse(url).hostname
    if host != HOST:
        raise Stop("odkaz mimo %s (%s) — zastavujem" % (HOST, host))
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with op.open(req, timeout=20) as r:
            return Page(r.geturl(), r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise Stop("HTTP %d na %s" % (e.code, urllib.parse.urlparse(url).path))


def kc(path, form):
    data = urllib.parse.urlencode(dict(form, client_id=CLIENT_ID)).encode()
    req = urllib.request.Request(KC + path, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def popis(page):
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<e-mail>", page.text)
    return "%s  |  %s" % (urllib.parse.urlparse(page.url).path, t[:300])


def schval(op, user_code):
    page = fetch(op, "https://%s/realms/o2/device?user_code=%s"
                 % (HOST, urllib.parse.quote(user_code)))
    for krok in range(1, MAX_STEPS + 1):
        # skript turnstile/api.js je v hlavicke kazdej stranky O2; zastavit treba
        # az pri skutocnom widgete (ten by chcel vysledok vyzvy, ktory nemame)
        if WIDGET.search(page.raw):
            raise Stop("stránka má ochranu proti automatizácii (Turnstile/CAPTCHA) — "
                       "tú neobchádzam. " + popis(page))
        vyber = None
        for f in page.forms:
            for b in f["buttons"]:
                if " ".join(b["text"].split()).lower() in OK_BUTTONS:
                    vyber = (f, b)
                    break
            if vyber:
                break
        if not vyber:
            print("  krok %d: žiadne tlačidlo na kliknutie, koniec tejto časti" % krok)
            return page
        f, b = vyber
        data = dict(f["hidden"])
        for n, t in f["fields"]:
            if n in USER_CODE_FIELDS:
                data[n] = user_code
            else:
                raise Stop("formulár chce pole '%s' (%s) — zastavujem. %s" % (n, t, popis(page)))
        if b["name"]:
            data[b["name"]] = b["value"]
        label = " ".join(b["text"].split()) or b["value"]
        print("  krok %d: %s  →  '%s'" % (krok, urllib.parse.urlparse(page.url).path, label))
        action = urllib.parse.urljoin(page.url, f["action"])
        if f["method"] == "post":
            page = fetch(op, action, urllib.parse.urlencode(data).encode())
        else:
            page = fetch(op, action + ("&" if "?" in action else "?") + urllib.parse.urlencode(data))
    raise Stop("príliš veľa krokov — zastavujem. " + popis(page))


def main():
    if "--uloz" in sys.argv:
        return uloz()
    jar, vals = nacitaj()
    ident = jwt_claims(vals["KEYCLOAK_IDENTITY"])
    if ident.get("exp"):
        print("KEYCLOAK_IDENTITY: exp %s%s" % (stamp(ident["exp"]),
              ", vydané %s" % stamp(ident["iat"]) if ident.get("iat") else ""))
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    dev = kc("/auth/device", {"scope": "openid"})
    if not dev.get("device_code"):
        sys.exit("O2 nevydalo kód zariadenia: %s" % dev)
    print("Kód zariadenia vyžiadaný, schvaľujem…")
    try:
        last = schval(op, dev["user_code"])
        print("  posledná stránka: " + popis(last))
    except Stop as e:
        sys.exit("ZASTAVENÉ: %s" % e)
    token, t0 = None, time.time()
    while time.time() - t0 < 30:
        res = kc("/token", {"grant_type": DEVICE_GRANT, "device_code": dev["device_code"]})
        if res.get("access_token"):
            token = res
            break
        if res.get("error") not in ("authorization_pending", "slow_down"):
            sys.exit("Token nevydaný: %s" % res.get("error_description") or res.get("error"))
        time.sleep(dev.get("interval", 5))
    if not token:
        sys.exit("Do 30 s neprišiel token — kód asi nebol schválený.")
    c = jwt_claims(token["access_token"])
    print("TOKEN VYDANÝ bez človeka.")
    print("  authenticated_via = %s" % c.get("authenticated_via"))
    print("  authenticated_via_subscriber_id: %s"
          % ("je" if c.get("authenticated_via_subscriber_id") else "CHÝBA (Kaltura ho odmietne)"))
    print("  expires_in=%s  refresh_expires_in=%s" % (token.get("expires_in"), token.get("refresh_expires_in")))
    novy = {c_.name: c_ for c_ in jar if c_.name in COOKIES}
    zmena = [n for n in COOKIES if novy[n].value != vals[n]]
    if zmena:
        print("Server cookie obnovil (%s) — ukladám nové hodnoty (kĺzavá platnosť?)." % ", ".join(zmena))
        for n in zmena:
            vals[n] = novy[n].value
        fd = os.open(FILE, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(vals, f)
    for n in COOKIES:
        if novy[n].expires:
            print("  %s: nové Expires %s" % (n, stamp(novy[n].expires)))
    if not zmena:
        print("Cookie ostali rovnaké (server ich nepredĺžil).")


if __name__ == "__main__":
    main()
