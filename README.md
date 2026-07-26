# rentwatch

Monitor degli annunci di **affitto su immobiliare.it**: scarica periodicamente
le inserzioni di una città, le tiene in un database SQLite e le mostra in una
dashboard con €/m², giorni sul mercato, cali di prezzo e statistiche per zona.

Nato da un problema concreto: cercare casa in affitto a Torino significa
ricontrollare a mano centinaia di annunci ogni giorno, senza modo di capire
quali sono nuovi, quali hanno abbassato il prezzo e quali sono già stati
affittati. rentwatch fa quel lavoro e presenta solo la differenza.

![Dashboard rentwatch](docs/dashboard.png)

## Cosa fa

- **Storico dei prezzi.** Ogni variazione viene registrata: la dashboard mostra
  di quanto è scesa la richiesta rispetto alla prima pubblicazione.
- **Annunci scomparsi.** Un annuncio che non compare più in una scansione
  completa viene marcato come rimosso (probabilmente affittato) ma resta nel
  database — così si vede quanto in fretta va via ciò che è interessante.
- **€/m² per zona.** Mediane per macrozona: il modo più rapido per capire se un
  canone è in linea o fuori mercato.
- **Filtro "novità".** Solo gli annunci apparsi nelle ultime 24 ore / 3 / 7
  giorni: la vista "cosa è cambiato da ieri".
- **Scarta (✕) e preferiti (♥).** Gli annunci scartati non ricompaiono più
  nemmeno dopo gli aggiornamenti; i preferiti restano tracciati anche quando
  vengono ritirati dal portale.
- **Prezzi sospetti.** Molte agenzie per studenti pubblicano la *stanza* come
  appartamento: prezzo a persona, superficie dell'alloggio intero. Un'euristica
  (€/m² < 5, oppure canone/locali < 120 € con 4+ locali) li segnala con un badge
  e li esclude dalle mediane, senza cancellarli: a volte sono davvero
  l'occasione giusta.
- **Report Markdown** rigenerato a ogni scansione, leggibile dal telefono.
- **Notifiche Telegram** opzionali per ogni nuovo annuncio.

## Requisiti

Python 3.11+ e tre dipendenze (`curl_cffi`, `fastapi`, `uvicorn`).

```bash
git clone https://github.com/Rehd96/rentwatch.git
cd rentwatch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

```bash
.venv/bin/python -m rentwatch scrape                # scansione completa
.venv/bin/python -m rentwatch scrape --max-pages 2  # prova veloce
.venv/bin/python -m rentwatch serve                 # dashboard su :8777
.venv/bin/python -m rentwatch report                # solo il report Markdown
```

La prima scansione popola il database senza inviare notifiche (sarebbe uno
sciame di messaggi per l'intero mercato); dalla seconda in poi segnala solo le
novità reali. La marcatura degli annunci rimossi avviene **solo** sulle
scansioni complete: con `--max-pages` non sparire dai risultati non significa
niente.

## Configurazione

`config.toml` definisce una o più ricerche con gli stessi parametri che
immobiliare.it usa nei propri URL:

```toml
[[searches]]
name = "Torino - affitti fino a 1000€, min 40 m²"
path = "/affitto-case/torino/"

[searches.params]
prezzoMassimo = 1000
superficieMinima = 40
# localiMinimo = 2
# idMZona[] = [...]        # macrozone
```

Per un'altra città servono `idComune` (e affini) di quella città: si leggono
dall'URL di una ricerca fatta sul sito. Torino è `idComune=9987`; affitto è
`idContratto=2`, residenziale `idCategoria=1`.

Le modifiche personali vanno in `config.local.toml` (ha la precedenza ed è in
`.gitignore`) — è anche il posto giusto per il token Telegram, che così non
finisce mai in un commit.

### Telegram (opzionale)

1. Crea un bot con [@BotFather](https://t.me/BotFather) e prendi il token.
2. Scrivi al bot, poi recupera il tuo chat id da [@userinfobot](https://t.me/userinfobot).
3. In `config.local.toml`:

```toml
[telegram]
enabled = true
bot_token = "…"
chat_id = "…"
```

## Come funziona

| File | Ruolo |
|---|---|
| `rentwatch/scraper.py` | client dell'API JSON interna del portale |
| `rentwatch/db.py` | schema SQLite, upsert, storico prezzi, migrazioni |
| `rentwatch/web.py` + `static/index.html` | dashboard FastAPI + single-page |
| `rentwatch/report.py` | snapshot Markdown |
| `rentwatch/notify.py` | notifiche Telegram |

Nessun parsing HTML: il sito alimenta le proprie pagine di ricerca con un
endpoint JSON (`/api-next/search-list/listings/`, gli stessi dati che stanno in
`__NEXT_DATA__`), quindi i campi arrivano già strutturati — prezzo, superficie,
locali, piano, ascensore, coordinate, agenzia.

### Note sull'API (scoperte sul campo)

Dettagli che costano tempo scoprire da soli:

- **`paramsCount` è obbligatorio.** Senza quel parametro nella query l'endpoint
  risponde `500`.
- **Massimo 2000 risultati (80 pagine) per ricerca.** Da `pag=81` la risposta è
  `HTTP 418`, con qualunque sessione o indirizzo IP: è un limite del backend,
  non un rate limit, e il campo `maxPages` dichiara più pagine di quelle che
  vengono davvero servite. Riprovare o attendere non serve. rentwatch aggira il
  limite partizionando automaticamente la ricerca in fasce di prezzo, ognuna
  sotto le 2000 unità.
- **Un client HTTP normale riceve `403`**: il controllo avviene sul TLS
  fingerprint, per questo il progetto usa
  [curl_cffi](https://github.com/lexiforest/curl_cffi) con impersonazione del
  browser. Nessun captcha, nessun proxy.

## Automazione

Unit systemd utente pronte in `deploy/` (scansione oraria + dashboard sempre
attiva):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/rentwatch-*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rentwatch-scrape.timer
systemctl --user enable --now rentwatch-web.service
```

In `deploy/vps/` ci sono scaffold **non testati** per l'hosting su VPS
(Dockerfile, compose con sidecar di scraping, esempio nginx con basic auth).
Se esponi la dashboard su internet mettila dietro autenticazione: contiene le
tue ricerche e i tuoi preferiti. Da valutare anche il blocco degli IP
datacenter, più sospetti di una connessione residenziale.

## Limiti noti

- Un solo portale (immobiliare.it). Per le sole notifiche multi-portale esiste
  [flathunter](https://github.com/flathunters/flathunter), che copre anche
  Idealista e Subito; rentwatch aggiunge storico prezzi, statistiche e
  dashboard su un portale solo.
- L'API è interna e non documentata: può cambiare senza preavviso. Se succede,
  la struttura dei dati si ritrova in `__NEXT_DATA__` di una pagina di ricerca.
- Nessuna test suite: la verifica è `scrape --max-pages 2` più un colpo d'occhio
  su `/api/overview`.
- Il database (`data/`) e i report generati non sono versionati: contengono i
  tuoi dati di ricerca.

## Licenza

[MIT](LICENSE) — il codice si può riusare, modificare e ridistribuire, con la
sola condizione di mantenere la nota di copyright. La licenza copre il software,
non i dati scaricati: gli annunci restano di immobiliare.it e delle agenzie che
li pubblicano.

## Uso responsabile

Progetto personale, pensato per **una sola persona che cerca casa**. Le
impostazioni predefinite tengono 1,5–3 secondi di pausa tra le richieste e una
sola scansione all'ora: un carico irrilevante per il portale, paragonabile a
qualcuno che sfoglia i risultati. Chi lo riusa è pregato di non alzare quei
ritmi, di non ridistribuire i contenuti scaricati e di ricordare che i dati
appartengono a immobiliare.it e alle agenzie che pubblicano gli annunci.
Nessuna affiliazione con Immobiliare.it Spa.
