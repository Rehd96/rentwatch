# TODO

## Deploy su VPS — istruzioni pronte in `VPS_COMMANDS.txt`

Topologia scelta: dashboard su `https://labustagialla.it/case/`, dietro il login
dell'app (non più basic auth di nginx), accanto a portfolio `/`, blog `/blog/`,
`/ezbk/` e `/cff/`. Scrape ogni 4 ore via systemd timer.

- [ ] Eseguire `VPS_COMMANDS.txt` passo per passo sul VPS
- [ ] STEP 4: verificare che lo scraping dal datacenter non venga bloccato da
      immobiliare.it. Se arriva 403, il piano B (già scritto nel file) è
      scrape da casa + `rsync` del DB verso il VPS
- [ ] STEP 9: creare il bot con @BotFather e incollare token + chat id nella
      pagina impostazioni
- [ ] Solo 80/443 aperti nel firewall; la 8777 resta su loopback
- [x] Login sulla dashboard (sessione firmata, PBKDF2, throttle per IP)
- [x] Scrape ogni 4 ore invece che ogni ora
- [x] Unit systemd per web, scrape+timer e bot
- [x] Blocco nginx per `/case/` (in `deploy/vps/nginx.conf.example` e nel file
      merged del repo HiImIon)
- [x] `Disallow: /case/` nel robots.txt del dominio (lo serve il portfolio)

Lo scaffold Docker in `deploy/vps/` resta **non testato**: è un'alternativa a
systemd, non il percorso descritto in VPS_COMMANDS.txt. Se non serve, si può
togliere.

## Mobile

- [x] Push del repo su GitHub: https://github.com/Rehd96/rentwatch
- [x] Dashboard raggiungibile dal telefono dietro HTTPS + login
- [x] Notifiche Telegram configurabili (filtri, template, ore di silenzio)
- [x] Bot con comandi: /stato /ultimi /preferiti /filtri /prezzo /superficie
      /silenzia /riattiva
- [ ] Verificare la resa della dashboard su schermo piccolo: la tabella annunci
      è nata per il desktop

## Funzionalità future

- [ ] Vista mappa nella dashboard (lat/lon già nel DB — Leaflet + OpenStreetMap)
- [ ] Secondo portale (Idealista/Subito) o integrazione flathunter per il confronto
- [ ] Grafico andamento nel tempo (canone mediano per settimana) quando ci saranno
      abbastanza run nello storico
- [ ] Filtro "escludi agenzie" / solo privati anche in dashboard (per ora è solo
      un filtro di notifica)
- [ ] Pulsante "scrape adesso" anche in home, non solo nelle impostazioni

## Fatto

- [x] Scraper immobiliare.it (curl_cffi, partizione per fasce di prezzo oltre i 2000 risultati)
- [x] SQLite con storico prezzi, first/last_seen, marcatura annunci rimossi
- [x] Dashboard FastAPI su :8777 (KPI, €/m² per zona, filtri, tema chiaro/scuro)
- [x] Report Markdown `reports/overview.md` rigenerato a ogni scrape
- [x] Notifiche Telegram
- [x] Ricerca limitata a ≤1000€ e ≥40 m² (`config.toml`, modificabile da browser)
- [x] Nascondi annuncio (✕), preferiti/watchlist (♥) e filtro "novità" in dashboard
- [x] Flag "€/stanza?" per prezzi sospetti da studenti (euristica €/m² + prezzo/locali)
- [x] Pagina impostazioni: ricerche, filtri di notifica, template, ore di silenzio
- [x] Avvisi sui ribassi di prezzo (`{old_price}`, `{delta}` nel template)
