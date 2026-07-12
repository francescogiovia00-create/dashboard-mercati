"""
mercati_lib.py
==============
Modulo condiviso: configurazione strumenti/feed e funzioni di recupero dati
(mercati + notizie). Usato da dashboard_server.py (dashboard live nel browser),
cosi' la lista di strumenti e la logica di calcolo restano in un solo posto.
"""

import re
import time
import random
import requests
import feedparser
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

# =====================================================================
#  1) STRUMENTI DA MONITORARE
# =====================================================================
# Per AGGIUNGERE uno strumento basta una riga:   "Nome leggibile": "TICKER",
# Il TICKER e' quello di Yahoo Finance (finance.yahoo.com -> cerca il nome ->
# il simbolo tra parentesi nel titolo, es. "AAPL", "ENEL.MI", "005930.KS").
# La VALUTA viene dedotta in automatico dal suffisso del ticker (vedi sotto),
# quindi normalmente non devi fare altro. Per casi strani usa VALUTE_OVERRIDE.
TICKERS = {
    # --- Indici ---
    "S&P 500": "^GSPC",
    # --- ETF (Borsa di Milano, in EUR) ---
    "Difesa Europea (Amundi DEFS)": "DEFS.MI",
    "Semiconduttori (VanEck SMH)": "SMH.MI",
    "Mercato Europeo (iShares SMEA)": "SMEA.MI",
    "Mercati Emergenti (iShares EIMI)": "EIMI.MI",
    "S&P 500 UCITS (iShares CSSPX)": "CSSPX.MI",
    # --- Azioni ---
    "Apple": "AAPL",
    "Nvidia": "NVDA",
    "AMD": "AMD",
    "Palantir": "PLTR",
    "Caterpillar": "CAT",
    "Samsung Electronics": "005930.KS",
    "TSMC": "TSM",
    "ASML": "ASML",
    # --- Aggiungi qui i tuoi (esempi gia' pronti, togli il # per attivarli) ---
    # "FTSE MIB": "FTSEMIB.MI",
    # "Nasdaq 100": "^NDX",
    # "Intel": "INTC",
    # "Broadcom": "AVGO",
    # "MP Materials (terre rare)": "MP",
    # "Bitcoin": "BTC-USD",
}

# Valuta dedotta dal suffisso del ticker Yahoo (semplificazione ad uso personale).
VALUTE_SUFFISSO = {
    ".MI": "EUR", ".DE": "EUR", ".PA": "EUR", ".AS": "EUR", ".MC": "EUR",
    ".BR": "EUR", ".F": "EUR", ".L": "GBp", ".SW": "CHF", ".KS": "KRW",
    ".KQ": "KRW", ".T": "JPY", ".HK": "HKD", ".TO": "CAD", ".SS": "CNY",
    ".SZ": "CNY",
}
# Eccezioni esplicite quando la regola sul suffisso non basta: "TICKER": "VALUTA"
VALUTE_OVERRIDE = {
    # "BTC-USD": "USD",
}


def valuta_per_ticker(ticker: str) -> str:
    """Restituisce il codice valuta (EUR/USD/KRW...) o "" per gli indici (punti)."""
    if ticker in VALUTE_OVERRIDE:
        return VALUTE_OVERRIDE[ticker]
    if ticker.startswith("^"):
        return ""  # indice: sono "punti", nessuna valuta
    for suffisso, valuta in VALUTE_SUFFISSO.items():
        if ticker.endswith(suffisso):
            return valuta
    return "USD"  # default: azioni/ETF quotati negli USA


# =====================================================================
#  2) NOTIZIE: FONTI RSS + ARGOMENTI
# =====================================================================
# Feed RSS ufficiali verificati (recuperiamo solo TITOLO + LINK, mai il testo).
RSS_FEEDS = {
    "Financial Times": "https://www.ft.com/markets?format=rss",
    "Il Sole 24 Ore (Finanza)": "https://www.ilsole24ore.com/rss/finanza.xml",
    "Il Sole 24 Ore (Tecnologia)": "https://www.ilsole24ore.com/rss/tecnologia.xml",
    "Il Sole 24 Ore (Mondo)": "https://www.ilsole24ore.com/rss/mondo.xml",
    "ANSA (Economia)": "https://www.ansa.it/sito/notizie/economia/economia_rss.xml",
    "Teleborsa (Energia)": "https://www.teleborsa.it/feed/rss/energia",
    "Teleborsa (Generale)": "https://www.teleborsa.it/feed/rss",
}

# Argomenti seguiti. Ogni parola chiave e' un pattern:
#   "parola"   -> match a PAROLA INTERA (es. "amd" non prende "camden")
#   "parola*"  -> match a PREFISSO   (es. "energetic*" prende "energetico/energetica")
# Il confronto e' case-insensitive su titolo + riassunto della notizia.
NEWS_TOPICS = {
    "🔬 Semiconduttori": [
        "semiconduttor*", "semiconductor*", "chip", "chips", "microchip*",
        "nvidia", "tsmc", "asml", "foundry", "fonderi*", "wafer",
        "nanometr*", "litografi*", "lithograph*", "silicon",
    ],
    "🖥️ Processori": [
        "processor*", "processore", "processori", "cpu", "gpu",
        "microprocessor*", "intel", "amd", "qualcomm", "snapdragon",
        "ryzen", "epyc", "xeon", "apple silicon", "arm holdings",
    ],
    "⚡ Energia": [
        "energia", "energetic*", "energy", "gas natural*", "rinnovabil*",
        "eolic*", "solare", "fotovoltaic*", "elettricit*", "petrolio",
        "oil", "gpl", "gnl", "lng", "nucleare", "nuclear",
    ],
    "💻 Tecnologia": [
        "tecnologi*", "technolog*", "intelligenza artificiale",
        "artificial intelligence", "big tech", "software", "cloud",
        "data center", "datacenter", "microsoft", "google", "alphabet",
        "meta platforms", "amazon", "startup", "cybersicurezza",
        "cybersecurity", "quantum",
    ],
    "🪨 Terre Rare & Materie Critiche": [
        "terre rare", "rare earth*", "neodimio", "neodymium", "disprosio",
        "dysprosium", "gallio", "gallium", "germanio", "germanium", "litio",
        "lithium", "cobalto", "cobalt", "materie prime critich*",
        "critical mineral*", "magneti permanenti",
    ],
    "🏦 Banche Centrali": [
        "banca central*", "banche central*", "central bank*", "federal reserve",
        "fed", "fomc", "bce", "ecb", "lagarde", "powell", "tasso d'interesse",
        "tassi d'interesse", "tassi di interesse", "interest rate*", "rate cut*",
        "rate hike*", "tagli dei tassi", "rialzo dei tassi", "inflazione",
        "inflation", "politica monetaria", "monetary policy",
        "quantitative easing", "quantitative tightening",
    ],
}

MAX_NEWS_PER_TOPIC = 4  # quante notizie mostrare al massimo per ciascun argomento


# =====================================================================
#  FUNZIONI: NOTIZIE
# =====================================================================
def _compila_keyword(keyword: str):
    """Trasforma una keyword ("amd", "energetic*") in una regex con confini di parola."""
    keyword = keyword.strip().lower()
    if keyword.endswith("*"):
        # prefisso: confine prima, poi qualsiasi continuazione
        return re.compile(r"\b" + re.escape(keyword[:-1]), re.IGNORECASE)
    # parola intera
    return re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)


def _scarica_singolo_feed(nome_fonte: str, url_feed: str, headers: dict):
    """Scarica e fa il parsing di un singolo feed RSS; usata in parallelo."""
    try:
        risposta = requests.get(url_feed, headers=headers, timeout=15, allow_redirects=True)
        risposta.raise_for_status()
        return nome_fonte, feedparser.parse(risposta.content)
    except Exception as errore:
        print(f"Avviso: impossibile leggere il feed '{nome_fonte}' ({errore})")
        return nome_fonte, None


def _timestamp_entry(entry) -> float:
    """Data di pubblicazione della notizia come timestamp (0 se assente)."""
    for campo in ("published_parsed", "updated_parsed"):
        valore = getattr(entry, campo, None)
        if valore:
            try:
                return time.mktime(valore)
            except Exception:
                pass
    return 0.0


def get_relevant_news():
    """
    Scarica i feed RSS IN PARALLELO e restituisce, per ogni argomento in
    NEWS_TOPICS, una lista di notizie ORDINATE dalla piu' recente. Ogni notizia
    e' un dizionario strutturato: {"titolo", "fonte", "link"}.
    Non vengono MAI recuperati i testi completi: solo titolo e link.
    """
    topic_regex = {
        topic: [_compila_keyword(k) for k in parole]
        for topic, parole in NEWS_TOPICS.items()
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }

    feed_scaricati = {}
    with ThreadPoolExecutor(max_workers=max(1, len(RSS_FEEDS))) as executor:
        futures = [
            executor.submit(_scarica_singolo_feed, nome_fonte, url_feed, headers)
            for nome_fonte, url_feed in RSS_FEEDS.items()
        ]
        for future in as_completed(futures):
            nome_fonte, feed = future.result()
            if feed is not None:
                feed_scaricati[nome_fonte] = feed

    # Raccogli TUTTI i match (con data), poi ordina per data e taglia: cosi' la
    # lista mostrata e' stabile e con le notizie piu' recenti in cima.
    grezzi = {topic: [] for topic in NEWS_TOPICS}
    titoli_visti = {topic: set() for topic in NEWS_TOPICS}

    for nome_fonte, feed in feed_scaricati.items():
        for entry in feed.entries:
            titolo = getattr(entry, "title", "").strip()
            if not titolo:
                continue
            riassunto = getattr(entry, "summary", "")
            testo = f"{titolo} {riassunto}"
            link = getattr(entry, "link", "")
            quando = _timestamp_entry(entry)

            for topic, regexes in topic_regex.items():
                chiave = titolo.lower()
                if chiave in titoli_visti[topic]:
                    continue
                if any(regex.search(testo) for regex in regexes):
                    titoli_visti[topic].add(chiave)
                    grezzi[topic].append({
                        "titolo": titolo,
                        "fonte": nome_fonte,
                        "link": link,
                        "_ts": quando,
                    })

    risultati = {}
    for topic, voci in grezzi.items():
        voci.sort(key=lambda v: v["_ts"], reverse=True)
        risultati[topic] = [
            {"titolo": v["titolo"], "fonte": v["fonte"], "link": v["link"]}
            for v in voci[:MAX_NEWS_PER_TOPIC]
        ]
    return risultati


# =====================================================================
#  FUNZIONI: DATI DI MERCATO
# =====================================================================
def _scarica_storico_batch(tickers, tentativi: int = 3):
    """
    Scarica lo storico a 1 anno per TUTTI i ticker in un'unica chiamata batch,
    con retry + backoff esponenziale. threads=False: recupero sequenziale e
    "gentile", per non farsi bloccare da Yahoo (errore 429) sul piano gratuito.
    """
    simboli = list(dict.fromkeys(tickers))  # unici, ordine preservato
    if not simboli:
        return None
    for tentativo in range(tentativi):
        try:
            dati = yf.download(
                tickers=simboli,
                period="1y",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=False,
                progress=False,
            )
            if dati is not None and not dati.empty:
                return dati
        except Exception as errore:
            print(f"Avviso: download storico fallito "
                  f"(tentativo {tentativo + 1}/{tentativi}): {errore}")
        # attesa crescente con un po' di casualita' prima di riprovare
        time.sleep((2 ** tentativo) + random.uniform(0, 1))
    return None


def _estrai_frame(dati, ticker: str, n_simboli: int = None):
    """
    Estrae il sotto-DataFrame di un singolo ticker dal risultato di yf.download.
    Rileva da solo se le colonne sono "a due livelli" (piu' ticker) o semplici
    (un ticker), senza fidarsi del numero di simboli: alcune versioni di yfinance
    restituiscono comunque il doppio livello anche con un solo ticker.
    """
    if dati is None:
        return None
    try:
        if isinstance(dati.columns, pd.MultiIndex):
            if ticker not in dati.columns.get_level_values(0):
                return None
            df = dati[ticker]
        else:
            df = dati
        df = df.dropna(how="all")
        return df if not df.empty else None
    except Exception:
        return None


# --- Cache dello "storico giornaliero" (base per chiusura precedente / YTD / volume medio) ---
# Lo storico a 1 anno serve solo per valori che cambiano al massimo una volta al
# giorno, quindi lo ricarichiamo di rado; il PREZZO ATTUALE arriva invece dai dati
# intraday (candele al minuto), recuperati a ogni ciclo. Cosi' il prezzo si aggiorna
# in continuazione ma non tempestiamo Yahoo con lo storico pesante.
_BASE_CACHE = {}            # ticker -> dizionario base (vedi _estrai_base_da_daily)
_BASE_TIMESTAMP = 0.0
BASE_TTL_SECONDI = 900      # ogni quanto ricaricare lo storico giornaliero (15 min)
SOGLIA_LIVE_SECONDI = 1800  # se l'ultima quotazione e' piu' vecchia di cosi', "mercato chiuso"


def _scarica_intraday_batch(tickers, tentativi: int = 3):
    """Scarica le candele al minuto della seduta corrente: da qui esce il prezzo che si muove."""
    simboli = list(dict.fromkeys(tickers))
    if not simboli:
        return None
    for tentativo in range(tentativi):
        try:
            dati = yf.download(
                tickers=simboli,
                period="1d",
                interval="1m",
                group_by="ticker",
                auto_adjust=False,
                threads=False,
                progress=False,
            )
            if dati is not None and not dati.empty:
                return dati
        except Exception as errore:
            print(f"Avviso: download intraday fallito "
                  f"(tentativo {tentativo + 1}/{tentativi}): {errore}")
        time.sleep((2 ** tentativo) + random.uniform(0, 1))
    return None


def _estrai_base_da_daily(df):
    """Dallo storico giornaliero: chiusura precedente, base YTD, volume medio, fallback."""
    chiuse = df["Close"].dropna()
    if len(chiuse) < 2:
        return None
    prev_close = float(chiuse.iloc[-2])   # chiusura della seduta PRECEDENTE (per la variazione)
    daily_last = float(chiuse.iloc[-1])   # ultima chiusura (fallback se manca l'intraday)

    volumi = df["Volume"].dropna() if "Volume" in df else chiuse.iloc[0:0]
    avg_volume = float(volumi.tail(63).mean()) if len(volumi) else None
    daily_volume = float(volumi.iloc[-1]) if len(volumi) else 0.0

    high_serie = df["High"].dropna() if "High" in df else chiuse
    low_serie = df["Low"].dropna() if "Low" in df else chiuse

    anno_corrente = chiuse.index[-1].year
    chiuse_ytd = chiuse[chiuse.index.year == anno_corrente]
    ytd_base = (float(chiuse_ytd.iloc[0])
                if len(chiuse_ytd) >= 1 and float(chiuse_ytd.iloc[0]) else None)

    # Serie storica completa (1 anno di chiusure) per il grafico di dettaglio:
    # e' leggera (~252 punti) e non entra mai nel payload di /api/dati, viene
    # letta separatamente dall'endpoint /api/storico solo quando serve.
    storico = [
        {"t": indice.strftime("%Y-%m-%d"), "c": float(valore)}
        for indice, valore in chiuse.items()
    ]

    return {
        "prev_close": prev_close,
        "daily_last": daily_last,
        "ytd_base": ytd_base,
        "avg_volume": avg_volume,
        "daily_volume": daily_volume,
        "daily_high": float(high_serie.iloc[-1]) if len(high_serie) else daily_last,
        "daily_low": float(low_serie.iloc[-1]) if len(low_serie) else daily_last,
        "date": chiuse.index[-1].strftime("%d/%m/%Y"),
        "storico": storico,
    }


def _estrai_live_da_intraday(df):
    """Dalle candele al minuto: ultimo prezzo, massimo/minimo di giornata, e se e' 'live'."""
    if df is None:
        return None
    chiuse = df["Close"].dropna()
    if len(chiuse) == 0:
        return None
    last_price = float(chiuse.iloc[-1])

    # "live" = l'ultima candela e' recente (mercato aperto ora). Con i dati gratuiti
    # Yahoo c'e' un ritardo tipico ~15 min, per questo la soglia e' larga.
    try:
        eta_secondi = time.time() - chiuse.index[-1].timestamp()
        live = eta_secondi < SOGLIA_LIVE_SECONDI
    except Exception:
        live = False

    high_serie = df["High"].dropna() if "High" in df else chiuse
    low_serie = df["Low"].dropna() if "Low" in df else chiuse
    vol_serie = df["Volume"].dropna() if "Volume" in df else None

    return {
        "last_price": last_price,
        "high": float(high_serie.max()) if len(high_serie) else last_price,
        "low": float(low_serie.min()) if len(low_serie) else last_price,
        "volume": float(vol_serie.sum()) if vol_serie is not None and len(vol_serie) else None,
        "live": live,
    }


def _componi_metriche(base, live, valuta, ticker):
    """Unisce base (giornaliero) + live (intraday) nel dizionario finale per uno strumento."""
    if base is None:
        return None
    prev_close = base["prev_close"]

    if live is not None:
        last_price = live["last_price"]
        high = live["high"]
        low = live["low"]
        volume = live["volume"] if live["volume"] is not None else base["daily_volume"]
        is_live = live["live"]
    else:
        # nessun dato intraday disponibile: ripiego sull'ultima chiusura giornaliera
        last_price = base["daily_last"]
        high = base["daily_high"]
        low = base["daily_low"]
        volume = base["daily_volume"]
        is_live = False

    change = last_price - prev_close
    pct_change = (change / prev_close) * 100 if prev_close else 0.0
    ytd_base = base["ytd_base"]
    ytd_pct = ((last_price - ytd_base) / ytd_base) * 100 if ytd_base else None
    avg_volume = base["avg_volume"]
    rapporto_volume = (volume / avg_volume) if (avg_volume and volume) else None

    return {
        "last_close": last_price,   # ORA e' il prezzo ATTUALE (intraday) o l'ultima chiusura
        "change": change,
        "pct_change": pct_change,
        "volume": volume if volume is not None else 0.0,
        "volume_medio": avg_volume,
        "rapporto_volume": rapporto_volume,
        "ytd_pct": ytd_pct,
        "high": high,
        "low": low,
        "date": base["date"],
        "valuta": valuta,
        "ticker": ticker,
        "live": is_live,
    }


def _aggiorna_base_cache(forza: bool = False):
    """Ricarica lo storico giornaliero solo se scaduto (o forzato); tiene la base vecchia se fallisce."""
    global _BASE_TIMESTAMP
    fresca = _BASE_CACHE and (time.time() - _BASE_TIMESTAMP) < BASE_TTL_SECONDI
    if fresca and not forza:
        return
    dati = _scarica_storico_batch(list(TICKERS.values()))
    if dati is None:
        return  # niente rete: continuo con la base precedente (se c'e')
    n_simboli = len(set(TICKERS.values()))
    nuova = {}
    for ticker in set(TICKERS.values()):
        df = _estrai_frame(dati, ticker, n_simboli)
        base = _estrai_base_da_daily(df) if df is not None else None
        if base is not None:
            nuova[ticker] = base
    if nuova:
        _BASE_CACHE.clear()
        _BASE_CACHE.update(nuova)
        _BASE_TIMESTAMP = time.time()


def get_market_data(ticker: str):
    """Dati per UN singolo strumento (comodo per test manuali o uso da script)."""
    df_daily = _estrai_frame(_scarica_storico_batch([ticker]), ticker, 1)
    base = _estrai_base_da_daily(df_daily) if df_daily is not None else None
    df_live = _estrai_frame(_scarica_intraday_batch([ticker]), ticker, 1)
    live = _estrai_live_da_intraday(df_live)
    return _componi_metriche(base, live, valuta_per_ticker(ticker), ticker)


def get_storico_ticker(ticker: str):
    """
    Restituisce la serie storica a 1 anno di UN ticker come lista di
    {"t": "AAAA-MM-GG", "c": chiusura}, oppure None se non e' in cache.
    La base viene popolata da collect_market_data()/_aggiorna_base_cache(),
    quindi in condizioni normali (dashboard avviata) e' gia' disponibile.
    """
    base = _BASE_CACHE.get(ticker)
    if base is None:
        _aggiorna_base_cache()  # tentativo di popolamento se la cache e' vuota
        base = _BASE_CACHE.get(ticker)
    return base.get("storico") if base else None


def collect_market_data():
    """
    Recupera i dati per tutti gli strumenti in TICKERS.
    - PREZZO ATTUALE: da un'unica chiamata batch intraday (candele al minuto), a
      ogni ciclo -> il valore si aggiorna in continuazione durante la seduta.
    - CHIUSURA PRECEDENTE / YTD / VOLUME MEDIO: dallo storico giornaliero, ricaricato
      di rado (cache con TTL) per non farsi bloccare da Yahoo.
    Restituisce (risultati_ordinati, positivi, negativi).
    """
    _aggiorna_base_cache()
    intraday = _scarica_intraday_batch(list(TICKERS.values()))
    n_simboli = len(set(TICKERS.values()))

    risultati = {}
    positivi = 0
    negativi = 0
    for nome, ticker in TICKERS.items():
        base = _BASE_CACHE.get(ticker)
        df_live = _estrai_frame(intraday, ticker, n_simboli)
        live = _estrai_live_da_intraday(df_live)
        metriche = _componi_metriche(base, live, valuta_per_ticker(ticker), ticker)
        if metriche is not None:
            if metriche["change"] >= 0:
                positivi += 1
            else:
                negativi += 1
        risultati[nome] = metriche

    return risultati, positivi, negativi
