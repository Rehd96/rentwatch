# rentwatch — istruzioni progetto

Monitor degli affitti di Torino su immobiliare.it: scraper → SQLite → dashboard
FastAPI + report Markdown. Obiettivo personale di Ion: trovare casa in affitto
senza controllare gli annunci a mano.

## Comandi

```bash
.venv/bin/python -m rentwatch scrape             # aggiorna DB + reports/overview.md
.venv/bin/python -m rentwatch scrape --max-pages 2   # test veloce (niente deactivation)
.venv/bin/python -m rentwatch serve              # dashboard http://127.0.0.1:8777
.venv/bin/python -m rentwatch serve --root-path /case   # dietro nginx su /case/
.venv/bin/python -m rentwatch report             # rigenera solo il report MD
.venv/bin/python -m rentwatch set-password       # aggiunge utente / cambia password
.venv/bin/python -m rentwatch list-users         # account della dashboard
.venv/bin/python -m rentwatch remove-user        # toglie un account
.venv/bin/python -m rentwatch telegram-test      # verifica token + chat id
.venv/bin/python -m rentwatch bot                # loop comandi Telegram
```

Non c'è una test suite: verificare con `scrape --max-pages 2` + login su
`/login` + `curl /api/overview` (401 senza cookie, 200 con).

## Architettura

- `rentwatch/scraper.py` — client dell'API interna `api-next/search-list/listings/`
  via **curl_cffi** `impersonate="chrome"` (HTTP semplice → 403 sul TLS fingerprint).
- `rentwatch/db.py` — SQLite `data/rentwatch.db`: `listings` (first/last_seen,
  is_active, `hidden` = scartato a mano), `price_history`, `scrape_runs`,
  `favourites`, `notify_queue`. Migrazioni additive in `_migrate()`.
- **Preferiti per utente**: tabella `favourites (listing_id, username)`. Il ♥ è
  personale, il ✕ `hidden` resta condiviso (scartare è una decisione comune).
  `listings.liked` sopravvive come flag derivato — lo tiene aggiornato
  `set_favourite()` — così report e query vecchie continuano a funzionare.
  `adopt_legacy_likes()` assegna al primo utente i ♥ messi prima degli account.
  `/api/listings` restituisce `liked_by` (tutti) e `liked` (solo il tuo).
- Gli annunci `hidden` restano fuori da `/api/listings` (default) e dalle
  tabelle del report, ma dentro le statistiche di mercato (mediane, zone).
- `db.is_suspect()` — euristica "prezzo per stanza/studenti" (€/m² < 5 o
  prezzo/locali < 120 con 4+ locali): flag soft, mai esclusione dal DB;
  esclusi però dalle mediane €/m² e dalle tabelle del report.
- `rentwatch/web.py` + `static/index.html` — dashboard single-page, dati da /api/*.
- `rentwatch/auth.py` — PBKDF2 + cookie di sessione firmato HMAC, solo stdlib.
  Il gate è un **middleware**, non una dependency per rotta: una rotta aggiunta
  domani nasce protetta. Pubbliche solo `/login` e `/healthz`.
  Gli account stanno in `[[auth.users]]`; `verify_login()` fa comunque un
  PBKDF2 su un hash fittizio quando l'utente non esiste, altrimenti il tempo di
  risposta direbbe quali nomi sono veri. `config._normalise_auth()` converte i
  vecchi `[auth] username/password_hash` singoli nella lista e li rimuove: una
  sola fonte di verità, migrata al primo salvataggio.
- `rentwatch/notify.py` — Telegram: filtri, template, ore di silenzio, ribassi.
  In ore di silenzio le notifiche finiscono in `notify_queue` e partono al primo
  run utile — non si perdono.
- `rentwatch/bot.py` — long-poll `getUpdates`, risponde **solo** alle chat in
  `[[telegram.recipients]]`. Niente webhook, niente porte esposte. Rilegge il
  config a ogni messaggio, così un destinatario appena aggiunto parla subito.
- **Destinatari multipli**: `[[telegram.recipients]]` (`chat_id` + `user`
  facoltativo che collega la chat a un account). `send_message()` senza
  `chat_id` manda a tutti e torna True se almeno uno ha ricevuto — una chat
  bloccata non deve far risultare fallita la notifica per gli altri.
  `check_credentials()` prova ogni destinatario e dice chi non ha ricevuto.
  `config._normalise_telegram()` converte il vecchio `chat_id` singolo.
- `rentwatch/settings_store.py` — scrive `config.local.toml` dalla dashboard
  (mini-emitter TOML: `tomllib` legge ma non scrive, e tomli-w non vale una
  dipendenza in più). Scrittura atomica + `.bak`.
- `rentwatch/report.py` — snapshot Markdown in `reports/overview.md` (per mobile).
- `config.toml` — ricerche e Telegram; `config.local.toml` (gitignored) ha precedenza.

## Montaggio sotto prefisso (`/case/`)

nginx toglie `/case` (`proxy_pass http://127.0.0.1:8777/;` con slash finale),
`--root-path /case` lo rimette su link, form di login e cookie. Le pagine sono
servite sostituendo `__BASE__`, quindi gli stessi file HTML funzionano sia alla
radice sia sotto prefisso. **Attenzione:** `request.url.path` include il
root_path — usare `app_path()` per i confronti, altrimenti `/login` smette di
essere pubblica e la pagina di login redirige a se stessa.

## Vincoli dell'API immobiliare.it (scoperti sul campo — non "migliorarli")

- Senza `paramsCount` nella query l'endpoint risponde 500.
- **Max 2000 risultati (80 pagine) per ricerca**: `pag=81` → HTTP 418 sempre,
  con qualunque sessione/IP; `maxPages` dichiara più pagine di quelle servite.
  Per questo `iter_listings` partiziona in fasce di prezzo. Backoff/retry sul
  418 di pagina 81 sono inutili.
- Tenere i delay (1.5–3 s) e la rotazione sessione: siamo ospiti.
- Torino = `idComune=9987`, affitto = `idContratto=2`, residenziale = `idCategoria=1`.

## Convenzioni

- UI e documentazione utente in italiano; codice e log in inglese.
- Nessuna dipendenza oltre requirements.txt senza motivo forte. Per questo auth
  e TOML-writer sono stdlib, e il form di login è parsato a mano invece di
  usare `fastapi.Form` (che tirerebbe dentro `python-multipart`).
- La deactivation degli annunci scatta solo su scrape completo (mai con --max-pages).
- Segreti (`password_hash`, `bot_token`) solo in `config.local.toml`, mai in
  `config.toml` e mai rimandati al browser: le API li restituiscono come
  `bot_token_set: true/false`.
