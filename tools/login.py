# -*- coding: utf-8 -*-
"""Prihlasenie doplnku cez Keycloak - zapisuje priamo do session.json.

Kaltura relaciu sa da obnovovat len ~7 dni (refreshSession), potom treba
nove prihlasenie. Tento skript ho spravi bez Kodi: vysledok ide do
session.json v addon_data, s UDID, ktore uz doplnok pouziva - nove UDID
by znamenalo dalsiu registraciu zariadenia.

Dve cesty, obe neinteraktivne (Claude Code ani Kodi nemaju stdin):

  1) access token z prehliadaca - spolahliva cesta na tomto stroji
     Prihlas sa na www.o2tv.sk, v DevTools > Network najdi POST na
     openid-connect/token a z odpovede vezmi access_token:

        python3 tools/login.py --access-token 'eyJ...'

  2) vlastny Keycloak tok - ked sa netreba delit o prehliadac s web appkou

        python3 tools/login.py                      # vypise URL
        python3 tools/login.py '<presmerovana URL>'  # dokonci

     Pozor: redirect konci na www.o2tv.sk/auth/ a tamojsia web appka
     ?code= spotrebuje skor, nez ho stihnes odovzdat sem - vtedy Keycloak
     vrati "invalid_grant: Code not valid". Bud jej to zakaz (DevTools >
     Request conditions > blokovanie *o2tv.sk*), alebo pouzi cestu 1.

Po prihlaseni zavri prehliadac s o2tv.sk - web appka doplnkovu relaciu
do par minut zabije (500016).
"""
import base64, hashlib, json, os, secrets, sys
import urllib.error, urllib.parse, urllib.request

KC = "https://identity.o2.sk/realms/o2/protocol/openid-connect"
CLIENT_ID = "o2-xtv-kaltura"
REDIRECT = "https://www.o2tv.sk/auth/"
SCOPE = "openid"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

PROFILE = os.environ.get("O2TV_PROFILE") or os.path.expanduser(
    "~/.kodi/userdata/addon_data/plugin.video.o2tv")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "resources", "lib"))
from api import O2API   # noqa: E402


class Store:
    """Nahrada za nastavenia Kodi - client_tag berie z jeho settings.xml."""

    def __init__(self, profile):
        self.tag = "9.66.1-PC"
        try:
            import re
            with open(os.path.join(profile, "settings.xml")) as f:
                m = re.search(r'id="client_tag"[^>]*>([^<]+)<', f.read())
            if m:
                self.tag = m.group(1)
        except Exception:
            pass

    def get(self, key):
        return self.tag if key == "client_tag" else ""

    def set(self, key, value):
        pass


def token_request(form):
    data = urllib.parse.urlencode(dict(form, client_id=CLIENT_ID)).encode()
    req = urllib.request.Request(KC + "/token", data=data, method="POST")
    req.add_header("content-type", "application/x-www-form-urlencoded")
    req.add_header("user-agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit("[!] Token endpoint %s: %s" % (e.code, e.read()[:300].decode("utf-8", "replace")))


def done(api, ls):
    import time
    print("\n[✓] session.json zapísaný")
    print("    UDID:", api.st_get("udid"))
    print("    KS platí do:", time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ls["expiry"]))))
    print("    refresh token:", "je" if api.st_get("refresh_token") else "CHÝBA — obnova nepôjde")
    print("\nZavri prehliadač s o2tv.sk a reštartuj Kodi.")


def main():
    api = O2API(Store(PROFILE), PROFILE, log=lambda m: print("[log]", m))
    pkce = os.path.join(PROFILE, "pkce.json")
    args = sys.argv[1:]

    if args and args[0] == "--access-token":
        if len(args) < 2:
            sys.exit("[!] Chýba token: tools/login.py --access-token 'eyJ...'")
        done(api, api.login_with_token(args[1].strip()))
        return

    if not args:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = KC + "/auth?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT, "code_challenge": challenge,
            "code_challenge_method": "S256", "scope": SCOPE,
            "prompt": "login", "state": secrets.token_hex(8),
            "nonce": secrets.token_hex(8)})
        with open(pkce, "w") as f:
            json.dump({"verifier": verifier}, f)
        os.chmod(pkce, 0o600)
        print("UDID doplnku:", api.st_get("udid") or "(žiadne — vytvorí sa nové)")
        print("\nOtvor a prihlás sa (heslo + SMS):\n\n" + url)
        print("\nPotom:  python3 %s '<presmerovaná URL>'" % os.path.abspath(__file__))
        return

    raw = args[0].strip()
    code = raw
    if "code=" in raw:
        q = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        code = urllib.parse.parse_qs(q).get("code", [raw])[0]
    try:
        with open(pkce) as f:
            verifier = json.load(f)["verifier"]
    except Exception:
        sys.exit("[!] Chýba %s — spusti najprv tools/login.py bez argumentov." % pkce)
    tok = token_request({"grant_type": "authorization_code", "code": code,
                         "redirect_uri": REDIRECT, "code_verifier": verifier})
    ls = api.login_with_token(tok["access_token"])
    os.remove(pkce)
    done(api, ls)


if __name__ == "__main__":
    main()
