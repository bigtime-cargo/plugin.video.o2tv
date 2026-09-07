# plugin.video.o2tv

Kodi doplnok pre O2 TV SK. Beží na Kubuntu (Kodi 21.3 z apt) a na Android TV /
Google TV (Kodi 21, net.kodinerds.maven.kodi21). Každé zariadenie je
sebestačné — žiadny server, žiadny sidecar.

## Štruktúra

    addon.xml                 verzia doplnku — MUSÍ sa zvýšiť pri každej zmene
    addon.py                  zoznam kanálov + prehrávanie (plugin:// URL)
    service.py                od štartu Kodi: denná obnova relácie + export
    resources/settings.xml    starý formát (Kodi 18-21): udid, clientTag, EPG dni
    resources/lib/api.py      Kaltura klient (login, refresh, kanály, EPG, playback)
    resources/lib/export.py   playlist.m3u + epg.xml pre IPTV Simple
    tools/login.py            prihlásenie mimo Kodi (do zipu sa nebalí)

Druhý repozitár: ~/kodi-repo (Kodi repozitár, Pages z /docs). Je natrvalo
pridaný ako pracovný adresár Claude Code cez permissions.additionalDirectories
v .claude/settings.local.json (necommituje sa — absolútna cesta tohto stroja).

## Kaltura API

- host https://3206.frp1.ott.kaltura.com/api_v3/service, partnerId 3206,
  apiVersion 5.4.0, jazyk slk, clientTag 9.66.1-PC
- kanály: asset/action/list, KalturaSearchAssetFilter,
  kSql (and asset_type='714'), pageSize 500;
  číslo kanála v metas.ChannelNumber.value, logo v images (imageTypeId 18)
- playback: multirequest → asset.getPlaybackContext,
  contexty PLAYBACK / CATCHUP / START_OVER, streamerType mpegdash
- licencia: Widevine URL zo sources[].drm[].licenseURL ide priamo do
  inputstream.adaptive.license_key — žiadny proxy
- EPG: asset/action/list s linear_media_id, dávky po 5 kanálov, pageSize 500

## CDN relácia — prečo mrzne archív po pauze

Segmenty nejdú z Kaltury, ale z CDN Cetin (Broadpeak). Manifest z
`sources[].url` sa 307-presmeruje na reláciu `bpk-token/2an@<token>/...`
a všetky segmenty idú relatívne k nej.

- Tá relácia umiera na nečinnosti. Namerané: odstup 30 s prejde, 40 s už
  vráti 403; nepretržité sťahovanie beží ľubovoľne dlho.
- Pri pauze si inputstream.adaptive nepýta segmenty, relácia zomrie a po
  odpauznutí dohrá buffer, potom 403 (6 pokusov) a Kodi prehrávanie ukončí.
- Obísť sa to nedá: segmenty žiadané priamo cez `aw-ucdn` host vracajú 403
  aj s `primaryToken`, redirektor obsluhuje len manifest.
- Ping manifestu na tokenizovanej URL timeout resetuje — overené, segment
  po 180 s "pauzy" s pingami každých 20 s vrátil 200. Na tom stojí keep-alive
  od 1.1.8: `addon.py` rozbalí presmerovanie sám a adresu odovzdá cez window
  property `o2tv.keepalive_url`, vlákno v `service.py` ju počas pauzy pinguje.
  Vlastné vlákno preto, že export EPG blokuje hlavnú slučku na desiatky sekúnd.
- Živému vysielaniu to nepomôže: manifest je dynamický s
  `timeShiftBufferDepth="PT1M1.440S"`, čiže pauza nad ~1 min nemá čo dohrať.

## Relácie — pravidlá, na ktorých sa nešetrí

Prihlásenie len raz cez Keycloak (access token z prehliadača), ďalej
ottuser/action/refreshSession. Stav v session.json v addon_data:
udid, ks, ks_expiry, ks_issued, refresh_token. V nastaveniach Kodi
sa nevypĺňa nič.

1. Každé zariadenie má vlastné UDID aj vlastné prihlásenie. Zdieľané UDID
   zabije reláciu aj refresh token predchádzajúceho zariadenia (500017).
2. Registrácií 30, súbežných streamov 2. Limit dvoch zariadení = streamy.
3. session.json sa na zariadenie kopíruje len raz. Staršia kópia prepíše
   živú reláciu mŕtvou.
4. Kaltura zneplatní refresh token spolu s KS (~7 dní) → obnova raz denne
   (MAX_AGE = 86400 v api.py), nie až pred vypršaním.
5. o2tv.sk nesmie ostať otvorené v prehliadači — webová appka zabije
   doplnkovú reláciu (500016 pár minút po prihlásení).
6. Po 500016 si doplnok raz sám skúsi obnoviť reláciu a zopakovať volanie.
7. UDID zo session.json má prednosť pred nastavením v Kodi (api.py, refresh).
   Nastavenie teda môže ukazovať iné UDID, než sa reálne používa — pri
   ladení sa pozeraj do session.json, nie do nastavení.
8. Zariadenie treba zapnúť aspoň raz za 7 dní. Obnova je aktívna operácia,
   pri vypnutom Kodi ju nemá kto spustiť a token zomrie s KS.
9. Zlyhanie obnovy nie je hneď vidieť: ks() vráti starú KS a doplnok hrá
   ďalej, kým KS platí. Preto service od 1.1.7 varuje, keď je stav starší
   než dva dni, a od 1.1.6 pri štarte overí zapisovateľnosť profilu —
   tichý pád st_set by znamenal, že sa obnovený token nikam neuloží.

## Prihlásenie — keď už refresh nepomôže (500017)

Refresh token žije len so svojou KS (~7 dní). Keď obnova vráti 500017, je
neobnoviteľný a treba nové prihlásenie: `tools/login.py` zapíše výsledok
priamo do session.json a použije UDID, ktoré tam už je — nové UDID by
znamenalo ďalšiu registráciu zariadenia.

Spoľahlivá cesta je access token z prehliadača:

1. prihlás sa na www.o2tv.sk (súkromné okno)
2. DevTools → Network → POST na `openid-connect/token` → z odpovede
   skopíruj `access_token`
3. `python3 tools/login.py --access-token 'eyJ...'`
4. zavri to okno — prihlásená web appka reláciu do pár minút zabije (500016)

Vlastný Keycloak tok (`tools/login.py` bez argumentov) na tomto stroji
neprejde: redirect končí na `www.o2tv.sk/auth/` a tamojšia appka `?code=`
spotrebuje skôr, než ho stihneme odovzdať — Keycloak potom vráti
`invalid_grant: Code not valid`. Dá sa jej to zakázať v DevTools →
Request conditions → blokovanie `*o2tv.sk*`.

### Telka (Android TV) — cez adb z PC

Prehliadač na telke je nepoužiteľný, tak sa prihlasuje z PC proti jej
profilu: `tools/login.py` berie cestu z `O2TV_PROFILE`, takže použije
UDID telky, nie PC. Kopírovaním hotového session.json by sa porušilo
pravidlo 3 — tu ide o vlastné prihlásenie pre jej UDID.

adb je v `~/platform-tools/adb` (stiahnuté od Googlu, sudo netreba —
`apt install adb` cez `!` neprejde, sudo nemá terminál na heslo).
Telka: 192.168.0.129:5555, ladenie ADB v Nastaveniach → Pre vývojárov,
prvé pripojenie treba na TV potvrdiť dialógom.

    A="$HOME/platform-tools/adb -s 192.168.0.129:5555"
    D=/storage/emulated/0/Android/data/net.kodinerds.maven.kodi21/files/.kodi
    T=<pracovný adresár>/telka

    $A connect 192.168.0.129:5555
    $A shell am force-stop net.kodinerds.maven.kodi21   # nech nezapisuje
    $A pull $D/userdata/addon_data/plugin.video.o2tv/session.json $T/
    O2TV_PROFILE=$T python3 tools/login.py --access-token 'eyJ...'
    $A push $T/session.json $D/userdata/addon_data/plugin.video.o2tv/
    $A shell monkey -p net.kodinerds.maven.kodi21 -c android.intent.category.LAUNCHER 1

Kodi treba zastaviť ešte pred `pull`: keby medzitým samo obnovilo reláciu,
push by mu vrátil starý token a zabil ju. Po pushi over `ls -l`, že súbor
ostal `u0_a95` — inak ho Kodi nebude vedieť prepísať. Log telky je v
`$D/temp/kodi.log`, dá sa čítať cez `$A shell grep -a o2tv ...`.

Poznámky z 7. 9. 2026:
- PC aj telka stratili refresh token takmer naraz (5. 9. a 4. 9.), každá
  s vlastným UDID. 500017 teda nemusí byť zdieľané UDID — vtedy to bolo
  niečo na strane O2 a nedalo sa tomu predísť
- prihlásenie zlým číslom vráti 500004 („neevidujeme aktívnu službu") —
  treba číslo služby, ku ktorej je O2 TV zriadená
- `~/o2tv/o2tv_keycloak_login.py` prihlasuje starý docker: berie UDID
  `JBLW…` z `~/o2tv/Data/o2_auth.json` a zapisuje do `Config.json`,
  nie do doplnku. Na doplnok používaj `tools/login.py`
- `!` v Claude Code nemá stdin, takže žiadne `input()` — preto je
  `tools/login.py` rozdelený na dva neinteraktívne kroky

## Publikovanie — po KAŽDEJ zmene doplnku

Zvýš verziu v addon.xml, inak Kodi aktualizáciu neuvidí.

    cd ~/plugin.video.o2tv && git add -A && git commit -m "popis" && git push
    cd ~/kodi-repo && ./build.sh && git add -A && git commit -m "release X.Y.Z" && git push

build.sh balí len súbory sledované gitom — inak by sa do zverejneného
zipu dostalo aj to, čo je v .gitignore (session.json, o2_auth.json, logy).

Adresár tools/ build.sh zo zipu vyhadzuje, takže zmena v ňom nie je
zmenou doplnku a verziu kvôli nej dvíhať netreba.

Na zariadení: repozitár → pravé tlačidlo → Skontrolovať aktualizácie
(Kodi má dennú cache) → update → reštart Kodi.

## Ladenie

Log ~/.kodi/temp/kodi.log, prepisuje sa pri každom štarte Kodi — chybu
najprv vyvolať, až potom čítať. Na Androide cez Kodi Logfile Uploader → View
(nie Upload, log obsahuje token a UDID).

    grep -a "o2tv" ~/.kodi/temp/kodi.log | tail -20

Riadok [o2tv] resolve: ukazuje vetvu (LIVE / CATCHUP / START_OVER).
Riadok [o2tv.service] zápis stavu overený musí prísť hneď po štarte.

Registrované zariadenia sa dajú vypísať cez householddevice/action/list.
Kubuntu a telka majú vlastné UDID; JBLW je osirelý pozostatok po starom
docker o2tv-iptvserveri (ten už nebeží, ostal po ňom ~/o2tv/Data a
prihlasovací skript s docker compose restart). Telka ho mala zdedený
kópiou session.json — 3. 9. 2026 dostala vlastné UDID.

## Ako pracovať v tomto repozitári

- Jeden krok naraz, počkaj na výstup.
- Nehádaj. Keď nie je jasné, čo sa deje, vypýtaj si log alebo výpis.
- Keď niečo neprejde, hľadaj príčinu, neopakuj ten istý pokus.
- Trade-offy pomenuj hneď, nie až keď na ne narazím.
- Skorší nesprávny záver priznaj.
- Odpovedaj po slovensky, stručne.

## Čo nikdy

- Necommitovať session.json, o2_auth.json, tokeny ani UDID.
- Nemazať ~/o2tv/o2tv_keycloak_login.py — treba ho na každé prihlásenie.
- Cron a o2tv-doctor.service sú vypnuté zámerne (rotovali by token).
