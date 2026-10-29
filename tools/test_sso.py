# -*- coding: utf-8 -*-
"""Offline testy resources/lib/sso.py a O2API.auto_login (bez siete, bez Kodi).

    cd ~/plugin.video.o2tv && python3 -m unittest tools.test_sso -v
"""
import json, os, stat, sys, tempfile, unittest
import urllib.error, urllib.parse, urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "resources", "lib"))
import sso            # noqa: E402
import api as api_mod   # noqa: E402

SECRET_ID = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9.SECRETSIGNATUREAAA"
SECRET_SES = "o2/SECRET-SESSION-uuid/SECRET-user-uuid"
COOKIES = {"KEYCLOAK_IDENTITY": SECRET_ID, "KEYCLOAK_SESSION": SECRET_SES}
CODE = "ABCD-EFGH"
BASE = "https://identity.o2.sk"
DEVICE_URL = BASE + "/realms/o2/device?user_code=" + CODE
CONT_URL = BASE + "/realms/o2/device/continue?sc=1"
CONSENT_URL = BASE + "/realms/o2/login-actions/consent?sc=2"

HEAD = ('<html><head><script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
        '</script><script>var x = 1;</script></head><body>')

# "Ste prihlaseny ako ..." - Pokracovat + vedlajsie "Pokracovat s inym uctom"
PAGE_CONTINUE = HEAD + '''
<p>Ste prihlásený ako jan.novak@example.sk</p>
<o2-form action="/realms/o2/device/continue?sc=1" method="post">
  <input type="hidden" name="session_code" value="SC1">
  <button type="submit" name="continue" value="yes">Pokračovať</button>
  <button type="submit" name="other" value="yes">Pokračovať s iným účtom</button>
</o2-form></body></html>'''

# variant: "iny ucet" vo vlastnom formulari, pred hlavnym
PAGE_CONTINUE_SPLIT = HEAD + '''
<form action="/realms/o2/device/other" method="post">
  <input type="hidden" name="session_code" value="SCX">
  <input type="submit" name="other" value="Pokračovať s iným účtom">
</form>
<form action="/realms/o2/device/continue?sc=1" method="post">
  <input type="hidden" name="session_code" value="SC1">
  <input type="submit" name="continue" value="Pokračovať">
</form></body></html>'''

# suhlas: o2-button type=button
PAGE_CONSENT = HEAD + '''
<o2-form action="/realms/o2/login-actions/consent?sc=2" method="post">
  <input type="hidden" name="code" value="CODE2">
  <o2-button type="button" name="accept" value="Yes">Povoliť</o2-button>
  <o2-button type="button" name="cancel" value="No">Zamietnuť</o2-button>
</o2-form></body></html>'''

PAGE_DONE = HEAD + "<p>Prihlásenie bolo úspešné. Môžete zavrieť toto okno.</p></body></html>"

PAGE_WIDGET = HEAD + '''<div class="cf-turnstile" data-sitekey="0x4AAA"></div>
<o2-form action="/realms/o2/device/continue?sc=1" method="post">
  <button type="submit" name="continue" value="yes">Pokračovať</button>
</o2-form></body></html>'''

PAGE_UNKNOWN_FIELD = HEAD + '''
<o2-form action="/realms/o2/device/continue?sc=1" method="post">
  <input type="text" name="phone_number">
  <button type="submit" name="continue" value="yes">Pokračovať</button>
</o2-form></body></html>'''

PAGE_USER_CODE_FIELD = HEAD + '''
<form action="/realms/o2/device/continue?sc=1" method="post">
  <input type="text" name="device_user_code">
  <button type="submit" name="continue" value="yes">Pokračovať</button>
</form></body></html>'''

PAGE_FOREIGN = HEAD + '''
<o2-form action="https://evil.example.com/steal" method="post">
  <input type="hidden" name="session_code" value="SC1">
  <button type="submit" name="continue" value="yes">Pokračovať</button>
</o2-form></body></html>'''

PAGE_ONLY_BAD = HEAD + '''
<o2-form action="/realms/o2/device/continue?sc=1" method="post">
  <button type="submit" name="other" value="yes">Pokračovať s iným účtom</button>
  <o2-button type="button" name="cancel" value="No">Zamietnuť</o2-button>
</o2-form></body></html>'''

PAGE_LOOP = PAGE_CONTINUE  # po kliknuti vrati tu istu stranku


class FakeResp:
    def __init__(self, url, body):
        self._url, self._body = url, body.encode("utf-8")

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """routes: {(metoda, url): html | (final_url, html) | Exception}"""

    def __init__(self, routes, jar=None, rotate=None):
        self.routes, self.jar, self.rotate = routes, jar, rotate
        self.requests = []   # (metoda, url, dict(telo))

    def open(self, req, timeout=None):
        method = "POST" if req.data is not None else "GET"
        body = dict(urllib.parse.parse_qsl(req.data.decode())) if req.data else {}
        self.requests.append((method, req.full_url, body))
        if self.rotate and self.jar is not None and len(self.requests) == 1:
            # server pri pouziti obnovi cookie (Set-Cookie)
            self.jar.set_cookie(sso._cookie("KEYCLOAK_IDENTITY", self.rotate))
        r = self.routes.get((method, req.full_url))
        if r is None:
            raise AssertionError("neočakávaná požiadavka %s %s" % (method, req.full_url))
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):
            return FakeResp(*r)
        return FakeResp(req.full_url, r)

    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]


def run_approve(routes, cookies=COOKIES, rotate=None, code=CODE):
    holder = {}

    def factory(jar):
        holder["op"] = FakeOpener(routes, jar, rotate)
        return holder["op"]
    try:
        return sso.approve(code, cookies, opener_factory=factory), holder["op"]
    except sso.SsoError as e:
        e.op = holder.get("op")
        raise


GOOD_ROUTES = {
    ("GET", DEVICE_URL): PAGE_CONTINUE,
    ("POST", CONT_URL): PAGE_CONSENT,
    ("POST", CONSENT_URL): PAGE_DONE,
}


class ParserTests(unittest.TestCase):
    def test_continue_page_buttons(self):
        p = sso.Page(DEVICE_URL, PAGE_CONTINUE)
        self.assertEqual(len(p.forms), 1)
        f = p.forms[0]
        self.assertEqual(f["method"], "post")
        self.assertEqual(f["hidden"], {"session_code": "SC1"})
        self.assertEqual([" ".join(b["text"].split()) for b in f["buttons"]],
                         ["Pokračovať", "Pokračovať s iným účtom"])
        f, b = sso._pick(p)
        self.assertEqual(b["name"], "continue")

    def test_pick_never_other_account(self):
        p = sso.Page(DEVICE_URL, PAGE_CONTINUE_SPLIT)
        f, b = sso._pick(p)
        self.assertEqual(b["name"], "continue")
        self.assertIsNone(sso._pick(sso.Page(DEVICE_URL, PAGE_ONLY_BAD)))

    def test_consent_page(self):
        p = sso.Page(CONT_URL, PAGE_CONSENT)
        f, b = sso._pick(p)
        self.assertEqual((b["name"], b["value"]), ("accept", "Yes"))
        self.assertEqual(f["hidden"], {"code": "CODE2"})
        texts = [b["text"] for b in f["buttons"]]
        self.assertIn("Zamietnuť", texts)

    def test_widget_regex(self):
        self.assertTrue(sso.WIDGET.search(PAGE_WIDGET))
        self.assertTrue(sso.WIDGET.search('<div class="g-recaptcha"></div>'))
        self.assertTrue(sso.WIDGET.search("<o2-turnstile></o2-turnstile>"))
        # samotny skript z hlavicky nie je widget
        self.assertFalse(sso.WIDGET.search(PAGE_CONTINUE))
        self.assertFalse(sso.WIDGET.search(
            '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'))


class ApproveTests(unittest.TestCase):
    def test_happy_path_exact_posts(self):
        new, op = run_approve(GOOD_ROUTES)
        self.assertEqual(new, COOKIES)
        self.assertEqual(op.requests[0][:2], ("GET", DEVICE_URL))
        posts = op.posts()
        self.assertEqual([p[1] for p in posts], [CONT_URL, CONSENT_URL])
        # 1. krok: skryte pole + presne tlacidlo Pokracovat
        self.assertEqual(posts[0][2], {"session_code": "SC1", "continue": "yes"})
        # 2. krok: suhlas = Povolit (accept), nie Zamietnut (cancel)
        self.assertEqual(posts[1][2], {"code": "CODE2", "accept": "Yes"})
        for _, _, body in posts:
            self.assertNotIn("other", body)
            self.assertNotIn("cancel", body)

    def test_split_forms_other_account_never_posted(self):
        routes = dict(GOOD_ROUTES)
        routes[("GET", DEVICE_URL)] = PAGE_CONTINUE_SPLIT
        new, op = run_approve(routes)
        urls = [p[1] for p in op.posts()]
        self.assertNotIn(BASE + "/realms/o2/device/other", urls)
        self.assertEqual(urls, [CONT_URL, CONSENT_URL])

    def test_user_code_field_filled(self):
        routes = dict(GOOD_ROUTES)
        routes[("GET", DEVICE_URL)] = PAGE_USER_CODE_FIELD
        _, op = run_approve(routes)
        self.assertEqual(op.posts()[0][2]["device_user_code"], CODE)

    def test_progress_has_labels_only(self):
        msgs = []
        sso.approve(CODE, COOKIES, progress=msgs.append,
                    opener_factory=lambda jar: FakeOpener(GOOD_ROUTES, jar))
        self.assertEqual(len(msgs), 2)
        self.assertIn("Pokračovať", msgs[0])
        self.assertIn("Povoliť", msgs[1])
        for m in msgs:
            self.assertNotIn("SECRET", m)

    def test_rotated_cookie_returned(self):
        rotated = "eyJhbGciOiJIUzI1NiJ9.NEW.ROTATED"
        new, _ = run_approve(GOOD_ROUTES, rotate=rotated)
        self.assertEqual(new["KEYCLOAK_IDENTITY"], rotated)
        self.assertEqual(new["KEYCLOAK_SESSION"], SECRET_SES)

    def test_turnstile_widget_stops(self):
        routes = {("GET", DEVICE_URL): PAGE_WIDGET}
        with self.assertRaises(sso.SsoError) as cm:
            run_approve(routes)
        self.assertIn("Turnstile", str(cm.exception))
        self.assertEqual(cm.exception.op.posts(), [])

    def test_script_only_does_not_stop(self):
        new, op = run_approve(GOOD_ROUTES)   # vsetky stranky maju len api.js
        self.assertEqual(len(op.posts()), 2)

    def test_unknown_field_stops(self):
        routes = {("GET", DEVICE_URL): PAGE_UNKNOWN_FIELD}
        with self.assertRaises(sso.SsoError) as cm:
            run_approve(routes)
        self.assertIn("phone_number", str(cm.exception))
        self.assertEqual(cm.exception.op.posts(), [])

    def test_foreign_host_stops(self):
        routes = {("GET", DEVICE_URL): PAGE_FOREIGN}
        with self.assertRaises(sso.SsoError) as cm:
            run_approve(routes)
        self.assertIn("evil.example.com", str(cm.exception))
        # na cudzi host sa nic neodoslalo (ani GET)
        self.assertEqual(len(cm.exception.op.requests), 1)

    def test_foreign_final_url_stops(self):
        routes = {("GET", DEVICE_URL): ("https://evil.example.com/x", PAGE_CONTINUE)}
        with self.assertRaises(sso.SsoError):
            run_approve(routes)

    def test_http_scheme_stops(self):
        self.assertRaises(sso.SsoError, sso._check_url, "http://identity.o2.sk/x")
        self.assertRaises(sso.SsoError, sso._check_url, "https://identity.o2.sk.evil.com/x")
        self.assertRaises(sso.SsoError, sso._check_url, "https://identity.o2.sk@evil.com/x")
        sso._check_url("https://identity.o2.sk/realms/o2/device")

    def test_too_many_steps(self):
        routes = {("GET", DEVICE_URL): PAGE_LOOP, ("POST", CONT_URL): PAGE_LOOP}
        with self.assertRaises(sso.SsoError) as cm:
            run_approve(routes)
        self.assertIn("veľa krokov", str(cm.exception))
        self.assertEqual(len(cm.exception.op.posts()), sso.MAX_STEPS)

    def test_only_wrong_buttons_no_post(self):
        routes = {("GET", DEVICE_URL): PAGE_ONLY_BAD}
        with self.assertRaises(sso.SsoError):
            run_approve(routes)
        # nic sa neodoslalo
        op = FakeOpener(routes)
        with self.assertRaises(sso.SsoError):
            sso.approve(CODE, COOKIES, opener_factory=lambda jar: op)
        self.assertEqual(op.posts(), [])

    def test_http_error_no_secrets(self):
        err = urllib.error.HTTPError(DEVICE_URL, 403, "Forbidden", {}, None)
        with self.assertRaises(sso.SsoError) as cm:
            run_approve({("GET", DEVICE_URL): err})
        self.assertIn("403", str(cm.exception))

    def test_network_error_no_secrets(self):
        err = urllib.error.URLError("timed out")
        with self.assertRaises(sso.SsoError) as cm:
            run_approve({("GET", DEVICE_URL): err})
        self.assertIn("sieť", str(cm.exception))

    def test_unexpected_error_wrapped(self):
        with self.assertRaises(sso.SsoError) as cm:
            run_approve({("GET", DEVICE_URL): KeyError(SECRET_ID)})
        self.assertNotIn("SECRET", str(cm.exception))

    def test_error_carries_rotated_cookies(self):
        rotated = "eyJhbGciOiJIUzI1NiJ9.NEW.ROTATED"
        with self.assertRaises(sso.SsoError) as cm:
            run_approve({("GET", DEVICE_URL): PAGE_WIDGET}, rotate=rotated)
        self.assertEqual(cm.exception.cookies["KEYCLOAK_IDENTITY"], rotated)

    def test_no_secrets_in_any_message(self):
        cases = [{("GET", DEVICE_URL): PAGE_WIDGET},
                 {("GET", DEVICE_URL): PAGE_UNKNOWN_FIELD},
                 {("GET", DEVICE_URL): PAGE_FOREIGN},
                 {("GET", DEVICE_URL): PAGE_ONLY_BAD},
                 {("GET", DEVICE_URL): PAGE_LOOP, ("POST", CONT_URL): PAGE_LOOP}]
        for routes in cases:
            with self.assertRaises(sso.SsoError) as cm:
                run_approve(routes)
            msg = str(cm.exception) + repr(cm.exception)
            for s in (SECRET_ID, SECRET_SES, "SECRET", "jan.novak@example.sk"):
                self.assertNotIn(s, msg)

    def test_bad_user_code_no_request(self):
        for bad in ("", "a b", "x" * 40, "AB;CD&x=1", None):
            op = FakeOpener({})
            with self.assertRaises(sso.SsoError):
                sso.approve(bad, COOKIES, opener_factory=lambda jar: op)
            self.assertEqual(op.requests, [])

    def test_bad_cookies_no_request(self):
        op = FakeOpener({})
        with self.assertRaises(sso.SsoError):
            sso.approve(CODE, {"KEYCLOAK_IDENTITY": "x"}, opener_factory=lambda jar: op)
        self.assertEqual(op.requests, [])


class RedirectTests(unittest.TestCase):
    def test_guard_refuses_foreign_redirect(self):
        g = sso._HostGuard()
        req = urllib.request.Request(DEVICE_URL)
        with self.assertRaises(sso.SsoError):
            g.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x")
        with self.assertRaises(sso.SsoError):
            g.redirect_request(req, None, 302, "Found", {}, "http://identity.o2.sk/x")
        self.assertIsNotNone(g.redirect_request(
            req, None, 302, "Found", {}, BASE + "/realms/o2/other"))


class CookieFileTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.path = os.path.join(self.d, "sub", "sso.json")

    def test_validate(self):
        self.assertEqual(sso.validate(dict(COOKIES, extra="zahodi sa")), COOKIES)
        for bad in (None, [], {}, {"KEYCLOAK_IDENTITY": "a"},
                    {"KEYCLOAK_IDENTITY": "a", "KEYCLOAK_SESSION": ""},
                    {"KEYCLOAK_IDENTITY": "a", "KEYCLOAK_SESSION": 5},
                    {"KEYCLOAK_IDENTITY": "a b", "KEYCLOAK_SESSION": "x"},
                    {"KEYCLOAK_IDENTITY": "a\r\nSet: x", "KEYCLOAK_SESSION": "x"},
                    {"KEYCLOAK_IDENTITY": "a;b", "KEYCLOAK_SESSION": "x"}):
            with self.assertRaises(sso.SsoError):
                sso.validate(bad)

    def test_validate_message_has_no_value(self):
        with self.assertRaises(sso.SsoError) as cm:
            sso.validate({"KEYCLOAK_IDENTITY": "tajne hodnota", "KEYCLOAK_SESSION": "x"})
        self.assertNotIn("tajne", str(cm.exception))

    def test_save_load_roundtrip_atomic(self):
        self.assertTrue(sso.save(self.path, COOKIES))
        self.assertEqual(sso.load(self.path), COOKIES)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["sso.json"])
        newv = dict(COOKIES, KEYCLOAK_IDENTITY="NEW.VALUE.X")
        self.assertTrue(sso.save(self.path, newv))
        self.assertEqual(sso.load(self.path), newv)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["sso.json"])

    def test_save_failure_keeps_old_and_does_not_raise(self):
        self.assertTrue(sso.save(self.path, COOKIES))
        self.assertFalse(sso.save(self.path, {"KEYCLOAK_IDENTITY": "x"}))   # neplatne
        with mock.patch("os.replace", side_effect=OSError("disk")):
            self.assertFalse(sso.save(self.path, dict(COOKIES, KEYCLOAK_SESSION="o2/x/y")))
        self.assertEqual(sso.load(self.path), COOKIES)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["sso.json"])
        # cesta, ktoru sa vytvorit neda
        self.assertFalse(sso.save(os.path.join(self.path, "x", "y.json"), COOKIES))

    def test_load_bad(self):
        self.assertIsNone(sso.load(os.path.join(self.d, "nie-je.json")))
        os.makedirs(os.path.dirname(self.path))
        for content in ("", "nie json", "[]", '{"KEYCLOAK_IDENTITY": "a"}'):
            with open(self.path, "w") as f:
                f.write(content)
            self.assertIsNone(sso.load(self.path))

    def test_identity_exp(self):
        self.assertEqual(sso.identity_exp(COOKIES), 4102444800)
        self.assertIsNone(sso.identity_exp({"KEYCLOAK_IDENTITY": "nie.jwt.x"}))
        self.assertIsNone(sso.identity_exp({}))
        self.assertIsNone(sso.identity_exp({"KEYCLOAK_IDENTITY": "a.e30.c"}))   # {} bez exp


class FakeStore:
    def __init__(self, **kw):
        self.d = dict(kw)

    def get(self, k):
        return self.d.get(k, "")

    def set(self, k, v):
        self.d[k] = v


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.path = os.path.join(self.d, "sso.json")
        self.logs = []
        self.api = api_mod.O2API(FakeStore(udid="UDID123456789012"), self.d,
                                 log=self.logs.append)
        self.api.st_set(udid="UDID123456789012", ks="OLDKS", refresh_token="OLDRT",
                        login_time=1000)
        self.api.device_start = lambda: {"user_code": CODE, "device_code": "DC",
                                         "interval": 5}
        self.polls = []

    def _state(self):
        with open(os.path.join(self.d, "session.json")) as f:
            return json.load(f)

    def test_success_saves_rotated_cookies_and_logs_in(self):
        sso.save(self.path, COOKIES)
        rotated = dict(COOKIES, KEYCLOAK_IDENTITY="ROTATED.COOKIE.VALUE")
        answers = [None, None, "ACCESSTOKEN"]
        self.api.device_poll = lambda dc: answers.pop(0)
        seen = {}

        def fake_login(tok):
            seen["tok"] = tok
            self.api.st_set(ks="NEWKS", refresh_token="NEWRT", login_time=2000)
            return {"expiry": 2000000000}
        self.api.login_with_token = fake_login
        sleeps = []
        with mock.patch.object(sso, "approve", return_value=rotated) as ap:
            ls = self.api.auto_login(self.path, sleep=sleeps.append)
        ap.assert_called_once_with(CODE, COOKIES)
        self.assertEqual(seen["tok"], "ACCESSTOKEN")
        self.assertEqual(sleeps, [5, 5])
        self.assertEqual(ls["expiry"], 2000000000)
        self.assertEqual(sso.load(self.path), rotated)
        self.assertEqual(self._state()["udid"], "UDID123456789012")
        for l in self.logs:
            for s in ("ACCESSTOKEN", "ROTATED", "SECRET"):
                self.assertNotIn(s, l)

    def test_login_with_token_real_keeps_udid_and_registers(self):
        sso.save(self.path, COOKIES)
        self.api.device_poll = lambda dc: "ACCESSTOKEN"
        calls = []

        def fake_kalt(action, body, _retry=False):
            calls.append(action)
            if action == "ottuser/action/anonymousLogin":
                return {"ks": "ANON"}
            if action == "ottuser/action/login":
                self.assertEqual(body["udid"], "UDID123456789012")
                return {"loginSession": {"ks": "NEWKS", "expiry": 2000000000,
                                         "refreshToken": "NEWRT"}}
            return {}
        self.api._kalt = fake_kalt
        self.api.ks = lambda: "NEWKS"
        with mock.patch.object(sso, "approve", return_value=dict(COOKIES)):
            self.api.auto_login(self.path)
        self.assertIn("householddevice/action/add", calls)
        st = self._state()
        self.assertEqual((st["udid"], st["ks"], st["refresh_token"]),
                         ("UDID123456789012", "NEWKS", "NEWRT"))
        self.assertFalse(st["login_time_est"])
        self.assertGreater(st["login_time"], 1000)

    def test_approve_failure_keeps_state_saves_cookies(self):
        sso.save(self.path, COOKIES)
        rotated = dict(COOKIES, KEYCLOAK_IDENTITY="ROTATED.COOKIE.VALUE")
        before = self._state()
        self.api.login_with_token = mock.Mock()
        err = sso.SsoError("stránka má ochranu proti automatizácii", rotated)
        with mock.patch.object(sso, "approve", side_effect=err):
            with self.assertRaises(api_mod.O2Error) as cm:
                self.api.auto_login(self.path)
        self.assertEqual(cm.exception.code, "SSO")
        self.api.login_with_token.assert_not_called()
        self.assertEqual(self._state(), before)
        self.assertEqual(sso.load(self.path), rotated)

    def test_login_failure_keeps_state(self):
        sso.save(self.path, COOKIES)
        before = self._state()
        self.api.device_poll = lambda dc: "ACCESSTOKEN"
        self.api._kalt = mock.Mock(side_effect=api_mod.O2Error("2026", "2026"))
        with mock.patch.object(sso, "approve", return_value=dict(COOKIES)):
            with self.assertRaises(api_mod.O2Error):
                self.api.auto_login(self.path)
        self.assertEqual(self._state(), before)

    def test_no_cookie_file(self):
        with self.assertRaises(api_mod.O2Error) as cm:
            self.api.auto_login(self.path)
        self.assertEqual(cm.exception.code, "NOSSO")

    def test_no_udid_no_new_one(self):
        sso.save(self.path, COOKIES)
        api2 = api_mod.O2API(FakeStore(), os.path.join(self.d, "other"))
        api2.device_start = mock.Mock()
        with self.assertRaises(api_mod.O2Error) as cm:
            api2.auto_login(self.path)
        self.assertEqual(cm.exception.code, "NOSESSION")
        api2.device_start.assert_not_called()

    def test_poll_timeout(self):
        sso.save(self.path, COOKIES)
        self.api.device_poll = lambda dc: None
        self.api.login_with_token = mock.Mock()
        n = []
        with mock.patch.object(sso, "approve", return_value=dict(COOKIES)):
            with self.assertRaises(api_mod.O2Error) as cm:
                self.api.auto_login(self.path, sleep=n.append)
        self.assertEqual(cm.exception.code, "SSO")
        self.assertLessEqual(len(n), 9)
        self.api.login_with_token.assert_not_called()

    def test_abort(self):
        sso.save(self.path, COOKIES)
        self.api.device_poll = lambda dc: None
        with mock.patch.object(sso, "approve", return_value=dict(COOKIES)):
            with self.assertRaises(api_mod.O2Error) as cm:
                self.api.auto_login(self.path, should_stop=lambda: True)
        self.assertEqual(cm.exception.code, "ABORT")


class ServiceStubs:
    """Nacita service.py s falosnymi modulmi Kodi, aby sa dala otestovat
    logika AutoLogin (poistky) bez Kodi."""

    @staticmethod
    def load():
        import importlib.util, types
        props = {}
        win = mock.Mock()
        win.getProperty.side_effect = lambda k: props.get(k, "")
        win.setProperty.side_effect = lambda k, v: props.__setitem__(k, v)
        gui = mock.MagicMock()
        gui.Window.return_value = win
        xaddon = mock.MagicMock()
        mods = {"xbmc": mock.MagicMock(), "xbmcaddon": xaddon, "xbmcgui": gui,
                "xbmcvfs": mock.MagicMock(), "export": mock.MagicMock()}
        with mock.patch.dict(sys.modules, mods):
            spec = importlib.util.spec_from_file_location(
                "o2tv_service_test", os.path.join(os.path.dirname(HERE), "service.py"))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        return m, gui, props


class ServiceAutoTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.gui, self.props = ServiceStubs.load()
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.path = os.path.join(self.d, "sso.json")
        self.api = mock.Mock()
        self.api.session_left.return_value = (10 * 86400, False)
        self.api.refresh_dead = False
        self.mon = mock.Mock()
        self.auto = self.svc.AutoLogin(self.api, self.path, self.mon)
        self.now = [1000000.0]
        p = mock.patch.object(self.svc.time, "time", lambda: self.now[0])
        p.start()
        self.addCleanup(p.stop)

    def _file(self):
        sso.save(self.path, COOKIES)

    def test_no_file_no_try(self):
        self.api.session_left.return_value = (0, False)
        self.assertFalse(self.auto.run())
        self.api.auto_login.assert_not_called()

    def test_plenty_left_no_try(self):
        self._file()
        self.assertFalse(self.auto.run())
        self.api.auto_login.assert_not_called()

    def test_threshold_two_days(self):
        self._file()
        self.api.session_left.return_value = (2 * 86400 + 1, False)
        self.assertFalse(self.auto.run())
        self.api.session_left.return_value = (2 * 86400, False)
        self.assertTrue(self.auto.run())
        self.api.auto_login.assert_called_once()
        self.gui.Dialog.return_value.notification.assert_called()
        self.assertIn("automaticky", self.gui.Dialog.return_value.notification.call_args[0][1])

    def test_dead_triggers_even_with_time_left(self):
        self._file()
        self.auto.dead = True
        self.assertTrue(self.auto.run())

    def test_failure_backoff_1h_and_pause_after_3(self):
        self._file()
        self.auto.dead = True
        self.api.auto_login.side_effect = self.svc.O2Error("zle", "SSO")
        self.assertFalse(self.auto.run())
        self.assertTrue(self.auto.failed_now)
        self.assertFalse(self.auto.run())          # hned znova nie
        self.assertEqual(self.api.auto_login.call_count, 1)
        self.now[0] += 3599
        self.assertFalse(self.auto.run())
        self.assertEqual(self.api.auto_login.call_count, 1)
        self.now[0] += 2
        self.auto.run()
        self.assertEqual(self.api.auto_login.call_count, 2)
        self.now[0] += 3601
        self.auto.run()
        self.assertEqual(self.api.auto_login.call_count, 3)
        self.assertTrue(self.auto.paused)
        self.now[0] += 10 * 3600
        self.auto.run()
        self.assertEqual(self.api.auto_login.call_count, 3)   # pauza do restartu

    def test_new_cookie_resets_pause(self):
        self._file()
        self.auto.dead = True
        self.auto.paused, self.auto.fails = True, 3
        self.assertFalse(self.auto.can_try())
        self.props[self.svc.SSO_PROP] = "123"
        self.assertTrue(self.auto.can_try())
        self.assertEqual(self.auto.fails, 0)

    def test_success_waits_6h_and_resets_counter(self):
        self._file()
        self.auto.dead, self.auto.fails = True, 2
        self.assertTrue(self.auto.run())
        self.assertEqual((self.auto.fails, self.auto.dead), (0, False))
        self.auto.dead = True
        self.now[0] += 6 * 3600 - 1
        self.assertFalse(self.auto.run())
        self.now[0] += 2
        self.assertTrue(self.auto.run())

    def test_abort_is_not_a_failure(self):
        self._file()
        self.auto.dead = True
        self.api.auto_login.side_effect = self.svc.O2Error("x", "ABORT")
        self.assertFalse(self.auto.run())
        self.assertEqual(self.auto.fails, 0)

    def test_unexpected_exception_not_logged_verbatim(self):
        self._file()
        self.auto.dead = True
        self.api.auto_login.side_effect = KeyError("TAJNE-COOKIE")
        logged = []
        with mock.patch.object(self.svc, "log", lambda m, e=False: logged.append(m)):
            self.assertFalse(self.auto.run())
        self.assertTrue(logged)
        self.assertFalse(any("TAJNE" in m for m in logged))

    def test_tick_defers_manual_toast_when_auto_possible(self):
        self._file()
        self.api.ks.side_effect = self.svc.O2Error("mrtve", "500017")
        notif = self.gui.Dialog.return_value.notification
        self.assertFalse(self.svc.tick(self.api, self.auto))
        self.assertTrue(self.auto.dead)
        notif.assert_not_called()
        # bez sso.json (alebo v pauze) ostava povodne upozornenie
        self.auto.paused = True
        self.assertFalse(self.svc.tick(self.api, self.auto))
        notif.assert_called_once()
        notif.reset_mock()
        self.assertFalse(self.svc.tick(self.api, None))
        notif.assert_called_once()

    def test_tick_flags_dead_refresh_with_valid_old_ks(self):
        self.api.ks.return_value = "OLD"
        self.api.refresh_dead = True
        self.assertTrue(self.svc.tick(self.api, self.auto))
        self.assertTrue(self.auto.dead)

    def test_busy_attempt_from_addon_blocks_service(self):
        self._file()
        self.auto.dead = True
        self.props[self.svc.AUTO_BUSY] = str(self.now[0] - 10)      # bezi 10 s
        self.assertFalse(self.auto.run())
        self.api.auto_login.assert_not_called()
        self.props[self.svc.AUTO_BUSY] = str(self.now[0] - 1000)    # zabudnuty zamok
        self.assertTrue(self.auto.run())

    def test_service_sets_and_clears_busy_around_attempt(self):
        self._file()
        self.auto.dead = True
        seen = []
        self.api.auto_login.side_effect = lambda *a, **k: seen.append(
            self.props.get(self.svc.AUTO_BUSY))
        self.assertTrue(self.auto.run())
        self.assertTrue(seen[0])                       # pocas pokusu zamok je
        self.gui.Window.return_value.clearProperty.assert_called_with(self.svc.AUTO_BUSY)

    def test_done_from_addon_clears_dead_state(self):
        self._file()
        self.auto.dead, self.auto.fails = True, 2
        self.api.refresh_dead = True
        self.props[self.svc.AUTO_DONE] = "123"         # addon sa uz prihlasil
        self.assertFalse(self.auto.run())
        self.api.auto_login.assert_not_called()
        self.assertEqual((self.auto.dead, self.auto.fails), (False, 0))
        self.assertFalse(self.api.refresh_dead)


class AddonStubs:
    """Nacita addon.py s falosnymi modulmi Kodi (profil v docasnom adresari)."""

    @staticmethod
    def load(profile):
        import importlib.util
        props = {}
        win = mock.Mock()
        win.getProperty.side_effect = lambda k: props.get(k, "")
        win.setProperty.side_effect = lambda k, v: props.__setitem__(k, v)
        win.clearProperty.side_effect = lambda k: props.pop(k, None)
        gui = mock.MagicMock()
        gui.Window.return_value = win
        addon = mock.MagicMock()
        addon.getAddonInfo.side_effect = lambda k: {
            "path": os.path.dirname(HERE), "profile": profile}[k]
        xaddon = mock.MagicMock()
        xaddon.Addon.return_value = addon
        vfs = mock.MagicMock()
        vfs.translatePath.side_effect = lambda p: p
        xbmc = mock.MagicMock()
        plugin = mock.MagicMock()
        mods = {"xbmc": xbmc, "xbmcaddon": xaddon, "xbmcgui": gui,
                "xbmcvfs": vfs, "xbmcplugin": plugin}
        with mock.patch.dict(sys.modules, mods), \
                mock.patch.object(sys, "argv", ["plugin://plugin.video.o2tv/", "1", ""]):
            spec = importlib.util.spec_from_file_location(
                "o2tv_addon_test", os.path.join(os.path.dirname(HERE), "addon.py"))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
        return m, gui, plugin, xbmc, props


class AddonReloginTests(unittest.TestCase):
    """Mrtva relacia pri prehravani/zozname/importe: prihlasenie hned, bez cakania
    na kolo sluzby."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.m, self.gui, self.plugin, self.xbmc, self.props = AddonStubs.load(self.d)
        self.sso_file = os.path.join(self.d, "sso.json")
        self.api = mock.Mock()
        self.dead = self.m.O2Error("mrtva", "500017")
        self.m.cdn_session = lambda u: u
        self.m.url_for = lambda **kw: "plugin://x/?" + urllib.parse.urlencode(kw)

    def _file(self):
        sso.save(self.sso_file, COOKIES)

    def _resolved(self):
        return self.plugin.setResolvedUrl.call_args[0][1]

    def test_play_relogins_and_retries_once(self):
        self._file()
        self.api.resolve.side_effect = [self.dead, ("http://m/mpd", None)]
        self.m.play(self.api, "840007")
        self.api.auto_login.assert_called_once_with(self.sso_file)
        self.assertEqual(self.api.resolve.call_count, 2)
        self.assertTrue(self._resolved())
        self.assertTrue(self.props.get(self.m.AUTO_DONE))      # service sa to dozvie
        self.assertNotIn(self.m.AUTO_BUSY, self.props)         # zamok uvolneny

    def test_play_without_sso_file_keeps_old_behaviour(self):
        self.api.resolve.side_effect = self.dead
        self.m.play(self.api, "840007")
        self.api.auto_login.assert_not_called()
        self.assertFalse(self._resolved())

    def test_play_failed_relogin_shows_error_without_retry(self):
        self._file()
        self.api.resolve.side_effect = self.dead
        self.api.auto_login.side_effect = self.m.O2Error("SSO zle", "SSO")
        self.m.play(self.api, "840007")
        self.assertEqual(self.api.resolve.call_count, 1)
        self.assertFalse(self._resolved())
        self.assertNotIn(self.m.AUTO_BUSY, self.props)
        self.assertNotIn(self.m.AUTO_DONE, self.props)

    def test_play_other_errors_do_not_trigger_relogin(self):
        self._file()
        self.api.resolve.side_effect = self.m.O2Error("siet", "NET")
        self.m.play(self.api, "840007")
        self.api.auto_login.assert_not_called()

    def test_play_nosession_triggers_relogin(self):
        self._file()
        self.api.resolve.side_effect = [self.m.O2Error("nie je", "NOSESSION"),
                                        ("http://m/mpd", None)]
        self.m.play(self.api, "1")
        self.api.auto_login.assert_called_once()

    def test_channels_relogin(self):
        self._file()
        self.api.channels.side_effect = [self.dead, [{"id": 1}]]
        self.assertEqual(self.m.channels_with_relogin(self.api), [{"id": 1}])
        self.api.auto_login.assert_called_once()
        self.api.channels.side_effect = self.dead              # druhy pokus tiez zlyha
        with self.assertRaises(self.m.O2Error):
            self.m.channels_with_relogin(self.api)

    def test_waits_for_service_attempt_instead_of_second_login(self):
        self._file()
        import time as _t
        self.props[self.m.AUTO_BUSY] = str(_t.time())          # sluzba prave bezi
        self.xbmc.sleep.side_effect = lambda ms: self.props.pop(self.m.AUTO_BUSY, None)
        self.api.ks.return_value = "KS"
        self.assertTrue(self.m.auto_relogin(self.api))
        self.api.auto_login.assert_not_called()

    def test_stale_busy_lock_is_ignored(self):
        self._file()
        import time as _t
        self.props[self.m.AUTO_BUSY] = str(_t.time() - 1000)
        self.assertTrue(self.m.auto_relogin(self.api))
        self.api.auto_login.assert_called_once()

    def test_import_logs_in_immediately_when_session_is_ending(self):
        src = os.path.join(self.d, "o2-sso.json")
        for left, expected in ((86400, 1), (-5, 1), (5 * 86400, 0)):
            sso.save(src, COOKIES)
            self.api.reset_mock()
            self.api.session_left.return_value = (left, False)
            with mock.patch.object(self.m, "sso_candidates", lambda: [src]):
                self.m.import_sso(self.api)
            self.assertFalse(os.path.exists(src))              # zdroj zmazany
            self.assertEqual(self.api.auto_login.call_count, expected)
            os.remove(self.sso_file)

    def test_no_secret_in_logs_or_toasts(self):
        self._file()
        self.api.resolve.side_effect = self.dead
        self.api.auto_login.side_effect = self.m.O2Error("zle", "SSO")
        self.m.play(self.api, "1")
        seen = " ".join(str(c) for c in self.xbmc.log.call_args_list)
        seen += " ".join(str(c) for c in self.gui.Dialog.return_value.notification.call_args_list)
        self.assertNotIn("SECRET", seen)


if __name__ == "__main__":
    unittest.main()
