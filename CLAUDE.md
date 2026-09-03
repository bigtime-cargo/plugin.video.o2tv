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

Druhý repozitár: ~/kodi-repo (Kodi repozitár, Pages z /docs).

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

## Publikovanie — po KAŽDEJ zmene doplnku

Zvýš verziu v addon.xml, inak Kodi aktualizáciu neuvidí.

    cd ~/plugin.video.o2tv && git add -A && git commit -m "popis" && git push
    cd ~/kodi-repo && ./build.sh && git add -A && git commit -m "release X.Y.Z" && git push

Na zariadení: repozitár → pravé tlačidlo → Skontrolovať aktualizácie
(Kodi má dennú cache) → update → reštart Kodi.

## Ladenie

Log ~/.kodi/temp/kodi.log, prepisuje sa pri každom štarte Kodi — chybu
najprv vyvolať, až potom čítať. Na Androide cez Kodi Logfile Uploader → View
(nie Upload, log obsahuje token a UDID).

    grep -a "o2tv" ~/.kodi/temp/kodi.log | tail -20

Riadok [o2tv] resolve: ukazuje vetvu (LIVE / CATCHUP / START_OVER).

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
