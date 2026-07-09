"""
Dashboard mercati in tempo (quasi) reale
==========================================
Server web locale che tiene aggiornati i dati di mercato e le notizie in
background, e serve una pagina HTML che si aggiorna DA SOLA ogni minuto —
non serve ricaricare la pagina ne' rilanciare lo script.

Perche' ogni minuto e non ogni secondo:
- I dati di Yahoo Finance non sono davvero "tick-by-tick" in tempo reale
- Interrogare il fornitore dati ogni secondo rischia di far bloccare il tuo IP
- Un aggiornamento al minuto e' un buon compromesso tra reattivita' e stabilita'

COME USARLO IN LOCALE:
1. Assicurati che "mercati_lib.py" sia nella STESSA cartella di questo file:
   da li' vengono riusati gli strumenti (TICKERS) e le notizie (RSS_FEEDS)
   che hai gia' configurato.
2. Lancia questo script (nessuna libreria aggiuntiva richiesta):
   python dashboard_server.py
3. Apri nel browser:
   http://localhost:8000
4. Lascia la finestra del terminale aperta: il server resta acceso e
   aggiorna i dati ogni minuto finche' non premi Ctrl+C per fermarlo.

COME METTERLO ONLINE GRATIS (Render.com):
Vedi la guida passo passo fornita insieme a questo file. In breve:
questo script legge automaticamente la porta dalla variabile d'ambiente
PORT quando e' disponibile (impostata da Render), quindi non serve
nessuna modifica al codice per farlo funzionare online.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mercati_lib import collect_market_data, get_relevant_news

# Forza l'output di print() a comparire SUBITO nei log (es. su Render), invece
# di restare "bufferizzato" e apparire solo a intermittenza o in ritardo.
sys.stdout.reconfigure(line_buffering=True)

INTERVALLO_AGGIORNAMENTO_SECONDI = 60
# In locale usa la porta 8000; su Render (o altro hosting cloud) la porta
# viene assegnata automaticamente tramite la variabile d'ambiente PORT.
PORTA = int(os.environ.get("PORT", 8000))
# In locale "localhost" basta; online serve "0.0.0.0" per accettare
# connessioni da internet e non solo dalla stessa macchina.
HOST = "0.0.0.0" if "PORT" in os.environ else "localhost"

# Cache condivisa tra il thread di aggiornamento in background e le richieste del browser
cache_dati = {
    "risultati": {},
    "positivi": 0,
    "negativi": 0,
    "news": {},
    "ultimo_aggiornamento": None,
}
lock_cache = threading.Lock()


def aggiorna_cache_periodicamente():
    """Gira in un thread separato per sempre: aggiorna i dati ogni INTERVALLO_AGGIORNAMENTO_SECONDI."""
    while True:
        try:
            risultati, positivi, negativi = collect_market_data()
            news = get_relevant_news()
            with lock_cache:
                cache_dati["risultati"] = risultati
                cache_dati["positivi"] = positivi
                cache_dati["negativi"] = negativi
                cache_dati["news"] = news
                cache_dati["ultimo_aggiornamento"] = datetime.now().strftime("%H:%M:%S")
            print(f"[{cache_dati['ultimo_aggiornamento']}] Dati aggiornati ({len(risultati)} strumenti).")
        except Exception as errore:
            print(f"Avviso: errore durante l'aggiornamento dei dati ({errore}). Riprovo tra un minuto.")
        time.sleep(INTERVALLO_AGGIORNAMENTO_SECONDI)


def dati_come_json() -> str:
    """Converte la cache in JSON, trasformando i tipi numpy/pandas in tipi nativi Python."""
    with lock_cache:
        risultati_serializzabili = {}
        for nome, dati in cache_dati["risultati"].items():
            if dati is None:
                risultati_serializzabili[nome] = None
            else:
                risultati_serializzabili[nome] = {
                    chiave: (float(valore) if valore is not None else None)
                    for chiave, valore in dati.items()
                    if chiave != "date"
                }

        return json.dumps({
            "risultati": risultati_serializzabili,
            "positivi": cache_dati["positivi"],
            "negativi": cache_dati["negativi"],
            "news": cache_dati["news"],
            "ultimo_aggiornamento": cache_dati["ultimo_aggiornamento"],
            "intervallo_secondi": INTERVALLO_AGGIORNAMENTO_SECONDI,
        })


# --- Pagina HTML servita al browser: costruisce l'interfaccia via JavaScript ---
# usiamo dei placeholder tipo __INTERVALLO_MS__ invece di f-string, per evitare
# di dover raddoppiare ogni singola graffa { } usata dal CSS e dal JavaScript.
TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mercati — Live</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
    :root {
        --bg-deep: #0B1220;
        --bg-card: #131C2E;
        --bg-card-hover: #1B2740;
        --accent-gold: #D4A017;
        --positive: #4FAE7D;
        --negative: #E2725B;
        --text-primary: #EDEFF2;
        --text-secondary: #8B93A7;
        --hairline: rgba(237, 239, 242, 0.09);
    }
    * { box-sizing: border-box; }
    body {
        margin: 0;
        background: var(--bg-deep);
        color: var(--text-primary);
        font-family: 'Inter', sans-serif;
        -webkit-font-smoothing: antialiased;
    }
    .ticker-tape {
        overflow: hidden;
        white-space: nowrap;
        background: #0E1626;
        border-bottom: 1px solid var(--hairline);
        padding: 10px 0;
    }
    .ticker-track {
        display: inline-block;
        animation: scroll-left 45s linear infinite;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 13px;
    }
    @keyframes scroll-left {
        from { transform: translateX(0); }
        to { transform: translateX(-50%); }
    }
    .ticker-item { padding: 0 28px; color: var(--text-secondary); }
    .ticker-item.up { color: var(--positive); }
    .ticker-item.down { color: var(--negative); }
    @media (prefers-reduced-motion: reduce) {
        .ticker-track { animation: none; }
    }
    .page { max-width: 1180px; margin: 0 auto; padding: 40px 24px 64px; }
    header.hero { margin-bottom: 20px; }
    .eyebrow {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 12px;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--accent-gold);
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .live-dot {
        width: 8px; height: 8px; border-radius: 50%;
        background: var(--positive);
        box-shadow: 0 0 0 0 rgba(79, 174, 125, 0.6);
        animation: pulse 2s infinite;
    }
    @keyframes pulse {
        0% { box-shadow: 0 0 0 0 rgba(79, 174, 125, 0.5); }
        70% { box-shadow: 0 0 0 8px rgba(79, 174, 125, 0); }
        100% { box-shadow: 0 0 0 0 rgba(79, 174, 125, 0); }
    }
    h1 {
        font-family: 'Space Grotesk', sans-serif;
        font-size: clamp(26px, 4vw, 38px);
        font-weight: 700;
        margin: 0 0 6px;
        letter-spacing: -0.01em;
    }
    .sottotitolo {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 12px;
        color: var(--text-secondary);
        margin-bottom: 16px;
    }
    .summary-chips { display: flex; gap: 12px; flex-wrap: wrap; }
    .chip {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 13px;
        padding: 6px 14px;
        border-radius: 999px;
        border: 1px solid var(--hairline);
        color: var(--text-secondary);
    }
    .chip.up { color: var(--positive); border-color: rgba(79, 174, 125, 0.35); }
    .chip.down { color: var(--negative); border-color: rgba(226, 114, 91, 0.35); }
    .section-title {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 15px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--text-secondary);
        margin: 40px 0 18px;
        padding-bottom: 10px;
        border-bottom: 1px solid var(--hairline);
    }
    .cards-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
        gap: 14px;
    }
    .card {
        background: var(--bg-card);
        border-radius: 10px;
        padding: 18px 20px;
        border-left: 3px solid var(--hairline);
        transition: background 0.4s ease, border-color 0.4s ease;
    }
    .card:hover { background: var(--bg-card-hover); }
    .card.up { border-left-color: var(--positive); }
    .card.down { border-left-color: var(--negative); }
    .card.unavailable { border-left-color: var(--hairline); opacity: 0.6; }
    .card-header { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 10px; }
    .card-name { font-size: 13px; color: var(--text-secondary); font-weight: 500; }
    .card-badge { font-family: 'IBM Plex Mono', monospace; font-size: 13px; font-weight: 500; white-space: nowrap; }
    .card-badge.up { color: var(--positive); }
    .card-badge.down { color: var(--negative); }
    .card-close { font-family: 'Space Grotesk', sans-serif; font-size: 26px; font-weight: 700; margin-bottom: 12px; }
    .card-row { display: flex; justify-content: space-between; font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-secondary); padding: 4px 0; }
    .card-row .up { color: var(--positive); }
    .card-row .down { color: var(--negative); }
    .volume-flag { color: var(--accent-gold); margin-left: 6px; }
    .card-status { font-size: 13px; color: var(--text-secondary); }
    .news-columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 28px; }
    .news-column h3 { font-size: 16px; margin: 0 0 14px; }
    .news-item { display: flex; flex-direction: column; gap: 4px; padding: 12px 0; border-bottom: 1px solid var(--hairline); text-decoration: none; color: var(--text-primary); }
    .news-item:last-child { border-bottom: none; }
    .news-item:hover .news-title { color: var(--accent-gold); }
    .news-title { font-size: 14px; font-weight: 500; line-height: 1.4; transition: color 0.15s ease; }
    .news-source { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.05em; }
    .news-empty { font-size: 13px; color: var(--text-secondary); padding: 8px 0; }
    footer { margin-top: 56px; padding-top: 20px; border-top: 1px solid var(--hairline); font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-secondary); }
    .stato-connessione { color: var(--negative); }
    @media (max-width: 600px) { .cards-grid { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>

<div class="ticker-tape"><div class="ticker-track" id="ticker-track"></div></div>

<div class="page">
    <header class="hero">
        <div class="eyebrow"><span class="live-dot"></span> Live — aggiornamento automatico</div>
        <h1>Mercati in tempo (quasi) reale</h1>
        <div class="sottotitolo" id="sottotitolo">In attesa del primo aggiornamento…</div>
        <div class="summary-chips">
            <span class="chip up" id="chip-up">0 in rialzo</span>
            <span class="chip down" id="chip-down">0 in ribasso</span>
        </div>
    </header>

    <div class="section-title">Portafoglio &amp; strumenti seguiti</div>
    <div class="cards-grid" id="cards-grid"></div>

    <div class="section-title">Notizie correlate</div>
    <div class="news-columns" id="news-columns"></div>

    <footer id="footer">Connessione al server in corso…</footer>
</div>

<script>
const INTERVALLO_MS = __INTERVALLO_MS__;

function formattaNumero(valore, decimali = 2) {
    return Number(valore).toLocaleString('it-IT', { minimumFractionDigits: decimali, maximumFractionDigits: decimali });
}

function costruisciTicker(risultati) {
    const pezzi = [];
    for (const [nome, dati] of Object.entries(risultati)) {
        if (!dati) continue;
        const classe = dati.change >= 0 ? 'up' : 'down';
        const segno = dati.change >= 0 ? '▲' : '▼';
        pezzi.push(`<span class="ticker-item ${classe}">${nome} ${formattaNumero(dati.last_close)} ${segno} ${dati.pct_change >= 0 ? '+' : ''}${formattaNumero(dati.pct_change)}%</span>`);
    }
    document.getElementById('ticker-track').innerHTML = pezzi.join('').repeat(2);
}

function costruisciCard(risultati) {
    const contenitore = document.getElementById('cards-grid');
    contenitore.innerHTML = '';
    for (const [nome, dati] of Object.entries(risultati)) {
        const card = document.createElement('div');
        if (!dati) {
            card.className = 'card unavailable';
            card.innerHTML = `<div class="card-name">${nome}</div><div class="card-status">Dati non disponibili</div>`;
            contenitore.appendChild(card);
            continue;
        }
        const classe = dati.change >= 0 ? 'up' : 'down';
        const freccia = classe === 'up' ? '▲' : '▼';
        const ytdTxt = (dati.ytd_pct !== null && dati.ytd_pct !== undefined) ? `${dati.ytd_pct >= 0 ? '+' : ''}${formattaNumero(dati.ytd_pct)}%` : 'n/d';
        const volumeFlag = (dati.rapporto_volume && dati.rapporto_volume >= 1.5) ? '<span class="volume-flag">⚡ volume anomalo</span>' : '';

        card.className = `card ${classe}`;
        card.innerHTML = `
            <div class="card-header">
                <span class="card-name">${nome}</span>
                <span class="card-badge ${classe}">${freccia} ${dati.pct_change >= 0 ? '+' : ''}${formattaNumero(dati.pct_change)}%</span>
            </div>
            <div class="card-close">${formattaNumero(dati.last_close)}</div>
            <div class="card-row"><span>Variazione</span><span class="${classe}">${dati.change >= 0 ? '+' : ''}${formattaNumero(dati.change)}</span></div>
            <div class="card-row"><span>YTD</span><span>${ytdTxt}</span></div>
            <div class="card-row"><span>Massimo / Minimo</span><span>${formattaNumero(dati.high)} / ${formattaNumero(dati.low)}</span></div>
            <div class="card-row"><span>Volume</span><span>${Math.round(dati.volume).toLocaleString('it-IT')} ${volumeFlag}</span></div>
        `;
        contenitore.appendChild(card);
    }
}

function costruisciNotizie(news) {
    const contenitore = document.getElementById('news-columns');
    contenitore.innerHTML = '';
    for (const [argomento, voci] of Object.entries(news)) {
        const colonna = document.createElement('div');
        colonna.className = 'news-column';
        let interno = `<h3>${argomento}</h3>`;
        if (voci && voci.length > 0) {
            for (const voceGrezza of voci) {
                // voceGrezza e' tipo: "• Titolo — _Fonte_\n  https://link"
                const [primaRiga, link] = voceGrezza.split('\\n');
                const titoloEFonte = primaRiga.replace('• ', '');
                const [titolo, fonte] = titoloEFonte.split(' — ');
                const fontePulita = (fonte || '').trim().replace(/_/g, '');
                const linkPulito = (link || '#').trim();
                const titoloPulito = (titolo || '').replace(/\\\\_/g, '_').replace(/\\\\\\*/g, '*');
                interno += `<a class="news-item" href="${linkPulito}" target="_blank" rel="noopener noreferrer">
                    <span class="news-title">${titoloPulito}</span>
                    <span class="news-source">${fontePulita}</span>
                </a>`;
            }
        } else {
            interno += '<div class="news-empty">Nessuna notizia rilevante trovata al momento</div>';
        }
        colonna.innerHTML = interno;
        contenitore.appendChild(colonna);
    }
}

async function aggiornaDashboard() {
    try {
        const risposta = await fetch('/api/dati', { cache: 'no-store' });
        if (!risposta.ok) throw new Error('Risposta non valida dal server');
        const dati = await risposta.json();

        document.getElementById('sottotitolo').textContent = `Ultimo aggiornamento: ${dati.ultimo_aggiornamento || '—'} · Prossimo tra ${dati.intervallo_secondi} secondi`;
        document.getElementById('chip-up').textContent = `${dati.positivi} in rialzo`;
        document.getElementById('chip-down').textContent = `${dati.negativi} in ribasso`;
        document.getElementById('footer').innerHTML = `Aggiornamento automatico ogni ${dati.intervallo_secondi} secondi · Dati di chiusura da Yahoo Finance · Notizie da Financial Times, Il Sole 24 Ore, Teleborsa`;

        costruisciTicker(dati.risultati);
        costruisciCard(dati.risultati);
        costruisciNotizie(dati.news);
    } catch (errore) {
        document.getElementById('footer').innerHTML = '<span class="stato-connessione">Connessione al server persa — verifica che lo script Python sia ancora in esecuzione.</span>';
    }
}

aggiornaDashboard();
setInterval(aggiornaDashboard, INTERVALLO_MS);
</script>
</body>
</html>"""

PAGINA_HTML = TEMPLATE_HTML.replace("__INTERVALLO_MS__", str(INTERVALLO_AGGIORNAMENTO_SECONDI * 1000))


class GestoreRichieste(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silenzia i log HTTP di default; teniamo solo i print del thread di aggiornamento

    def do_GET(self):
        if self.path.startswith("/api/dati"):
            corpo = dati_come_json().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        else:
            corpo = PAGINA_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)


if __name__ == "__main__":
    print("Avvio il recupero dati in background...")
    thread_aggiornamento = threading.Thread(target=aggiorna_cache_periodicamente, daemon=True)
    thread_aggiornamento.start()

    # Il server si avvia SUBITO, senza aspettare il primo aggiornamento:
    # importante per l'hosting cloud, che si aspetta una risposta rapida
    # sulla porta assegnata. La pagina mostrera' "in attesa" finche' i
    # primi dati non sono pronti (di solito pochi secondi).
    server = ThreadingHTTPServer((HOST, PORTA), GestoreRichieste)
    print(f"Server avviato su {HOST}:{PORTA}")
    if HOST == "localhost":
        print(f"Apri http://localhost:{PORTA} nel browser.")
    print("Premi Ctrl+C per fermare il server (solo in locale).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer fermato.")
