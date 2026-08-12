# -*- coding: utf-8 -*-
import json, time, secrets, string
import urllib.request, urllib.error

KALT = "https://3206.frp1.ott.kaltura.com/api_v3/service"
API_VER, PARTNER_ID, LANG = "5.4.0", 3206, "slk"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


class O2Error(Exception):
    pass


def gen_udid():
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))


class O2API:
    def __init__(self, store, state_dir=None):
        # store = Kodi nastavenia (statické: udid, client_tag, kvalita)
        # state = vlastný JSON súbor (meniace sa: ks, expiry, refresh token)
        self.s = store
        self.state_dir = state_dir
        self._state = None

    # ---------- stav mimo nastavení ----------
    def _state_path(self):
        import os
        return os.path.join(self.state_dir or ".", "session.json")

    def st_load(self):
        if self._state is None:
            try:
                with open(self._state_path()) as f:
                    self._state = json.load(f)
            except Exception:
                self._state = {}
        return self._state

    def st_get(self, key, default=""):
        return self.st_load().get(key, default)

    def st_set(self, **kw):
        import os, tempfile
        st = self.st_load()
        st.update(kw)
        self._state = st
        d = self.state_dir or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d)
            with os.fdopen(fd, "w") as f:
                json.dump(st, f)
            os.replace(tmp, self._state_path())
        except Exception:
            pass

    # ---------- HTTP ----------
    def _post(self, url, body, raw=None, ctype=None):
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("content-type", ctype or "application/json;charset=UTF-8")
        req.add_header("accept", "*/*")
        req.add_header("user-agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise O2Error("HTTP %s: %s" % (e.code, e.read()[:300]))

    def _kalt(self, action, body):
        tag = self.s.get("client_tag")
        payload = {"clientTag": tag, "apiVersion": API_VER, "partnerId": PARTNER_ID}
        payload.update(body)
        url = "%s/%s?format=1&clientTag=%s" % (KALT, action, tag)
        res = json.loads(self._post(url, payload).decode("utf-8"))
        result = res.get("result", res)
        if isinstance(result, dict) and result.get("error"):
            raise O2Error(str(result["error"]))
        return result

    # ---------- session ----------
    def ks(self):
        now = int(time.time())
        try:
            exp = int(self.st_get("ks_expiry") or 0)
        except Exception:
            exp = 0
        cur = self.st_get("ks")
        if cur and exp - now > 300:
            return cur
        return self.refresh()

    def refresh(self):
        # token zo stavu má prednosť; nastavenie je len počiatočný vklad
        rt = self.st_get("refresh_token") or self.s.get("refresh_token")
        udid = self.st_get("udid") or self.s.get("udid")
        if not rt or not udid:
            raise O2Error("Chýba refresh token alebo UDID v nastaveniach.")
        cur = self.st_get("ks") or self.s.get("ks") or ""
        if not cur:
            cur = self._kalt("ottuser/action/anonymousLogin",
                             {"language": "*"})["ks"]
        res = self._kalt("ottuser/action/refreshSession", {
            "language": LANG, "ks": cur,
            "refreshToken": rt, "udid": udid,
        })
        ls = res.get("loginSession", res)
        self.st_set(ks=ls["ks"], ks_expiry=int(ls["expiry"]),
                    refresh_token=ls.get("refreshToken") or rt)
        return ls["ks"]

    def login_with_token(self, access_token):
        udid = self.st_get("udid") or self.s.get("udid") or gen_udid()
        anon = self._kalt("ottuser/action/anonymousLogin", {"language": "*"})
        res = self._kalt("ottuser/action/login", {
            "language": LANG, "ks": anon["ks"], "udid": udid,
            "username": "11111", "password": "11111",
            "extraParams": {
                "loginType": {"objectType": "KalturaStringValue", "value": "accessToken"},
                "accessToken": {"objectType": "KalturaStringValue", "value": access_token},
            },
        })
        ls = res["loginSession"]
        self.st_set(udid=udid)
        self.st_set(ks=ls["ks"], ks_expiry=int(ls["expiry"]),
                    refresh_token=ls.get("refreshToken") or "")
        return ls

    # ---------- kanály ----------
    def channels(self):
        res = self._kalt("asset/action/list", {
            "ks": self.ks(), "language": LANG,
            "filter": {"objectType": "KalturaSearchAssetFilter",
                       "kSql": "(and asset_type='714')"},
            "pager": {"objectType": "KalturaFilterPager",
                      "pageIndex": 1, "pageSize": 500},
        })
        out = []
        for x in res.get("objects", []) or []:
            imgs = x.get("images") or []
            logo = None
            for im in imgs:
                if im.get("imageTypeId") == 18:
                    logo = im.get("url"); break
            if not logo and imgs:
                logo = imgs[0].get("url")
            if logo:
                logo = "%s/height/320/width/480" % logo
            num = ((x.get("metas") or {}).get("ChannelNumber") or {}).get("value")
            out.append({"id": str(x["id"]), "name": x.get("name", ""),
                        "number": num, "logo": logo})
        out.sort(key=lambda c: (c["number"] is None, c["number"]))
        return out

    # ---------- prehrávanie ----------
    def playback_context(self, asset_id, ref_type, asset_type, context):
        ks = self.ks()
        body = {"clientTag": self.s.get("client_tag"), "apiVersion": API_VER,
                "partnerId": PARTNER_ID, "ks": ks}
        body["1"] = {"service": "asset", "action": "get",
                     "assetReferenceType": ref_type, "id": asset_id, "ks": ks}
        body["2"] = {"service": "asset", "action": "getPlaybackContext",
                     "assetType": asset_type, "assetId": asset_id,
                     "contextDataParams": {
                         "objectType": "KalturaPlaybackContextOptions",
                         "context": context, "streamerType": "mpegdash",
                         "urlType": "DIRECT",
                         "adapterData": {"codec": {"value": "AVC"},
                                         "quality": {"value": "UHD"}}},
                     "ks": ks}
        url = "%s/multirequest" % KALT
        res = json.loads(self._post(url, body).decode("utf-8"))
        result = res.get("result", [])
        pc = result[1] if isinstance(result, list) and len(result) > 1 else result
        if isinstance(pc, dict) and pc.get("error"):
            raise O2Error(str(pc["error"]))
        return pc

    @staticmethod
    def pick_source(pc):
        srcs = (pc or {}).get("sources", []) or []
        wv = next((s for s in srcs if str(s.get("type", "")).upper() == "DASH_WV"), None)
        dash = next((s for s in srcs if str(s.get("type", "")).upper().startswith("DASH")), None)
        return wv or dash or (srcs[0] if srcs else None)

    @staticmethod
    def widevine_url(pc):
        for d in (O2API.pick_source(pc) or {}).get("drm", []) or []:
            if "WIDEVINE" in str(d.get("scheme", "")).upper():
                return d.get("licenseURL")
        return None

    @staticmethod
    def not_entitled(pc):
        for m in (pc or {}).get("messages", []) or []:
            if str(m.get("code", "")).upper() == "NOTENTITLED":
                return True
        return False

    def resolve(self, cid, start_ts=None, end_ts=None):
        """Vráti (manifest_url, license_url)."""
        now = int(time.time())
        if start_ts and end_ts and int(start_ts) < now - 10:
            ctx = "CATCHUP" if int(end_ts) < now - 10 else "START_OVER"
            prog = self._find_programme(cid, int(start_ts), int(end_ts))
            self.last_mode = "%s cid=%s start=%s prog=%s" % (ctx, cid, start_ts, prog)
            pc = self.playback_context(prog, "epg_internal", "epg", ctx) if prog \
                else self.playback_context(cid, "media", "media", "PLAYBACK")
        else:
            self.last_mode = "LIVE cid=%s start=%s end=%s" % (cid, start_ts, end_ts)
            pc = self.playback_context(cid, "media", "media", "PLAYBACK")
        src = self.pick_source(pc)
        if not src:
            raise O2Error("Žiadny DASH zdroj.")
        return src.get("url"), self.widevine_url(pc)

    def _find_programme(self, cid, start_ts, end_ts=None):
        # IPTV Simple pridáva buffery, preto hľadáme program cez stred rozsahu
        mid = int((start_ts + end_ts) / 2) if end_ts else int(start_ts)
        ksql = ("(and linear_media_id:'%s' start_date <= '%d' end_date >= '%d' "
                "asset_type='epg' auto_fill=true)" % (cid, mid, mid))
        res = self._kalt("asset/action/list", {
            "ks": self.ks(), "language": LANG,
            "filter": {"objectType": "KalturaSearchAssetFilter",
                       "orderBy": "START_DATE_ASC", "kSql": ksql},
            "pager": {"objectType": "KalturaFilterPager",
                      "pageSize": 50, "pageIndex": 1},
        })
        objs = res.get("objects", []) or []
        for o in objs:
            if int(o.get("startDate", 0)) <= mid <= int(o.get("endDate", 0)):
                return o["id"]
        return objs[0]["id"] if objs else None

    # ---------- EPG ----------
    def epg_batch(self, channel_ids, start_ts, end_ts):
        ors = " ".join("linear_media_id:'%s'" % c for c in channel_ids)
        ksql = ("(and (or %s) start_date >= '%d' end_date <= '%d' "
                "asset_type='epg' auto_fill=true)" % (ors, start_ts, end_ts))
        out, page = [], 1
        while True:
            res = self._kalt("asset/action/list", {
                "ks": self.ks(), "language": LANG,
                "filter": {"objectType": "KalturaSearchAssetFilter",
                           "orderBy": "START_DATE_ASC", "kSql": ksql},
                "pager": {"objectType": "KalturaFilterPager",
                          "pageSize": 500, "pageIndex": page},
            })
            objs = res.get("objects", []) or []
            out += objs
            if not objs or len(out) >= int(res.get("totalCount", 0)) or page > 25:
                break
            page += 1
        return out

    # ---------- clientTag ----------
    def update_client_tag(self):
        """Stiahne aktuálny clientTag z o2tv.sk JS bundle."""
        import re
        req = urllib.request.Request("https://www.o2tv.sk/", headers={"user-agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
        for m in re.finditer(r'src="([^"]+\.js)"', html):
            src = m.group(1)
            if src.startswith("/"):
                src = "https://www.o2tv.sk" + src
            try:
                rq = urllib.request.Request(src, headers={"user-agent": UA})
                with urllib.request.urlopen(rq, timeout=20) as r2:
                    js = r2.read().decode("utf-8", "ignore")
            except Exception:
                continue
            t = re.search(r'clientTag["\']?\s*[:=]\s*["\']([\d.]+-[A-Za-z]+)["\']', js)
            if t:
                self.s.set("client_tag", t.group(1))
                return t.group(1)
        return None

    # ---------- filter predplatného ----------
    def entitled_ids(self, channel_ids, progress_cb=None):
        """Vráti množinu ID kanálov, ktoré máš v predplatnom.
        progress_cb(hotovo, celkom) sa volá priebežne (na zobrazenie postupu)."""
        ok = set()
        total = len(channel_ids)
        for i, cid in enumerate(channel_ids):
            try:
                pc = self.playback_context(cid, "media", "media", "PLAYBACK")
                if not self.not_entitled(pc):
                    ok.add(cid)
            except O2Error:
                ok.add(cid)      # pri chybe kanál radšej nechaj
            except Exception:
                ok.add(cid)
            if progress_cb:
                progress_cb(i + 1, total)
        # poistka zo sidecaru: podozrivo málo -> neprepisuj cache
        if total and not ok:
            return None
        return ok

    def entitled_cached(self, channel_ids, ttl=43200, progress_cb=None):
        """Filter s 12h cache v nastaveniach addonu."""
        now = int(time.time())
        try:
            ts_ = int(self.s.get("entitled_ts") or 0)
            cached = self.s.get("entitled_ids") or ""
        except Exception:
            ts_, cached = 0, ""
        if cached and now - ts_ < ttl:
            return set(cached.split(","))
        ok = self.entitled_ids(channel_ids, progress_cb)
        if ok is None:
            return set(channel_ids)   # dočasne nechaj všetky
        self.s.set("entitled_ids", ",".join(sorted(ok)))
        self.s.set("entitled_ts", str(now))
        return ok
