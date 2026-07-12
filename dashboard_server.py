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
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#060A14">
<title>Mercati finanziari</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
    :root {
        --bg-deep: #060A14;
        --bg-1: #0A101E;
        --surface: #111A2B;
        --surface-2: #16223A;
        --surface-3: #1B294380;
        --name-giallo: #FFD60A;
        --accent-gold: #E3B341;
        --positive: #34D399;
        --positive-dim: rgba(52, 211, 153, 0.14);
        --negative: #F87171;
        --negative-dim: rgba(248, 113, 113, 0.14);
        --text-primary: #EAEEF6;
        --text-secondary: #8B93A7;
        --text-faint: #59617690;
        --hairline: rgba(237, 239, 242, 0.08);
        --hairline-strong: rgba(237, 239, 242, 0.15);
        --focus: #7CC4FF;
        --shadow-card: 0 1px 2px rgba(0,0,0,0.4);
        --shadow-pop: 0 24px 60px rgba(0,0,0,0.55);
        --r-sm: 8px; --r-md: 12px; --r-lg: 16px;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
        margin: 0;
        min-height: 100dvh;
        background:
            radial-gradient(1200px 600px at 85% -10%, rgba(227, 179, 65, 0.06), transparent 60%),
            radial-gradient(900px 500px at 0% 0%, rgba(52, 211, 153, 0.05), transparent 55%),
            var(--bg-deep);
        color: var(--text-primary);
        font-family: 'IBM Plex Sans', system-ui, sans-serif;
        font-size: 16px;
        line-height: 1.5;
        -webkit-font-smoothing: antialiased;
    }
    a { color: inherit; }
    :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px; }
    .num { font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; }
    .up { color: var(--positive); }
    .down { color: var(--negative); }

    /* --- Nastro scorrevole quotazioni --- */
    .ticker-tape {
        overflow: hidden; white-space: nowrap;
        background: linear-gradient(180deg, #0B1322, #08101C);
        border-bottom: 1px solid var(--hairline);
        padding: 9px 0;
        -webkit-mask-image: linear-gradient(90deg, transparent, #000 4%, #000 96%, transparent);
                mask-image: linear-gradient(90deg, transparent, #000 4%, #000 96%, transparent);
    }
    .ticker-track {
        display: inline-block;
        animation: scroll-left 60s linear infinite;
        font-family: 'IBM Plex Mono', monospace; font-size: 13px;
        font-variant-numeric: tabular-nums;
    }
    .ticker-tape:hover .ticker-track { animation-play-state: paused; }
    @keyframes scroll-left { from { transform: translateX(0); } to { transform: translateX(-50%); } }
    .ticker-item { padding: 0 26px; color: var(--text-secondary); display: inline-flex; align-items: center; gap: 7px; }
    .ticker-item b { color: var(--text-primary); font-weight: 500; }
    .ticker-item svg { width: 9px; height: 9px; }
    @media (prefers-reduced-motion: reduce) { .ticker-track { animation: none; } }

    /* --- Header sticky --- */
    .topbar {
        position: sticky; top: 0; z-index: 30;
        background: rgba(8, 13, 24, 0.72);
        -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
        border-bottom: 1px solid var(--hairline);
    }
    .topbar-inner {
        max-width: 1200px; margin: 0 auto; padding: 14px 24px;
        display: flex; align-items: center; justify-content: space-between; gap: 16px;
    }
    .brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
    .brand-mark {
        width: 34px; height: 34px; border-radius: 9px; flex-shrink: 0;
        background: linear-gradient(145deg, var(--accent-gold), #b5842a);
        display: grid; place-items: center; color: #1a1206;
        box-shadow: 0 4px 14px rgba(227,179,65,0.25);
    }
    .brand-mark svg { width: 20px; height: 20px; }
    .brand-text { min-width: 0; }
    .brand-title { font-size: 16px; font-weight: 700; letter-spacing: -0.01em; line-height: 1.1; }
    .brand-sub {
        font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; letter-spacing: 0.1em;
        text-transform: uppercase; color: var(--text-secondary); margin-top: 2px;
    }
    .live-pill {
        display: inline-flex; align-items: center; gap: 8px; flex-shrink: 0;
        font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-secondary);
        border: 1px solid var(--hairline); border-radius: 999px; padding: 6px 12px 6px 11px;
        background: var(--surface-3);
    }
    .live-pill .val { color: var(--text-primary); }
    .live-dot {
        width: 8px; height: 8px; border-radius: 50%; background: var(--positive);
        box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6); animation: pulse 2.4s infinite;
    }
    @keyframes pulse {
        0% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.5); }
        70% { box-shadow: 0 0 0 7px rgba(52, 211, 153, 0); }
        100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0); }
    }
    @media (prefers-reduced-motion: reduce) { .live-dot { animation: none; } }

    .page { max-width: 1200px; margin: 0 auto; padding: 30px 24px 72px; }
    .sottotitolo {
        font-family: 'IBM Plex Mono', monospace; font-size: 12px;
        color: var(--text-secondary); margin: 2px 0 22px; line-height: 1.6;
    }

    /* --- KPI tiles --- */
    .stat-row {
        display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 34px;
    }
    .stat {
        background: linear-gradient(180deg, var(--surface), var(--bg-1));
        border: 1px solid var(--hairline); border-radius: var(--r-md);
        padding: 16px 18px; box-shadow: var(--shadow-card); position: relative; overflow: hidden;
    }
    .stat::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--hairline-strong); }
    .stat.pos::before { background: var(--positive); }
    .stat.neg::before { background: var(--negative); }
    .stat.gold::before { background: var(--accent-gold); }
    .stat-label {
        font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; letter-spacing: 0.09em;
        text-transform: uppercase; color: var(--text-secondary); display: flex; align-items: center; gap: 6px;
    }
    .stat-label svg { width: 13px; height: 13px; opacity: 0.9; }
    .stat-value { font-size: 30px; font-weight: 700; margin-top: 8px; letter-spacing: -0.02em; line-height: 1;
        font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; }
    .stat.pos .stat-value { color: var(--positive); }
    .stat.neg .stat-value { color: var(--negative); }

    /* --- Titoli sezione --- */
    .section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
        margin: 6px 0 18px; padding-bottom: 12px; border-bottom: 1px solid var(--hairline); }
    .section-title { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-secondary); }
    .section-meta { font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--text-faint); }

    /* --- Toolbar --- */
    .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 14px 20px; margin-bottom: 20px; }
    .toolbar-group { display: flex; align-items: center; gap: 8px; }
    .toolbar-group > label {
        font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; letter-spacing: 0.08em;
        text-transform: uppercase; color: var(--text-secondary);
    }
    .select-wrap { position: relative; display: inline-flex; align-items: center; }
    .select-wrap svg { position: absolute; right: 10px; width: 13px; height: 13px; color: var(--text-secondary); pointer-events: none; }
    .toolbar select {
        appearance: none; -webkit-appearance: none;
        background: var(--surface); color: var(--text-primary);
        border: 1px solid var(--hairline-strong); border-radius: var(--r-sm);
        padding: 8px 30px 8px 12px;
        font-family: 'IBM Plex Mono', monospace; font-size: 12px; cursor: pointer;
        transition: border-color 0.15s ease;
    }
    .toolbar select:hover { border-color: var(--text-secondary); }
    .toolbar select:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
    .segmented { display: inline-flex; flex-wrap: wrap; gap: 6px; }
    .segmented button {
        background: var(--surface); color: var(--text-secondary);
        border: 1px solid var(--hairline); border-radius: 999px; padding: 7px 14px;
        font-family: 'IBM Plex Mono', monospace; font-size: 12px; cursor: pointer;
        transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }
    .segmented button:hover { color: var(--text-primary); border-color: var(--hairline-strong); }
    .segmented button[aria-pressed="true"] {
        color: #12100a; background: var(--accent-gold); border-color: var(--accent-gold); font-weight: 600;
    }

    /* --- Griglia card --- */
    .cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(248px, 1fr)); gap: 14px; }
    .card {
        background: linear-gradient(180deg, var(--surface), var(--bg-1));
        border: 1px solid var(--hairline); border-left: 3px solid var(--hairline-strong);
        border-radius: var(--r-md); padding: 16px 18px; box-shadow: var(--shadow-card);
        transition: transform 0.18s ease, border-color 0.25s ease, box-shadow 0.25s ease, background 0.25s ease;
    }
    .card.up { border-left-color: var(--positive); }
    .card.down { border-left-color: var(--negative); }
    .card.unavailable { border-left-color: var(--hairline-strong); opacity: 0.55; }
    .card.clickabile { cursor: pointer; }
    .card.clickabile:hover { transform: translateY(-3px); background: linear-gradient(180deg, var(--surface-2), var(--surface)); box-shadow: 0 10px 26px rgba(0,0,0,0.4); }
    .card.clickabile:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
    .card-settore {
        font-family: 'IBM Plex Mono', monospace; font-size: 9.5px; letter-spacing: 0.09em;
        text-transform: uppercase; color: var(--text-secondary); margin-bottom: 10px;
        display: inline-block; padding: 2px 7px; border: 1px solid var(--hairline); border-radius: 5px;
    }
    .card-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 12px; }
    .card-name { font-size: 14px; color: var(--name-giallo); font-weight: 600; display: inline-flex; align-items: center; }
    .live-inline { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 7px; flex-shrink: 0; background: var(--text-secondary); }
    .live-inline.live { background: var(--positive); box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.6); animation: pulse 2.4s infinite; }
    @media (prefers-reduced-motion: reduce) { .live-inline.live { animation: none; } }
    .card-badge {
        font-family: 'IBM Plex Mono', monospace; font-size: 12.5px; font-weight: 600; white-space: nowrap;
        display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px;
    }
    .card-badge svg { width: 9px; height: 9px; }
    .card-badge.up { color: var(--positive); background: var(--positive-dim); }
    .card-badge.down { color: var(--negative); background: var(--negative-dim); }
    .card-close { font-size: 27px; font-weight: 700; margin-bottom: 14px; letter-spacing: -0.02em;
        font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; }
    .card-close .valuta { font-size: 14px; font-weight: 500; color: var(--text-secondary); margin-left: 5px; font-family: 'IBM Plex Sans', sans-serif; }
    .card-row { display: flex; justify-content: space-between; gap: 8px;
        font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums;
        font-size: 12px; color: var(--text-secondary); padding: 4px 0; border-top: 1px solid var(--hairline); }
    .card-row:first-of-type { border-top: none; }
    .card-row > span:last-child { color: var(--text-primary); }
    .volume-flag { display: inline-flex; align-items: center; gap: 3px; color: var(--accent-gold); margin-left: 6px; }
    .volume-flag svg { width: 11px; height: 11px; }
    .card-status { font-size: 13px; color: var(--text-secondary); }

    /* --- Skeleton di caricamento --- */
    .skeleton { position: relative; overflow: hidden; background: var(--surface); }
    .skeleton .sk-line { height: 12px; border-radius: 5px; background: var(--surface-2); margin: 10px 0; }
    .skeleton .sk-line.lg { height: 26px; width: 60%; }
    .skeleton .sk-line.sm { width: 40%; }
    .skeleton::after {
        content: ""; position: absolute; inset: 0; transform: translateX(-100%);
        background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent);
        animation: shimmer 1.4s infinite;
    }
    @keyframes shimmer { 100% { transform: translateX(100%); } }
    @media (prefers-reduced-motion: reduce) { .skeleton::after { animation: none; } }

    /* --- Notizie --- */
    .news-columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
    .news-column {
        background: linear-gradient(180deg, var(--surface), var(--bg-1));
        border: 1px solid var(--hairline); border-radius: var(--r-md); padding: 18px 20px;
    }
    .news-column h3 { font-size: 14px; margin: 0 0 12px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
    .news-column h3 .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-gold); }
    .news-item { display: flex; flex-direction: column; gap: 4px; padding: 11px 0; border-bottom: 1px solid var(--hairline); text-decoration: none; color: var(--text-primary); }
    .news-item:last-child { border-bottom: none; padding-bottom: 0; }
    .news-item:hover .news-title { color: var(--accent-gold); }
    .news-title { font-size: 13.5px; font-weight: 500; line-height: 1.45; transition: color 0.15s ease; }
    .news-source { font-family: 'IBM Plex Mono', monospace; font-size: 10.5px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.06em; }
    .news-empty { font-size: 13px; color: var(--text-secondary); padding: 6px 0; }

    footer { margin-top: 56px; padding-top: 22px; border-top: 1px solid var(--hairline);
        font-family: 'IBM Plex Mono', monospace; font-size: 11.5px; color: var(--text-secondary); line-height: 1.7; }
    .stato-connessione { color: var(--negative); display: inline-flex; align-items: center; gap: 8px; }
    .empty-filtro { grid-column: 1 / -1; padding: 40px 0; text-align: center; color: var(--text-secondary); font-family: 'IBM Plex Mono', monospace; font-size: 13px; }

    /* --- Modale grafico storico --- */
    .modal-overlay {
        position: fixed; inset: 0; background: rgba(4, 7, 14, 0.7);
        -webkit-backdrop-filter: blur(5px); backdrop-filter: blur(5px);
        display: none; align-items: center; justify-content: center; padding: 24px; z-index: 60;
    }
    .modal-overlay.aperto { display: flex; animation: overlay-in 0.2s ease; }
    @keyframes overlay-in { from { opacity: 0; } to { opacity: 1; } }
    .modal-box {
        background: linear-gradient(180deg, var(--surface), var(--bg-1));
        border: 1px solid var(--hairline-strong); border-radius: var(--r-lg);
        width: 100%; max-width: 780px; padding: 24px 26px 28px; position: relative; box-shadow: var(--shadow-pop);
    }
    .modal-overlay.aperto .modal-box { animation: box-in 0.24s cubic-bezier(0.16,1,0.3,1); }
    @keyframes box-in { from { transform: translateY(12px) scale(0.98); opacity: 0; } to { transform: none; opacity: 1; } }
    @media (prefers-reduced-motion: reduce) { .modal-overlay.aperto, .modal-overlay.aperto .modal-box { animation: none; } }
    .modal-close {
        position: absolute; top: 16px; right: 16px; width: 34px; height: 34px; border-radius: 8px;
        background: var(--surface-2); border: 1px solid var(--hairline); color: var(--text-secondary);
        display: grid; place-items: center; cursor: pointer; transition: color 0.15s ease, background 0.15s ease;
    }
    .modal-close:hover { color: var(--text-primary); background: var(--surface-3); }
    .modal-close svg { width: 16px; height: 16px; }
    .modal-titolo { font-size: 21px; font-weight: 700; color: var(--name-giallo); margin: 0 0 4px; padding-right: 40px; letter-spacing: -0.01em; }
    .modal-sub { font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--text-secondary); margin-bottom: 18px; }
    .modal-chart { width: 100%; position: relative; touch-action: none; }
    .modal-chart svg { width: 100%; height: auto; display: block; }
    .chart-axis { font-family: 'IBM Plex Mono', monospace; font-size: 10px; fill: var(--text-secondary); }
    .chart-grid { stroke: var(--hairline); stroke-width: 1; }
    .crosshair { pointer-events: none; opacity: 0; transition: opacity 0.12s ease; }
    .crosshair.on { opacity: 1; }
    .cross-line { stroke: var(--text-secondary); stroke-width: 1; stroke-dasharray: 3 3; }
    .chart-tip {
        position: absolute; pointer-events: none; opacity: 0; transform: translate(-50%, -130%);
        background: var(--bg-deep); border: 1px solid var(--hairline-strong); border-radius: 8px;
        padding: 7px 10px; font-family: 'IBM Plex Mono', monospace; font-size: 11px; white-space: nowrap;
        box-shadow: 0 8px 20px rgba(0,0,0,0.5); transition: opacity 0.12s ease; z-index: 2;
    }
    .chart-tip.on { opacity: 1; }
    .chart-tip .tip-date { color: var(--text-secondary); }
    .chart-tip .tip-val { color: var(--text-primary); font-weight: 600; }

    @media (max-width: 720px) {
        .stat-row { grid-template-columns: 1fr 1fr; }
        .topbar-inner { padding: 12px 16px; }
        .page { padding: 24px 16px 64px; }
    }
    @media (max-width: 480px) {
        .cards-grid { grid-template-columns: 1fr; }
        .brand-sub { display: none; }
        .stat-value { font-size: 26px; }
    }
</style>
</head>
<body>

<header class="topbar">
    <div class="topbar-inner">
        <div class="brand">
            <span class="brand-mark" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-6 4 4 6-8"/><path d="M15 7h4v4"/></svg>
            </span>
            <span class="brand-text">
                <span class="brand-title">Mercati finanziari</span>
                <span class="brand-sub">Dashboard live</span>
            </span>
        </div>
        <div class="live-pill">
            <span class="live-dot" aria-hidden="true"></span>
            <span>Aggiornato <span class="val" id="ultimo-agg">—</span></span>
        </div>
    </div>
</header>

<div class="ticker-tape" aria-hidden="true"><div class="ticker-track" id="ticker-track"></div></div>

<main class="page">
    <p class="sottotitolo" id="sottotitolo">In attesa del primo aggiornamento…</p>

    <section class="stat-row" aria-label="Riepilogo mercato">
        <div class="stat pos">
            <div class="stat-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>In rialzo</div>
            <div class="stat-value" id="stat-up">0</div>
        </div>
        <div class="stat neg">
            <div class="stat-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l6 6 4-4 8 8"/><path d="M17 17h4v-4"/></svg>In ribasso</div>
            <div class="stat-value" id="stat-down">0</div>
        </div>
        <div class="stat pos">
            <div class="stat-label"><span class="live-dot" style="width:9px;height:9px;animation:none" aria-hidden="true"></span>Mercati aperti</div>
            <div class="stat-value" id="stat-live" style="color:var(--text-primary)">0</div>
        </div>
        <div class="stat gold">
            <div class="stat-label"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/></svg>Strumenti</div>
            <div class="stat-value" id="stat-tot" style="color:var(--text-primary)">0</div>
        </div>
    </section>

    <div class="section-head">
        <h2 class="section-title">Portafoglio &amp; strumenti seguiti</h2>
        <span class="section-meta" id="conteggio-visibili"></span>
    </div>
    <div class="toolbar">
        <div class="toolbar-group">
            <label for="ordina">Ordina</label>
            <span class="select-wrap">
                <select id="ordina">
                    <option value="nome">Nome (A–Z)</option>
                    <option value="variazione">Variazione %</option>
                    <option value="ytd">YTD %</option>
                    <option value="volume">Rapporto volume</option>
                    <option value="prezzo">Prezzo</option>
                </select>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
            </span>
        </div>
        <div class="toolbar-group">
            <label for="settore">Settore</label>
            <span class="select-wrap">
                <select id="settore">
                    <option value="tutti">Tutti i settori</option>
                </select>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
            </span>
        </div>
        <div class="toolbar-group segmented" id="filtri" role="group" aria-label="Filtra strumenti">
            <button data-filtro="tutti" aria-pressed="true">Tutti</button>
            <button data-filtro="rialzo" aria-pressed="false">In rialzo</button>
            <button data-filtro="ribasso" aria-pressed="false">In ribasso</button>
            <button data-filtro="volume" aria-pressed="false">Volume anomalo</button>
            <button data-filtro="nd" aria-pressed="false">Non disp.</button>
        </div>
    </div>
    <div class="cards-grid" id="cards-grid"></div>

    <div class="section-head" style="margin-top:44px">
        <h2 class="section-title">Notizie correlate</h2>
    </div>
    <div class="news-columns" id="news-columns"></div>

    <footer id="footer">Connessione al server in corso…</footer>
</main>

<div class="modal-overlay" id="modal" role="dialog" aria-modal="true" aria-labelledby="modal-titolo">
    <div class="modal-box">
        <button class="modal-close" id="modal-close" aria-label="Chiudi grafico">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
        <h3 class="modal-titolo" id="modal-titolo"></h3>
        <div class="modal-sub" id="modal-sub"></div>
        <div class="modal-chart" id="modal-chart"></div>
    </div>
</div>

<script>
const POLLING_MS = __POLLING_MS__;

// Icone SVG inline (niente emoji: rendering coerente e controllabile via CSS)
const ICONA = {
    su: '<svg viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="M6 2.5l4 6H2z"/></svg>',
    giu: '<svg viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="M6 9.5l-4-6h8z"/></svg>',
    bolt: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13 2L4 13h6l-1 9 9-12h-6z"/></svg>',
};

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
        const val = simboloValuta(dati.valuta);
        pezzi.push(`<span class="ticker-item ${classe}"><b>${escapeHtml(nome)}</b> ${formattaNumero(dati.last_close)}${val ? ' ' + escapeHtml(val) : ''} ${classe === 'up' ? ICONA.su : ICONA.giu} ${dati.pct_change >= 0 ? '+' : ''}${formattaNumero(dati.pct_change)}%</span>`);
    }
    document.getElementById('ticker-track').innerHTML = pezzi.length ? pezzi.join('').repeat(2) : '';
}

// Stato dell'interfaccia: ultimi dati ricevuti + criterio di ordinamento e filtro
// scelti dall'utente. Ordinare/filtrare NON richiede una nuova chiamata al
// server: si ridisegnano le card a partire da STATO.risultati.
const STATO = { risultati: {}, ordine: 'nome', filtro: 'tutti', settore: 'tutti', primoCarico: true };

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

// Scheletri mostrati al primissimo caricamento, prima che arrivino i dati.
function mostraScheletri(n = 8) {
    const contenitore = document.getElementById('cards-grid');
    let html = '';
    for (let i = 0; i < n; i++) {
        html += `<div class="card skeleton" aria-hidden="true">
            <div class="sk-line sm"></div>
            <div class="sk-line lg"></div>
            <div class="sk-line"></div>
            <div class="sk-line"></div>
        </div>`;
    }
    contenitore.innerHTML = html;
}

function renderCards() {
    const contenitore = document.getElementById('cards-grid');
    contenitore.innerHTML = '';
    const voci = Object.entries(STATO.risultati)
        .filter(([nome, dati]) => passaFiltro(dati) && passaSettore(dati))
        .sort(confrontaVoci);

    const conteggio = document.getElementById('conteggio-visibili');
    if (conteggio) conteggio.textContent = voci.length ? `${voci.length} visibili` : '';

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
        const freccia = classe === 'up' ? ICONA.su : ICONA.giu;
        const ytdTxt = (dati.ytd_pct !== null && dati.ytd_pct !== undefined) ? `${dati.ytd_pct >= 0 ? '+' : ''}${formattaNumero(dati.ytd_pct)}%` : 'n/d';
        const volumeFlag = (dati.rapporto_volume && dati.rapporto_volume >= 1.5) ? `<span class="volume-flag">${ICONA.bolt} anomalo</span>` : '';
        const val = simboloValuta(dati.valuta);
        const segno = dati.pct_change >= 0 ? '+' : '';

        const statoLive = dati.live
            ? '<span class="live-inline live" title="In tempo reale (ritardo ~15 min)"></span>'
            : '<span class="live-inline" title="Mercato chiuso — ultima chiusura"></span>';

        const settoreTag = dati.settore ? `<div class="card-settore">${escapeHtml(dati.settore)}</div>` : '';
        card.className = `card clickabile ${classe}`;
        card.setAttribute('role', 'button');
        card.setAttribute('tabindex', '0');
        card.setAttribute('aria-label', `${nome}, ${formattaNumero(dati.last_close)} ${dati.valuta || ''}, ${segno}${formattaNumero(dati.pct_change)}% — apri grafico storico`);
        card.innerHTML = `
            ${settoreTag}
            <div class="card-header">
                <span class="card-name">${statoLive}${escapeHtml(nome)}</span>
                <span class="card-badge ${classe}">${freccia} ${segno}${formattaNumero(dati.pct_change)}%</span>
            </div>
            <div class="card-close">${formattaNumero(dati.last_close)}${val ? `<span class="valuta">${escapeHtml(val)}</span>` : ''}</div>
            <div class="card-row"><span>Variazione</span><span class="${classe}">${dati.change >= 0 ? '+' : ''}${formattaNumero(dati.change)}</span></div>
            <div class="card-row"><span>YTD</span><span class="${(dati.ytd_pct || 0) >= 0 ? 'up' : 'down'}">${ytdTxt}</span></div>
            <div class="card-row"><span>Massimo / Minimo</span><span>${formattaNumero(dati.high)} / ${formattaNumero(dati.low)}</span></div>
            <div class="card-row"><span>Volume</span><span>${Math.round(dati.volume).toLocaleString('it-IT')} ${volumeFlag}</span></div>
        `;
        contenitore.appendChild(card);
    }
}

function aggiornaStatistiche(dati) {
    const valori = Object.values(dati.risultati || {});
    const totale = valori.length;
    const live = valori.filter(d => d && d.live).length;
    document.getElementById('stat-up').textContent = dati.positivi;
    document.getElementById('stat-down').textContent = dati.negativi;
    document.getElementById('stat-live').textContent = live;
    document.getElementById('stat-tot').textContent = totale;
    document.getElementById('ultimo-agg').textContent = dati.ultimo_aggiornamento || '—';
}

function costruisciNotizie(news) {
    const contenitore = document.getElementById('news-columns');
    contenitore.innerHTML = '';
    for (const [argomento, voci] of Object.entries(news)) {
        const colonna = document.createElement('div');
        colonna.className = 'news-column';
        let interno = `<h3><span class="dot" aria-hidden="true"></span>${escapeHtml(argomento)}</h3>`;
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
        document.getElementById('footer').innerHTML = `Prezzi da Yahoo Finance (tempo reale con ~15 min di ritardo, o ultima chiusura a mercato chiuso) · Notizie da Financial Times, Il Sole 24 Ore, ANSA, Teleborsa · Il pallino verde = mercato aperto`;

        STATO.risultati = dati.risultati;
        STATO.primoCarico = false;
        aggiornaStatistiche(dati);
        aggiornaOpzioniSettore();
        costruisciTicker(dati.risultati);
        renderCards();
        costruisciNotizie(dati.news);
    } catch (errore) {
        document.getElementById('footer').innerHTML = '<span class="stato-connessione">Connessione al server persa — verifica che lo script Python sia ancora in esecuzione.</span>';
    }
}

// --- Grafico storico interattivo (dettaglio al click su una card) ---
// Salviamo qui la geometria per l'hover crosshair.
const GRAFICO = { serie: null, W: 720, H: 300, padL: 6, padR: 6, padT: 16, padB: 26, min: 0, max: 1, colore: '#34D399' };

function nX(i) { return GRAFICO.padL + (i / (GRAFICO.serie.length - 1)) * (GRAFICO.W - GRAFICO.padL - GRAFICO.padR); }
function nY(c) { return GRAFICO.padT + (1 - (c - GRAFICO.min) / ((GRAFICO.max - GRAFICO.min) || 1)) * (GRAFICO.H - GRAFICO.padT - GRAFICO.padB); }

function disegnaGrafico(serie) {
    const sub = document.getElementById('modal-sub');
    const cont = document.getElementById('modal-chart');
    if (!serie || serie.length < 2) {
        sub.textContent = 'Storico non disponibile per questo strumento.';
        cont.innerHTML = '';
        return;
    }
    const valori = serie.map(p => p.c);
    const min = Math.min(...valori), max = Math.max(...valori);
    const primo = serie[0], ultimo = serie[serie.length - 1];
    const salita = ultimo.c >= primo.c;
    const colore = salita ? '#34D399' : '#F87171';
    Object.assign(GRAFICO, { serie, min, max, colore });
    const { W, H, padL, padR, padB } = GRAFICO;

    const punti = serie.map((p, i) => `${nX(i).toFixed(1)},${nY(p.c).toFixed(1)}`).join(' ');
    const area = `${padL.toFixed(1)},${(H - padB).toFixed(1)} ${punti} ${(W - padR).toFixed(1)},${(H - padB).toFixed(1)}`;
    const dataInizio = primo.t.split('-').reverse().join('/');
    const dataFine = ultimo.t.split('-').reverse().join('/');
    const yMax = nY(max), yMin = nY(min), yMid = (yMax + yMin) / 2;

    cont.innerHTML = `
        <svg id="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Andamento prezzo nell'ultimo anno">
            <defs>
                <linearGradient id="grad-area" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="${colore}" stop-opacity="0.26"/>
                    <stop offset="100%" stop-color="${colore}" stop-opacity="0"/>
                </linearGradient>
            </defs>
            <line class="chart-grid" x1="${padL}" y1="${yMax.toFixed(1)}" x2="${W - padR}" y2="${yMax.toFixed(1)}"/>
            <line class="chart-grid" x1="${padL}" y1="${yMid.toFixed(1)}" x2="${W - padR}" y2="${yMid.toFixed(1)}"/>
            <line class="chart-grid" x1="${padL}" y1="${yMin.toFixed(1)}" x2="${W - padR}" y2="${yMin.toFixed(1)}"/>
            <polygon points="${area}" fill="url(#grad-area)"/>
            <polyline points="${punti}" fill="none" stroke="${colore}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            <g class="crosshair" id="crosshair">
                <line class="cross-line" id="cross-line" y1="${padT_val()}" y2="${(H - padB).toFixed(1)}"/>
                <circle id="cross-dot" r="4" fill="${colore}" stroke="var(--bg-deep)" stroke-width="2"/>
            </g>
            <text class="chart-axis" x="${padL}" y="${(yMax - 5).toFixed(1)}">max ${formattaNumero(max)}</text>
            <text class="chart-axis" x="${padL}" y="${(yMin + 13).toFixed(1)}">min ${formattaNumero(min)}</text>
            <text class="chart-axis" x="${padL}" y="${H - 7}">${dataInizio}</text>
            <text class="chart-axis" x="${W - padR}" y="${H - 7}" text-anchor="end">${dataFine}</text>
        </svg>
        <div class="chart-tip" id="chart-tip"></div>`;

    const variazione = ((ultimo.c - primo.c) / primo.c) * 100;
    sub.innerHTML = `Ultimo anno · ${dataInizio} → ${dataFine} · `
        + `<span style="color:${colore}">${variazione >= 0 ? '+' : ''}${formattaNumero(variazione)}% sul periodo</span>`;

    collegaHoverGrafico();
}

function padT_val() { return GRAFICO.padT.toFixed(1); }

function collegaHoverGrafico() {
    const svg = document.getElementById('chart-svg');
    const cross = document.getElementById('crosshair');
    const line = document.getElementById('cross-line');
    const dot = document.getElementById('cross-dot');
    const tip = document.getElementById('chart-tip');
    if (!svg) return;

    function muovi(clientX) {
        const rect = svg.getBoundingClientRect();
        const svgX = (clientX - rect.left) * (GRAFICO.W / rect.width);
        const frazione = (svgX - GRAFICO.padL) / (GRAFICO.W - GRAFICO.padL - GRAFICO.padR);
        let i = Math.round(frazione * (GRAFICO.serie.length - 1));
        i = Math.max(0, Math.min(GRAFICO.serie.length - 1, i));
        const p = GRAFICO.serie[i];
        const x = nX(i), y = nY(p.c);
        line.setAttribute('x1', x); line.setAttribute('x2', x);
        dot.setAttribute('cx', x); dot.setAttribute('cy', y);
        cross.classList.add('on');
        const data = p.t.split('-').reverse().join('/');
        tip.innerHTML = `<span class="tip-date">${data}</span> · <span class="tip-val">${formattaNumero(p.c)}</span>`;
        tip.style.left = (x / GRAFICO.W * rect.width) + 'px';
        tip.style.top = (y / GRAFICO.H * rect.height) + 'px';
        tip.classList.add('on');
    }
    function esci() { cross.classList.remove('on'); tip.classList.remove('on'); }

    svg.addEventListener('pointermove', (e) => muovi(e.clientX));
    svg.addEventListener('pointerleave', esci);
    svg.addEventListener('pointerdown', (e) => muovi(e.clientX));
}

async function apriDettaglio(nome, ticker) {
    const modal = document.getElementById('modal');
    modal.classList.add('aperto');
    document.getElementById('modal-titolo').textContent = nome;
    document.getElementById('modal-sub').textContent = (ticker || '') + ' — caricamento storico…';
    document.getElementById('modal-chart').innerHTML = '';
    document.getElementById('modal-close').focus();
    if (!ticker) { document.getElementById('modal-sub').textContent = 'Ticker non disponibile.'; return; }
    try {
        const risposta = await fetch('/api/storico?ticker=' + encodeURIComponent(ticker), { cache: 'no-store' });
        const dati = await risposta.json();
        disegnaGrafico(dati.storico);
    } catch (errore) {
        document.getElementById('modal-sub').textContent = 'Impossibile caricare lo storico.';
    }
}

let ultimoTrigger = null;
function chiudiModal() {
    document.getElementById('modal').classList.remove('aperto');
    if (ultimoTrigger) { ultimoTrigger.focus(); ultimoTrigger = null; }
}

// --- Wiring eventi (una sola volta) ---
function apriDaCard(card) {
    ultimoTrigger = card;
    apriDettaglio(card.dataset.nome, card.dataset.ticker);
}
document.getElementById('cards-grid').addEventListener('click', (e) => {
    const card = e.target.closest('.card.clickabile');
    if (card) apriDaCard(card);
});
document.getElementById('cards-grid').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const card = e.target.closest('.card.clickabile');
    if (card) { e.preventDefault(); apriDaCard(card); }
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
    document.querySelectorAll('#filtri button').forEach(b => b.setAttribute('aria-pressed', 'false'));
    btn.setAttribute('aria-pressed', 'true');
    STATO.filtro = btn.dataset.filtro;
    renderCards();
});
document.getElementById('modal-close').addEventListener('click', chiudiModal);
document.getElementById('modal').addEventListener('click', (e) => {
    if (e.target.id === 'modal') chiudiModal(); // click sullo sfondo scuro
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') chiudiModal(); });

mostraScheletri();
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
