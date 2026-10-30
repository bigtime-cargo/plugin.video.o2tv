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
    resources/lib/sso.py      automatické schválenie kódu zariadenia (SSO cookie)
    tools/test_sso.py         offline testy (python3 -m unittest tools.test_sso -v)
    tools/auto_approve.py     ručná skúška schválenia na ostrom O2
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

## Archív bežiaceho programu a pokračovanie na ďalší (1.1.18)

Zmerané 14. 10. 2026 na telke (ISA 21.5.25):

- Program pustený z EPG, kým ešte beží, dá CDN ako **dynamic** manifest
  (`timeShiftBufferDepth` = od začiatku programu po teraz). Po konci
  programu ten istý manifest prepne na `static` s plnou dĺžkou — ale ISA
  zmenu typu ignoruje, ďalej ho každé 2 s obnovuje a koniec nenahlási.
  Obraz stál na 54:57 z 55:00 ešte 25 min po konci, takže Kodi
  `pvrplayback.autoplaynextprogramme` (zapnuté) sa nikdy nespustilo.
  Rieši to `EndWatch` v `service.py`: stojaca pozícia ≤ 15 s pred koncom
  10 s, program podľa EPG skončil → `Player.Open {"broadcastid": ďalší}`.
- Bez `inputstream.adaptive.play_timeshift_buffer` ISA dynamic manifest
  púšťa od živého okraja, nie od začiatku programu. Doplnok ho nastavuje
  pri každom archíve (`start_ts`); static manifest ho ignoruje.
- Obnova po mŕtvej CDN relácii (pauza nad 45 min alebo 403) musí program
  z EPG pustiť znova cez EPG (`Player.Stop` + `Player.Open {"broadcastid"}`),
  nie cez `o2tv.play_url`. S adresou doplnku Kodi zabudne, že hrá program
  z EPG, a po riadnom EOF nepustí ďalší. Stalo sa 15. 10. 2026, opravené
  v 1.1.19 (`KeepAlive.reopen_epg`).
- Po `Player.Stop` nestačí čakať na `isPlaying()` = false: prehrávač to
  hlási hneď na začiatku `CloseFile`, PVR program „pustí z hlavy“ až po jeho
  dobehnutí. Dovtedy `Player.Open {"broadcastid"}` vráti `OK` a nič nespraví
  (`CPVRGUIActionsPlayback::PlayEpgTag` pri `IsPlayingEpgTag` len prepne na
  celú obrazovku). 19. 10. 2026 sa zatváranie pri zamrznutom CDN ťahalo 4 s
  a obnova skončila v menu (`obnova sa nerozbehla do 30 s`). Od 1.1.20 sa
  čaká aj na `PVR.IsPlayingEpgTag` = false a po `Open` sa overí, že niečo
  hrá (2 pokusy, potom adresa doplnku). Keď sa starý program nezavrie ani
  do 15 s, `Open` sa nevolá vôbec a ide rovno adresa doplnku — inak by
  kontrola `isPlaying()` po `Open` zachytila ešte ten starý stream a
  obnova by sa tvárila ako úspešná.
- IPTV Simple pre bežiaci program posiela `end_ts` = **teraz − 5 min**
  a `start_ts` = začiatok − 5 min. Program, ktorý začal pred < 5 min, tak
  celý leží mimo rozsahu a hľadanie cez stred rozsahu vrátilo
  predchádzajúci program. `_find_programme` preto berie program so
  začiatkom najbližšie k `start_ts + BEGIN_BUFFER`.
- Názov `CATCHUP`/`START_OVER` v logu `resolve:` o type manifestu nič
  nehovorí — kvôli tomu `end_ts` je bežiaci program skoro vždy `CATCHUP`.

## Odolnosť služby (1.1.21–1.1.22, audit 29. 10. 2026)

- `_post` mení sieťové chyby (`OSError`: URLError, timeout) na `O2Error(code="NET")`,
  zlý JSON v `_kalt` na `O2Error(code="BADJSON")`. Inak obišli každé `except O2Error`
  a ukončili hlavnú slučku služby (do reštartu Kodi žiadna obnova ani export).
  Slučka v `main()` je navyše celá v `try` — nesmie skončiť.
- `ks()` po zlyhanej obnove 1 h (500017) / 2 min (iné) nevolá refresh znova
  (`_hold_until`, len v rámci procesu). Predtým export skúšal mŕtvy refresh pri
  každom volaní API (13× za 46 s).
- `KeepAlive.ours()`: keď 20 s (`FOREIGN_FORGET`) hrá cudzie video, adresy O2
  sa zabudnú — inak by `restart()` nahradil film zo SC kanálom O2. Za O2 sa
  považuje manifest z `KEEPALIVE_PROP`, `plugin://plugin.video.o2tv/` aj `pvr://`
  (IPTV Simple na telke číta len náš export). Ktorú adresu vráti
  `getPlayingFile()` pri pluginovej položke, na telke overené nie je.
- Zlyhaná dávka EPG: `export_all` vyhodí `Incomplete` a nezapíše nič, kým je
  staré `epg.xml` mladšie než 24 h (`KEEP_OLD_EPG`); staršie prepíše aj neúplným.
  Neúspešný export skúša znova o 30 min (`EXPORT_RETRY`), nie každých 10.
- `entitled_ids` pri sieťovej chybe vráti None (nič sa neuloží, použije sa
  posledný známy zoznam) a pri vypínaní Kodi skončí; neúplný výsledok sa
  neukladá na 12 h.
- Na telku sa nasadzuje cez `~/kontrola-TCL/nastroje/kontrola-zaplat.sh --oprav`
  (porovnáva obsah, nie číslo verzie).

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
   (MAX_AGE = 86400 v api.py), nie až pred vypršaním. `refreshSession`
   vracia **ten istý** refresh token (nerotuje) a žiadnu jeho expiráciu —
   merané 11. 9. 2026 dvoma obnovami za sebou. Takže token neprežíva svoju
   KS a nemá zmysel hľadať chybu v tom, čo doplnok ukladá.
   **Obnova životnosť tokenu nepredlžuje.** Token je viazaný na KS z
   prihlásenia, nie na tú poslednú obnovenú — po ~7 dňoch vráti 500017 aj
   zariadenie, ktoré sa poctivo obnovovalo každý deň. Nové prihlásenie je
   teda pravidelná údržba, nie porucha: PC aj telka padli 4.–5. 9., znova
   13. 9. (obe v ten istý deň, každá s vlastným UDID) a nanovo prihlásené
   15. 9. 2026. Hľadať príčinu v doplnku netreba — stačí device flow.
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
10. Od 1.1.16 ukáže service pri každom štarte Kodi odpočet „Prihlásenie
   platí ešte X dní" (`session_notice`). Ráta sa z nového poľa `login_time`
   v session.json, nie z `ks_expiry` — ten sa každou obnovou posúva, takže
   by ukazoval stále 7 dní. Počas behu sa toast ozve, len keď ostávajú
   ≤ 2 dni alebo obnova padá, a nanajvýš raz za 6 hodín. Zariadeniu bez
   `login_time` sa hodnota raz odhadne z `ks_expiry` (`login_time_est`,
   toast to prizná slovom „odhad") — odhad je optimistický o toľko, koľko
   relácia už beží.

## Prihlásenie — keď už refresh nepomôže (500017)

Refresh token žije len so svojou KS (~7 dní). Keď obnova vráti 500017, je
neobnoviteľný a treba nové prihlásenie: `tools/login.py` zapíše výsledok
priamo do session.json a použije UDID, ktoré tam už je — nové UDID by
znamenalo ďalšiu registráciu zariadenia.

### Kód zariadenia — od 1.1.11 hlavná cesta

Keycloak O2 povoľuje device flow, takže prihlásenie ide priamo z Kodi
(doplnok → „Prihlásiť sa nanovo (kód zariadenia)") alebo z terminálu
`python3 tools/login.py --device`. Doplnok ukáže kód, ten sa potvrdí na
`o2.sk/zariadenie` z mobilu. Na telke tým odpadá celý adb postup nižšie.

**Prihlasovať sa treba číslom služby a SMS kódom.** Prihlásenie e-mailom
a heslom vydá token, ktorý vyzerá rovnako, ale chýba mu claim
`authenticated_via_subscriber_id` (a `authenticated_via` je `PASSWORD`
namiesto `OTP`) — Kaltura taký token odmietne s 2026 „Activation token
not found". Overené 11. 9. 2026 na všetkých troch cestách.

Technicky (api.py, `device_start` / `device_poll`): POST na
`identity.o2.sk/realms/o2/protocol/openid-connect/auth/device` s
`client_id=o2-xtv-kaltura`, potom sa každých `interval` sekúnd pýta token
grantom `urn:ietf:params:oauth:grant-type:device_code`, kým nepríde niečo
iné než `authorization_pending`. Access token ide do `login_with_token`,
teda do `ottuser/action/login` s `loginType=accessToken` a UDID zo stavu.

### Registrácia zariadenia do domácnosti

Prihlásenie zariadenie do domácnosti **nezapíše**. Bez toho má nové UDID
platnú reláciu, kanály aj EPG mu chodia, ale `getPlaybackContext` vráti
`1003 Device not in household` a neprehrá sa nič. Na PC a telke to nebolo
vidieť — tie registráciu dostali ešte pred doplnkom.

Od 1.1.15 to rieši `register_device()` v api.py: po prihlásení zavolá
`householddevice/action/add`, a `playback_context` sa pri 1003 raz sám
zaregistruje a volanie zopakuje (takže zariadenia prihlásené staršou
verziou sa spravia samy).

**`brandId` je povinné.** Bez neho Kaltura odmietne nové UDID s
`1002 Device type not allowed`. Používa sa 22 = rodina 5 (PC/MAC), ktorú
majú všetky registrované zariadenia domácnosti a ktorej zodpovedá aj
clientTag `...-PC`. Overené 14. 9. 2026: bez brandId 1002, s ním OK.
Už registrované UDID vráti `1015` — duplicitu kontroluje skôr než typ,
takže na overení brandId sa existujúce zariadenie použiť nedá.

Zoznam zariadení: `householddevice/action/list` (bez `filter`, ten vráti
500054). Vypíše udid, brandId, deviceFamilyId a stav.

### Access token z prehliadača — záložná cesta

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

### Čo sa skúšalo a nefunguje (15. 9. 2026, netreba opakovať)

- **udržať Kaltura reláciu cez dlhodobú Keycloak SSO reláciu** — nápad:
  keby Keycloak refresh token žil dlho, dal by sa ním pravidelne získavať
  čerstvý access token a Kalture sa prihlasovať nanovo bez človeka.
  Zmerané 15. 9. 2026 na device-flow tokene klienta `o2-xtv-kaltura`:
  `refresh_expires_in` je 300 s a `grant_type=refresh_token` ho
  nepredlžuje — tri volania po sebe (300 → 277 → 206, presne s plynúcim
  reálnym časom) potvrdili tvrdý strop SSO relácie, nie idle timeout.
  Webová appka o2tv.sk (JS bundle, 15. 9. 2026) preto Keycloak token
  používa len ako jednorazový bootstrap do `ottuser/action/login` — od
  tej chvíle beží všetko na Kaltura KS/refreshToken, nezávisle od
  Keycloaku. Jej vlastný obnovovací endpoint (`CZ/action/Invoke`,
  „Refresh Token") je na niečo iné a pre partnera 3206 vracia 404, takže
  ani ten nie je náhradná cesta. Kaltura väzba KS↔refreshToken (~7 dní,
  pravidlo 4) je teda skutočný a jediný strop — obídenie neexistuje.

### Čo sa skúšalo a nefunguje (11. 9. 2026, netreba opakovať)

- **offline_access** — `identity.o2.sk` ho má v `scopes_supported`, ale
  token vráti `not_allowed: Offline tokens not allowed for the user or
  client`. Trvalý token teda neexistuje.
- **direct grant (heslo)** — klient `o2-xtv-kaltura` ho povoľuje a token
  vydá bez SMS, lenže bez `authenticated_via_subscriber_id` → Kaltura 2026.
  Keycloak tokeny navyše žijú len 300 s, takže ani ako záloha nepomôžu.
- **naprogramovaný formulárový login** — prihlasovacia stránka Keycloaku
  je chránená **Cloudflare Turnstile** („Prihlásenie nebolo vyhodnotené
  ako bezpečné"). Obchádzať sa nebude.

Záver (11. 9.): bez človeka sa nedá prihlásiť heslom ani OTP. Neskôr sa
našla cesta cez SSO cookie z prehliadača, ktorá schváli device flow sama —
viď „Automatické prihlásenie (1.2.0)“.

### Telka (Android TV) — cez adb z PC (od 1.1.11 už len záloha)

Prvá voľba je prihlásenie kódom zariadenia priamo v Kodi na telke. Tento
postup ostáva pre prípad, že by device flow prestal fungovať.

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

## Automatické prihlásenie (1.2.0)

Doplnok sa po vypršaní relácie (500017, pravidlo 4) vie prihlásiť nanovo
sám — bez človeka, bez kódu na mobile. Ručné prihlásenie (kód zariadenia)
ostáva ako záloha a nič z neho sa neodstránilo.

**Ako to ide:** `O2API.auto_login(cookies_path)` (api.py) vyžiada kód
zariadenia (`device_start`), `sso.approve` (resources/lib/sso.py, bez
importov Kodi) ho schváli cez SSO cookie ako používateľ rukou na
`o2.sk/zariadenie` (POST „Pokračovať“, POST „Povoliť“), potom sa v cykle
(max ~40 s, interval z odpovede) volá `device_poll` a token ide do
`login_with_token` — rovnaká cesta ako po ručnom device flow (ukladá
`login_time`, ponecháva existujúce UDID, volá `register_device`). Nové UDID
sa nevyrába; bez UDID auto_login skončí s `NOSESSION`. Pri zlyhaní sa
session.json nemení. Server cookie pri použití obnoví (nová hodnota
KEYCLOAK_IDENTITY v Set-Cookie), doplnok ju uloží späť.

**Kde je cookie:** `sso.json` v profile doplnku (addon_data/plugin.video.o2tv/),
práva 600, atómový zápis. Do Kodi sa dostane tlačidlom v nastaveniach
(„Automatické prihlásenie“ → „Importovať SSO cookie zo súboru“): hľadá
`o2-sso.json` v `/storage/emulated/0/Download/`, `~/Downloads`, `~/`
(inak ponúkne výber súboru), po overení ho uloží a **zdrojový súbor zmaže**.
„Zmazať SSO cookie“ odstráni `sso.json`. Cookie je JSON
`{"KEYCLOAK_IDENTITY": "...", "KEYCLOAK_SESSION": "..."}` (DevTools →
Application → Cookies → https://identity.o2.sk). Nikdy do settings.xml.

**Kedy sa spúšťa (service.py, `AutoLogin`):** v hlavnej slučke (aj raz po
štarte), keď `sso.json` existuje A (`session_left()` ≤ 2 dni ALEBO refresh
vrátil 500017). Poistky: po neúspechu najbližší pokus o 1 h, po úspechu nie
skôr ako o 6 h, max 3 neúspechy po sebe, potom pauza do reštartu Kodi
(nový import cookie ju vynuluje). Úspech = toast „Prihlásenie obnovené
automaticky“; pri zlyhaní ostáva pôvodný toast o ručnom prihlásení.

**Poistky v sso.py:** klikne sa LEN na tlačidlá s popiskom presne
„Pokračovať“ a „Povoliť“ (nikdy „Pokračovať s iným účtom“, „Zamietnuť“).
Zastaví sa pri neznámom poli formulára, pri skutočnom widgete
Turnstile/CAPTCHA (`WIDGET`; samotný skript `turnstile/api.js` v hlavičke
šablóny widget nie je), pri odkaze (aj presmerovaní) na iný host než
`identity.o2.sk` a po viac než 8 krokoch. Schvaľuje sa len kód, ktorý si
doplnok práve sám vyžiadal. **Turnstile sa neobchádza** — ak ho O2 pridá aj
na túto cestu, auto_login zlyhá s hláškou a treba ručné prihlásenie.

**Čo je overené:** `tools/auto_approve.py` (29. 10. 2026, ostrý O2): dva POST-y
(Pokračovať, Povoliť), token endpoint vydal token s `authenticated_via=OTP`
a `authenticated_via_subscriber_id`; cookie je kĺzavé (server ho pri každom
použití obnoví, ~154 dní). Celý reťazec `auto_login` → `login_with_token`
overený naostro v Kodi 29. 10. 2026 na telke (pokus → úspech za ~3 s) aj na PC
(offline testy: `python3 -m unittest tools.test_sso -v`).

**Okamžité prihlásenie z doplnku (1.2.1):** služba kontroluje reláciu len raz za
10 min, takže pri 500017 medzi kolami by používateľ videl chybu, hoci cookie
existuje (stalo sa 29. 10. na PC hneď po importe). `addon.py` preto pri 500017 /
NOSESSION v `play()` a v zozname kanálov zavolá `auto_relogin()` (progress
„Obnovujem prihlásenie…", potom jedno opakovanie) a rovnako po importe cookie,
keď do konca ostáva ≤ 2 dni. Súbehu so službou bráni window property
`o2tv.auto_busy` (čas začiatku, zabudnutý zámok po 150 s); úspešné prihlásenie
z doplnku nastaví `o2tv.auto_done`, podľa čoho služba zahodí príznak mŕtveho
refreshu a nepokúša sa znova.

**Bezpečnosť:** SSO cookie je kľúč k CELÉMU O2 účtu (nielen k O2 TV). Nikdy
do gitu (`sso.json`, `o2-sso.json` sú v .gitignore), do logov, výnimiek,
toastov ani do settings.xml. Zdrojový `o2-sso.json` sa po importe maže.
Kto má telku/profil, má účet — preto práva 600 a import len na vlastné zariadenia.

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

## Preskakovanie reklám (1.3.0, 30. 10. 2026)

Manifest odvysielaného programu (static) nesie `EventStream` so SCTE-35
`splice_insert` (cue-out s `break_duration`, opakovaný každých ~10–20 s, a
cue-in). `resources/lib/ads.py` z nich skladá bloky, `AdSkip` v `service.py`
pri vstupe do bloku spraví `seekTime` na jeho koniec (každý blok najviac raz).
Nastavenie `skip_ads` (Prehrávanie), predvolene **vypnuté**. Testy:
`python3 -m unittest tools.test_ads`.

- Poloha = `presentationTime − presentationTimeOffset` (s od začiatku programu).
- Bežiaci program (dynamic) EventStream v MPD nemá, značky idú len inband
  (emsg v segmentoch) — nepodporované; služba manifest znova číta každých 30 s,
  takže ak sa tam raz objavia, zaberie to samo.
- V logu Kodi: `[o2tv.service] reklamné bloky (...)` a `reklama a:bb–c:dd — preskakujem`.
