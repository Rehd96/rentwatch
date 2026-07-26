# rentwatch — istruzioni progetto

Monitor degli affitti di Torino su immobiliare.it: scraper → SQLite → dashboard
FastAPI + report Markdown. Obiettivo personale di Ion: trovare casa in affitto
senza controllare gli annunci a mano.

## Comandi

```bash
.venv/bin/python -m rentwatch scrape             # aggiorna DB + reports/overview.md
.venv/bin/python -m rentwatch scrape --max-pages 2   # test veloce (niente deactivation)
.venv/bin/python -m rentwatch serve              # dashboard http://127.0.0.1:8777
.venv/bin/python -m rentwatch report             # rigenera solo il report MD
```

Non c'è una test suite: verificare con `scrape --max-pages 2` + `curl /api/overview`.

## Architettura

- `rentwatch/scraper.py` — client dell'API interna `api-next/search-list/listings/`
  via **curl_cffi** `impersonate="chrome"` (HTTP semplice → 403 sul TLS fingerprint).
- `rentwatch/db.py` — SQLite `data/rentwatch.db`: `listings` (first/last_seen,
  is_active, `hidden` = scartato a mano, `liked` = preferito/watchlist),
  `price_history`, `scrape_runs`. Migrazioni additive in `_migrate()`.
- Gli annunci `hidden` restano fuori da `/api/listings` (default) e dalle
  tabelle del report, ma dentro le statistiche di mercato (mediane, zone).
- `db.is_suspect()` — euristica "prezzo per stanza/studenti" (€/m² < 5 o
  prezzo/locali < 120 con 4+ locali): flag soft, mai esclusione dal DB;
  esclusi però dalle mediane €/m² e dalle tabelle del report.
- `rentwatch/web.py` + `static/index.html` — dashboard single-page, dati da /api/*.
- `rentwatch/report.py` — snapshot Markdown in `reports/overview.md` (per mobile).
- `config.toml` — ricerche e Telegram; `config.local.toml` (gitignored) ha precedenza.

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
- Nessuna dipendenza oltre requirements.txt senza motivo forte.
- La deactivation degli annunci scatta solo su scrape completo (mai con --max-pages).
