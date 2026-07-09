"""
mercati_lib.py
==============
Modulo condiviso: configurazione strumenti/feed e funzioni di recupero dati
(mercati + notizie). Usato sia da report_mercati_telegram.py (report giornaliero
su Telegram) sia da dashboard_server.py (dashboard live nel browser), cosi' la
lista di strumenti e la logica di calcolo restano in un solo posto.
"""

import requests
import feedparser
import yfinance as yf
from datetime import datetime

# ============ CONFIGURAZIONE (da compilare/personalizzare) ============

# --- Feed RSS ufficiali verificati (titolo + link, mai testo completo) ---
RSS_FEEDS = {
    "Financial Times": "https://www.ft.com/markets?format=rss",
    "Il Sole 24 Ore": "https://www.ilsole24ore.com/rss/finanza.xml",
    "Teleborsa (Energia)": "https://www.teleborsa.it/feed/rss/energia",
    "Teleborsa (Generale)": "https://www.teleborsa.it/feed/rss",
}

# Parole chiave per ogni argomento che vuoi seguire.
# Un titolo/riassunto viene incluso se contiene almeno una di queste parole (case-insensitive).
NEWS_TOPICS = {
    "🔬 Semiconduttori": [
        "semiconduttor", "semiconductor", "chip", "nvidia", "tsmc",
        "asml", " amd ", "intel", "microchip",
    ],
    "⚡ Energia Europea": [
        "energia", "energetic", "energy", "gas naturale", "rinnovabil",
        "eolico", "solare", "elettricit", "petrolio", "gpl", "gnl",
    ],
}

MAX_NEWS_PER_TOPIC = 4  # quante notizie mostrare al massimo per ciascun argomento

# Mercati/strumenti da monitorare: nome leggibile -> ticker Yahoo Finance
TICKERS = {
    "S&P 500": "^GSPC",
    "Apple": "AAPL",
    "Difesa Europea (Amundi DEFS)": "DEFS.MI",
    "Semiconduttori (VanEck SMH)": "SMH.MI",
    "Mercato Europeo (iShares SMEA)": "SMEA.MI",
    "Mercati Emergenti (iShares EIMI)": "EIMI.MI",
    "S&P 500 UCITS (iShares CSSPX)": "CSSPX.MI",
    "Nvidia": "NVDA",
    "AMD": "AMD",
    "Palantir": "PLTR",
    "Caterpillar": "CAT",
    "Samsung Electronics": "005930.KS",
    "TSMC": "TSM",
    "ASML": "ASML",
    # Aggiungi altri strumenti qui, es:
    # "FTSE MIB": "FTSEMIB.MI",
    # "Nasdaq": "^IXIC",
    # "Bitcoin": "BTC-USD",
}
# =========================================================


def escape_markdown(testo: str) -> str:
    """
    Neutralizza i caratteri speciali di Telegram Markdown (_ * ` [ ])
    in testi che arrivano da fonti esterne (es. titoli di notizie RSS),
    cosi' non rompono la formattazione del messaggio.
    """
    if not testo:
        return testo
    caratteri_speciali = ["_", "*", "`", "[", "]"]
    for carattere in caratteri_speciali:
        testo = testo.replace(carattere, f"\\{carattere}")
    return testo


def get_relevant_news():
    """
    Scarica i feed RSS configurati e restituisce, per ogni argomento in NEWS_TOPICS,
    una lista di notizie (titolo + fonte + link) il cui titolo o riassunto contiene
    una delle parole chiave associate.
    Non vengono MAI recuperati o salvati i testi completi degli articoli: solo
    titolo e link, cosi' il link porta all'articolo originale sul sito della fonte.
    """
    risultati = {topic: [] for topic in NEWS_TOPICS}

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for nome_fonte, url_feed in RSS_FEEDS.items():
        try:
            risposta = requests.get(url_feed, headers=headers, timeout=15)
            risposta.raise_for_status()
            feed = feedparser.parse(risposta.content)
        except Exception as errore:
            print(f"Avviso: impossibile leggere il feed '{nome_fonte}' ({errore})")
            continue

        for entry in feed.entries:
            titolo = getattr(entry, "title", "")
            riassunto = getattr(entry, "summary", "")
            testo_da_controllare = f"{titolo} {riassunto}".lower()
            link = getattr(entry, "link", "")

            for topic, parole_chiave in NEWS_TOPICS.items():
                if len(risultati[topic]) >= MAX_NEWS_PER_TOPIC:
                    continue
                if any(parola in testo_da_controllare for parola in parole_chiave):
                    titolo_sicuro = escape_markdown(titolo)
                    voce = f"• {titolo_sicuro} — _{nome_fonte}_\n  {link}"
                    if voce not in risultati[topic]:
                        risultati[topic].append(voce)

    return risultati


def get_market_data(ticker: str):
    """Recupera chiusura, variazione giornaliera, YTD e volume medio per uno strumento."""
    stock = yf.Ticker(ticker)

    hist = stock.history(period="1y")
    if len(hist) < 2:
        return None

    last_close = hist["Close"].iloc[-1]
    prev_close = hist["Close"].iloc[-2]
    change = last_close - prev_close
    pct_change = (change / prev_close) * 100
    volume = hist["Volume"].iloc[-1]
    day_high = hist["High"].iloc[-1]
    day_low = hist["Low"].iloc[-1]

    anno_corrente = hist.index[-1].year
    hist_ytd = hist[hist.index.year == anno_corrente]
    if len(hist_ytd) >= 1:
        primo_close_anno = hist_ytd["Close"].iloc[0]
        ytd_pct = ((last_close - primo_close_anno) / primo_close_anno) * 100
    else:
        ytd_pct = None

    hist_recente = hist.tail(63)
    volume_medio = hist_recente["Volume"].mean()
    rapporto_volume = volume / volume_medio if volume_medio else None

    return {
        "last_close": last_close,
        "change": change,
        "pct_change": pct_change,
        "volume": volume,
        "volume_medio": volume_medio,
        "rapporto_volume": rapporto_volume,
        "ytd_pct": ytd_pct,
        "high": day_high,
        "low": day_low,
        "date": hist.index[-1].strftime("%d/%m/%Y"),
    }


def collect_market_data():
    """
    Recupera i dati per tutti gli strumenti in TICKERS una sola volta.
    Restituisce (risultati, positivi, negativi).
    """
    risultati = {}
    positivi = 0
    negativi = 0
    for name, ticker in TICKERS.items():
        data = get_market_data(ticker)
        risultati[name] = data
        if data is not None:
            if data["change"] >= 0:
                positivi += 1
            else:
                negativi += 1
    return risultati, positivi, negativi
