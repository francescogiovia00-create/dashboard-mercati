import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from mercati_lib import collect_market_data, get_relevant_news, get_storico_ticker

# Forza l'output di print() a comparire SUBITO nei log (es. su Render), invece
# di restare "bufferizzato" e apparire solo a intermittenza o in ritardo.
sys.stdout.reconfigure(line_buffering=True)

# Ogni quanto il server RICALCOLA i dati in background. Adesso recupera il
# PREZZO ATTUALE (intraday), che si muove durante la seduta, quindi 60s da' la
# sensazione "live" restando gentile con Yahoo (recupero sequenziale, non a
# raffica). Se dovessi vedere blocchi, alza questo valore (es. 120).
INTERVALLO_AGGIORNAMENTO_SECONDI = 60
# Ogni quanto il BROWSER ricarica i dati dalla cache del server (operazione
# leggerissima: legge solo la cache, non interroga Yahoo).
INTERVALLO_POLLING_FRONTEND_SECONDI = 60

# In locale usa la porta 8000; su Render (o altro hosting) la porta arriva
# dalla variabile d'ambiente PORT.
PORTA = int(os.environ.get("PORT", 8000))
# In locale "localhost" basta; online serve "0.0.0.0" per accettare
# connessioni da internet e non solo dalla stessa macchina.
HOST = "0.0.0.0" if "PORT" in os.environ else "localhost"

# Orario mostrato all'utente: sempre ora italiana, anche se il server (Render)
# gira in UTC. Se il fuso non e' disponibile (Windows senza tzdata) si usa
# l'ora locale della macchina, che sul PC italiano e' comunque quella giusta.
try:
    from zoneinfo import ZoneInfo
    FUSO_ITALIA = ZoneInfo("Europe/Rome")
except Exception:
    FUSO_ITALIA = None


def ora_italiana() -> str:
    adesso = datetime.now(FUSO_ITALIA) if FUSO_ITALIA else datetime.now()
    return adesso.strftime("%H:%M:%S")


# Campi numerici (numpy/pandas) da convertire in float per il JSON.
CAMPI_NUMERICI = {
    "last_close", "change", "pct_change", "volume", "volume_medio",
    "rapporto_volume", "ytd_pct", "high", "low",
}
# Campi non numerici passati cosi' come sono (stringhe / booleani).
CAMPI_TESTO = {"valuta", "ticker", "live", "settore"}

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
    """Gira in un thread separato: aggiorna i dati ogni INTERVALLO_AGGIORNAMENTO_SECONDI."""
    while True:
        try:
            risultati, positivi, negativi = collect_market_data()
            news = get_relevant_news()
            disponibili = sum(1 for v in risultati.values() if v is not None)
            with lock_cache:
                cache_dati["risultati"] = risultati
                cache_dati["positivi"] = positivi
                cache_dati["negativi"] = negativi
                cache_dati["news"] = news
                cache_dati["ultimo_aggiornamento"] = ora_italiana()
            print(f"[{cache_dati['ultimo_aggiornamento']}] Dati aggiornati "
                  f"({disponibili}/{len(risultati)} strumenti disponibili).")
            if disponibili == 0:
                print("  ! Nessuno strumento disponibile: probabile blocco/limite "
                      "di Yahoo Finance. Riprovo al prossimo ciclo.")
        except Exception as errore:
            print(f"Avviso: errore durante l'aggiornamento ({errore}). Riprovo dopo.")
        time.sleep(INTERVALLO_AGGIORNAMENTO_SECONDI)


def dati_come_json() -> str:
    """Converte la cache in JSON, trasformando i tipi numpy/pandas in tipi nativi."""
    with lock_cache:
        risultati_serializzabili = {}
        for nome, dati in cache_dati["risultati"].items():
            if dati is None:
                risultati_serializzabili[nome] = None
                continue
            riga = {}
            for chiave, valore in dati.items():
                if chiave in CAMPI_NUMERICI:
                    riga[chiave] = float(valore) if valore is not None else None
                elif chiave in CAMPI_TESTO:
                    riga[chiave] = valore
            risultati_serializzabili[nome] = riga

        return json.dumps({
            "risultati": risultati_serializzabili,
            "positivi": cache_dati["positivi"],
            "negativi": cache_dati["negativi"],
            "news": cache_dati["news"],
            "ultimo_aggiornamento": cache_dati["ultimo_aggiornamento"],
            "intervallo_secondi": INTERVALLO_AGGIORNAMENTO_SECONDI,
        })


# --- Pagina HTML servita al browser: costruisce l'interfaccia via JavaScript ---
# usiamo placeholder tipo __POLLING_MS__ invece di f-string, per non dover
# raddoppiare ogni graffa { } del CSS e del JavaScript.
TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mercati finanziari</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
    :root {
        --bg-deep: #0B1220;
        --bg-card: #131C2E;
        --bg-card-hover: #1B2740;
        --accent-gold: #D4A017;
        --name-giallo: #FFD60A;
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
    .card-name { font-size: 13px; color: var(--name-giallo); font-weight: 600; }
    .live-inline { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 6px; vertical-align: middle; background: var(--text-secondary); }
    .live-inline.live { background: var(--positive); box-shadow: 0 0 0 0 rgba(79, 174, 125, 0.6); animation: pulse 2s infinite; }
    .card-badge { font-family: 'IBM Plex Mono', monospace; font-size: 13px; font-weight: 500; white-space: nowrap; }
    .card-badge.up { color: var(--positive); }
    .card-badge.down { color: var(--negative); }
    .card-close { font-family: 'Space Grotesk', sans-serif; font-size: 26px; font-weight: 700; margin-bottom: 12px; }
    .card-close .valuta { font-size: 14px; font-weight: 500; color: var(--text-secondary); margin-left: 4px; }
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
    /* --- Barra strumenti: ordina + filtra --- */
    .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 16px; margin-bottom: 18px; }
    .toolbar-group { display: flex; align-items: center; gap: 8px; }
    .toolbar-group > label {
        font-family: 'IBM Plex Mono', monospace; font-size: 11px; letter-spacing: 0.08em;
        text-transform: uppercase; color: var(--text-secondary);
    }
    .toolbar select {
        background: var(--bg-card); color: var(--text-primary);
        border: 1px solid var(--hairline); border-radius: 8px; padding: 7px 12px;
        font-family: 'IBM Plex Mono', monospace; font-size: 12px; cursor: pointer;
    }
    .toolbar select:focus { outline: none; border-color: var(--accent-gold); }
    .filtri { flex-wrap: wrap; }
    .filtri button {
        background: transparent; color: var(--text-secondary);
        border: 1px solid var(--hairline); border-radius: 999px; padding: 6px 14px;
        font-family: 'IBM Plex Mono', monospace; font-size: 12px; cursor: pointer;
        transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }
    .filtri button:hover { color: var(--text-primary); border-color: rgba(237,239,242,0.25); }
    .filtri button.attivo { color: var(--bg-deep); background: var(--accent-gold); border-color: var(--accent-gold); font-weight: 500; }
    .card.clickabile { cursor: pointer; }
    .card.clickabile:hover { transform: translateY(-2px); }
    .card-settore {
        font-family: 'IBM Plex Mono', monospace; font-size: 10px; letter-spacing: 0.08em;
        text-transform: uppercase; color: var(--text-secondary); margin-bottom: 8px;
    }
    .card { transition: background 0.4s ease, border-color 0.4s ease, transform 0.15s ease; }
    .empty-filtro { grid-column: 1 / -1; padding: 28px 0; text-align: center; color: var(--text-secondary); font-family: 'IBM Plex Mono', monospace; font-size: 13px; }

    /* --- Modal grafico storico --- */
    .modal-overlay {
        position: fixed; inset: 0; background: rgba(7, 11, 20, 0.72);
        backdrop-filter: blur(4px); display: none; align-items: center; justify-content: center;
        padding: 24px; z-index: 50;
    }
    .modal-overlay.aperto { display: flex; }
    .modal-box {
        background: var(--bg-card); border: 1px solid var(--hairline); border-radius: 14px;
        width: 100%; max-width: 760px; padding: 26px 28px 30px; position: relative;
        box-shadow: 0 24px 60px rgba(0,0,0,0.5);
    }
    .modal-close {
        position: absolute; top: 16px; right: 18px; background: transparent; border: none;
        color: var(--text-secondary); font-size: 26px; line-height: 1; cursor: pointer; padding: 4px;
    }
    .modal-close:hover { color: var(--text-primary); }
    .modal-titolo { font-family: 'Space Grotesk', sans-serif; font-size: 22px; font-weight: 700; color: var(--name-giallo); margin-bottom: 6px; padding-right: 32px; }
    .modal-sub { font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-secondary); margin-bottom: 18px; }
    .modal-chart { width: 100%; }
    .modal-chart svg { width: 100%; height: auto; display: block; }
    .chart-axis { font-family: 'IBM Plex Mono', monospace; font-size: 10px; fill: var(--text-secondary); }

    @media (max-width: 600px) { .cards-grid { grid-template-columns: 1fr 1fr; } }
</style>
</head>
<body>

<div class="ticker-tape"><div class="ticker-track" id="ticker-track"></div></div>

<div class="page">
    <header class="hero">
        <div class="eyebrow"><span class="live-dot"></span> Live — aggiornamento automatico</div>
        <h1>Mercati finanziari</h1>
        <div class="sottotitolo" id="sottotitolo">In attesa del primo aggiornamento…</div>
        <div class="summary-chips">
            <span class="chip up" id="chip-up">0 in rialzo</span>
            <span class="chip down" id="chip-down">0 in ribasso</span>
        </div>
    </header>

    <div class="section-title">Portafoglio &amp; strumenti seguiti</div>
    <div class="toolbar">
        <div class="toolbar-group">
            <label for="ordina">Ordina</label>
            <select id="ordina">
                <option value="nome">Nome (A–Z)</option>
                <option value="variazione">Variazione %</option>
                <option value="ytd">YTD %</option>
                <option value="volume">Rapporto volume</option>
                <option value="prezzo">Prezzo</option>
            </select>
        </div>
        <div class="toolbar-group">
            <label for="settore">Settore</label>
            <select id="settore">
                <option value="tutti">Tutti i settori</option>
            </select>
        </div>
        <div class="toolbar-group filtri" id="filtri">
            <button data-filtro="tutti" class="attivo">Tutti</button>
            <button data-filtro="rialzo">In rialzo</button>
            <button data-filtro="ribasso">In ribasso</button>
            <button data-filtro="volume">Volume anomalo</button>
            <button data-filtro="nd">Non disp.</button>
        </div>
    </div>
    <div class="cards-grid" id="cards-grid"></div>

    <div class="section-title">Notizie correlate</div>
    <div class="news-columns" id="news-columns"></div>

    <footer id="footer">Connessione al server in corso…</footer>
</div>

<div class="modal-overlay" id="modal">
    <div class="modal-box">
        <button class="modal-close" id="modal-close" aria-label="Chiudi">&times;</button>
        <div class="modal-titolo" id="modal-titolo"></div>
        <div class="modal-sub" id="modal-sub"></div>
        <div class="modal-chart" id="modal-chart"></div>
    </div>
</div>

<script>
const POLLING_MS = __POLLING_MS__;

function escapeHtml(valore) {
    return String(valore == null ? '' : valore)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formattaNumero(valore, decimali = 2) {
    return Number(valore).toLocaleString('it-IT', { minimumFractionDigits: decimali, maximumFractionDigits: decimali });
}

function simboloValuta(codice) {
    const mappa = { EUR: '€', USD: '$', GBP: '£', GBp: 'p', CHF: 'CHF', JPY: '¥', KRW: '₩', HKD: 'HK$', CAD: 'C$', CNY: '¥', SEK: 'kr' };
    if (!codice) return '';
    return mappa[codice] || codice;
}

function costruisciTicker(risultati) {
    const pezzi = [];
    for (const [nome, dati] of Object.entries(risultati)) {
        if (!dati) continue;
        const classe = dati.change >= 0 ? 'up' : 'down';
        const segno = dati.change >= 0 ? '▲' : '▼';
        const val = simboloValuta(dati.valuta);
        pezzi.push(`<span class="ticker-item ${classe}">${escapeHtml(nome)} ${formattaNumero(dati.last_close)}${val ? ' ' + escapeHtml(val) : ''} ${segno} ${dati.pct_change >= 0 ? '+' : ''}${formattaNumero(dati.pct_change)}%</span>`);
    }
    document.getElementById('ticker-track').innerHTML = pezzi.join('').repeat(2);
}

// Stato dell'interfaccia: ultimi dati ricevuti + criterio di ordinamento e filtro
// scelti dall'utente. Ordinare/filtrare NON richiede una nuova chiamata al
// server: si ridisegnano le card a partire da STATO.risultati.
const STATO = { risultati: {}, ordine: 'nome', filtro: 'tutti', settore: 'tutti' };

function passaFiltro(dati) {
    switch (STATO.filtro) {
        case 'rialzo': return dati && dati.change >= 0;
        case 'ribasso': return dati && dati.change < 0;
        case 'volume': return dati && dati.rapporto_volume && dati.rapporto_volume >= 1.5;
        case 'nd': return !dati;
        default: return true;
    }
}

function passaSettore(dati) {
    if (STATO.settore === 'tutti') return true;
    return dati && dati.settore === STATO.settore;
}

// Popola il menu "Settore" con i settori realmente presenti nei dati, senza
// perdere la scelta corrente. Ricostruisce solo se l'elenco e' cambiato.
function aggiornaOpzioniSettore() {
    const sel = document.getElementById('settore');
    const settori = [...new Set(
        Object.values(STATO.risultati).filter(Boolean).map(d => d.settore).filter(Boolean)
    )].sort((a, b) => a.localeCompare(b, 'it'));
    const attuali = [...sel.options].slice(1).map(o => o.value);
    if (JSON.stringify(attuali) === JSON.stringify(settori)) return;
    const scelto = sel.value;
    sel.innerHTML = '<option value="tutti">Tutti i settori</option>'
        + settori.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('');
    sel.value = (scelto === 'tutti' || settori.includes(scelto)) ? scelto : 'tutti';
    STATO.settore = sel.value;
}

function valoreOrdine(dati) {
    switch (STATO.ordine) {
        case 'variazione': return dati.pct_change;
        case 'ytd': return (dati.ytd_pct !== null && dati.ytd_pct !== undefined) ? dati.ytd_pct : -Infinity;
        case 'volume': return dati.rapporto_volume || -Infinity;
        case 'prezzo': return dati.last_close;
        default: return 0;
    }
}

function confrontaVoci([nomeA, a], [nomeB, b]) {
    // Gli strumenti non disponibili finiscono sempre in fondo.
    if (!a && b) return 1;
    if (a && !b) return -1;
    if (!a && !b) return nomeA.localeCompare(nomeB, 'it');
    if (STATO.ordine === 'nome') return nomeA.localeCompare(nomeB, 'it');
    return valoreOrdine(b) - valoreOrdine(a); // numerico: decrescente (i "top" in cima)
}

function renderCards() {
    const contenitore = document.getElementById('cards-grid');
    contenitore.innerHTML = '';
    const voci = Object.entries(STATO.risultati)
        .filter(([nome, dati]) => passaFiltro(dati) && passaSettore(dati))
        .sort(confrontaVoci);

    if (voci.length === 0) {
        contenitore.innerHTML = '<div class="empty-filtro">Nessuno strumento corrisponde al filtro.</div>';
        return;
    }

    for (const [nome, dati] of voci) {
        const card = document.createElement('div');
        if (!dati) {
            card.className = 'card unavailable';
            card.innerHTML = `<div class="card-name">${escapeHtml(nome)}</div><div class="card-status">Dati non disponibili</div>`;
            contenitore.appendChild(card);
            continue;
        }
        card.dataset.nome = nome;
        card.dataset.ticker = dati.ticker || '';
        const classe = dati.change >= 0 ? 'up' : 'down';
        const freccia = classe === 'up' ? '▲' : '▼';
        const ytdTxt = (dati.ytd_pct !== null && dati.ytd_pct !== undefined) ? `${dati.ytd_pct >= 0 ? '+' : ''}${formattaNumero(dati.ytd_pct)}%` : 'n/d';
        const volumeFlag = (dati.rapporto_volume && dati.rapporto_volume >= 1.5) ? '<span class="volume-flag">⚡ volume anomalo</span>' : '';
        const val = simboloValuta(dati.valuta);

        const statoLive = dati.live
            ? '<span class="live-inline live" title="In tempo reale (ritardo ~15 min)"></span>'
            : '<span class="live-inline" title="Mercato chiuso — ultima chiusura"></span>';

        const settoreTag = dati.settore ? `<div class="card-settore">${escapeHtml(dati.settore)}</div>` : '';
        card.className = `card clickabile ${classe}`;
        card.innerHTML = `
            ${settoreTag}
            <div class="card-header">
                <span class="card-name">${statoLive}${escapeHtml(nome)}</span>
                <span class="card-badge ${classe}">${freccia} ${dati.pct_change >= 0 ? '+' : ''}${formattaNumero(dati.pct_change)}%</span>
            </div>
            <div class="card-close">${formattaNumero(dati.last_close)}${val ? `<span class="valuta">${escapeHtml(val)}</span>` : ''}</div>
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
        let interno = `<h3>${escapeHtml(argomento)}</h3>`;
        if (voci && voci.length > 0) {
            for (const v of voci) {
                const link = encodeURI(v.link || '#');
                interno += `<a class="news-item" href="${link}" target="_blank" rel="noopener noreferrer">
                    <span class="news-title">${escapeHtml(v.titolo)}</span>
                    <span class="news-source">${escapeHtml(v.fonte)}</span>
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

        document.getElementById('sottotitolo').textContent = `Prezzi in tempo reale (ritardo ~15 min) · ultimo dato: ${dati.ultimo_aggiornamento || '—'} (ora italiana) · aggiornati ogni ${dati.intervallo_secondi}s`;
        document.getElementById('chip-up').textContent = `${dati.positivi} in rialzo`;
        document.getElementById('chip-down').textContent = `${dati.negativi} in ribasso`;
        document.getElementById('footer').innerHTML = `Prezzi da Yahoo Finance (tempo reale con ~15 min di ritardo, o ultima chiusura a mercato chiuso) · Notizie da Financial Times, Il Sole 24 Ore, ANSA, Teleborsa · Il pallino verde = mercato aperto`;

        STATO.risultati = dati.risultati;
        aggiornaOpzioniSettore();
        costruisciTicker(dati.risultati);
        renderCards();
        costruisciNotizie(dati.news);
    } catch (errore) {
        document.getElementById('footer').innerHTML = '<span class="stato-connessione">Connessione al server persa — verifica che lo script Python sia ancora in esecuzione.</span>';
    }
}

// --- Grafico storico (dettaglio al click su una card) ---
function disegnaGrafico(serie) {
    const sub = document.getElementById('modal-sub');
    const cont = document.getElementById('modal-chart');
    if (!serie || serie.length < 2) {
        sub.textContent = 'Storico non disponibile per questo strumento.';
        cont.innerHTML = '';
        return;
    }
    const W = 720, H = 300, padL = 6, padR = 6, padT = 14, padB = 26;
    const valori = serie.map(p => p.c);
    const min = Math.min(...valori), max = Math.max(...valori);
    const primo = serie[0], ultimo = serie[serie.length - 1];
    const salita = ultimo.c >= primo.c;
    const colore = salita ? '#4FAE7D' : '#E2725B';
    const nX = i => padL + (i / (serie.length - 1)) * (W - padL - padR);
    const nY = c => padT + (1 - (c - min) / ((max - min) || 1)) * (H - padT - padB);
    const punti = serie.map((p, i) => `${nX(i).toFixed(1)},${nY(p.c).toFixed(1)}`).join(' ');
    const area = `${padL.toFixed(1)},${(H - padB).toFixed(1)} ${punti} ${(W - padR).toFixed(1)},${(H - padB).toFixed(1)}`;

    const dataInizio = primo.t.split('-').reverse().join('/');
    const dataFine = ultimo.t.split('-').reverse().join('/');

    cont.innerHTML = `
        <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">
            <defs>
                <linearGradient id="grad-area" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="${colore}" stop-opacity="0.28"/>
                    <stop offset="100%" stop-color="${colore}" stop-opacity="0"/>
                </linearGradient>
            </defs>
            <polygon points="${area}" fill="url(#grad-area)"/>
            <polyline points="${punti}" fill="none" stroke="${colore}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            <text class="chart-axis" x="${padL}" y="${nY(max) - 4}">max ${formattaNumero(max)}</text>
            <text class="chart-axis" x="${padL}" y="${nY(min) + 12}">min ${formattaNumero(min)}</text>
            <text class="chart-axis" x="${padL}" y="${H - 6}">${dataInizio}</text>
            <text class="chart-axis" x="${W - padR}" y="${H - 6}" text-anchor="end">${dataFine}</text>
        </svg>`;

    const variazione = ((ultimo.c - primo.c) / primo.c) * 100;
    sub.innerHTML = `Ultimo anno · ${dataInizio} → ${dataFine} · `
        + `<span style="color:${colore}">${variazione >= 0 ? '+' : ''}${formattaNumero(variazione)}% sul periodo</span>`;
}

async function apriDettaglio(nome, ticker) {
    const modal = document.getElementById('modal');
    modal.classList.add('aperto');
    document.getElementById('modal-titolo').textContent = nome;
    document.getElementById('modal-sub').textContent = (ticker || '') + ' — caricamento storico…';
    document.getElementById('modal-chart').innerHTML = '';
    if (!ticker) { document.getElementById('modal-sub').textContent = 'Ticker non disponibile.'; return; }
    try {
        const risposta = await fetch('/api/storico?ticker=' + encodeURIComponent(ticker), { cache: 'no-store' });
        const dati = await risposta.json();
        disegnaGrafico(dati.storico);
    } catch (errore) {
        document.getElementById('modal-sub').textContent = 'Impossibile caricare lo storico.';
    }
}

function chiudiModal() { document.getElementById('modal').classList.remove('aperto'); }

// --- Wiring eventi (una sola volta) ---
document.getElementById('cards-grid').addEventListener('click', (e) => {
    const card = e.target.closest('.card.clickabile');
    if (card) apriDettaglio(card.dataset.nome, card.dataset.ticker);
});
document.getElementById('ordina').addEventListener('change', (e) => {
    STATO.ordine = e.target.value;
    renderCards();
});
document.getElementById('settore').addEventListener('change', (e) => {
    STATO.settore = e.target.value;
    renderCards();
});
document.getElementById('filtri').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-filtro]');
    if (!btn) return;
    document.querySelectorAll('#filtri button').forEach(b => b.classList.remove('attivo'));
    btn.classList.add('attivo');
    STATO.filtro = btn.dataset.filtro;
    renderCards();
});
document.getElementById('modal-close').addEventListener('click', chiudiModal);
document.getElementById('modal').addEventListener('click', (e) => {
    if (e.target.id === 'modal') chiudiModal(); // click sullo sfondo scuro
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') chiudiModal(); });

aggiornaDashboard();
setInterval(aggiornaDashboard, POLLING_MS);
</script>
</body>
</html>"""

PAGINA_HTML = TEMPLATE_HTML.replace(
    "__POLLING_MS__", str(INTERVALLO_POLLING_FRONTEND_SECONDI * 1000)
)


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
        elif self.path.startswith("/api/storico"):
            query = parse_qs(urlparse(self.path).query)
            ticker = (query.get("ticker") or [""])[0]
            serie = get_storico_ticker(ticker) if ticker else None
            corpo = json.dumps({
                "ticker": ticker,
                "storico": serie or [],
            }).encode("utf-8")
            self.send_response(200 if serie else 404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
        elif self.path == "/healthz":
            corpo = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
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
    # importante per l'hosting cloud, che si aspetta una risposta rapida sulla
    # porta assegnata. La pagina mostra "in attesa" finche' i primi dati non
    # sono pronti (di solito pochi secondi).
    server = ThreadingHTTPServer((HOST, PORTA), GestoreRichieste)
    print(f"Server avviato su {HOST}:{PORTA}")
    if HOST == "localhost":
        print(f"Apri http://localhost:{PORTA} nel browser.")
    print("Premi Ctrl+C per fermare il server (solo in locale).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer fermato.")
