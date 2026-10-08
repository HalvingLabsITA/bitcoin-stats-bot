"""
Bitcoin Stats Bot — Halving Labs v4.0
Canale: @hlbitcoinstats
"""
import math
import re
import logging
import httpx
import time
import asyncio
import feedparser
import html
import random
import os
import json
from collections import Counter
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, time as datetime_time
import pytz
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    ApplicationHandlerStop,
)
# ── Configurazione ────────────────────────────────────────────────────────────
# Log applicativo con rotazione in-app (5 MB × 5 file = ~25 MB max). Prima era un
# FileHandler senza rotazione: /app/data/bot.log cresceva illimitato (era a 81 MB).
LOG_PATH = "/app/data/bot.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_file_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8"
)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),   # console → log Docker (già cappato a 10m×3)
        _file_handler,             # log persistente con rotazione
    ]
)
# httpx/telegram loggavano a INFO l'URL completo delle richieste, TOKEN del bot
# compreso in chiaro (…/bot<token>/sendMessage). A WARNING il token non finisce
# più nel file di log. apscheduler a WARNING per togliere lo spam di getUpdates.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
# Log leggibile solo dal proprietario: conteneva il token in chiaro ed era 644.
try:
    os.chmod(LOG_PATH, 0o600)
except OSError:
    pass
logger = logging.getLogger(__name__)
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN non impostato. Esegui: export BOT_TOKEN=il_tuo_token")
CHANNEL_ID = "@hlbitcoinstats"
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))  # 0 = nessun admin
CHECK_INTERVAL_BLOCKS = 300      # secondi tra un check blocco e l'altro
WHALE_TX_PAGE_SIZE    = 25       # mempool.space restituisce le tx a pagine di 25
MAX_WHALE_TX_SCAN     = 5000     # tetto di sicurezza: max tx scansionate per blocco
# ── Soglie Whale ──────────────────────────────────────────────────────────────
# Due binari indipendenti (vedi get_whale_txs):
#   • OG Whale  → per ETÀ delle monete: monete "antiche" che si risvegliano.
#                 Rileva P2PK (gratis, tipico 2009-2011) + coin-age via blocco
#                 d'origine. Esclude per costruzione exchange/CoinJoin, che
#                 muovono monete fresche.
#   • Mega Whale → per QUANTITÀ pura: movimenti giganteschi (spesso exchange,
#                 ma a quelle cifre sono comunque notizia).
MEGA_WHALE_BTC        = 10_000   # BTC minimi per un Mega Whale alert (quantità)
OG_CUTOFF_YEAR        = 2013     # monete create ENTRO quest'anno = OG (età)
OG_P2PK_MIN_BTC       = 20       # importo min. per segnalare una spesa P2PK
OG_AGE_MIN_BTC        = 100      # importo min. per attivare il lookup coin-age
OG_MAX_INPUT_LOOKUPS  = 2        # input (più grossi) ispezionati per tx candidata
OG_MAX_BLOCK_LOOKUPS  = 80       # tetto lookup d'origine per blocco (budget chiamate)
# ── Mining / Filtro Miner ─────────────────────────────────────────────────────
# Pool "solo/lottery": singoli miner indipendenti. Ogni voce ha un nome mostrato
# + gli alias con cui mempool.space può etichettare i suoi blocchi (per il match).
# Compaiono SINGOLARMENTE nel menu del filtro (non come categoria unica).
SOLO_POOLS = [
    ("Solo CK",     ["Solo CK", "SoloCK", "solo.ckpool.org", "ckpool"]),
    ("Public Pool", ["Public Pool", "PublicPool", "public-pool.io"]),
    ("AtlasPool",   ["AtlasPool", "solo.atlaspool.io"]),
    ("SoloPool",    ["SoloPool", "btc.solopool.org"]),
    ("HMI",         ["HMI", "solo.homeminingitalia.org", "solo.homemining.org", "Home Mining Italia"]),
]
SOLO_NAMES    = [d for d, _ in SOLO_POOLS]
_SOLO_ALIAS   = {d: al for d, al in SOLO_POOLS}
# Lista piatta di tutti gli alias solo: usata dall'alert "Lottery" del canale.
LOTTERY_POOLS = [a for _, al in SOLO_POOLS for a in al]
POOLS_TTL            = 6 * 3600   # cache lista pool note (mempool.space): 6 ore
MINER_MENU_MAX_POOLS = 20         # quante pool NOTE (oltre alle solo) nel menu
cache       = {"data": None, "last_update": 0}
news_cache  = {"entries": None, "last_update": 0}
pools_cache = {"names": None,   "last_update": 0}   # lista pool note (mempool.space)
# ── Persistenza ───────────────────────────────────────────────────────────────
DATA_DIR   = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "last_block.txt")
def load_last_block() -> int:
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return 0
    except Exception as e:
        # 0 = "riparti da capo": se il file è illeggibile è meglio un giro ampio
        # che saltare blocchi in silenzio. Ma va saputo.
        logger.error(f"last_block illeggibile ({e}): riparto da 0.")
        return 0


# ── Scritture atomiche ────────────────────────────────────────────────────────
# Prima ogni save_* scriveva direttamente sul file finale con open(..., "w"):
# un kill a metà scrittura lasciava un JSON troncato. E i load_* inghiottivano
# l'errore restituendo {}, quindi il salvataggio successivo ripartiva da un
# dizionario vuoto e riscriveva il file — cancellando tutti gli alert di tutti
# gli utenti. Ora si scrive su un temporaneo + os.replace (atomico su POSIX).
def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_json(path: str, data) -> None:
    _atomic_write(path, json.dumps(data, indent=2))


def _load_json(path: str) -> dict:
    """
    Legge un JSON. Se è illeggibile NON restituisce {} in silenzio — sarebbe la
    premessa della cancellazione descritta sopra: mette da parte il file corrotto
    e lo segnala nel log, così il contenuto è recuperabile a mano.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as e:
        broken = f"{path}.corrotto-{int(time.time())}"
        try:
            os.replace(path, broken)
            logger.error(f"{path} illeggibile ({e}) — spostato in {broken}. Riparto da vuoto.")
        except OSError as e2:
            logger.error(f"{path} illeggibile ({e}) e non spostabile ({e2}).")
        return {}
    except OSError as e:
        logger.error(f"{path} non leggibile ({e}): riparto da vuoto.")
        return {}


def save_last_block(height: int):
    try:
        _atomic_write(STATE_FILE, str(height))
    except OSError as e:
        logger.error(f"Impossibile salvare last_block: {e}")
last_notified_block = load_last_block()
# ── Persistenza Alert Utenti ──────────────────────────────────────────────────
ALERTS_FILE = os.path.join(DATA_DIR, "alerts.json")
def load_alerts() -> dict:
    """Carica gli alert dal file JSON. Struttura: {chat_id: {"above": float|None, "below": float|None}}"""
    return _load_json(ALERTS_FILE)
def save_alerts(data: dict):
    try:
        _write_json(ALERTS_FILE, data)
    except OSError as e:
        logger.error(f"Impossibile salvare alerts: {e}")
# ── Persistenza Filtro Miner (abbonamenti blocco per-utente) ──────────────────
MINER_FILTERS_FILE = os.path.join(DATA_DIR, "miner_filters.json")
def load_miner_filters() -> dict:
    """Struttura: {chat_id: {"pools": [nomi_pool]}}  (solo e custom incluse per nome).
    Nota: i vecchi record possono avere "include_solo": bool → migrati in /filtraminer."""
    return _load_json(MINER_FILTERS_FILE)
def save_miner_filters(data: dict):
    try:
        _write_json(MINER_FILTERS_FILE, data)
    except OSError as e:
        logger.error(f"Impossibile salvare miner_filters: {e}")
# ── Stati conversazione ───────────────────────────────────────────────────────
SCELTA_CONV, IMPORTO_CONV           = range(2)
TX_STATUS_STATE, BLOCK_FINDER_STATE = range(10, 12)
ALERT_DIR_STATE, ALERT_PRICE_STATE  = range(20, 22)
# ── Helpers ───────────────────────────────────────────────────────────────────
def esc_md(text) -> str:
    """
    Escapa i caratteri speciali del Markdown *legacy* di Telegram (`_ * [ ` `` ` ``)
    nel testo dinamico/esterno (es. nomi dei pool di mining). Senza questo, un
    nome contenente '*' o '_' fa fallire send_message con 'Can't parse entities'.
    Per il testo con link (news) usiamo invece parse_mode HTML + html.escape,
    più robusto sulle etichette dei link.
    """
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text
def format_it(value, decimali: int = 0) -> str:
    try:
        if decimali == 0:
            return f"{int(value):,}".replace(",", ".")
        formattato = f"{value:,.{decimali}f}"
        return formattato.replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)
def fg_emoji(value: int) -> str:
    if value <= 24:  return "🔴"
    if value <= 44:  return "🟠"
    if value <= 55:  return "🟡"
    if value <= 74:  return "🟢"
    return "💚"
def fg_label_it(classification: str) -> str:
    mapping = {
        "Extreme Fear":  "Paura Estrema",
        "Fear":          "Paura",
        "Neutral":       "Neutrale",
        "Greed":         "Avidità",
        "Extreme Greed": "Avidità Estrema",
    }
    return mapping.get(classification, classification)
# ── Fetching dati Bitcoin ─────────────────────────────────────────────────────
async def get_bitcoin_data() -> dict | None:
    curr_time = time.time()
    if cache["data"] and (curr_time - cache["last_update"] < 60):
        return cache["data"]
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            h_res     = await client.get("https://mempool.space/api/blocks/tip/height")
            height    = int(h_res.text)
            b_list    = await client.get("https://mempool.space/api/v1/blocks")
            b         = b_list.json()[0]
            f_res     = await client.get("https://mempool.space/api/v1/fees/recommended")
            d_res     = await client.get("https://mempool.space/api/v1/difficulty-adjustment")
            p_e       = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCEUR")
            p_u       = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            diff = d_res.json()
            data = {
                "height":         height,
                "tx_count":       b.get("tx_count") or b.get("size", 0),
                "miner":          b.get("extras", {}).get("pool", {}).get("name", "Sconosciuto"),
                "block_hash":     b.get("id", ""),
                "difficulty":     b.get("difficulty", 0),
                "fast":           f_res.json().get("fastestFee", 0),
                "medium":         f_res.json().get("halfHourFee", 0),
                "slow":           f_res.json().get("hourFee", 0),
                "diff_change":    diff.get("difficultyChange", 0),
                "diff_remaining": diff.get("remainingBlocks", 0),
                "price_eur":      float(p_e.json()["price"]),
                "price_usd":      float(p_u.json()["price"]),
            }
            cache["data"], cache["last_update"] = data, curr_time
            return data
        except Exception as e:
            logger.error(f"Errore API Bitcoin: {e}")
            return cache.get("data")
# ── Fetching Fear & Greed ─────────────────────────────────────────────────────
async def get_fear_greed() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
            data = r.json()["data"][0]
            value = int(data["value"])
            return {
                "value":          value,
                "classification": data["value_classification"],
                "label_it":       fg_label_it(data["value_classification"]),
                "emoji":          fg_emoji(value),
            }
    except Exception as e:
        logger.error(f"Errore Fear & Greed API: {e}")
        return None
# ── Fetching Lightning Network ────────────────────────────────────────────────
async def get_lightning_stats() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://mempool.space/api/v1/lightning/statistics/latest")
            d = r.json().get("latest", {})
            capacity_btc = d.get("total_capacity", 0) / 100_000_000
            avg_cap_btc  = d.get("avg_capacity",   0) / 100_000_000
            return {
                "node_count":    d.get("node_count", 0),
                "channel_count": d.get("channel_count", 0),
                "capacity_btc":  round(capacity_btc, 2),
                "avg_cap_btc":   round(avg_cap_btc, 4),
                "tor_nodes":     d.get("tor_nodes", 0),
                "clearnet_nodes":d.get("clearnet_nodes", 0),
            }
    except Exception as e:
        logger.error(f"Errore Lightning API: {e}")
        return None
# ── Pool di mining note (per il Filtro Miner) ─────────────────────────────────
async def get_known_pools() -> list:
    """
    Nomi delle pool note, da mempool.space, ordinate per blocchi trovati
    nell'ultima settimana. In cache POOLS_TTL per non martellare l'API.
    """
    now = time.time()
    if pools_cache["names"] and now - pools_cache["last_update"] < POOLS_TTL:
        return pools_cache["names"]
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get("https://mempool.space/api/v1/mining/pools/1w")
            data = r.json()
        # "Unknown"/"Sconosciuto" sono blocchi non attribuiti: fuori dal menu.
        names = [
            p["name"] for p in data.get("pools", [])
            if p.get("name") and p["name"].strip().lower() not in ("unknown", "sconosciuto")
        ]
        if names:
            pools_cache["names"]       = names
            pools_cache["last_update"] = now
    except Exception as e:
        logger.error(f"Errore fetch pool note: {e}")
    return pools_cache["names"] or []

def is_solo_miner(miner: str) -> bool:
    """True se il miner è una pool solo/lottery (singolo miner indipendente)."""
    return any(p.lower() in (miner or "").lower() for p in LOTTERY_POOLS)

def pool_aliases(name: str) -> list:
    """Alias di match per una pool selezionata: le solo hanno varianti, le pool
    note e quelle scritte a mano dall'utente combaciano col nome stesso."""
    return _SOLO_ALIAS.get(name, [name])

def miner_matches(miner: str, selected: list) -> bool:
    """True se il miner del blocco combacia con una delle pool scelte dall'utente
    (match esatto o sottostringa case-insensitive: comodo per le voci a mano)."""
    m = (miner or "").lower()
    if not m:
        return False
    for name in selected:
        for a in pool_aliases(name):
            al = a.lower()
            if al and (al == m or al in m):
                return True
    return False
# ── Fetching Whale Transactions ───────────────────────────────────────────────
def is_coinjoin(tx: dict) -> bool:
    """
    Euristica per identificare — ed ESCLUDERE — le transazioni CoinJoin, che
    altrimenti finirebbero notificate come falsi 'whale' quando il totale degli
    output supera la soglia (es. i grandi round Wasabi/WabiSabi da centinaia di BTC).

    Un CoinJoin unisce molti partecipanti indipendenti. Segnali su cui ci basiamo:
      1. Denominazione dominante — un gruppo cospicuo di output ha lo STESSO valore
         (le "denominazioni" tipiche dei coinjoin), su una tx con molti input.
      2. Poche denominazioni distinte — molti output ma pochi valori diversi,
         perché gli importi si ripetono (round con denominazioni standard).

    In caso di dubbio preferiamo escludere: meglio saltare un possibile whale che
    spammare il canale con un coinjoin. Un pagamento batch d'exchange (pochi input,
    molti output di importi vari) NON viene classificato come coinjoin.
    """
    vin  = tx.get("vin",  [])
    vout = tx.get("vout", [])
    n_in, n_out = len(vin), len(vout)

    # Serve un minimo di partecipanti (molti input E molti output) per un CoinJoin.
    # Il vecchio codice richiedeva >=10 input: alziamo la sensibilità a >=5 input
    # e >=10 output senza catturare i normali pagamenti (1-2 input).
    if n_in < 5 or n_out < 10:
        return False

    values = [v.get("value", 0) for v in vout if v.get("value", 0) > 0]
    if not values:
        return False

    counts               = Counter(values)
    top_value, top_count = counts.most_common(1)[0]
    equal_ratio          = top_count / len(values)
    distinct             = len(counts)

    # Segnale 1 — denominazione dominante: >=5 output identici e almeno il 20%
    # degli output condivide lo stesso valore (soglia abbassata da 0.30 a 0.20
    # per intercettare anche i round WabiSabi, più eterogenei).
    dominant_denomination = top_count >= 5 and equal_ratio >= 0.20

    # Segnale 2 — poche denominazioni distinte rispetto al numero di output:
    # con >=20 output e <= n_out/3 valori diversi gli importi si ripetono molto,
    # firma tipica di un round di mixing.
    few_distinct_values = n_out >= 20 and distinct <= max(3, n_out // 3)

    if dominant_denomination or few_distinct_values:
        logger.info(
            f"CoinJoin rilevato e ignorato: {tx.get('txid', '')[:16]}… "
            f"(in={n_in}, out={n_out}, denom_top={top_count}/{len(values)}, "
            f"distinti={distinct}, ratio={equal_ratio:.2f})"
        )
        return True
    return False

# Cache dell'anno d'origine per txid, così non ri-chiediamo lo stesso prevout più
# volte (né tra tx diverse dello stesso blocco, né tra scansioni successive).
_origin_year_cache: dict[str, int | None] = {}

async def _origin_year(client: httpx.AsyncClient, txid: str) -> int | None:
    """
    Anno di conferma della tx che ha CREATO un UTXO (il prevout speso da un input).
    mempool.space non espone l'altezza del prevout dentro il blocco, quindi facciamo
    una chiamata a /tx/:txid e leggiamo status.block_time. Risultato in cache.
    """
    if txid in _origin_year_cache:
        return _origin_year_cache[txid]
    year: int | None = None
    try:
        r = await client.get(f"https://mempool.space/api/tx/{txid}")
        if r.status_code == 200:
            st = r.json().get("status", {})
            bt = st.get("block_time")
            if bt:
                year = datetime.utcfromtimestamp(bt).year
    except Exception as e:
        logger.warning(f"Origin lookup fallito per {txid[:16]}…: {e}")
    _origin_year_cache[txid] = year
    return year

def _era_label(year: int | None) -> str:
    """Etichetta leggibile per l'era della moneta risvegliata."""
    if year is None:
        return "primi anni"
    if year == 2009:
        return "🚨 Satoshi-era (2009)"
    if year == 2010:
        return "🥇 2010"
    return f"{year}"

async def get_whale_txs(block_hash: str | None = None) -> dict:
    """
    Analizza TUTTO il blocco e classifica le tx interessanti su due binari:

      • "og"   — OG Whale: monete "antiche" che si muovono. Una tx è OG se
                 spende un output P2PK (tipico 2009-2011, rilevato gratis dal tipo
                 del prevout) oppure se un suo input consistente proviene da una
                 moneta creata entro OG_CUTOFF_YEAR (coin-age via blocco d'origine).
                 Exchange e CoinJoin muovono monete fresche → non rientrano.
      • "mega" — Mega Whale: tx con output totali ≥ MEGA_WHALE_BTC (quantità pura),
                 CoinJoin esclusi.

    Il coin-age costa 1 chiamata per input ispezionato: la attiviamo solo sopra
    OG_AGE_MIN_BTC, guardando al più OG_MAX_INPUT_LOOKUPS input per tx e con un
    tetto di OG_MAX_BLOCK_LOOKUPS lookup per blocco (budget chiamate).
    """
    mega_sats = MEGA_WHALE_BTC   * 100_000_000
    p2pk_min  = OG_P2PK_MIN_BTC  * 100_000_000
    age_min   = OG_AGE_MIN_BTC   * 100_000_000
    og, mega  = [], []
    lookups_left = OG_MAX_BLOCK_LOOKUPS
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            if not block_hash:
                r = await client.get("https://mempool.space/api/blocks/tip/hash")
                block_hash = r.text.strip()
            index = 0
            while index < MAX_WHALE_TX_SCAN:
                r = await client.get(
                    f"https://mempool.space/api/block/{block_hash}/txs/{index}"
                )
                if r.status_code != 200:
                    logger.warning(f"Whale scan: HTTP {r.status_code} a index {index}")
                    break
                txs = r.json()
                if not txs:
                    break
                for tx in txs:
                    vin       = tx.get("vin", [])
                    total_out = sum(v.get("value", 0) for v in tx.get("vout", []))

                    # ── Binario Mega (quantità) ──────────────────────────────
                    if total_out >= mega_sats and not is_coinjoin(tx):
                        mega.append({
                            "txid":      tx["txid"],
                            "total_btc": round(total_out / 100_000_000, 2),
                            "outputs":   len(tx.get("vout", [])),
                        })

                    # ── Binario OG (età) ─────────────────────────────────────
                    if not vin or vin[0].get("is_coinbase"):
                        continue   # coinbase: non "sveglia" nulla

                    # Segnale 1 — spesa P2PK (gratis): input a partire da 2009-2011.
                    p2pk_sats = sum(
                        v["prevout"]["value"]
                        for v in vin
                        if v.get("prevout", {}).get("scriptpubkey_type") == "p2pk"
                    )
                    is_p2pk = p2pk_sats >= p2pk_min

                    # Segnale 2 — coin-age: solo per tx consistenti e nel budget.
                    og_year: int | None = None
                    if is_p2pk or total_out >= age_min:
                        # Ispeziona gli input più grossi (con prevout non-coinbase).
                        candidates = sorted(
                            (v for v in vin if v.get("prevout")),
                            key=lambda v: v["prevout"].get("value", 0),
                            reverse=True,
                        )[:OG_MAX_INPUT_LOOKUPS]
                        for v in candidates:
                            if lookups_left <= 0:
                                break
                            lookups_left -= 1
                            y = await _origin_year(client, v["txid"])
                            if y is not None and (og_year is None or y < og_year):
                                og_year = y
                            await asyncio.sleep(0.1)

                    is_old = og_year is not None and og_year <= OG_CUTOFF_YEAR
                    if is_p2pk or is_old:
                        og.append({
                            "txid":      tx["txid"],
                            "total_btc": round(total_out / 100_000_000, 2),
                            "og_btc":    round(max(p2pk_sats, 0) / 100_000_000, 2),
                            "year":      og_year,
                            "is_p2pk":   is_p2pk,
                            "era":       _era_label(og_year),
                        })

                if len(txs) < WHALE_TX_PAGE_SIZE:
                    break              # ultima pagina raggiunta
                index += WHALE_TX_PAGE_SIZE
                await asyncio.sleep(0.15)   # cortesia verso mempool.space
    except Exception as e:
        logger.error(f"Errore whale detection: {e}")
    # OG ordinati dai più antichi (year None in coda), Mega per importo.
    og.sort(key=lambda x: (x["year"] is None, x["year"] or 9999, -x["total_btc"]))
    mega.sort(key=lambda x: x["total_btc"], reverse=True)
    return {"og": og, "mega": mega}
# ── Fetching News (6 fonti, max 5 articoli, solo Bitcoin, ordinati per data) ──
FONTI_NEWS = [
    ("https://it.cointelegraph.com/rss/tag/bitcoin",      "🇮🇹", "CoinTelegraph IT"),
    ("https://en.cryptonomist.ch/feed/",                  "🇮🇹", "Cryptonomist"),
    ("https://it.beincrypto.com/feed/",                   "🇮🇹", "BeInCrypto IT"),
    ("https://bitcoinmagazine.com/.rss/full/",            "🇺🇸", "Bitcoin Magazine"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/",   "🇺🇸", "CoinDesk"),
    ("https://decrypt.co/feed",                           "🇺🇸", "Decrypt"),
]
# ── Filtro Bitcoin-only ────────────────────────────────────────────────────────
BITCOIN_KEYWORDS = {
    "bitcoin", "btc", "satoshi", "sats", "halving", "lightning network",
    "lightning", "on-chain", "onchain", "mempool", "hash rate", "hashrate",
    "mining", "miner", "proof of work", "pow", "taproot", "ordinals",
    "runes", "inscriptions", "etf bitcoin", "spot bitcoin",
}
ALTCOIN_KEYWORDS = {
    "ethereum", "eth ", " eth,", " eth.", "solana", "sol ", "xrp", "ripple",
    "cardano", "ada ", "dogecoin", "doge", "shiba", "bnb", "binance coin",
    "polygon", "matic", "avalanche", "avax", "polkadot", "dot ", "chainlink",
    "link ", "litecoin", "ltc", "tron", "trx", "sui ", "aptos", "apt ",
    "near ", "cosmos", "atom ", "algorand", "algo", "stellar", "xlm",
    "uniswap", "uni ", "aave", "compound", "defi", "nft", "altcoin",
    "altcoins", "memecoin", "meme coin", "shitcoin",
}
def is_bitcoin_news(title: str, summary: str = "") -> bool:
    """Restituisce True se l'articolo riguarda Bitcoin."""
    text = (title + " " + summary).lower()
    for kw in ALTCOIN_KEYWORDS:
        if kw in text:
            if "bitcoin" not in text and "btc" not in text:
                return False
    for kw in BITCOIN_KEYWORDS:
        if kw in text:
            return True
    return False
async def get_news_entries() -> list:
    curr_time = time.time()
    if news_cache["entries"] and (curr_time - news_cache["last_update"] < 600):
        return news_cache["entries"]
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.8,en-US;q=0.5,en;q=0.3",
    }
    entries = []
    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        for url, flag, source in FONTI_NEWS:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    logger.warning(f"Fonte {source}: HTTP {r.status_code}")
                    continue
                feed = feedparser.parse(r.text)
                if not feed.entries:
                    logger.warning(f"Fonte {source}: nessun articolo nel feed")
                    continue
                count = 0
                for e in feed.entries:
                    if count >= 2:
                        break
                    title   = html.unescape(getattr(e, "title", ""))
                    summary = html.unescape(getattr(e, "summary", ""))
                    if not is_bitcoin_news(title, summary):
                        logger.info(f"Fonte {source}: articolo scartato (non Bitcoin) — {title[:60]}")
                        continue
                    pub = getattr(e, "published_parsed", None)
                    pub_ts = time.mktime(pub) if pub else 0.0
                    entries.append({
                        "flag":    flag,
                        "source":  source,
                        "title":   title,
                        "link":    getattr(e, "link", "#"),
                        "pub_ts":  pub_ts,
                    })
                    count += 1
                logger.info(f"Fonte {source}: OK — {count} articoli Bitcoin accettati")
            except Exception as ex:
                logger.warning(f"Fonte {source} non disponibile: {ex}")
    if not entries:
        news_cache["entries"]     = []
        news_cache["last_update"] = curr_time
        return []
    entries.sort(key=lambda x: x["pub_ts"], reverse=True)
    top5 = entries[:5]
    news_cache["entries"]     = top5
    news_cache["last_update"] = curr_time
    return top5
async def get_news_text() -> str:
    """Restituisce le news in formato HTML (parse_mode='HTML').

    Titoli e link provengono da feed esterni e vanno passati con html.escape:
    con il Markdown legacy un titolo contenente '_', '*', '[' o ']' faceva
    fallire l'invio ('Can't parse entities'), saltando /news e l'alert news.
    """
    entries = await get_news_entries()
    if not entries:
        return "⚠️ Al momento non è stato possibile recuperare le ultime notizie. Riprova tra poco."
    return "\n\n".join(
        f"{e['flag']} <b>{html.escape(e['source'])}</b>\n"
        f"📰 <a href=\"{html.escape(e['link'], quote=True)}\">{html.escape(e['title'])}</a>"
        for e in entries
    )
# ── Automazioni canale ────────────────────────────────────────────────────────
async def notify_miner_subscribers(context, h, miner, is_lottery, tx_count, reward_btc):
    """
    Invia in privato la notifica del blocco agli utenti il cui Filtro Miner
    combacia: pool selezionata (match esatto sul nome) oppure blocco solo/lottery
    se hanno attivato il toggle. Il canale resta invariato: questo è additivo.
    """
    filtri = load_miner_filters()
    if not filtri:
        return
    for chat_id, cfg in list(filtri.items()):
        try:
            if not miner_matches(miner, cfg.get("pools", [])):
                continue
            if is_lottery:
                text = (
                    f"🦅 *Blocco Solo/Lottery!*\n\n"
                    f"📦 Altezza: `{format_it(h)}`\n"
                    f"⛏️ Miner: *{esc_md(miner)}*\n"
                    f"🔢 Transazioni: `{format_it(tx_count)}`\n"
                    f"💰 Reward: `{format_it(reward_btc, 3)} BTC`"
                )
            else:
                text = (
                    f"⛏️ *Nuovo Blocco — {esc_md(miner)}*\n\n"
                    f"📦 Altezza: `{format_it(h)}`\n"
                    f"🔢 Transazioni: `{format_it(tx_count)}`\n"
                    f"💰 Reward: `{format_it(reward_btc, 3)} BTC`"
                )
            await context.bot.send_message(
                chat_id=int(chat_id), text=text, parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Errore DM Filtro Miner a {chat_id}: {e}")

async def block_monitor(context: ContextTypes.DEFAULT_TYPE):
    global last_notified_block
    d = await get_bitcoin_data()
    if not d:
        return
    await check_alerts(context)
    current_height = d["height"]
    if last_notified_block == 0:
        last_notified_block = current_height
        save_last_block(current_height)
        logger.info(f"Block monitor inizializzato al blocco {current_height}")
        return
    if current_height <= last_notified_block:
        return
    missed = current_height - last_notified_block
    if missed > 1:
        logger.warning(f"Rilevati {missed} blocchi persi ({last_notified_block+1} → {current_height})")
    async with httpx.AsyncClient(timeout=20.0) as client:
        for h in range(last_notified_block + 1, current_height + 1):
            try:
                r_blocks = await client.get("https://mempool.space/api/v1/blocks")
                blocks   = r_blocks.json()
                b = next((x for x in blocks if x.get("height") == h), None)
                if not b:
                    r_b   = await client.get(f"https://mempool.space/api/v1/blocks/{h}")
                    b = next((x for x in r_b.json() if x.get("height") == h), None)
                if not b:
                    logger.warning(f"Blocco {h} non trovato")
                    continue
                miner          = b.get("extras", {}).get("pool", {}).get("name", "Sconosciuto")
                tx_count       = b.get("tx_count") or b.get("size", 0)
                total_fees_btc = b.get("extras", {}).get("totalFees", 0) / 100_000_000
                reward_btc     = (50 / (2 ** (h // 210_000))) + total_fees_btc
                block_hash     = b.get("id", "")
                is_lottery = is_solo_miner(miner)
                if is_lottery:
                    msg = (
                        f"⚠️ *Attenzione!*\n"
                        f"*Blocco Lottery Mining Trovato!*\n\n"
                        f"📦 Altezza: `{format_it(h)}`\n"
                        f"⛏️ Miner: *{esc_md(miner)}*\n"
                        f"🔢 Transazioni: `{format_it(tx_count)}`\n"
                        f"💰 Reward: `{format_it(reward_btc, 3)} BTC`\n\n"
                        f"🦅 Un singolo miner indipendente ha trovato questo blocco!"
                    )
                    sent = await context.bot.send_message(
                        chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown"
                    )
                    try:
                        await context.bot.pin_chat_message(
                            chat_id=CHANNEL_ID, message_id=sent.message_id,
                            disable_notification=True
                        )
                    except Exception as e:
                        logger.error(f"Errore pin: {e}")
                else:
                    msg = (
                        f"🧱 *Nuovo Blocco Rilevato!*\n\n"
                        f"📦 Altezza: `{format_it(h)}`\n"
                        f"⛏️ Miner: *{esc_md(miner)}*\n"
                        f"🔢 Transazioni: `{format_it(tx_count)}`\n"
                        f"💰 Reward: `{format_it(reward_btc, 3)} BTC`"
                    )
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID, text=msg, parse_mode="Markdown"
                    )
                # Notifiche private ai sottoscrittori del Filtro Miner
                await notify_miner_subscribers(
                    context, h, miner, is_lottery, tx_count, reward_btc
                )
                if block_hash:
                    whales = await get_whale_txs(block_hash)
                    for w in whales["og"][:3]:
                        amount = (
                            f"💸 P2PK: `{format_it(w['og_btc'], 2)} BTC` "
                            f"(tx {format_it(w['total_btc'], 2)} BTC)"
                            if w["is_p2pk"]
                            else f"💸 Importo: `{format_it(w['total_btc'], 2)} BTC`"
                        )
                        og_msg = (
                            f"🐋 *OG Whale — Monete Risvegliate!*\n\n"
                            f"📦 Blocco: `{format_it(h)}`\n"
                            f"🕰️ Era: *{esc_md(w['era'])}*\n"
                            f"{amount}\n"
                            f"🔗 [Visualizza TX](https://mempool.space/tx/{w['txid']})"
                        )
                        await context.bot.send_message(
                            chat_id=CHANNEL_ID, text=og_msg, parse_mode="Markdown",
                            disable_web_page_preview=True
                        )
                    for w in whales["mega"][:3]:
                        mega_msg = (
                            f"🐳 *Mega Whale Alert!*\n\n"
                            f"📦 Blocco: `{format_it(h)}`\n"
                            f"💸 Importo: `{format_it(w['total_btc'], 2)} BTC`\n"
                            f"🔗 [Visualizza TX](https://mempool.space/tx/{w['txid']})"
                        )
                        await context.bot.send_message(
                            chat_id=CHANNEL_ID, text=mega_msg, parse_mode="Markdown",
                            disable_web_page_preview=True
                        )
                last_notified_block = h
                save_last_block(h)
                if missed > 1 and h < current_height:
                    await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"Errore notifica blocco {h}: {e}")
                break
async def send_news_alert(context: ContextTypes.DEFAULT_TYPE):
    d    = await get_bitcoin_data()
    fg   = await get_fear_greed()
    news = await get_news_text()
    prezzo = ""
    if d:
        prezzo = (
            f"💰 <b>Prezzo Bitcoin</b>\n"
            f"💶 <code>€ {format_it(d['price_eur'], 2)}</code> | "
            f"💵 <code>$ {format_it(d['price_usd'], 2)}</code>\n\n"
        )
    fear_line = ""
    if fg:
        fear_line = (
            f"{fg['emoji']} <b>Fear &amp; Greed Index:</b> "
            f"<code>{fg['value']} — {html.escape(fg['label_it'])}</code>\n\n"
        )
    await context.bot.send_message(
        CHANNEL_ID,
        f"{prezzo}{fear_line}🗞 <b>Ultime Notizie Bitcoin</b>\n\n{news}",
        parse_mode="HTML",
        disable_web_page_preview=True
    )
async def send_prezzo_canale(context: ContextTypes.DEFAULT_TYPE):
    d  = await get_bitcoin_data()
    fg = await get_fear_greed()
    if not d:
        return
    fear_line = ""
    if fg:
        fear_line = f"\n{fg['emoji']} Fear & Greed: `{fg['value']} — {fg['label_it']}`"
    await context.bot.send_message(
        CHANNEL_ID,
        f"💰 *Prezzo Bitcoin*\n\n"
        f"💶 `€ {format_it(d['price_eur'], 2)}`\n"
        f"💵 `$ {format_it(d['price_usd'], 2)}`"
        f"{fear_line}",
        parse_mode="Markdown"
    )
# ── Job: controllo alert prezzo ──────────────────────────────────────────────
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    d = await get_bitcoin_data()
    if not d:
        return
    price = d["price_eur"]
    alerts = load_alerts()
    # Le soglie scattate si annotano qui e si applicano alla fine su un file
    # riletto fresco. Tra un invio e l'altro ci sono degli await, e in quella
    # finestra /alert può aver scritto: salvando il dict letto all'inizio si
    # cancellavano le modifiche fatte nel frattempo.
    scattati: list[tuple[str, str]] = []
    for chat_id, soglie in list(alerts.items()):
        above = soglie.get("above")
        below = soglie.get("below")
        try:
            if above and price >= above:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"🚨 *Alert Prezzo Scattato\\!*\n\n"
                        f"📈 BTC ha superato la soglia di `€ {format_it(above, 0)}`\n"
                        f"💶 Prezzo attuale: `€ {format_it(price, 2)}`"
                    ),
                    parse_mode="Markdown"
                )
                scattati.append((chat_id, "above"))
                logger.info(f"Alert ABOVE inviato a {chat_id} — prezzo: {price}")
            if below and price <= below:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        f"🚨 *Alert Prezzo Scattato\\!*\n\n"
                        f"📉 BTC è sceso sotto la soglia di `€ {format_it(below, 0)}`\n"
                        f"💶 Prezzo attuale: `€ {format_it(price, 2)}`"
                    ),
                    parse_mode="Markdown"
                )
                scattati.append((chat_id, "below"))
                logger.info(f"Alert BELOW inviato a {chat_id} — prezzo: {price}")
        except Exception as e:
            logger.error(f"Errore invio alert a {chat_id}: {e}")
    if scattati:
        _azzera_soglie_scattate(scattati)


def _azzera_soglie_scattate(scattati: list[tuple[str, str]]) -> None:
    """
    Azzera le sole soglie indicate, applicandole a una lettura fresca del file.
    Un utente che ha entrambe le soglie a None viene rimosso dal file.
    """
    alerts  = load_alerts()
    cambiato = False
    for chat_id, chiave in scattati:
        voce = alerts.get(chat_id)
        if not isinstance(voce, dict) or voce.get(chiave) is None:
            continue
        voce[chiave] = None
        if voce.get("above") is None and voce.get("below") is None:
            del alerts[chat_id]
        cambiato = True
    if cambiato:
        save_alerts(alerts)
# ── Filtro Miner (menu inline per-utente) ─────────────────────────────────────
MINER_MENU_TEXT = (
    "⛏️ *Filtra Miner*\n\n"
    "Scegli le pool di cui vuoi ricevere la notifica del blocco *in privato*. "
    "Tocca per selezionare/deselezionare, poi premi *Salva*.\n\n"
    "🦅 = pool solo mining (singoli miner indipendenti).\n"
    "✍️ *Aggiungi la tua pool* per inserirne una a mano (anche non in lista).\n\n"
    "_Il canale @hlbitcoinstats continua comunque a pubblicare ogni blocco._"
)

# Nomi delle pool note correnti: serve solo a distinguere le voci custom (✍️)
# da quelle note nel rendering dei bottoni. Aggiornato in _build_pool_list.
KNOWN_POOL_HINT = {"names": set()}

def _pool_label(name: str) -> str:
    """Etichetta del bottone: 🦅 per le solo, ✍️ per le pool aggiunte a mano."""
    if name in SOLO_NAMES:
        return f"🦅 {name}"
    if name in KNOWN_POOL_HINT["names"]:
        return name
    return f"✍️ {name}"

def render_miner_menu(pools: list, selected: set) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, name in enumerate(pools):
        mark = "✅" if name in selected else "⬜"
        row.append(InlineKeyboardButton(f"{mark} {_pool_label(name)}", callback_data=f"mf:t:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ Aggiungi la tua pool", callback_data="mf:add")])
    rows.append([
        InlineKeyboardButton("💾 Salva",            callback_data="mf:save"),
        InlineKeyboardButton("🗑 Disattiva filtro", callback_data="mf:clear"),
    ])
    return InlineKeyboardMarkup(rows)

def _build_pool_list(known: list, selected: set) -> list:
    """Pool note (top N) + tutte le solo (singolarmente) + eventuali custom salvate."""
    base = known[:MINER_MENU_MAX_POOLS]
    KNOWN_POOL_HINT["names"] = set(base)
    for n in SOLO_NAMES:
        if n not in base:
            base.append(n)
    for p in selected:
        if p not in base:
            base.append(p)
    return base

async def _send_miner_menu(chat, c, pools, selected, prefix: str = ""):
    c.user_data["mf_pools"]    = pools
    c.user_data["mf_selected"] = selected
    await chat.reply_text(
        (prefix + MINER_MENU_TEXT) if prefix else MINER_MENU_TEXT,
        parse_mode="Markdown", disable_web_page_preview=True,
        reply_markup=render_miner_menu(pools, selected),
    )

async def filtraminer_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    known = await get_known_pools()
    if not known and not SOLO_NAMES:
        await u.message.reply_text(
            "⚠️ Non riesco a recuperare la lista delle pool ora. Riprova tra poco."
        )
        return
    saved    = load_miner_filters().get(str(u.effective_chat.id), {})
    selected = set(saved.get("pools", []))
    # Migrazione dai vecchi iscritti col toggle unico "include_solo".
    if saved.get("include_solo"):
        selected |= set(SOLO_NAMES)
    pools = _build_pool_list(known, selected)
    await _send_miner_menu(u.message, c, pools, selected)

async def miner_filter_cb(u: Update, c: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    pools = c.user_data.get("mf_pools")
    if pools is None:
        await q.edit_message_text("⏳ Menu scaduto. Riaprilo con /filtraminer.")
        return
    selected = c.user_data.setdefault("mf_selected", set())
    data     = q.data
    if data.startswith("mf:t:"):
        idx = int(data.split(":")[2])
        if 0 <= idx < len(pools):
            name = pools[idx]
            selected.discard(name) if name in selected else selected.add(name)
        await q.edit_message_reply_markup(render_miner_menu(pools, selected))
    elif data == "mf:add":
        c.user_data["mf_awaiting_custom"] = True
        await q.message.reply_text(
            "✍️ Scrivimi il *nome della pool* da aggiungere (anche parziale).\n"
            "Per più pool, mandami un messaggio per ciascuna.\n\n"
            "Annulla con /annulla.",
            parse_mode="Markdown"
        )
    elif data == "mf:clear":
        filtri = load_miner_filters()
        filtri.pop(str(u.effective_chat.id), None)
        save_miner_filters(filtri)
        c.user_data["mf_selected"] = set()
        await q.edit_message_text(
            "🗑 Filtro miner disattivato. Non riceverai più notifiche blocco in privato."
        )
    elif data == "mf:save":
        filtri  = load_miner_filters()
        chat_id = str(u.effective_chat.id)
        if not selected:
            filtri.pop(chat_id, None)
            save_miner_filters(filtri)
            await q.edit_message_text("🗑 Nessuna selezione: filtro miner disattivato.")
            return
        filtri[chat_id] = {"pools": sorted(selected)}
        save_miner_filters(filtri)
        righe = "\n".join(f"• {esc_md(_pool_label(p))}" for p in sorted(selected))
        await q.edit_message_text(
            f"✅ *Filtro miner salvato!*\n\n"
            f"Riceverai il blocco in privato per:\n{righe}\n\n"
            f"Modifica con /filtraminer · disattiva con /filtrastop",
            parse_mode="Markdown"
        )

async def miner_custom_text(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Cattura il nome pool scritto a mano dopo '✍️ Aggiungi la tua pool'.
    Gira in un gruppo separato: se non stiamo aspettando, lascia passare oltre."""
    if not c.user_data.get("mf_awaiting_custom"):
        return
    c.user_data["mf_awaiting_custom"] = False
    name = (u.message.text or "").strip()
    if not name:
        return
    if len(name) > 40:
        await u.message.reply_text("❌ Nome troppo lungo (max 40 caratteri). Riprova da /filtraminer.")
        raise ApplicationHandlerStop
    if len(c.user_data.get("mf_pools", [])) >= 100:
        await u.message.reply_text("❌ Troppe pool in lista. Riprova da /filtraminer.")
        raise ApplicationHandlerStop
    pools    = c.user_data.get("mf_pools", [])
    selected = c.user_data.setdefault("mf_selected", set())
    if name not in pools:
        pools.append(name)
    selected.add(name)
    await _send_miner_menu(
        u.message, c, pools, selected,
        prefix=f"➕ Aggiunta e selezionata *{esc_md(name)}*.\n\n"
    )
    raise ApplicationHandlerStop

async def annulla_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if c.user_data.pop("mf_awaiting_custom", None):
        await u.message.reply_text("Ok, annullato. Riapri il menu con /filtraminer.")

async def filtrastop_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    filtri = load_miner_filters()
    if filtri.pop(str(u.effective_chat.id), None) is not None:
        save_miner_filters(filtri)
        await u.message.reply_text("🗑 Filtro miner disattivato.")
    else:
        await u.message.reply_text(
            "Non hai un filtro miner attivo. Impostane uno con /filtraminer."
        )
# ── Handler comandi ───────────────────────────────────────────────────────────
async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "🚀 *Bitcoin Stats 4.0 — Halving Labs*\n\n"
        "*📊 Mercato*\n"
        "/prezzo — Prezzo BTC\n"
        "/feargreed — Fear \\& Greed Index\n\n"
        "*⛓️ Blockchain*\n"
        "/timechain — Stato blockchain\n"
        "/difficolta — Difficoltà mining\n"
        "/halving — Countdown halving\n"
        "/lightning — Statistiche Lightning Network\n\n"
        "*🛠 Strumenti*\n"
        "/convertitore — Converti EUR ↔ SATS\n"
        "/tx — Controlla transazione\n"
        "/blockfinder — Cerca un blocco\n"
        "/whale — OG Whale e Mega Whale\n\n"
        "*🔔 Alert Personali*\n"
        "/alert — Imposta un alert di prezzo\n"
        "/alertinfo — Vedi i tuoi alert attivi\n"
        "/alertstop — Cancella i tuoi alert\n"
        "/filtraminer — Notifiche blocco per pool scelte\n\n"
        "*📚 Info*\n"
        "/news — Ultime notizie\n"
        "/glossario — Termini Bitcoin \\(50 voci\\)\n"
        "/contattaci — Contatta Halving Labs\n"
        "/dona — Supporta il progetto",
        parse_mode="MarkdownV2"
    )
async def prezzo_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = await get_bitcoin_data()
    if d:
        await u.message.reply_text(
            f"💰 *Prezzo Bitcoin*\n\n"
            f"💶 `€ {format_it(d['price_eur'], 2)}`\n"
            f"💵 `$ {format_it(d['price_usd'], 2)}`",
            parse_mode="Markdown"
        )
async def feargreed_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    fg = await get_fear_greed()
    if not fg:
        await u.message.reply_text("❌ Impossibile recuperare il Fear & Greed Index. Riprova.")
        return
    filled = round(fg["value"] / 10)
    bar    = "█" * filled + "░" * (10 - filled)
    await u.message.reply_text(
        f"{fg['emoji']} *Fear & Greed Index*\n\n"
        f"`{bar}`\n\n"
        f"📊 Valore: `{fg['value']} / 100`\n"
        f"🏷 Sentiment: *{fg['label_it']}*\n\n"
        f"_L'indice misura le emozioni dominanti del mercato crypto. "
        f"Paura estrema può indicare opportunità di acquisto; "
        f"avidità estrema può segnalare un mercato surriscaldato._",
        parse_mode="Markdown"
    )
async def lightning_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    ln = await get_lightning_stats()
    if not ln:
        await u.message.reply_text("❌ Impossibile recuperare i dati Lightning. Riprova.")
        return
    await u.message.reply_text(
        f"⚡ *Lightning Network*\n\n"
        f"🌐 Nodi attivi: `{format_it(ln['node_count'])}`\n"
        f"🔗 Canali aperti: `{format_it(ln['channel_count'])}`\n"
        f"💧 Capacità totale: `{format_it(ln['capacity_btc'], 2)} BTC`\n"
        f"📏 Capacità media canale: `{format_it(ln['avg_cap_btc'], 4)} BTC`\n\n"
        f"🔒 Nodi Tor: `{format_it(ln['tor_nodes'])}`\n"
        f"🌍 Nodi Clearnet: `{format_it(ln['clearnet_nodes'])}`",
        parse_mode="Markdown"
    )
async def whale_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔍 Analisi ultimo blocco in corso...")
    whales = await get_whale_txs()
    og, mega = whales["og"], whales["mega"]
    if not og and not mega:
        await u.message.reply_text(
            f"🐋 *Whale Alert*\n\n"
            f"Nessuna OG Whale (monete create entro il {OG_CUTOFF_YEAR}) né "
            f"Mega Whale (≥ {format_it(MEGA_WHALE_BTC)} BTC) nell'ultimo blocco.",
            parse_mode="Markdown"
        )
        return
    sezioni = []
    if og:
        righe = []
        for i, w in enumerate(og[:5], 1):
            imp = (f"{format_it(w['og_btc'], 2)} BTC P2PK"
                   if w["is_p2pk"] else f"{format_it(w['total_btc'], 2)} BTC")
            righe.append(
                f"{i}. 🕰️ *{esc_md(w['era'])}* — `{imp}`\n"
                f"   🔗 [Visualizza TX](https://mempool.space/tx/{w['txid']})"
            )
        sezioni.append("🐋 *OG Whale — Monete Risvegliate*\n\n" + "\n\n".join(righe))
    if mega:
        righe = []
        for i, w in enumerate(mega[:5], 1):
            righe.append(
                f"{i}. `{format_it(w['total_btc'], 2)} BTC`\n"
                f"   🔗 [Visualizza TX](https://mempool.space/tx/{w['txid']})"
            )
        sezioni.append(
            f"🐳 *Mega Whale — TX ≥ {format_it(MEGA_WHALE_BTC)} BTC*\n\n"
            + "\n\n".join(righe)
        )
    await u.message.reply_text(
        "\n\n———\n\n".join(sezioni),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )
async def news_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        await get_news_text(),
        parse_mode="HTML",
        disable_web_page_preview=True
    )
async def timechain_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = await get_bitcoin_data()
    if d:
        await u.message.reply_text(
            f"⛓️ *Timechain*\n\n"
            f"📦 Altezza Blocco: `{format_it(d['height'])}`\n"
            f"⛏️ Ultimo Miner: *{esc_md(d['miner'])}*\n"
            f"🔢 Transazioni: `{format_it(d['tx_count'])}`\n\n"
            f"⚡ *Fee Raccomandate*\n"
            f"🚀 Rapida: `{d['fast']} sat/vB`\n"
            f"⏱ Media: `{d['medium']} sat/vB`\n"
            f"🐢 Economica: `{d['slow']} sat/vB`",
            parse_mode="Markdown"
        )
async def halving_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = await get_bitcoin_data()
    if d:
        rem           = 210_000 - (d["height"] % 210_000)
        giorni_stimati = round((rem * 10) / 60 / 24)
        data_stimata  = datetime.now(pytz.timezone("Europe/Rome")) + timedelta(days=giorni_stimati)
        mesi_it       = ["Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
                         "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
        data_str      = f"{mesi_it[data_stimata.month - 1]} {data_stimata.year}"
        epoch_attuale = d["height"] // 210_000
        reward_ora    = 50 / (2 ** epoch_attuale)
        reward_dopo   = 50 / (2 ** (epoch_attuale + 1))
        await u.message.reply_text(
            f"⏳ *Prossimo Halving*\n\n"
            f"🔢 Blocchi rimanenti: `{format_it(rem)}`\n"
            f"📅 Giorni stimati: `~{format_it(giorni_stimati)}`\n"
            f"📆 Data stimata: `{data_str}`\n\n"
            f"💰 Reward attuale: `{reward_ora} BTC`\n"
            f"✂️ Reward dopo halving: `{reward_dopo} BTC`",
            parse_mode="Markdown"
        )
async def diff_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = await get_bitcoin_data()
    if d:
        diff_tera     = d["difficulty"] / 1_000_000_000_000
        giorni_stimati = round((d["diff_remaining"] * 10) / 60 / 24, 1)
        segno         = "+" if d["diff_change"] >= 0 else ""
        await u.message.reply_text(
            f"⚖️ *Difficoltà di Mining*\n\n"
            f"📊 Difficoltà attuale: `{format_it(diff_tera, 1)} T`\n"
            f"📈 Variazione stimata: `{segno}{d['diff_change']:.2f}%`\n"
            f"🔄 Reset tra: `{format_it(d['diff_remaining'])}` blocchi (~{giorni_stimati} giorni)",
            parse_mode="Markdown"
        )
async def pubblica_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    """Comando admin: pubblica un messaggio sul canale. Solo per ADMIN_ID."""
    if u.effective_user.id != ADMIN_ID:
        return  # ignora silenziosamente chi non è admin
    testo = u.message.text.partition(" ")[2].strip()
    if not testo:
        await u.message.reply_text(
            "📢 Uso: `/pubblica Il tuo messaggio qui`\n\n"
            "Il testo verrà pubblicato su @hlbitcoinstats.",
            parse_mode="Markdown"
        )
        return
    try:
        await c.bot.send_message(
            chat_id=CHANNEL_ID,
            text=testo,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        await u.message.reply_text("✅ Messaggio pubblicato sul canale!")
        logger.info(f"Admin ha pubblicato sul canale: {testo[:80]}")
    except Exception as e:
        await u.message.reply_text(f"❌ Errore durante la pubblicazione: {e}")
        logger.error(f"Errore pubblica_cmd: {e}")

async def contattaci_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "📬 *Contattaci*\n\n"
        "Hai domande, suggerimenti o vuoi collaborare con Halving Labs?\n\n"
        "✉️ Scrivici a:\n"
        "`info@halvinglabs.it`\n\n"
        "Risponderemo il prima possibile! 🧡",
        parse_mode="Markdown"
    )
async def dona_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "☕ *Supporta il Progetto*\n\n"
        "Se trovi utile questo bot puoi supportarne\n"
        "lo sviluppo con una donazione in Bitcoin!\n\n"
        "⛓️ *On-Chain:*\n"
        "`bc1q3r5mxja4232wvuv56z8avul4h5d3feup8hpe3aj4nkdlqzffud8sqhe7a0`\n\n"
        "⚡ *Lightning Network:*\n"
        "`HalvingLabs@blitzwalletapp.com`\n\n"
        "Grazie per il supporto! 🧡",
        parse_mode="Markdown"
    )
# ── Conversazione: Alert Prezzo ──────────────────────────────────────────────
async def alert_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    d = await get_bitcoin_data()
    prezzo_attuale = f"💶 Prezzo attuale: `€ {format_it(d['price_eur'], 2)}`\n\n" if d else ""
    kb = [["📈 Sopra (prezzo sale)", "📉 Sotto (prezzo scende)"]]
    await u.message.reply_text(
        f"🔔 *Imposta Alert Prezzo*\n\n"
        f"{prezzo_attuale}"
        f"Vuoi essere avvisato quando BTC sale *sopra* una soglia o scende *sotto*?",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return ALERT_DIR_STATE
async def alert_dir(u: Update, c: ContextTypes.DEFAULT_TYPE):
    testo = u.message.text
    if "Sopra" in testo:
        c.user_data["alert_dir"] = "above"
    else:
        c.user_data["alert_dir"] = "below"
    await u.message.reply_text(
        f"A quale prezzo in € vuoi l'alert?\n_(Es: 95000)_",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return ALERT_PRICE_STATE
async def alert_price(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        soglia = float(u.message.text.replace(".", "").replace(",", "."))
        if not math.isfinite(soglia) or soglia <= 0 or soglia > 100_000_000:
            raise ValueError
    except ValueError:
        await u.message.reply_text("❌ Inserisci un numero valido, es: `95000`", parse_mode="Markdown")
        return ALERT_PRICE_STATE
    direction = c.user_data.get("alert_dir", "above")
    chat_id   = str(u.effective_chat.id)
    alerts = load_alerts()
    if chat_id not in alerts:
        alerts[chat_id] = {"above": None, "below": None}
    alerts[chat_id][direction] = soglia
    save_alerts(alerts)
    simbolo = "📈 sopra" if direction == "above" else "📉 sotto"
    await u.message.reply_text(
        f"✅ *Alert impostato\\!*\n\n"
        f"Ti avviserò quando BTC andrà {simbolo} `€ {format_it(soglia, 0)}`\\.\n"
        f"Usa /alertstop per cancellarlo in qualsiasi momento\\.",
        parse_mode="MarkdownV2"
    )
    return ConversationHandler.END
async def alertinfo_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat_id = str(u.effective_chat.id)
    alerts  = load_alerts()
    soglie  = alerts.get(chat_id, {})
    above   = soglie.get("above")
    below   = soglie.get("below")
    if not above and not below:
        await u.message.reply_text(
            "🔕 Non hai alert attivi.\nUsa /alert per impostarne uno."
        )
        return
    righe = ["🔔 *I tuoi alert attivi:*\n"]
    if above:
        righe.append(f"📈 Sopra: `€ {format_it(above, 0)}`")
    if below:
        righe.append(f"📉 Sotto: `€ {format_it(below, 0)}`")
    await u.message.reply_text("\n".join(righe), parse_mode="Markdown")
async def alertstop_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat_id = str(u.effective_chat.id)
    alerts  = load_alerts()
    if chat_id in alerts:
        del alerts[chat_id]
        save_alerts(alerts)
        await u.message.reply_text("🗑 Alert cancellati. Usa /alert per impostarne di nuovi.")
    else:
        await u.message.reply_text("ℹ️ Non hai alert attivi da cancellare.")
# ── Conversazione: Convertitore ───────────────────────────────────────────────
async def conv_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    kb = [["EUR ➔ SATS", "SATS ➔ EUR"]]
    await u.message.reply_text(
        "Scegli la conversione:",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
    )
    return SCELTA_CONV
async def conv_tipo(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data["tipo"] = u.message.text
    await u.message.reply_text("Inserisci l'importo:", reply_markup=ReplyKeyboardRemove())
    return IMPORTO_CONV
async def conv_exec(u: Update, c: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(u.message.text.replace(",", "."))
        if not math.isfinite(val) or val < 0:
            raise ValueError
        d   = await get_bitcoin_data()
        if c.user_data["tipo"] == "EUR ➔ SATS":
            res = (val / d["price_eur"]) * 100_000_000
            await u.message.reply_text(
                f"💶 {val}€ = *{format_it(res)} SATS*", parse_mode="Markdown"
            )
        else:
            res = (val / 100_000_000) * d["price_eur"]
            await u.message.reply_text(
                f"₿ {format_it(val)} SATS = *{format_it(res, 2)}€*", parse_mode="Markdown"
            )
    except Exception:
        await u.message.reply_text("❌ Inserisci un numero valido.")
    return ConversationHandler.END
# ── Conversazione: TX Status ──────────────────────────────────────────────────
async def tx_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("📑 Invia il TXID da verificare:")
    return TX_STATUS_STATE
TXID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
async def tx_exec(u: Update, c: ContextTypes.DEFAULT_TYPE):
    txid = (u.message.text or "").strip()
    if not TXID_RE.match(txid):
        await u.message.reply_text("❌ TXID non valido (servono 64 caratteri esadecimali).")
        return ConversationHandler.END
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r      = await client.get(f"https://mempool.space/api/tx/{txid}")
            tx     = r.json()
            status = "✅ Confermata" if tx["status"]["confirmed"] else "⏳ In Mempool"
            blocco = (
                f" nel blocco `{format_it(tx['status']['block_height'])}`"
                if tx["status"]["confirmed"] else ""
            )
            fee_sat = tx.get("fee", 0)
            await u.message.reply_text(
                f"{status}{blocco}\n💸 Fee: `{format_it(fee_sat)} sat`",
                parse_mode="Markdown"
            )
        except Exception:
            await u.message.reply_text("❌ TXID non trovato o non valido.")
    return ConversationHandler.END
# ── Conversazione: Block Finder ───────────────────────────────────────────────
async def blockfinder_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔍 Inserisci l'altezza del blocco da cercare:")
    return BLOCK_FINDER_STATE
async def blockfinder_exec(u: Update, c: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            height  = int(u.message.text.strip())
            r       = await client.get(f"https://mempool.space/api/v1/blocks/{height}")
            blocks  = r.json()
            b       = next((x for x in blocks if x.get("height") == height), None)
            if not b:
                await u.message.reply_text("❌ Blocco non trovato.")
                return ConversationHandler.END
            miner          = b.get("extras", {}).get("pool", {}).get("name", "Sconosciuto")
            tx_count       = b.get("tx_count") or b.get("size", 0)
            total_fees_btc = b.get("extras", {}).get("totalFees", 0) / 100_000_000
            reward_btc     = (50 / (2 ** (height // 210_000))) + total_fees_btc
            ts             = datetime.utcfromtimestamp(b["timestamp"]).strftime("%d/%m/%Y %H:%M UTC")
            await u.message.reply_text(
                f"🧱 *Blocco {format_it(height)}*\n\n"
                f"📅 Data: `{ts}`\n"
                f"⛏️ Miner: *{esc_md(miner)}*\n"
                f"🔢 Transazioni: `{format_it(tx_count)}`\n"
                f"💰 Reward: `{format_it(reward_btc, 3)} BTC`\n"
                f"🔗 [Visualizza su mempool.space](https://mempool.space/block/{b['id']})",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        except ValueError:
            await u.message.reply_text("❌ Inserisci un numero intero valido.")
        except Exception as e:
            logger.error(f"Blockfinder error: {e}")
            await u.message.reply_text("❌ Blocco non trovato.")
    return ConversationHandler.END
# ── Glossario (50 termini) ────────────────────────────────────────────────────
GL_DB = {
    "Satoshi":        "🔸 *Satoshi (sat)*: unità minima di Bitcoin. 1 BTC = 100.000.000 satoshi. Prende il nome dal creatore pseudonimo di Bitcoin, Satoshi Nakamoto.",
    "Halving":        "✂️ *Halving*: ogni ~4 anni (ogni 210.000 blocchi) la ricompensa per i miner viene dimezzata. Riduce l'emissione di nuovi BTC nel tempo, rendendolo progressivamente più scarso.",
    "Timechain":      "⛓️ *Timechain*: il nome originale della blockchain Bitcoin. È un registro pubblico e immutabile di tutte le transazioni mai avvenute.",
    "Mempool":        "🗂 *Mempool*: sala d'attesa delle transazioni non ancora confermate. I miner scelgono da qui quali includere nel prossimo blocco, di solito privilegiando le fee più alte.",
    "Fee":            "💸 *Fee*: commissione pagata per far includere una transazione in un blocco. Si misura in sat/vByte. Più è alta, più veloce viene confermata.",
    "Nodo":           "🖥 *Nodo*: un computer che scarica e verifica tutti i blocchi della blockchain. Chi gestisce un nodo non si fida di nessuno: verifica tutto da solo.",
    "Mining":         "⛏️ *Mining*: processo con cui i miner competono per aggiungere un nuovo blocco alla timechain. Consumano energia per risolvere un calcolo difficile e ricevono BTC come ricompensa.",
    "Wallet":         "👛 *Wallet*: portafoglio digitale che conserva le chiavi private per accedere ai tuoi BTC. Non contiene bitcoin: contiene le chiavi per spenderli.",
    "Chiave Privata": "🔑 *Chiave Privata*: codice segreto che ti permette di firmare le transazioni e spendere i tuoi BTC. Chi la conosce controlla i tuoi fondi. Non condividerla mai.",
    "UTXO":           "🔄 *UTXO (Unspent Transaction Output)*: output di una transazione non ancora speso. Il saldo del tuo wallet è la somma di tutti i tuoi UTXO. Bitcoin non ha conti come una banca: usa questo sistema a monete discrete.",
    "Hash":           "🔐 *Hash*: funzione matematica che trasforma qualsiasi dato in una stringa di lunghezza fissa. È la base della sicurezza di Bitcoin: anche cambiare un solo carattere produce un hash completamente diverso.",
    "Proof of Work":  "💪 *Proof of Work*: meccanismo di consenso che richiede lavoro computazionale reale (energia) per proporre un blocco. Rende estremamente costoso per un attaccante riscrivere la storia della blockchain.",
    "Nonce":          "🎲 *Nonce*: numero casuale che i miner modificano continuamente per cercare un hash valido. Trovare il nonce giusto è il 'lavoro' del mining.",
    "Firma Digitale": "✍️ *Firma Digitale*: prova crittografica che dimostra che sei il proprietario dei fondi e che autorizzi la transazione, senza rivelare la tua chiave privata.",
    "Chiave Pubblica":"🔓 *Chiave Pubblica*: derivata matematicamente dalla chiave privata, viene usata per ricevere BTC. Può essere condivisa liberamente senza rischi.",
    "Indirizzo":      "📫 *Indirizzo Bitcoin*: stringa alfanumerica derivata dalla chiave pubblica che rappresenta la destinazione di un pagamento. Simile all'IBAN di un conto bancario.",
    "Blocco Genesis":  "🌱 *Blocco Genesis*: il primo blocco mai minato della blockchain Bitcoin (blocco 0), il 3 gennaio 2009. Satoshi vi ha incluso un titolo di giornale come prova della data: 'Chancellor on brink of second bailout for banks'.",
    "Conferme":        "✅ *Conferme*: numero di blocchi minati dopo quello che contiene la tua transazione. Più conferme = più sicurezza. 6 conferme sono considerate irreversibili.",
    "Orphan Block":    "👻 *Orphan Block*: blocco validamente minato ma non incluso nella catena principale perché un altro miner ha trovato un blocco alla stessa altezza quasi contemporaneamente. Non riceve ricompensa.",
    "Block Reward":    "🏆 *Block Reward*: ricompensa totale che riceve il miner che trova un blocco. È composta dal sussidio (attualmente 3,125 BTC) più tutte le fee delle transazioni incluse.",
    "21 Milioni":      "🔒 *21 Milioni*: il numero massimo di Bitcoin che esisteranno mai, scritto nel codice sin dall'inizio. Questa scarsità programmata è uno dei pilastri del valore di Bitcoin.",
    "SegWit":          "📦 *SegWit (Segregated Witness)*: aggiornamento del 2017 che ha separato i dati della firma dal corpo della transazione. Ha aumentato la capacità dei blocchi e ridotto le fee medie.",
    "Taproot":         "🌳 *Taproot*: aggiornamento del 2021 che migliora la privacy, l'efficienza e la flessibilità delle transazioni. Rende i contratti complessi (multisig, Lightning) indistinguibili da transazioni normali.",
    "Multisig":        "🔐 *Multisig (Multi-firma)*: wallet che richiede più firme per autorizzare una transazione (es. 2 su 3). Aumenta la sicurezza distribuendo il controllo dei fondi.",
    "CoinJoin":        "🔀 *CoinJoin*: tecnica che unisce più transazioni di utenti diversi in una sola, rendendo difficile tracciare chi ha pagato chi. Migliora la privacy on-chain.",
    "RBF":             "⚡ *RBF (Replace-By-Fee)*: permette di sostituire una transazione in attesa con una nuova versione che paga una fee più alta, accelerandone la conferma.",
    "Timelock":        "⏰ *Timelock*: meccanismo che blocca i fondi fino a un certo momento (basato sul tempo o sull'altezza del blocco). Usato nei canali Lightning e nei contratti avanzati.",
    "Seed Phrase":     "📝 *Seed Phrase*: sequenza di 12 o 24 parole che rappresenta la chiave master del tuo wallet. Con questa frase puoi recuperare tutti i tuoi BTC su qualsiasi dispositivo. Conservala offline e al sicuro.",
    "Cold Wallet":     "🧊 *Cold Wallet*: wallet offline, mai connesso a internet. È la forma più sicura per conservare BTC a lungo termine, al riparo da attacchi informatici.",
    "Hot Wallet":      "🔥 *Hot Wallet*: wallet connesso a internet. Più comodo per i pagamenti quotidiani, ma più esposto ai rischi di sicurezza rispetto a un cold wallet.",
    "Hardware Wallet": "💾 *Hardware Wallet*: dispositivo fisico (es. Ledger, Trezor) che conserva le chiavi private offline. Combina la sicurezza del cold storage con la praticità di firmare transazioni.",
    "Self-Custody":    "🛡 *Self-Custody*: il principio di custodire i propri BTC senza affidarsi a terzi. 'Not your keys, not your coins': se non controlli la chiave privata, non controlli veramente i tuoi bitcoin.",
    "Hashrate":        "💻 *Hashrate*: potenza computazionale totale dedicata al mining di Bitcoin. Un hashrate alto significa una rete più sicura e più difficile da attaccare.",
    "ASIC":            "🤖 *ASIC*: chip specializzato progettato esclusivamente per il mining di Bitcoin. Enormemente più efficiente di una GPU o CPU per questo scopo specifico.",
    "Pool Mining":     "🤝 *Pool di Mining*: gruppo di miner che uniscono la potenza di calcolo e si dividono le ricompense proporzionalmente. Riduce la varianza rispetto al mining in solitaria.",
    "Full Node":       "🖧 *Full Node*: nodo che scarica e verifica l'intera blockchain, applicando autonomamente tutte le regole del protocollo. È la forma più sovrana di partecipare alla rete Bitcoin.",
    "Lightning Network":"⚡ *Lightning Network*: rete di pagamento Layer 2 costruita su Bitcoin. Permette transazioni istantanee e con fee bassissime aprendo canali di pagamento tra due parti.",
    "Canale Lightning": "🔗 *Canale Lightning*: connessione diretta tra due nodi Lightning che permette di scambiare pagamenti istantanei senza registrarli on-chain ad ogni transazione.",
    "Nodo Lightning":   "📡 *Nodo Lightning*: software che partecipa alla rete Lightning, apre canali con altri nodi e può instradare pagamenti di terzi (guadagnando piccole fee di routing).",
    "Invoice":          "🧾 *Invoice Lightning*: richiesta di pagamento sulla rete Lightning con importo fisso e scadenza. Chi deve pagare la 'scansiona' o la incolla nel proprio wallet.",
    "LNURL":            "🔗 *LNURL*: protocollo che semplifica i pagamenti Lightning. Permette indirizzi leggibili (es. nome@dominio.com) al posto delle invoice lunghe e complesse.",
    "Layer 2":          "🏗 *Layer 2*: soluzioni costruite sopra Bitcoin per aumentare velocità e scalabilità. La Lightning Network è il Layer 2 più usato di Bitcoin.",
    "DCA":             "📅 *DCA (Dollar Cost Averaging)*: strategia che prevede acquisti periodici di BTC a importo fisso, indipendentemente dal prezzo. Riduce l'impatto della volatilità nel tempo.",
    "HODL":            "💎 *HODL*: termine nato da un errore di battitura ('hold'), ora acronimo di 'Hold On for Dear Life'. Indica la strategia di tenere BTC a lungo termine senza vendere durante i cali.",
    "ATH":             "📈 *ATH (All Time High)*: il prezzo massimo storico mai raggiunto da un asset. Quando Bitcoin supera il suo ATH precedente, si parla di 'price discovery'.",
    "Market Cap":      "💹 *Market Cap*: capitalizzazione di mercato, calcolata come prezzo × offerta circolante. Misura il valore totale di tutti i Bitcoin in circolazione.",
    "Whitepaper":      "📄 *Whitepaper*: il documento tecnico originale di Bitcoin, pubblicato da Satoshi Nakamoto il 31 ottobre 2008. Si intitola 'Bitcoin: A Peer-to-Peer Electronic Cash System'.",
    "CEX":             "🏦 *CEX (Exchange Centralizzato)*: piattaforma gestita da un'azienda (es. Binance, Coinbase) per comprare e vendere crypto. Richiede KYC e custodisce i fondi al posto tuo.",
    "DEX":             "🔄 *DEX (Exchange Decentralizzato)*: piattaforma di scambio senza intermediari, dove le transazioni avvengono direttamente tra i wallet degli utenti.",
    "KYC":             "🪪 *KYC (Know Your Customer)*: procedura di identificazione richiesta dagli exchange centralizzati. Richiede documenti d'identità e riduce la privacy delle transazioni.",
}
GL_KEYS     = list(GL_DB.keys())
GL_KEYBOARD = [GL_KEYS[i:i+3] for i in range(0, len(GL_KEYS), 3)]
GL_PATTERN  = "^(" + "|".join(GL_KEYS) + ")$"
async def gl_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"📖 *Glossario Bitcoin* — {len(GL_DB)} termini\nScegli un termine:",
        reply_markup=ReplyKeyboardMarkup(GL_KEYBOARD, resize_keyboard=True, one_time_keyboard=True),
        parse_mode="Markdown"
    )
async def gl_msg(u: Update, c: ContextTypes.DEFAULT_TYPE):
    term = u.message.text
    if term in GL_DB:
        await u.message.reply_text(
            GL_DB[term],
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Attendi che la rete sia pronta (importante dopo riavvio Umbrel/sistema)
    import socket
    logger.info("Attendo connessione di rete...")
    for attempt in range(30):
        try:
            socket.setdefaulttimeout(3)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("api.telegram.org", 443))
            logger.info(f"Rete pronta dopo {attempt * 5} secondi ✅")
            break
        except Exception:
            logger.info(f"Rete non ancora pronta, attendo... ({attempt+1}/30)")
            time.sleep(5)

    app = Application.builder().token(TOKEN).build()
    jq  = app.job_queue
    tz  = pytz.timezone("Europe/Rome")
    jq.run_repeating(block_monitor, interval=CHECK_INTERVAL_BLOCKS, first=5)
    for h in [9, 13, 21]:
        jq.run_daily(send_news_alert, time=datetime_time(h, 0, tzinfo=tz))
    jq.run_daily(send_prezzo_canale, time=datetime_time(17, 0, tzinfo=tz))
    app.add_handler(CommandHandler("start",       start_cmd))
    app.add_handler(CommandHandler("prezzo",      prezzo_cmd))
    app.add_handler(CommandHandler("news",        news_cmd))
    app.add_handler(CommandHandler("timechain",   timechain_cmd))
    app.add_handler(CommandHandler("difficolta",  diff_cmd))
    app.add_handler(CommandHandler("halving",     halving_cmd))
    app.add_handler(CommandHandler("feargreed",   feargreed_cmd))
    app.add_handler(CommandHandler("lightning",   lightning_cmd))
    app.add_handler(CommandHandler("whale",       whale_cmd))
    app.add_handler(CommandHandler("pubblica",    pubblica_cmd))
    app.add_handler(CommandHandler("contattaci",  contattaci_cmd))
    app.add_handler(CommandHandler("dona",        dona_cmd))
    app.add_handler(CommandHandler("glossario",   gl_start))
    app.add_handler(CommandHandler("alertinfo",   alertinfo_cmd))
    app.add_handler(CommandHandler("alertstop",   alertstop_cmd))
    app.add_handler(CommandHandler("filtraminer", filtraminer_cmd))
    app.add_handler(CommandHandler("filtrastop",  filtrastop_cmd))
    app.add_handler(CommandHandler("annulla",     annulla_cmd))
    app.add_handler(CallbackQueryHandler(miner_filter_cb, pattern=r"^mf:"))
    # Cattura il nome pool scritto a mano dopo "✍️ Aggiungi la tua pool".
    # Gruppo -1: gira prima, ma cede il passo se non stiamo aspettando input.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, miner_custom_text),
        group=-1,
    )
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(GL_PATTERN),
        gl_msg
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("convertitore", conv_start)],
        states={
            SCELTA_CONV:  [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_tipo)],
            IMPORTO_CONV: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_exec)],
        },
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("tx", tx_start)],
        states={
            TX_STATUS_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, tx_exec)],
        },
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("alert", alert_start)],
        states={
            ALERT_DIR_STATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_dir)],
            ALERT_PRICE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_price)],
        },
        fallbacks=[CommandHandler("alertstop", alertstop_cmd)]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("blockfinder", blockfinder_start)],
        states={
            BLOCK_FINDER_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, blockfinder_exec)],
        },
        fallbacks=[]
    ))
    async def on_startup(app):
        d  = await get_bitcoin_data()
        fg = await get_fear_greed()
        if d:
            fear_line = ""
            if fg:
                fear_line = f"\n{fg['emoji']} Fear & Greed: `{fg['value']} — {fg['label_it']}`"
            await app.bot.send_message(
                CHANNEL_ID,
                f"🔄 *Bot Riavviato!* — Halving Labs v4.0\n\n"
                f"⛓️ Blocco attuale: `{format_it(d['height'])}`\n"
                f"⛏️ Miner: *{esc_md(d['miner'])}*\n"
                f"💰 Prezzo: `€ {format_it(d['price_eur'], 2)}` | `$ {format_it(d['price_usd'], 2)}`"
                f"{fear_line}",
                parse_mode="Markdown"
            )
    app.post_init = on_startup
    logger.info("Bot Halving Labs v4.0 avviato ✅")
    app.run_polling()
