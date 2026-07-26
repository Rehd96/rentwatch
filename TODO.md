# TODO

## Deploy su VPS (scaffold pronti in `deploy/vps/` — DA TESTARE)

- [ ] Copiare il progetto sul VPS (git clone o rsync; il DB si rigenera da solo)
- [ ] `cd deploy/vps && docker compose up -d --build` — **scaffold mai testato**:
      verificare build e che lo scraper giri nel container
- [ ] Cambiare la password di basic auth in `deploy/vps/nginx.conf.example`
      (`htpasswd -c ./htpasswd rentwatch`) prima di esporre la dashboard
- [ ] Puntare un dominio/sottodominio al VPS e attivare HTTPS
      (nginx + certbot, oppure sostituire nginx con Caddy che fa TLS da solo)
- [ ] Valutare se lo scraping dal datacenter del VPS viene bloccato da
      immobiliare.it (gli IP datacenter sono più sospetti di quelli residenziali):
      se sì, scrape da casa + rsync/push del DB verso il VPS
- [ ] Aprire solo 80/443 nel firewall del VPS; la porta 8777 resta interna

## Mobile

- [x] Push del repo su GitHub: https://github.com/Rehd96/rentwatch
- [ ] Scegliere un canale per il report da mobile. `reports/` non è più
      versionato (conterrebbe i preferiti in chiaro in un repo pubblico), quindi
      le opzioni sono:
      - dashboard sul VPS dietro HTTPS + basic auth (vedi sezione Deploy)
      - notifiche Telegram (già implementate, serve solo il token)
      - repo/gist **privato** separato dove pushare solo `reports/overview.md`
- [ ] In alternativa (o in aggiunta): con la dashboard sul VPS dietro HTTPS+auth,
      il telefono accede direttamente al sito
- [ ] Configurare Telegram in `config.toml` (token da @BotFather + chat id):
      notifiche push immediate per i nuovi annunci — il canale mobile più rapido

## Funzionalità future

- [ ] Vista mappa nella dashboard (lat/lon già nel DB — Leaflet + OpenStreetMap)
- [ ] Secondo portale (Idealista/Subito) o integrazione flathunter per il confronto
- [ ] Grafico andamento nel tempo (canone mediano per settimana) quando ci saranno
      abbastanza run nello storico
- [ ] Filtro "escludi agenzie" / solo privati (campo agency già salvato)

## Fatto

- [x] Scraper immobiliare.it (curl_cffi, partizione per fasce di prezzo oltre i 2000 risultati)
- [x] SQLite con storico prezzi, first/last_seen, marcatura annunci rimossi
- [x] Dashboard FastAPI su :8777 (KPI, €/m² per zona, filtri, tema chiaro/scuro)
- [x] Report Markdown `reports/overview.md` rigenerato a ogni scrape
- [x] Notifiche Telegram (codice pronto, manca solo il token in config)
- [x] systemd user units per scrape orario + web (`deploy/`, da abilitare)
- [x] Ricerca limitata a ≤1000€ e ≥40 m² (`config.toml`)
- [x] Nascondi annuncio (✕), preferiti/watchlist (♥) e filtro "novità" in dashboard
- [x] Flag "€/stanza?" per prezzi sospetti da studenti (euristica €/m² + prezzo/locali)
- [x] Repo GitHub privato con report leggibile da mobile
