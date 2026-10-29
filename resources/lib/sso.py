# -*- coding: utf-8 -*-
"""Automaticke schvalenie kodu zariadenia O2 cez SSO cookie (bez Kodi importov).

Robi presne to, co pri prihlaseni klikas rukou na o2.sk/zariadenie:
kod -> "Ste prihlaseny ako ..." (Pokracovat) -> suhlas (Povolit) -> hotovo.
Schvaluje len kod, ktory si doplnok prave sam vyziadal (device flow klienta
o2-xtv-kaltura), nie lubovolny.

BEZPECNOST
  - cookie KEYCLOAK_IDENTITY a KEYCLOAK_SESSION su kluc k celemu O2 uctu.
    Nikdy sa nelogu, nedavaju do vynimiek ani do toastov, do gitu nepatria.
  - klikne sa LEN na tlacidla s popiskom presne "Pokračovať" a "Povoliť"
    (nikdy "Pokračovať s iným účtom", "Zamietnuť").
  - zastavi sa (SsoError) pri neznamom poli formulara, pri skutocnom widgete
    Turnstile/CAPTCHA (tu sa NIC neobchadza), pri odkaze na iny host nez
    identity.o2.sk a po viac ako MAX_STEPS krokoch.
"""
import base64, html.parser, http.cookiejar, json, os, re, tempfile, time
import urllib.error, urllib.parse, urllib.request

HOST = "identity.o2.sk"
COOKIES = ("KEYCLOAK_IDENTITY", "KEYCLOAK_SESSION")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
OK_BUTTONS = ("pokračovať", "povoliť")   # presne tieto, nikdy "iným účtom"
USER_CODE_FIELDS = ("device_user_code", "user_code")
MAX_STEPS = 8
TIMEOUT = 15            # s na jednu poziadavku
TOTAL_BUDGET = 90       # s na celé schvalenie
MAX_READ = 2 * 1024 * 1024
# skript turnstile/api.js je v hlavicke kazdej stranky O2 a NIE JE widget;
# zastavit treba az pri skutocnom widgete (ten by chcel vysledok vyzvy)
WIDGET = re.compile(r'cf-turnstile|data-sitekey|turnstile\.render|g-recaptcha'
                    r'|h-captcha|<o2-turnstile|<o2-captcha', re.I)
_USER_CODE = re.compile(r"^[A-Za-z0-9-]{4,24}$")
# hodnota cookie: tlacitelne ASCII bez medzery, '"', ',' a ';'
_VALUE_OK = re.compile(r"^[\x21\x23-\x2b\x2d-\x3a\x3c-\x7e]+$")


class SsoError(Exception):
    """Zrozumitelna slovenska sprava, bez tajomstiev. `cookies` drzi
    prip. obnovene cookie, ktore sa maju ulozit aj napriek zlyhaniu."""

    def __init__(self, msg, cookies=None):
        Exception.__init__(self, msg)
        self.cookies = cookies


# ---------- cookie: validacia, nacitanie, ulozenie ----------
def validate(vals):
    """Vrati cisty dict len s COOKIES, alebo vyhodi SsoError. Hodnoty sa do
    spravy nikdy nedavaju, len nazvy."""
    if not isinstance(vals, dict):
        raise SsoError("SSO cookie musí byť JSON objekt s kľúčmi %s." % " a ".join(COOKIES))
    out = {}
    for n in COOKIES:
        v = vals.get(n)
        if not isinstance(v, str) or not v.strip():
            raise SsoError("V SSO cookie chýba %s." % n)
        v = v.strip()
        if len(v) > 8192 or not _VALUE_OK.match(v):
            raise SsoError("SSO cookie %s má neplatný formát." % n)
        out[n] = v
    return out


def load(path):
    """Nacita a overi cookie zo suboru; None, ked subor chyba alebo je zly."""
    try:
        with open(path, encoding="utf-8") as f:
            return validate(json.load(f))
    except (OSError, ValueError, SsoError):
        return None


def save(path, vals):
    """Atomicky zapis (docasny subor + os.replace), prava 600 ak sa da.
    Vracia True/False, nikdy nevyhodi vynimku."""
    tmp = None
    try:
        vals = validate(vals)
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".sso-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(vals, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def jwt_claims(tok):
    try:
        p = tok.split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def identity_exp(vals):
    """exp z JWT v KEYCLOAK_IDENTITY (epoch sekundy), alebo None."""
    try:
        exp = jwt_claims(vals["KEYCLOAK_IDENTITY"]).get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


# ---------- HTTP ----------
def _cookie(name, value):
    return http.cookiejar.Cookie(
        0, name, value, None, False, HOST, True, False, "/realms/o2/", True,
        True, None, False, None, None, {})


def _make_jar(vals):
    jar = http.cookiejar.CookieJar()
    for n in COOKIES:
        jar.set_cookie(_cookie(n, vals[n]))
    return jar


def _collect(jar, vals):
    """Aktualne cookie z jaru; co server nevratil (alebo zmazal), ostane."""
    out = dict(vals)
    for c in jar:
        if c.name in COOKIES and c.value and _VALUE_OK.match(c.value):
            out[c.name] = c.value
    return out


def _check_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.hostname != HOST:
        raise SsoError("odkaz mimo %s (%s) — zastavujem" % (HOST, (p.hostname or "?")[:60]))


class _HostGuard(urllib.request.HTTPRedirectHandler):
    """Presmerovanie mimo identity.o2.sk (alebo na http) sa nenasleduje."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def _default_opener(jar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                       _HostGuard())


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


def popis(page):
    """Kratky popis stranky do spravy o chybe: cesta + text bez e-mailov
    a dlhych cisel (telefon). Cookie ani tokeny tu nikdy nie su."""
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<e-mail>", page.text)
    t = re.sub(r"\d{6,}", "<č>", t)
    return "%s  |  %s" % (urllib.parse.urlparse(page.url).path, t[:160])


def _fetch(op, url, data=None):
    _check_url(url)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with op.open(req, timeout=TIMEOUT) as r:
            final = r.geturl()
            _check_url(final)
            return Page(final, r.read(MAX_READ).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        e.close()
        raise SsoError("HTTP %d na %s" % (e.code, urllib.parse.urlparse(url).path))
    except SsoError:
        raise
    except OSError as e:
        raise SsoError("sieť: %s" % (getattr(e, "reason", None) or type(e).__name__))


def _pick(page):
    for f in page.forms:
        for b in f["buttons"]:
            if " ".join(b["text"].split()).lower() in OK_BUTTONS:
                return f, b
    return None


def _walk(op, user_code, progress):
    t0 = time.time()
    page = _fetch(op, "https://%s/realms/o2/device?user_code=%s"
                  % (HOST, urllib.parse.quote(user_code)))
    clicks = 0
    for krok in range(1, MAX_STEPS + 1):
        if time.time() - t0 > TOTAL_BUDGET:
            raise SsoError("schvaľovanie trvá príliš dlho — zastavujem")
        if WIDGET.search(page.raw):
            raise SsoError("stránka má ochranu proti automatizácii (Turnstile/CAPTCHA) — "
                           "tú neobchádzam. " + popis(page))
        vyber = _pick(page)
        if not vyber:
            if not clicks:
                raise SsoError("na stránke nie je tlačidlo Pokračovať/Povoliť (SSO prihlásenie "
                               "asi vypršalo alebo kód neplatí). " + popis(page))
            return
        f, b = vyber
        data = dict(f["hidden"])
        for n, t in f["fields"]:
            if n in USER_CODE_FIELDS:
                data[n] = user_code
            else:
                raise SsoError("formulár chce pole '%s' (%s) — zastavujem. %s"
                               % (n[:40], t, popis(page)))
        if b["name"]:
            data[b["name"]] = b["value"]
        label = " ".join(b["text"].split()) or b["value"]
        if progress:
            progress("krok %d: %s → '%s'"
                     % (krok, urllib.parse.urlparse(page.url).path, label))
        action = urllib.parse.urljoin(page.url, f["action"])
        clicks += 1
        if f["method"] == "post":
            page = _fetch(op, action, urllib.parse.urlencode(data).encode())
        else:
            page = _fetch(op, action + ("&" if "?" in action else "?")
                          + urllib.parse.urlencode(data))
    raise SsoError("príliš veľa krokov — zastavujem. " + popis(page))


def approve(user_code, cookies, opener_factory=None, progress=None):
    """Schvali kod zariadenia `user_code` (ktory si volajuci prave vyziadal)
    pod SSO prihlasenim z `cookies`. Vracia dict (obnovenych) cookie, ktory
    treba ulozit. `opener_factory(jar)` vracia objekt s open(req, timeout=)
    (v testoch falosny). `progress(text)` dostava bezpecne popisy krokov.
    Pri chybe vyhodi SsoError; jeho .cookies drzi cookie na ulozenie."""
    code = str(user_code or "").strip()
    if not _USER_CODE.match(code):
        raise SsoError("Neplatný kód zariadenia.")
    vals = validate(cookies)
    jar = _make_jar(vals)
    try:
        op = (opener_factory or _default_opener)(jar)
        _walk(op, code, progress)
    except SsoError as e:
        e.cookies = _collect(jar, vals)
        raise
    except Exception as e:
        # nic neznameho nesmie preniest hodnoty ani cele spravy ďalej
        raise SsoError("neočakávaná chyba pri schvaľovaní (%s)" % type(e).__name__,
                       _collect(jar, vals))
    return _collect(jar, vals)
