# rentwatch — affitti a Torino senza controllare 100 annunci a mano

Servizio locale che scarica periodicamente gli annunci di affitto di
**immobiliare.it** per Torino, li salva in SQLite e li mostra in una dashboard
con filtri, €/m², giorni sul mercato, cali di prezzo e statistiche per zona.
Opzionale: notifica Telegram per ogni nuovo annuncio.

## Come funziona

- `rentwatch/scraper.py` chiama l'API JSON interna di immobiliare.it
  (`/api-next/search-list/listings/`) tramite `curl_cffi` con impersonazione
  Chrome (una richiesta HTTP normale riceve 403 dal controllo del TLS
  fingerprint). Nessun parsing HTML: i dati arrivano già strutturati.
- `rentwatch/db.py` salva tutto in `data/rentwatch.db`: prima/ultima
  apparizione di ogni annuncio, storico prezzi, annunci rimossi.
- `rentwatch/web.py` + `static/index.html`: dashboard su `http://127.0.0.1:8777`.

## Setup

```bash
cd ~/Immobiliare_homes
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

```bash
# scarica/aggiorna tutti gli annunci (prima volta: ~4 minuti per ~120 pagine)
.venv/bin/python -m rentwatch scrape

# test veloce su poche pagine
.venv/bin/python -m rentwatch scrape --max-pages 3

# dashboard su http://127.0.0.1:8777
.venv/bin/python -m rentwatch serve

# rigenera solo il report Markdown
.venv/bin/python -m rentwatch report
```

## Report Markdown (per il telefono)

Ogni scrape rigenera **`reports/overview.md`**: panoramica, ultimi annunci,
cali di prezzo e mediane per zona in puro Markdown. Pushando il repo su GitHub
(privato) il report è leggibile da mobile dopo ogni aggiornamento; in
alternativa la dashboard stessa può girare su un VPS — scaffold Docker + nginx
in `deploy/vps/`, passi in `TODO.md`.

## Configurazione (`config.toml`)

Le ricerche usano gli stessi parametri degli URL di immobiliare.it
(`prezzoMassimo`, `localiMinimo`, `superficieMinima`, …). Si possono definire
più ricerche `[[searches]]`. Per modifiche personali senza toccare il file
versionato, copiare in `config.local.toml` (ha precedenza ed è in .gitignore).

### Telegram (opzionale)

1. Crea un bot con [@BotFather](https://t.me/BotFather) → ottieni il token.
2. Scrivi al bot, poi prendi il tuo chat id da [@userinfobot](https://t.me/userinfobot).
3. In `config.toml`:

```toml
[telegram]
enabled = true
bot_token = "123456:ABC..."
chat_id = "12345678"
```

Alla prima scansione (backlog) non viene inviato nulla; dalle successive, un
messaggio per ogni nuovo annuncio (max 20 per esecuzione).

## Scansione automatica ogni ora (systemd user timer)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/rentwatch-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rentwatch-scrape.timer   # scrape ogni ora
systemctl --user enable --now rentwatch-web.service    # dashboard sempre attiva
```

Verifica: `systemctl --user list-timers` e `journalctl --user -u rentwatch-scrape`.

## Note

- **Rate limiting**: il client attende 1.5–3 s tra le pagine (configurabile con
  `request_delay`), ruota la sessione ogni 40 pagine e fa backoff su 418/429.
- **Limite di 2000 risultati**: l'API risponde **HTTP 418** oltre pagina 80 di
  qualunque ricerca (`maxPages` mente). Le ricerche più grandi vengono
  partizionate automaticamente in fasce di prezzo, ognuna sotto il limite.
- **Annunci "rimossi"**: se un annuncio non compare più in una scansione
  completa viene marcato inattivo (probabilmente affittato) ma resta nel DB —
  utile per capire quanto restano sul mercato.
- **Annunci nascosti**: il bottone ✕ nella dashboard marca un annuncio come
  "non mi interessa" (`hidden` nel DB): sparisce da dashboard e report ma il
  flag sopravvive agli scrape. Recuperabile con "mostra nascosti" → ↩.
- **Filtro "novità"**: mostra solo gli annunci apparsi negli ultimi 1/3/7
  giorni — il modo rapido per vedere solo cosa c'è di nuovo dall'ultimo giro.
- **Prezzi sospetti (€/stanza?)**: molte agenzie per studenti pubblicano la
  stanza come "appartamento" (prezzo a persona, superficie dell'intero
  alloggio). Euristica: €/m² < 5, oppure prezzo/locali < 120€ con 4+ locali.
  Il badge li segnala, "nascondi prezzi sospetti" (attivo di default) li
  toglie dalla vista e dalle mediane €/m² — un click e riappaiono.
- **Preferiti (♥)**: i preferiti restano visibili con "❤ solo preferiti"
  anche dopo la rimozione dell'annuncio (stato "rimosso") — utile per vedere
  quanto in fretta spariscono le case interessanti. Sezione dedicata anche
  in `reports/overview.md`.
- **Altri portali (Idealista, Subito)**: per la sola notifica di nuovi annunci
  su più portali c'è [flathunter](https://github.com/flathunters/flathunter)
  (supporta Immobiliare, Idealista e Subito, notifiche Telegram, Docker).
  rentwatch copre Immobiliare.it con in più storico prezzi e dashboard.
- L'endpoint API richiede i parametri `paramsCount` e `path`, altrimenti
  risponde 500. Se immobiliare.it cambia l'API, controllare la struttura in
  `__NEXT_DATA__` di una pagina di ricerca.
