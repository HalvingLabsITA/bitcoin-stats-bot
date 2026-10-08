"""
Bitcoin Stats Dashboard — Backend FastAPI v2.0
Allineato con il bot Telegram v4.0:
  - Fear & Greed Index (alternative.me)
  - Lightning Network stats (mempool.space)
  - 6 fonti news, ordinate per data, top 5
  - Tutti gli endpoint originali invariati
"""

import asyncio
import html
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import feedparser
import httpx
import pytz
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Bitcoin Stats Dashboard", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
cache:    dict = {"data": None,    "last_update": 0}
news_cache: dict = {"entries": None, "last_update": 0}
fg_cache:   dict = {"data": None,    "last_update": 0}
ln_cache:   dict = {"data": None,    "last_update": 0}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def format_it(value, decimali: int = 0) -> str:
    try:
        if decimali == 0:
            return f"{int(value):,}".replace(",", ".")
        return f"{value:,.{decimali}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
async def get_bitcoin_data() -> Optional[dict]:
    now = time.time()
    if cache["data"] and (now - cache["last_update"] < 60):
        return cache["data"]

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            h_res    = await client.get("https://mempool.space/api/blocks/tip/height")
            height   = int(h_res.text)
            b_res    = await client.get("https://mempool.space/api/v1/blocks")
            b        = b_res.json()[0]
            f_res    = await client.get("https://mempool.space/api/v1/fees/recommended")
            d_res    = await client.get("https://mempool.space/api/v1/difficulty-adjustment")
            p_e      = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCEUR")
            p_u      = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")

            diff = d_res.json()
            fees = f_res.json()
            data = {
                "height":        height,
                "tx_count":      b.get("tx_count") or b.get("size", 0),
                "miner":         b.get("extras", {}).get("pool", {}).get("name", "Sconosciuto"),
                "difficulty":    b.get("difficulty", 0),
                "fast":          fees.get("fastestFee", 0),
                "medium":        fees.get("halfHourFee", 0),
                "slow":          fees.get("hourFee", 0),
                "diff_change":   diff.get("difficultyChange", 0),
                "diff_remaining": diff.get("remainingBlocks", 0),
                "price_eur":     float(p_e.json()["price"]),
                "price_usd":     float(p_u.json()["price"]),
                "timestamp":     now,
            }
            cache["data"], cache["last_update"] = data, now
            return data
        except Exception as e:
            logger.error(f"Errore API Bitcoin: {e}")
            return cache.get("data")


async def get_fear_greed() -> Optional[dict]:
    """Fear & Greed Index — alternative.me. Cache 1h."""
    now = time.time()
    if fg_cache["data"] and (now - fg_cache["last_update"] < 3600):
        return fg_cache["data"]

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
            entry = r.json()["data"][0]
            fg_cache["data"] = {
                "value": int(entry["value"]),
                "label": entry["value_classification"],
            }
            fg_cache["last_update"] = now
            return fg_cache["data"]
        except Exception as e:
            logger.error(f"Errore Fear & Greed: {e}")
            return fg_cache.get("data")


async def get_lightning_stats() -> Optional[dict]:
    """Lightning Network — mempool.space. Cache 10 min."""
    now = time.time()
    if ln_cache["data"] and (now - ln_cache["last_update"] < 600):
        return ln_cache["data"]

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get("https://mempool.space/api/v1/lightning/statistics/latest")
            latest = r.json().get("latest", {})
            ln_cache["data"] = {
                "node_count":    latest.get("node_count", 0),
                "channel_count": latest.get("channel_count", 0),
                "capacity_btc":  round(latest.get("total_capacity", 0) / 1e8, 0),
            }
            ln_cache["last_update"] = now
            return ln_cache["data"]
        except Exception as e:
            logger.error(f"Errore Lightning: {e}")
            return ln_cache.get("data")


FONTI_NEWS = [
    ("https://it.cointelegraph.com/rss/tag/bitcoin", "🇮🇹", "CoinTelegraph IT"),
    ("https://en.cryptonomist.ch/feed/",             "🇮🇹", "Cryptonomist"),
    ("https://it.beincrypto.com/feed/",              "🇮🇹", "BeInCrypto IT"),
    ("https://bitcoinmagazine.com/.rss/full/",       "🇺🇸", "Bitcoin Magazine"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "🇺🇸", "CoinDesk"),
    ("https://decrypt.co/feed",                      "🇺🇸", "Decrypt"),
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
    """6 fonti, max 2 per fonte, solo Bitcoin, ordinate per data, top 5. Cache 10 min."""
    now = time.time()
    if news_cache["entries"] and (now - news_cache["last_update"] < 600):
        return news_cache["entries"]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HalvingLabsBot/4.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    all_entries = []
    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        for url, flag, source in FONTI_NEWS:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                feed = feedparser.parse(r.text)
                count = 0
                for e in feed.entries:
                    if count >= 2:
                        break
                    title   = html.unescape(e.get("title", "–"))
                    summary = html.unescape(e.get("summary", ""))
                    if not is_bitcoin_news(title, summary):
                        logger.info(f"News scartata [{source}]: {title[:60]}")
                        continue
                    pub = getattr(e, "published_parsed", None)
                    all_entries.append({
                        "flag":        flag,
                        "source":      source,
                        "title":       title,
                        "link":        e.get("link", "#"),
                        "published":   getattr(e, "published", ""),
                        "pub_ts":      time.mktime(pub) if pub else 0,
                    })
                    count += 1
            except Exception as ex:
                logger.error(f"News error {url}: {ex}")

    # Ordina per data decrescente, prendi le prime 5
    all_entries.sort(key=lambda x: x["pub_ts"], reverse=True)
    top5 = all_entries[:5]
    # Rimuovi il campo interno pub_ts prima di restituire
    for e in top5:
        e.pop("pub_ts", None)

    news_cache["entries"] = top5
    news_cache["last_update"] = now
    return top5


def compute_halving(height: int) -> dict:
    blocks_per_halving = 210_000
    epoch              = height // blocks_per_halving
    mined_in_epoch     = height % blocks_per_halving
    remaining          = blocks_per_halving - mined_in_epoch
    progress_pct       = round((mined_in_epoch / blocks_per_halving) * 100, 2)
    giorni             = round((remaining * 10) / 60 / 24)
    data_est           = datetime.now(pytz.timezone("Europe/Rome")) + timedelta(days=giorni)
    mesi_it = ["Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
                "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"]
    return {
        "remaining_blocks":  remaining,
        "giorni_stimati":    giorni,
        "data_stimata":      f"{mesi_it[data_est.month - 1]} {data_est.year}",
        "progress_pct":      progress_pct,
        "current_epoch":     epoch,
        "reward_attuale":    50 / (2 ** epoch),
        "reward_prossimo":   50 / (2 ** (epoch + 1)),
    }


# ---------------------------------------------------------------------------
# REST Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
async def serve_dashboard():
    return FileResponse("dashboard.html")


@app.get("/api/data")
async def api_data():
    d = await get_bitcoin_data()
    if not d:
        return {"error": "Dati non disponibili"}
    return {**d, "halving": compute_halving(d["height"])}


@app.get("/api/feargreed")
async def api_feargreed():
    data = await get_fear_greed()
    if not data:
        return {"error": "Fear & Greed non disponibile"}
    return data


@app.get("/api/lightning")
async def api_lightning():
    data = await get_lightning_stats()
    if not data:
        return {"error": "Lightning Network non disponibile"}
    return data


@app.get("/api/news")
async def api_news():
    entries = await get_news_entries()
    return {"entries": entries}


WHALE_THRESHOLD_BTC = 100

@app.get("/api/whale")
async def api_whale():
    """Cerca transazioni ≥ 100 BTC nell'ultimo blocco."""
    threshold_sats = WHALE_THRESHOLD_BTC * 100_000_000
    whales = []
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.get("https://mempool.space/api/blocks/tip/hash")
            block_hash = r.text.strip()
            r2 = await client.get(f"https://mempool.space/api/block/{block_hash}/txs/0")
            txs = r2.json()
            r3 = await client.get("https://mempool.space/api/blocks/tip/height")
            height = int(r3.text)
            for tx in txs:
                total_out = sum(v.get("value", 0) for v in tx.get("vout", []))
                if total_out >= threshold_sats:
                    whales.append({
                        "txid":      tx["txid"],
                        "total_btc": round(total_out / 100_000_000, 2),
                        "outputs":   len(tx.get("vout", [])),
                    })
            whales.sort(key=lambda x: x["total_btc"], reverse=True)
            return {
                "block_height": height,
                "threshold_btc": WHALE_THRESHOLD_BTC,
                "whales": whales[:10],
            }
    except Exception as e:
        logger.error(f"Errore whale: {e}")
        return {"error": str(e)}


@app.get("/api/tx/{txid}")
async def api_tx(txid: str):
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(f"https://mempool.space/api/tx/{txid.strip()}")
            if r.status_code != 200:
                return {"error": "TXID non trovato"}
            tx = r.json()
            return {
                "confirmed":    tx["status"]["confirmed"],
                "block_height": tx["status"].get("block_height"),
                "fee":          tx.get("fee"),
                "size":         tx.get("size"),
                "txid":         txid,
            }
        except Exception as e:
            return {"error": str(e)}


@app.get("/api/block/{height}")
async def api_block(height: int):
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(f"https://mempool.space/api/v1/blocks/{height}")
            blocks = r.json()
            b = next((x for x in blocks if x.get("height") == height), None)
            if not b:
                return {"error": "Blocco non trovato"}
            miner       = b.get("extras", {}).get("pool", {}).get("name", "Sconosciuto")
            total_fees  = b.get("extras", {}).get("totalFees", 0) / 1e8
            reward_btc  = (50 / (2 ** (height // 210_000))) + total_fees
            ts          = datetime.utcfromtimestamp(b["timestamp"]).strftime("%d/%m/%Y %H:%M UTC")
            return {
                "height":     height,
                "miner":      miner,
                "tx_count":   b.get("tx_count") or b.get("size", 0),
                "reward_btc": round(reward_btc, 8),
                "timestamp":  ts,
                "block_id":   b.get("id", ""),
                "size":       b.get("size", 0),
                "weight":     b.get("weight", 0),
            }
        except Exception as e:
            return {"error": str(e)}


@app.get("/api/convert")
async def api_convert(amount: float, direction: str = "eur_to_sats"):
    d = await get_bitcoin_data()
    if not d:
        return {"error": "Prezzo non disponibile"}
    if direction == "eur_to_sats":
        sats = (amount / d["price_eur"]) * 100_000_000
        return {"input": amount, "output": round(sats), "unit": "sats"}
    else:
        eur = (amount / 100_000_000) * d["price_eur"]
        return {"input": amount, "output": round(eur, 2), "unit": "eur"}


# ---------------------------------------------------------------------------
# WebSocket — push live ogni 30s
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        d = await get_bitcoin_data()
        if d:
            await ws.send_json({**d, "halving": compute_halving(d["height"])})
        while True:
            await asyncio.sleep(30)
            d = await get_bitcoin_data()
            if d:
                await manager.broadcast({**d, "halving": compute_halving(d["height"])})
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
