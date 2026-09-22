#!/usr/bin/env python3
"""
combined_xrp_intel_report.py

Crypto Intelligence Report Bot — 2025/2026 Edition

Fixes in this version:
- GITHUB_EVENT_NAME used to detect scheduled vs manual runs (DST-proof).
- News fetching actually implemented and posted to DISCORD_WEBHOOK_NEWS.
- tweet_on_force defaults True so manual dispatch also posts to X.
- Surge alerts deduplicated properly via state file.
- Deep-merge DEFAULT_CONFIG into config.json so missing keys never zero-out runtime behavior.
- Validate config["coins"] and fall back to defaults if empty/invalid.
- X posting uses Tweepy v1.1 media upload + Tweepy v2 create_tweet.
- Charts are optional: if mplfinance/Pillow are missing, tweets become text-only.

Fixes in the 2026-09 repair pass:
- RUN_MODE=surge makes a run surge-check only. Before, the ~12-min surge cron was also a
  GitHub "schedule" event, so it was treated as a scheduled run and posted full reports,
  tweets and news every 12 minutes.
- Surge cooldown state lives in its own file (surge_state.json) so the surge lane and the
  report lane can both commit without clobbering each other.
- Surge detection in surge mode uses real 60-minute price change from 1-minute bars.
- last_alert.json legacy keys are migrated/pruned (mixed-type keys crashed the news prune).
- History CSV loader strips git conflict markers and never wipes data on a parse error.
- RSI no longer reports 0 when there are no down bars (should be 100); NaN-safe BB/prob.
- 24H change is now a true 24-hour change (was ~16-20h).
- News coin matching uses word boundaries ("sol" no longer matches "solution").
- Discord webhooks retry on 429; CryptoCompare calls accept CRYPTOCOMPARE_API_KEY and retry.
- X posts fall back to text-only if media upload fails; length uses X's weighted counting.

2026-09 scoring / outlook pass:
- Position confidence is dominated by Daily + 4H structure, not RSI/BB.
- Ranging/choppy higher timeframes cap Position confidence.
- Scalper confidence weights 1H > 15m > 5m and cannot go extreme unless all 3 agree.
- RSI 70+ is labeled Overbought / Extended, not Sell.
- Reports include Position Outlook and Scalper Outlook condition labels.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import tempfile
import time
from io import StringIO
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
import pandas as pd
from discord_webhook import DiscordWebhook, DiscordEmbed

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np              # noqa: E402
import tweepy                   # noqa: E402

CONFIG_FILE  = "config.json"
STATE_FILE   = "last_alert.json"
SURGE_STATE_FILE = "surge_state.json"
HISTORY_DIR  = "history"

DEFAULT_CONFIG: Dict[str, Any] = {
    "coins": ["XRP", "BTC", "ETH", "ADA", "SOL", "HBAR", "ZEC"],
    "scheduled_hours_est": [8, 12, 16, 21, 0, 4],
    "surge_threshold_pct": 5.0,
    "surge_cooldown_hours": 2,
    "history_max_rows": 10000,
    "post_reports_to_x": True,
    "tweet_on_force": True,
    "tweet_report_symbols": ["XRP", "BTC"],
    "news_pages_max": 6,
    "news_allowlist_domains": [
        "coindesk.com",
        "cointelegraph.com",
        "decrypt.co",
        "ambcrypto.com",
        "u.today",
        "theblock.co",
        "beincrypto.com",
        "cryptoslate.com",
        "bitcoinmagazine.com",
        "cryptonews.com",
    ],
    "news_max_per_coin": 5,
    "news_global_cap": 45,
    "xrp_tweet_charts_enabled": True,
    "xrp_tweet_chart_bars": {"Daily": 140, "4H": 220, "1H": 260, "15m": 260},
    "btc_tweet_charts_enabled": True,
    "btc_tweet_chart_bars": {"Daily": 140, "4H": 220, "1H": 260, "15m": 260},
    "histominute_limit_15m": 1200,
    "histominute_limit_5m": 1200,
}

CRYPTOCOMPARE_HISTOHOUR   = "https://min-api.cryptocompare.com/data/v2/histohour"
CRYPTOCOMPARE_HISTOMINUTE = "https://min-api.cryptocompare.com/data/v2/histominute"
CRYPTOCOMPARE_PRICE      = "https://min-api.cryptocompare.com/data/price"
NEWS_ENDPOINT             = "https://min-api.cryptocompare.com/data/v2/news/"


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_config() -> Dict[str, Any]:
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f) or {}
        except Exception as e:
            print(f"⚠️  Config load failed ({e}) → using defaults")

    cfg = _deep_merge(DEFAULT_CONFIG, cfg)
    _validate_config(cfg)

    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass

    return cfg


def _validate_config(cfg: Dict[str, Any]) -> None:
    coins = cfg.get("coins")
    if isinstance(coins, str):
        coins = [c.strip().upper() for c in coins.split(",") if c.strip()]
    if not isinstance(coins, list):
        coins = []
    coins = [str(c).upper().strip() for c in coins if str(c).strip()]
    if not coins:
        print("⚠️  config.coins is empty/invalid → using DEFAULT_CONFIG.coins")
        coins = list(DEFAULT_CONFIG["coins"])
    cfg["coins"] = coins

    hrs = cfg.get("scheduled_hours_est")
    if not isinstance(hrs, list):
        hrs = []
    fixed_hrs: List[int] = []
    for h in hrs:
        try:
            fixed_hrs.append(int(h))
        except Exception:
            continue
    if not fixed_hrs:
        fixed_hrs = list(DEFAULT_CONFIG["scheduled_hours_est"])
    cfg["scheduled_hours_est"] = fixed_hrs

    for k in ["surge_threshold_pct", "surge_cooldown_hours"]:
        try:
            cfg[k] = float(cfg.get(k, DEFAULT_CONFIG[k]))
        except Exception:
            cfg[k] = float(DEFAULT_CONFIG[k])

    for k in ["history_max_rows", "news_pages_max", "news_max_per_coin", "news_global_cap"]:
        try:
            cfg[k] = int(cfg.get(k, DEFAULT_CONFIG[k]))
        except Exception:
            cfg[k] = int(DEFAULT_CONFIG[k])


def _load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _ensure(root: dict, *keys: str, default: dict | list | None = None) -> dict:
    ref = root
    for k in keys[:-1]:
        ref = ref.setdefault(k, {})
    ref.setdefault(keys[-1], {} if default is None else default)
    return root


def _sanitize_state(st: Dict[str, Any]) -> Dict[str, Any]:
    st = dict(st or {})
    posted = st.get("news_posted")
    if isinstance(posted, dict):
        st["news_posted"] = {k: v for k, v in posted.items()
                             if isinstance(k, str) and k.startswith("http") and isinstance(v, str)}
    else:
        st["news_posted"] = {}
    for dead in ("news", "news_posted_24h"):
        st.pop(dead, None)
    return st


def _load_surge_state(legacy: Dict[str, Any]) -> Dict[str, Any]:
    surge: Dict[str, Any] = {}
    if os.path.exists(SURGE_STATE_FILE):
        try:
            with open(SURGE_STATE_FILE, "r") as f:
                surge = json.load(f) or {}
        except Exception:
            surge = {}
    for k in [k for k in list(legacy) if k.endswith("_last_surge")]:
        surge.setdefault(k, legacy[k])
        legacy.pop(k, None)
    return surge


config      = _load_config()
state       = _sanitize_state(_load_state())
surge_state = _load_surge_state(state)

eastern    = pytz.timezone("America/New_York")
now_est    = datetime.now(eastern)
today_key  = now_est.strftime("%Y-%m-%d")

COINS = {
    "XRP":  {"color": 0x9B59B6, "thumb": "https://cryptologos.cc/logos/xrp-xrp-logo.png"},
    "BTC":  {"color": 0xF7931A, "thumb": "https://cryptologos.cc/logos/bitcoin-btc-logo.png"},
    "ETH":  {"color": 0x627EEA, "thumb": "https://cryptologos.cc/logos/ethereum-eth-logo.png"},
    "ADA":  {"color": 0x0033AD, "thumb": "https://cryptologos.cc/logos/cardano-ada-logo.png"},
    "SOL":  {"color": 0x14F195, "thumb": "https://cryptologos.cc/logos/solana-sol-logo.png"},
    "HBAR": {"color": 0x222222, "thumb": "https://cryptologos.cc/logos/hedera-hashgraph-hbar-logo.png"},
    "ZEC":  {"color": 0xF4B728, "thumb": "https://cryptologos.cc/logos/zcash-zec-logo.png"},
}


@dataclass(frozen=True)
class TwitterClients:
    v2: tweepy.Client
    v1: tweepy.API


def get_twitter_clients() -> Optional[TwitterClients]:
    api_key      = os.getenv("X_API_KEY")
    api_secret   = os.getenv("X_API_SECRET")
    access_token = os.getenv("X_ACCESS_TOKEN")
    access_secret = os.getenv("X_ACCESS_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        print("⚠️  X: Missing credentials")
        return None

    try:
        v2 = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
        v1   = tweepy.API(auth, wait_on_rate_limit=True)
        print("✓ X clients initialized (v2 + v1.1 media)")
        return TwitterClients(v2=v2, v1=v1)
    except Exception as e:
        print(f"❌ X setup failed: {e}")
        return None


twitter_clients = get_twitter_clients()

pathlib.Path(HISTORY_DIR).mkdir(exist_ok=True)


_CONFLICT_RE = re.compile(r"^(<<<<<<<|=======|>>>>>>>)")
_HIST_COLS   = ["timestamp", "open", "high", "low", "close", "volume"]


def _read_history_csv(file_path: str) -> pd.DataFrame:
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        lines = [ln for ln in f if not _CONFLICT_RE.match(ln)]
    return pd.read_csv(StringIO("".join(lines)), on_bad_lines="skip")


def _backup_corrupt(file_path: str) -> None:
    try:
        bak = f"{file_path}.corrupt-{int(time.time())}.bak"
        shutil.copy2(file_path, bak)
        print(f"   ↳ original kept as {bak}")
    except Exception:
        pass


def safe_load_history(coin: str) -> pd.DataFrame:
    file_path = os.path.join(HISTORY_DIR, f"{coin}.csv")
    empty = pd.DataFrame(columns=_HIST_COLS)
    if not os.path.exists(file_path):
        empty.to_csv(file_path, index=False)
        return empty
    try:
        df = _read_history_csv(file_path)
        if not set(_HIST_COLS).issubset(df.columns):
            print(f"⚠️  {coin}: CSV missing columns → starting fresh (original backed up)")
            _backup_corrupt(file_path)
            return empty
        df = df.dropna(subset=["timestamp", "close"], how="any")
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df = df.dropna(subset=["timestamp"])
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"])
        return df.sort_values("timestamp").drop_duplicates("timestamp")
    except Exception as e:
        print(f"⚠️  {coin}: CSV load error ({e}) → continuing without local history")
        _backup_corrupt(file_path)
        return empty


def safe_save_history(coin: str, df: pd.DataFrame) -> None:
    try:
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").drop_duplicates("timestamp")
        max_rows = int(config.get("history_max_rows", 10000))
        if len(df) > max_rows:
            df = df.iloc[-max_rows:]
        file_path = os.path.join(HISTORY_DIR, f"{coin}.csv")
        df.to_csv(file_path, index=False)
        print(f"✓ {coin}: Saved {len(df)} history rows")
    except Exception as e:
        print(f"❌ {coin}: Failed to save history: {e}")


def _cc_to_ohlcv_df(data_points: List[dict]) -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "timestamp": pd.to_datetime(d["time"], unit="s", utc=True),
                "open":      float(d["open"]),
                "high":      float(d["high"]),
                "low":       float(d["low"]),
                "close":     float(d["close"]),
                "volume":    float(d.get("volumeto", d.get("volumefrom", 0.0))),
            }
            for d in data_points
            if d.get("time", 0) > 0
        ]
    )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").drop_duplicates("timestamp")


def _cc_get(url: str, params: Dict[str, Any], timeout: int = 20, tries: int = 3) -> requests.Response:
    headers = {}
    key = os.getenv("CRYPTOCOMPARE_API_KEY", "").strip()
    if key:
        headers["authorization"] = f"Apikey {key}"
    last_exc: Optional[Exception] = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _cc_points(r: requests.Response) -> List[dict]:
    payload = r.json()
    if isinstance(payload, dict) and payload.get("Response") == "Error":
        raise ValueError(payload.get("Message", "CryptoCompare error"))
    return (payload.get("Data") or {}).get("Data") or []


def _fetch_histohour_raw(coin: str, limit: int = 2000) -> List[dict]:
    for tsym in ("USDT", "USD"):
        try:
            r = _cc_get(CRYPTOCOMPARE_HISTOHOUR, {"fsym": coin, "tsym": tsym, "limit": limit})
            data_points = _cc_points(r)
            if data_points:
                return data_points
        except Exception as e:
            print(f"⚠️  {coin}: histohour tsym={tsym} error → {e}")
    return []


def fetch_data(coin: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    history_df = safe_load_history(coin)
    try:
        data_points = _fetch_histohour_raw(coin)
        if not data_points:
            raise ValueError("Empty histohour data")

        df_new = _cc_to_ohlcv_df(data_points)
        if df_new.empty:
            raise ValueError("Empty histohour dataframe")

        combined = pd.concat([history_df, df_new], ignore_index=True)
        combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True)
        combined = combined.sort_values("timestamp").drop_duplicates("timestamp")
        safe_save_history(coin, combined)

        hourly   = combined.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        df_4h    = hourly.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        df_daily = hourly.resample("1D").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()

        print(f"✓ {coin}: {len(hourly)} hourly bars")
        return hourly, df_4h, df_daily
    except Exception as e:
        print(f"⚠️  {coin}: histohour failed → {e} (fallback local)")
        if history_df.empty:
            return None, None, None
        hourly   = history_df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        df_4h    = hourly.resample("4h").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        df_daily = hourly.resample("1D").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        return hourly, df_4h, df_daily


def fetch_live_price(coin: str, tsym: str = "USD") -> Optional[float]:
    try:
        r = _cc_get(CRYPTOCOMPARE_PRICE, {"fsym": coin, "tsyms": tsym})
        payload = r.json()
        if isinstance(payload, dict) and payload.get("Response") == "Error":
            raise ValueError(payload.get("Message", "CryptoCompare error"))
        value = payload.get(tsym)
        if value is None:
            raise ValueError(f"No {tsym} price returned")
        return float(value)
    except Exception as e:
        print(f"⚠️  {coin}: live price fetch failed → {e} (using candle close)")
        return None


def fetch_histominute(coin: str, aggregate: int, limit: int, tsym: str = "USDT") -> Optional[pd.DataFrame]:
    try:
        r = _cc_get(
            CRYPTOCOMPARE_HISTOMINUTE,
            {"fsym": coin, "tsym": tsym, "limit": int(limit), "aggregate": int(aggregate)},
        )
        data_points = _cc_points(r)
        df = _cc_to_ohlcv_df(data_points)
        if df.empty:
            return None
        return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        print(f"⚠️  {coin}: histominute agg={aggregate} failed → {e}")
        return None


NEWS_RSS_FEEDS = [
    ("CoinDesk",         "https://www.coindesk.com/arc/outboundfeeds/rss/",  "coindesk.com"),
    ("CoinTelegraph",    "https://cointelegraph.com/rss",                     "cointelegraph.com"),
    ("Decrypt",          "https://decrypt.co/feed",                           "decrypt.co"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed",                  "bitcoinmagazine.com"),
    ("BeInCrypto",       "https://beincrypto.com/feed/",                      "beincrypto.com"),
    ("CryptoSlate",      "https://cryptoslate.com/feed/",                     "cryptoslate.com"),
    ("CryptoNews",       "https://cryptonews.com/news/feed/",                 "cryptonews.com"),
    ("AMBCrypto",        "https://ambcrypto.com/feed/",                       "ambcrypto.com"),
    ("U.Today",          "https://u.today/rss",                               "u.today"),
    ("The Block",        "https://www.theblock.co/rss.xml",                   "theblock.co"),
]

COIN_KEYWORDS: Dict[str, List[str]] = {
    "XRP":  ["xrp", "ripple"],
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "eth"],
    "ADA":  ["cardano", "ada"],
    "SOL":  ["solana", "sol"],
    "HBAR": ["hedera", "hbar", "hashgraph"],
    "ZEC":  ["zcash", "zec"],
}


def _parse_rss(xml_text: str, source_name: str, source_domain: str) -> List[dict]:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    articles = []
    try:
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        items = root.findall(".//item")
        if not items:
            items = root.findall(".//atom:entry", ns)

        def _get_text(el_or_none) -> str:
            if el_or_none is None:
                return ""
            return (el_or_none.text or "").strip()

        def _find(item, rss_tag: str, atom_tag: str = "") -> str:
            el = item.find(rss_tag)
            if el is not None:
                return _get_text(el)
            if atom_tag:
                el = item.find(f"atom:{atom_tag}", ns)
                if el is not None:
                    return _get_text(el)
            return ""

        for item in items:
            title = _find(item, "title")
            url   = _find(item, "link")

            if not url:
                link_el = item.find("atom:link", ns)
                if link_el is not None:
                    url = link_el.attrib.get("href", "")

            if not url:
                link_el = item.find("link")
                if link_el is not None and link_el.tail:
                    url = link_el.tail.strip()

            pub_raw = _find(item, "pubDate") or _find(item, "published", "published") or _find(item, "updated", "updated")

            try:
                pub_ts = int(parsedate_to_datetime(pub_raw).timestamp())
            except Exception:
                try:
                    pub_ts = int(datetime.fromisoformat(pub_raw.replace("Z", "+00:00")).timestamp())
                except Exception:
                    pub_ts = 0

            description = _find(item, "description") or _find(item, "summary", "summary") or ""
            description = re.sub(r"<[^>]+>", "", description)[:300].strip()

            if title and url:
                articles.append({
                    "url":          url,
                    "title":        title,
                    "body":         description,
                    "published_on": pub_ts,
                    "source_info":  {"name": source_name},
                    "imageurl":     "",
                    "_domain":      source_domain,
                })
    except Exception as e:
        print(f"⚠️  RSS parse error ({source_name}): {e}")
    return articles


def _coin_match(article: dict, coins: List[str]) -> str:
    text = (article.get("title", "") + " " + article.get("body", "")).lower()
    for coin in coins:
        for kw in COIN_KEYWORDS.get(coin, [coin.lower()]):
            if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text):
                return coin
    return "GENERAL"


def fetch_news(coins: List[str]) -> List[dict]:
    allowlist    = {d.lower() for d in config.get("news_allowlist_domains", [])}
    max_per_coin = int(config.get("news_max_per_coin", 5))
    global_cap   = int(config.get("news_global_cap", 45))

    seen_urls: set = set()
    collected: List[dict] = []
    coin_counts: Dict[str, int] = {c: 0 for c in coins}
    coin_counts["GENERAL"] = 0

    headers = {"User-Agent": "CryptoIntelBot/2.0 (RSS reader)"}

    for source_name, feed_url, domain in NEWS_RSS_FEEDS:
        if len(collected) >= global_cap:
            break
        if allowlist and domain not in allowlist:
            continue
        try:
            r = requests.get(feed_url, headers=headers, timeout=12)
            if r.status_code != 200:
                print(f"  [news] {source_name}: HTTP {r.status_code}")
                continue
            articles = _parse_rss(r.text, source_name, domain)
            added = 0
            for art in articles:
                if len(collected) >= global_cap:
                    break
                url = art.get("url", "")
                if not url or url in seen_urls or _news_already_posted(url):
                    seen_urls.add(url)
                    continue
                coin_tag = _coin_match(art, coins)
                cap = max_per_coin if coin_tag != "GENERAL" else global_cap
                if coin_counts.get(coin_tag, 0) >= cap:
                    continue
                seen_urls.add(url)
                art["_matched_coin"] = coin_tag
                collected.append(art)
                coin_counts[coin_tag] = coin_counts.get(coin_tag, 0) + 1
                added += 1
            print(f"  [news] {source_name}: {len(articles)} items → {added} new")
        except Exception as e:
            print(f"⚠️  RSS fetch {source_name}: {e}")

    print(f"  [news] {len(collected)} articles collected (allowlist={'active' if allowlist else 'disabled'})")
    collected.sort(key=lambda a: a.get("published_on", 0), reverse=True)
    return collected[:global_cap]


def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _news_already_posted(url: str) -> bool:
    posted = state.get("news_posted", {})
    return url in posted


def _mark_news_posted(url: str) -> None:
    _ensure(state, "news_posted", default={})
    state["news_posted"][url] = now_est.isoformat()
    posted = state["news_posted"]
    if len(posted) > 500:
        oldest_keys = sorted(posted, key=lambda k: str(posted[k]))[:len(posted) - 500]
        for k in oldest_keys:
            del posted[k]


def post_news_to_discord(articles: List[dict]) -> int:
    webhook_url = os.getenv("DISCORD_WEBHOOK_NEWS")
    if not webhook_url:
        print("⚠️  DISCORD_WEBHOOK_NEWS not set — skipping news post")
        return 0

    posted_count = 0
    for art in articles:
        url = art.get("url", "")
        if not url or _news_already_posted(url):
            continue

        title      = art.get("title", "No title")[:256]
        body       = art.get("body", "")[:300].strip()
        source     = art.get("source_info", {}).get("name", _extract_domain(url))
        pub_ts     = art.get("published_on", 0)
        coin_tag   = art.get("_matched_coin", "CRYPTO")
        img_url    = art.get("imageurl", "")

        color = COINS.get(coin_tag, {}).get("color", 0x7289DA)
        pub_dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc) if pub_ts else now_est

        webhook = DiscordWebhook(url=webhook_url, rate_limit_retry=True)
        embed   = DiscordEmbed(
            title       = title,
            description = f"{body}{'...' if len(art.get('body','')) > 300 else ''}\n\n[Read more]({url})",
            color       = color,
            url         = url,
        )
        embed.set_author(name=f"📰 {source}  •  {coin_tag}")
        if img_url:
            embed.set_image(url=img_url)
        embed.set_footer(text=f"Crypto Intel News  •  {pub_dt.strftime('%b %d, %Y %I:%M %p UTC')}")
        embed.set_timestamp()
        webhook.add_embed(embed)

        try:
            resp = webhook.execute()
            ok   = hasattr(resp, "status_code") and 200 <= resp.status_code < 300
            if ok:
                _mark_news_posted(url)
                posted_count += 1
            else:
                status = getattr(resp, "status_code", "?")
                print(f"⚠️  News Discord post failed (HTTP {status}): {title[:60]}")
        except Exception as e:
            print(f"⚠️  News Discord post exception: {e}")

    print(f"✓ News: posted {posted_count} new articles to Discord")
    return posted_count


def market_structure(df: pd.DataFrame, timeframe: str) -> str:
    try:
        if df is None or len(df) < 50:
            return "No Data"
        window    = 10 if timeframe == "Daily" else 15
        high_roll = df["high"].rolling(2 * window + 1, center=True).max()
        low_roll  = df["low"].rolling(2 * window + 1, center=True).min()
        highs     = df["high"][df["high"] == high_roll].dropna().tail(4)
        lows      = df["low"][df["low"] == low_roll].dropna().tail(4)
        if len(highs) < 3 or len(lows) < 3:
            return "Ranging/Choppy — No clear trend, price is sideways"
        h1, h2, h3 = highs.iloc[-1], highs.iloc[-2], highs.iloc[-3]
        l1, l2, l3 = lows.iloc[-1], lows.iloc[-2], lows.iloc[-3]
        if h1 > h2 > h3 and l1 > l2 > l3:
            return "Bullish (HH+HL) — Uptrend"
        if h1 < h2 < h3 and l1 < l2 < l3:
            return "Bearish (LH+LL) — Downtrend"
        return "Ranging/Choppy — Sideways"
    except Exception:
        return "Unavailable"


def _rsi_from_close(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(~((loss == 0) & (gain > 0)), 100.0)
    rsi = rsi.where(~((loss == 0) & (gain == 0)), 50.0)
    return rsi


def calculate_rsi(df_4h: pd.DataFrame) -> Tuple[int, str]:
    if df_4h is None or len(df_4h) < 15:
        return 50, "No Data"
    try:
        rsi_val  = int(_rsi_from_close(df_4h["close"], 14).iloc[-1])
        return rsi_val, f"{rsi_val} → {rsi_stance(rsi_val)}"
    except Exception:
        return 50, "Error"


def bollinger_analysis(df_4h: pd.DataFrame) -> Dict[str, object]:
    if df_4h is None or len(df_4h) < 20:
        return {"dist_pct": 50, "squeeze": "No Data", "breakout": "No Data"}
    df        = df_4h.copy()
    df["mid"] = df["close"].rolling(20).mean()
    df["std"] = df["close"].rolling(20).std()
    df["upper"] = df["mid"] + df["std"] * 2
    df["lower"] = df["mid"] - df["std"] * 2
    df["distance_from_lower"] = (df["close"] - df["lower"]) / (df["upper"] - df["lower"])
    latest  = df.iloc[-1]
    prev    = df.iloc[-2]
    dist_raw = float(latest["distance_from_lower"])
    dist_pct = round(dist_raw * 100, 2) if np.isfinite(dist_raw) else 50.0

    squeeze = "No Data"
    if len(df) >= 100:
        bb_width         = float(latest["upper"] - latest["lower"])
        historical_width = float((df["upper"] - df["lower"]).rolling(100).quantile(0.1).iloc[-1])
        squeeze          = "SQUEEZE ACTIVE" if bb_width < historical_width else "No Squeeze"

    if prev["close"] <= prev["upper"] and latest["close"] > latest["upper"]:
        breakout = "BULLISH BREAKOUT"
    elif prev["close"] >= prev["lower"] and latest["close"] < latest["lower"]:
        breakout = "BEARISH BREAKOUT"
    else:
        breakout = "No breakout"

    return {"dist_pct": dist_pct, "squeeze": squeeze, "breakout": breakout}


def _fmt_compact_usd(n: float) -> str:
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.1f}K"
    return f"{sign}${n:,.0f}"


def volume_analysis(df_4h: pd.DataFrame, lookback: int = 20) -> Dict[str, Any]:
    """Current 4H bar volume vs its trailing average, paired with which way
    price moved on that bar — 'high volume' only means something once you
    know whether it came with a bullish or bearish candle.
    """
    empty = {"last": 0.0, "avg": 0.0, "ratio": 1.0, "label": "No Data",
              "direction": "Flat", "read": "No Data"}
    if df_4h is None or len(df_4h) < lookback + 1 or "volume" not in df_4h.columns:
        return empty

    vol  = df_4h["volume"]
    last = float(vol.iloc[-1])
    avg  = float(vol.iloc[-(lookback + 1):-1].mean())

    if not np.isfinite(avg) or avg <= 0:
        return {**empty, "last": last}

    ratio = last / avg
    if ratio >= 2.0:
        label = "Very High"
    elif ratio >= 1.4:
        label = "High"
    elif ratio >= 0.7:
        label = "Average"
    elif ratio >= 0.4:
        label = "Low"
    else:
        label = "Very Low"

    last_open  = float(df_4h["open"].iloc[-1])
    last_close = float(df_4h["close"].iloc[-1])
    if last_close > last_open:
        direction = "Up"
    elif last_close < last_open:
        direction = "Down"
    else:
        direction = "Flat"

    # Does the volume back up the move, or undercut it?
    high_vol = ratio >= 1.4
    low_vol  = ratio < 0.7
    if direction == "Flat":
        read = "No Clear Move"
    elif high_vol:
        read = f"{direction} Move — Volume Confirms"
    elif low_vol:
        read = f"{direction} Move — Volume Unconfirmed (Weak Participation)"
    else:
        read = f"{direction} Move — Normal Volume"

    return {"last": last, "avg": avg, "ratio": round(ratio, 2), "label": label,
             "direction": direction, "read": read}


def calculate_market_confidence(bb: dict, rsi_val: int, daily_struct: str, h4_struct: str) -> int:
    score = 50.0

    if "Bullish" in daily_struct:
        score += 18
    elif "Bearish" in daily_struct:
        score -= 18

    if "Bullish" in h4_struct:
        score += 15
    elif "Bearish" in h4_struct:
        score -= 15

    ranging_count = sum("Ranging/Choppy" in s for s in (daily_struct, h4_struct))
    if ranging_count == 2:
        score = min(score, 65.0)
    elif ranging_count == 1:
        score = min(score, 78.0)

    if 52 <= rsi_val <= 68:
        score += 4
    elif 68 < rsi_val <= 75:
        score += 1
    elif rsi_val > 75:
        score -= min(7, (rsi_val - 75) * 0.35)
    elif 32 <= rsi_val < 48:
        score -= 2
    elif rsi_val < 32:
        score += 2

    dist = float(bb.get("dist_pct", 50))
    if not np.isfinite(dist):
        dist = 50.0
    if dist > 100:
        score -= min(8, (dist - 100) * 0.20)
    elif 55 <= dist <= 80:
        score += 2

    if ranging_count == 2:
        score = min(score, 65.0)

    return int(max(5, min(95, round(score))))


def calculate_scalper_confidence(
    bias_1h: Dict[str, str], bias_15m: Dict[str, str], bias_5m: Dict[str, str],
    rsi_val: int, bb: dict,
) -> int:
    score = 50.0

    for bias, weight in zip(
        [bias_1h.get("bias", ""), bias_15m.get("bias", ""), bias_5m.get("bias", "")],
        [20, 15, 10],
    ):
        if "BULLISH" in bias:
            score += weight
        elif "BEARISH" in bias:
            score -= weight

    if 50 <= rsi_val <= 68:
        score += 5
    elif rsi_val > 80:
        score -= 12
    elif rsi_val > 70:
        score -= 5
    elif rsi_val < 30:
        score -= 4

    dist = float(bb.get("dist_pct", 50))
    if np.isfinite(dist) and dist > 100:
        score -= min(8, (dist - 100) * 0.20)
    elif np.isfinite(dist) and 55 <= dist <= 80:
        score += 2

    biases = [
        bias_1h.get("bias", ""),
        bias_15m.get("bias", ""),
        bias_5m.get("bias", ""),
    ]
    bullish_count = sum("BULLISH" in b for b in biases)
    if bullish_count < 3:
        score = min(score, 82.0)
    if bullish_count <= 1:
        score = min(score, 68.0)

    return int(max(5, min(95, round(score))))


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _aligned_bull(row: pd.Series) -> bool:
    return bool(row["close"] > row["e9"] > row["e21"])


def _aligned_bear(row: pd.Series) -> bool:
    return bool(row["close"] < row["e9"] < row["e21"])


def run_scalper_ema(df: pd.DataFrame) -> Dict[str, str]:
    if df is None or len(df) < 21:
        return {"bias": "No Data", "signal": "Need 21+ bars"}
    d        = df.copy()
    d["e9"]  = _ema(d["close"], 9)
    d["e21"] = _ema(d["close"], 21)
    last     = d.iloc[-1]
    prev     = d.iloc[-2] if len(d) >= 22 else last

    if _aligned_bull(last):
        return {"bias": "BULLISH 🔼", "signal": "Confirmed — Price Above Both EMAs"}
    if _aligned_bear(last):
        return {"bias": "BEARISH 🔽", "signal": "Confirmed — Price Below Both EMAs"}

    # Alignment is broken on this candle. Instead of a generic "Confirmation
    # lost", describe exactly how it broke — a shallow pullback above EMA21,
    # a full collapse through both EMAs, an EMA9/EMA21 cross, or plain
    # compression — so 1H/15m/5m states read as genuinely different signals
    # rather than three copies of the same label.
    close, e9, e21 = float(last["close"]), float(last["e9"]), float(last["e21"])
    was_bull = _aligned_bull(prev)
    was_bear = _aligned_bear(prev)
    gap_pct  = abs(e9 - e21) / e21 * 100 if e21 else 0.0

    if gap_pct < 0.05:
        return {"bias": "NEUTRAL ⚪", "signal": "EMA Compression — EMA9 ≈ EMA21"}

    if e9 >= e21:
        # EMA structure is still bullish (EMA9 above EMA21). Either price
        # has only dipped below EMA9, or it has fully collapsed below both.
        if close < e21:
            detail = "Collapse — Price Below Both EMAs (EMA9 > EMA21 intact)"
        else:
            detail = "Pullback — Price Below EMA9, Above EMA21"
        prefix = "Confirmation Lost — " if was_bull else ""
        return {"bias": "NEUTRAL ⚪", "signal": prefix + detail}

    # EMA9 below EMA21: bearish structure. Either price has only poked
    # above EMA9, or it has fully reclaimed above both.
    if close > e21:
        detail = "Reclaim — Price Above Both EMAs (EMA9 < EMA21 intact)"
    else:
        detail = "Bounce — Price Above EMA9, Below EMA21"
    prefix = "Confirmation Lost — " if was_bear else ""
    return {"bias": "NEUTRAL ⚪", "signal": prefix + detail}


def _bias_kind(bias: str) -> str:
    b = bias or ""
    if "BULLISH" in b:
        return "BULLISH"
    if "BEARISH" in b:
        return "BEARISH"
    return "NEUTRAL"


def extension_level(rsi_val: int, bb: dict) -> str:
    dist = float((bb or {}).get("dist_pct", 50) or 50)
    if not np.isfinite(dist):
        dist = 50.0
    if rsi_val >= 75 or dist >= 110 or rsi_val <= 25 or dist <= 0:
        return "High"
    if rsi_val >= 70 or dist >= 100 or rsi_val <= 30 or dist <= 10:
        return "Elevated"
    return "Normal"


def position_outlook(
    daily_struct: str, h4_struct: str, rsi_val: int, bb: dict, confidence: int,
) -> Dict[str, Any]:
    ranging_count = sum("Ranging" in (s or "") or "Choppy" in (s or "") for s in (daily_struct, h4_struct))
    bull_count = sum("Bullish" in (s or "") for s in (daily_struct, h4_struct))
    bear_count = sum("Bearish" in (s or "") for s in (daily_struct, h4_struct))
    ext = extension_level(rsi_val, bb)

    if bull_count == 2:
        headline, trend, stance = "BULLISH — Daily/4H Aligned", "Confirmed uptrend", "BULLISH"
        if ext in ("High", "Elevated"):
            headline = "BULLISH — EXTENDED"
    elif bear_count == 2:
        headline, trend, stance = "BEARISH — Daily/4H Aligned", "Confirmed downtrend", "BEARISH"
        if ext in ("High", "Elevated"):
            headline = "BEARISH — EXTENDED"
    elif ranging_count == 2:
        headline, trend, stance = "NEUTRAL — Daily/4H Range", "Unconfirmed", "NEUTRAL"
    elif bull_count == 1 or bear_count == 1:
        headline, trend, stance = "MIXED — Partial Higher-TF Trend", "Partially confirmed", "MIXED"
    else:
        headline, trend, stance = "NEUTRAL — No Daily/4H Trend", "Unconfirmed", "NEUTRAL"

    return {
        "headline": headline,
        "trend": trend,
        "extension": ext,
        "stance": stance,
        "confidence": confidence,
    }


def scalper_outlook(
    bias_1h: Dict[str, str], bias_15m: Dict[str, str], bias_5m: Dict[str, str],
    rsi_val: int, bb: dict, confidence: int,
) -> Dict[str, Any]:
    k1 = _bias_kind(bias_1h.get("bias", ""))
    k15 = _bias_kind(bias_15m.get("bias", ""))
    k5 = _bias_kind(bias_5m.get("bias", ""))
    ext = extension_level(rsi_val, bb)
    sig5 = bias_5m.get("signal", k5)

    if k1 == "BULLISH" and k15 == "BULLISH" and k5 == "BULLISH":
        headline = "BULLISH — ALIGNED"
        align = "1H/15m/5m: Aligned"
        five = "Confirmed"
        if ext in ("High", "Elevated"):
            headline = "BULLISH — EXTENDED"
    elif k1 == "BEARISH" and k15 == "BEARISH" and k5 == "BEARISH":
        headline = "BEARISH — ALIGNED"
        align = "1H/15m/5m: Aligned"
        five = "Confirmed"
        if ext in ("High", "Elevated"):
            headline = "BEARISH — EXTENDED"
    elif k1 == "BULLISH" and k15 == "BULLISH":
        align = "1H/15m: Aligned"
        five = "Confirmation lost" if k5 != "BULLISH" else "Confirmed"
        if ext in ("High", "Elevated") and k5 != "BULLISH":
            headline = "BULLISH — EXTENDED / 5M UNCONFIRMED"
        elif ext in ("High", "Elevated"):
            headline = "BULLISH — EXTENDED"
        else:
            headline = "BULLISH — 5M UNCONFIRMED"
    elif k1 == "BEARISH" and k15 == "BEARISH":
        align = "1H/15m: Aligned"
        five = "Confirmation lost" if k5 != "BEARISH" else "Confirmed"
        if ext in ("High", "Elevated") and k5 != "BEARISH":
            headline = "BEARISH — EXTENDED / 5M UNCONFIRMED"
        elif ext in ("High", "Elevated"):
            headline = "BEARISH — EXTENDED"
        else:
            headline = "BEARISH — 5M UNCONFIRMED"
    else:
        headline = "MIXED — SHORT-TERM"
        align = f"1H: {k1} • 15m: {k15}"
        # 5m is fully aligned bullish/bearish → say so plainly rather than
        # repeating the raw "Confirmed — Price Below Both EMAs" text, which is already
        # shown verbatim in the Scalper Bias field below this one.
        if k5 == "BULLISH":
            five = "Bullish (Aligned)"
        elif k5 == "BEARISH":
            five = "Bearish (Aligned)"
        else:
            five = sig5 or "Neutral"

    return {
        "headline": headline,
        "align": align,
        "five": five,
        "extension": ext,
        "confidence": confidence,
    }


def check_surge(
    coin: str,
    hourly_df: pd.DataFrame,
    minute_df: Optional[pd.DataFrame] = None,
) -> Tuple[bool, float, float, str]:
    if minute_df is not None and len(minute_df) >= 30:
        current_price  = float(minute_df["close"].iloc[-1])
        hour_ago_price = float(minute_df["close"].iloc[0])
    elif hourly_df is not None and len(hourly_df) >= 2:
        current_price  = float(hourly_df["close"].iloc[-1])
        hour_ago_price = float(hourly_df["close"].iloc[-2])
    else:
        return False, 0.0, 0.0, "neutral"

    if hour_ago_price <= 0:
        return False, 0.0, current_price, "neutral"
    surge_pct = ((current_price - hour_ago_price) / hour_ago_price) * 100.0
    direction = "up" if surge_pct > 0 else "down"

    last_alert_time = surge_state.get(f"{coin}_last_surge")
    if last_alert_time:
        try:
            last_time = datetime.fromisoformat(last_alert_time)
            if last_time.tzinfo is None:
                last_time = eastern.localize(last_time)
        except Exception:
            last_time = now_est - timedelta(hours=999)
        cooldown_hours = float(config.get("surge_cooldown_hours", 2))
        if (now_est - last_time) < timedelta(hours=cooldown_hours):
            return False, surge_pct, current_price, direction

    threshold = float(config.get("surge_threshold_pct", 5.0))
    if abs(surge_pct) >= threshold:
        print(f"🚨 {coin}: SURGE {surge_pct:+.2f}%")
        return True, surge_pct, current_price, direction

    return False, surge_pct, current_price, direction


def send_surge_alert(coin: str, surge_pct: float, price: float, direction: str) -> bool:
    webhook_url = os.getenv(f"DISCORD_WEBHOOK_{coin}")
    if not webhook_url:
        return False

    webhook = DiscordWebhook(url=webhook_url, rate_limit_retry=True)
    title   = f"🚀 SURGE UP — {coin} Alert" if direction == "up" else f"📉 SURGE DOWN — {coin} Alert"
    color   = 0x00FF00 if direction == "up" else 0xFF0000
    embed   = DiscordEmbed(title=title, description=f"**{surge_pct:+.2f}%** in last hour", color=color)
    embed.set_thumbnail(url=COINS[coin]["thumb"])
    embed.add_embed_field(name="Price", value=f"${price:,.6f}", inline=True)
    embed.set_footer(text=f"Surge Alert • {now_est.strftime('%I:%M %p %Z')}")
    embed.set_timestamp()
    webhook.add_embed(embed)

    resp = webhook.execute()
    ok   = hasattr(resp, "status_code") and 200 <= resp.status_code < 300
    if ok:
        surge_state[f"{coin}_last_surge"] = now_est.isoformat()
    return ok


def rsi_stance(rsi: int) -> str:
    if rsi >= 70:
        return "Overbought / Extended"
    if rsi <= 30:
        return "Oversold"
    if rsi >= 55:
        return "Bullish Momentum"
    if rsi <= 45:
        return "Bearish Momentum"
    return "Neutral"


def bb_squeeze_flag(bb: dict) -> str:
    return "On" if (bb or {}).get("squeeze") == "SQUEEZE ACTIVE" else "Off"

def bb_breakout_code(bb: dict) -> int:
    bo = (bb or {}).get("breakout", "No breakout")
    if bo == "BULLISH BREAKOUT":  return 1
    if bo == "BEARISH BREAKOUT":  return -1
    return 0

def bb_stance_simple(bb: dict) -> str:
    bo = bb_breakout_code(bb)
    if bo == 1:  return "Bull BO"
    if bo == -1: return "Bear BO"
    if (bb or {}).get("squeeze") == "SQUEEZE ACTIVE": return "Wait"
    return "Neutral"

def bias_sig(bias_label: str) -> str:
    if "BULLISH" in (bias_label or ""): return "Long"
    if "BEARISH" in (bias_label or ""): return "Short"
    return "Wait"

def prob_stance(prob: int) -> str:
    if prob >= 60: return "Lean long"
    if prob <= 39: return "Lean short"
    return "Neutral"

def _trend_short(struct: str) -> str:
    if "Bullish" in (struct or ""): return "Bull"
    if "Bearish" in (struct or ""): return "Bear"
    return "Range"

def _x_weight(ch: str) -> int:
    cp = ord(ch)
    if cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247:
        return 1
    return 2


def x_length(text: str) -> int:
    return sum(_x_weight(c) for c in text)


def trim_to_277(text: str) -> str:
    if x_length(text) <= 277:
        return text
    lines = text.split("\n")
    while len(lines) > 1 and x_length("\n".join(lines)) > 277:
        lines.pop(-2)
    out = "\n".join(lines)
    while x_length(out) > 277:
        out = out[:-1]
    return out

def tweet_hashtags(coin: str) -> str:
    if coin == "BTC": return "#BTC #Bitcoin #Crypto #Trading"
    if coin == "XRP": return "#XRP #Crypto #Trading"
    return f"#{coin} #Crypto #Trading"


def _charts_available() -> bool:
    try:
        import mplfinance  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def _to_mpf(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.index = pd.to_datetime(d.index, utc=True)
    d = d.rename(columns={"open": "Open", "high": "High", "low": "Low",
                           "close": "Close", "volume": "Volume"})
    return d[["Open", "High", "Low", "Close", "Volume"]]


def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    return _rsi_from_close(close, period)


def add_indicator_columns(d: pd.DataFrame) -> pd.DataFrame:
    out = d.copy()
    out["EMA9"]  = out["Close"].ewm(span=9,  adjust=False).mean()
    out["EMA21"] = out["Close"].ewm(span=21, adjust=False).mean()
    mid          = out["Close"].rolling(20).mean()
    std          = out["Close"].rolling(20).std()
    out["BB_MID"] = mid
    out["BB_UP"]  = mid + 2 * std
    out["BB_LO"]  = mid - 2 * std
    out["RSI14"]  = rsi_series(out["Close"], 14)
    return out


def render_single_timeframe_chart(df: pd.DataFrame, title: str, out_path: str) -> str:
    import mplfinance as mpf

    mpf_df = add_indicator_columns(_to_mpf(df)).dropna().copy()
    if len(mpf_df) < 50:
        mpf_df = _to_mpf(df).dropna().copy()

    last_price = float(mpf_df["Close"].iloc[-1])

    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        marketcolors=mpf.make_marketcolors(
            up="#22c55e", down="#ef4444", edge="inherit", wick="inherit",
            volume="inherit", ohlc="inherit"
        ),
        gridstyle=":",
        gridcolor="#334155",
        facecolor="#0b1220",
        figcolor="#0b1220",
        rc={"font.size": 9, "axes.labelcolor": "#e5e7eb", "xtick.color": "#94a3b8", "ytick.color": "#94a3b8"},
    )

    addplots = []
    if "EMA9" in mpf_df.columns:
        addplots.append(mpf.make_addplot(mpf_df["EMA9"], color="#facc15", width=1.15))
    if "EMA21" in mpf_df.columns:
        addplots.append(mpf.make_addplot(mpf_df["EMA21"], color="#38bdf8", width=1.15))
    if "BB_UP" in mpf_df.columns:
        addplots.append(mpf.make_addplot(mpf_df["BB_UP"], color="#a78bfa", width=0.85))
        addplots.append(mpf.make_addplot(mpf_df["BB_MID"], color="#94a3b8", width=0.75, linestyle="--"))
        addplots.append(mpf.make_addplot(mpf_df["BB_LO"], color="#a78bfa", width=0.85))
    if "RSI14" in mpf_df.columns:
        addplots.append(mpf.make_addplot(mpf_df["RSI14"], panel=2, color="#fb923c", width=1.0, secondary_y=False))
        addplots.append(mpf.make_addplot(pd.Series(70.0, index=mpf_df.index), panel=2, color="#64748b", width=0.65, linestyle="--", secondary_y=False))
        addplots.append(mpf.make_addplot(pd.Series(30.0, index=mpf_df.index), panel=2, color="#64748b", width=0.65, linestyle="--", secondary_y=False))

    fig, axes = mpf.plot(
        mpf_df,
        type="candle",
        addplot=addplots if addplots else None,
        volume="Volume" in mpf_df.columns,
        title=f"{title}   |   ${last_price:,.4f}",
        ylabel="Price",
        ylabel_lower="Volume",
        panel_ratios=(5, 2, 2),
        hlines=dict(hlines=[last_price], colors=["#f8fafc"], linestyle="-.", linewidths=0.8),
        returnfig=True,
        figsize=(10.0, 6.0),
        tight_layout=True,
        style=style,
        xrotation=0,
        datetime_format="%m-%d %H:%M",
        warn_too_much_data=10000,
    )

    try:
        axes[-1].set_ylabel("RSI", color="#e5e7eb")
        axes[-1].set_ylim(0, 100)
    except Exception:
        pass

    fig.savefig(out_path, dpi=180, facecolor="#0b1220", bbox_inches="tight")
    plt.close(fig)
    return out_path


def stitch_2x2(img_paths: List[str], out_path: str) -> str:
    from PIL import Image
    imgs     = [Image.open(p).convert("RGB") for p in img_paths]
    w        = max(i.width  for i in imgs)
    h        = max(i.height for i in imgs)
    canvas   = Image.new("RGB", (w * 2, h * 2), "white")
    positions = [(0, 0), (w, 0), (0, h), (w, h)]
    for im, pos in zip(imgs, positions):
        canvas.paste(im.resize((w, h)), pos)
    canvas.save(out_path, format="PNG", optimize=True)
    return out_path


def get_chart_cfg(coin: str) -> Tuple[bool, Dict[str, int]]:
    key_enabled = f"{coin.lower()}_tweet_charts_enabled"
    key_bars    = f"{coin.lower()}_tweet_chart_bars"
    enabled     = bool(config.get(key_enabled, False))
    bars        = config.get(key_bars, {}) or {}
    return enabled, {str(k): int(v) for k, v in bars.items()}


def build_discord_chart_images(
    coin: str,
    df_daily: pd.DataFrame,
    df_4h: pd.DataFrame,
    hourly: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> List[str]:
    enabled, bars_cfg = get_chart_cfg(coin)
    if not enabled:
        return []
    if not _charts_available():
        print("ℹ️  Charts skipped (mplfinance/Pillow missing)")
        return []

    pathlib.Path("charts").mkdir(exist_ok=True)
    pathlib.Path("charts", "discord").mkdir(parents=True, exist_ok=True)

    def tail(df: pd.DataFrame, n: int) -> pd.DataFrame:
        return df.iloc[-n:] if (df is not None and not df.empty and n > 0) else df

    d_daily = tail(df_daily, bars_cfg.get("Daily", 140))
    d_4h    = tail(df_4h,    bars_cfg.get("4H",    220))
    d_1h    = tail(hourly,   bars_cfg.get("1H",    260))
    d_15    = tail(df_15m,   bars_cfg.get("15m",   260))

    if any(x is None or x.empty for x in [d_daily, d_4h, d_1h, d_15]):
        return []

    out_dir = pathlib.Path("charts", "discord")
    paths = [
        (d_daily, "Daily", out_dir / f"{coin.lower()}_daily.png"),
        (d_4h,    "4H",    out_dir / f"{coin.lower()}_4h.png"),
        (d_1h,    "1H",    out_dir / f"{coin.lower()}_1h.png"),
        (d_15,    "15m",   out_dir / f"{coin.lower()}_15m.png"),
    ]

    results: List[str] = []
    for df, timeframe, out_path in paths:
        results.append(render_single_timeframe_chart(df, f"{coin} — {timeframe}", str(out_path)))
    return results


def build_tweet_chart_image(
    coin: str,
    df_daily: pd.DataFrame,
    df_4h: pd.DataFrame,
    hourly: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> Optional[str]:
    enabled, bars_cfg = get_chart_cfg(coin)
    if not enabled:
        return None
    if not _charts_available():
        print("ℹ️  Charts skipped (mplfinance/Pillow missing)")
        return None

    pathlib.Path("charts").mkdir(exist_ok=True)

    def tail(df: pd.DataFrame, n: int) -> pd.DataFrame:
        return df.iloc[-n:] if (df is not None and not df.empty and n > 0) else df

    d_daily = tail(df_daily, bars_cfg.get("Daily", 140))
    d_4h    = tail(df_4h,    bars_cfg.get("4H",    220))
    d_1h    = tail(hourly,   bars_cfg.get("1H",    260))
    d_15    = tail(df_15m,   bars_cfg.get("15m",   260))

    if any(x is None or x.empty for x in [d_daily, d_4h, d_1h, d_15]):
        return None

    with tempfile.TemporaryDirectory() as td:
        p1 = render_single_timeframe_chart(d_daily, f"{coin} — Daily", os.path.join(td, "daily.png"))
        p2 = render_single_timeframe_chart(d_4h,    f"{coin} — 4H",    os.path.join(td, "4h.png"))
        p3 = render_single_timeframe_chart(d_1h,    f"{coin} — 1H",    os.path.join(td, "1h.png"))
        p4 = render_single_timeframe_chart(d_15,    f"{coin} — 15m",   os.path.join(td, "15m.png"))
        out_path = os.path.join("charts", f"{coin.lower()}_tweet.png")
        return stitch_2x2([p1, p2, p3, p4], out_path)


def upload_media_and_tweet(coin: str, text: str, image_path: Optional[str]) -> bool:
    if not twitter_clients:
        return False
    try:
        media_ids = None
        if image_path and os.path.exists(image_path):
            try:
                media     = twitter_clients.v1.media_upload(filename=image_path)
                media_ids = [int(media.media_id)]
            except Exception as e:
                print(f"⚠️  {coin}: X media upload failed → posting text-only ({e})")
        resp = twitter_clients.v2.create_tweet(text=text, media_ids=media_ids)
        ok   = bool(resp and getattr(resp, "data", None))
        if ok:
            print(f"✓ {coin}: Posted to X (ID: {resp.data.get('id')})")
        return ok
    except Exception as e:
        print(f"⚠️  {coin}: X post failed → {e}")
        return False


def send_report(coin: str, hourly: pd.DataFrame, df_4h: pd.DataFrame, df_daily: pd.DataFrame) -> bool:
    webhook_url = os.getenv(f"DISCORD_WEBHOOK_{coin}")
    if not webhook_url or df_4h is None or df_4h.empty:
        return False

    live_price = fetch_live_price(coin, "USD")
    price = live_price if live_price is not None else float(df_4h["close"].iloc[-1])
    change_24h = (price / float(hourly["close"].iloc[-25]) - 1) * 100 if len(hourly) >= 25 else 0.0

    bb           = bollinger_analysis(df_4h)
    vol_info     = volume_analysis(df_4h)
    rsi_val, _   = calculate_rsi(df_4h)
    daily_struct = market_structure(df_daily, "Daily")
    h4_struct    = market_structure(df_4h, "4H")
    bias_1h      = run_scalper_ema(hourly)

    df_15m = fetch_histominute(coin, aggregate=15, limit=int(config.get("histominute_limit_15m", 1200)))
    if df_15m is None:
        df_15m = hourly.resample("15min").ffill().dropna()
    df_5m = df_15m.resample("5min").ffill().dropna()

    bias_15m = run_scalper_ema(df_15m)
    bias_5m  = run_scalper_ema(df_5m)

    position_confidence = calculate_market_confidence(bb, rsi_val, daily_struct, h4_struct)
    scalper_confidence  = calculate_scalper_confidence(bias_1h, bias_15m, bias_5m, rsi_val, bb)
    pos_out = position_outlook(daily_struct, h4_struct, rsi_val, bb, position_confidence)
    scalp_out = scalper_outlook(bias_1h, bias_15m, bias_5m, rsi_val, bb, scalper_confidence)

    webhook = DiscordWebhook(url=webhook_url, rate_limit_retry=True)
    embed   = DiscordEmbed(title=f"{coin} Market Report", color=COINS[coin]["color"])
    embed.set_thumbnail(url=COINS[coin]["thumb"])
    embed.add_embed_field(name="💰 Price",      value=f"${price:,.4f}\n24H: `{change_24h:+.2f}%`", inline=True)
    embed.add_embed_field(name="📊 RSI",        value=f"{rsi_val} → {rsi_stance(rsi_val)}",         inline=True)
    embed.add_embed_field(name="📈 Volatility", value=f"BB Pos: {bb['dist_pct']:.1f}%\n{bb['squeeze']}\n{bb['breakout']}", inline=True)
    if vol_info["label"] != "No Data":
        embed.add_embed_field(
            name="📊 Volume (4H)",
            value=f"{_fmt_compact_usd(vol_info['last'])}\n{vol_info['ratio']:.2f}x avg — {vol_info['label']}\n{vol_info['read']}",
            inline=True,
        )
    embed.add_embed_field(name="📐 Structure",  value=f"Daily: {daily_struct}\n4H: {h4_struct}",    inline=False)
    embed.add_embed_field(
        name="🎯 Position Outlook",
        value=(
            f"**{pos_out['headline']}**\n"
            f"Confidence: {position_confidence}%\n"
            f"Trend: {pos_out['trend']}\n"
            f"Extension: {pos_out['extension']}"
        ),
        inline=True,
    )
    embed.add_embed_field(
        name="⚡ Scalper Outlook",
        value=(
            f"**{scalp_out['headline']}**\n"
            f"Confidence: {scalper_confidence}%\n"
            f"{scalp_out['align']}\n"
            f"5m: {scalp_out['five']}\n"
            f"Extension: {scalp_out['extension']}"
        ),
        inline=True,
    )
    embed.add_embed_field(
        name="⚡ Scalper Bias",
        value=(
            f"1H: {bias_1h['bias']} — {bias_1h['signal']}\n"
            f"15m: {bias_15m['bias']} — {bias_15m['signal']}\n"
            f"5m: {bias_5m['bias']} — {bias_5m['signal']}"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Crypto Intelligence • {now_est.strftime('%I:%M %p %Z')}")
    embed.set_timestamp()

    discord_chart_paths = build_discord_chart_images(coin, df_daily, df_4h, hourly, df_15m)
    chart_files = []
    if discord_chart_paths:
        try:
            for chart_path in discord_chart_paths:
                if chart_path and os.path.exists(chart_path):
                    chart_file = open(chart_path, "rb")
                    chart_files.append(chart_file)
                    webhook.add_file(file=chart_file, filename=os.path.basename(chart_path))
            print(f"✓ {coin}: Discord charts prepared → {len(chart_files)} separate images")
        except Exception as e:
            print(f"⚠️  {coin}: Discord chart attachment failed → {e}")

    webhook.add_embed(embed)
    try:
        resp = webhook.execute()
    finally:
        for chart_file in chart_files:
            try:
                chart_file.close()
            except Exception:
                pass
    discord_success = hasattr(resp, "status_code") and 200 <= resp.status_code < 300
    if discord_success:
        print(f"✓ {coin}: Discord report sent")
    else:
        status = getattr(resp, "status_code", "?")
        print(f"⚠️  {coin}: Discord report failed (HTTP {status})")

    twitter_success = False
    tweet_symbols   = {s.upper() for s in (config.get("tweet_report_symbols") or [])}
    if twitter_clients and bool(config.get("post_reports_to_x", True)) and coin.upper() in tweet_symbols:
        if os.getenv("FORCE_FULL_REPORT") == "true" and not bool(config.get("tweet_on_force", True)):
            print(f"🛑 {coin}: Skipping X tweet on FORCE (tweet_on_force=False in config)")
        else:
            tweet_time = now_est.strftime("%I:%M%p").lstrip("0")
            tweet = (
                f"📊 ${coin} {tweet_time}\n"
                f"💰 ${price:,.4f} ({change_24h:+.2f}%)\n"
                f"📐 D:{_trend_short(daily_struct)} • 4H:{_trend_short(h4_struct)}\n"
                f"RSI {rsi_val} → {rsi_stance(rsi_val)}\n"
                f"🎯 {pos_out['headline']} ({position_confidence}%)\n"
                f"⚡ {scalp_out['headline']} ({scalper_confidence}%)\n"
                f"1H:{bias_1h['bias']} 15m:{bias_15m['bias']} 5m:{bias_5m['bias']}\n"
                f"{tweet_hashtags(coin)}"
            )
            tweet_text      = trim_to_277(tweet)
            chart_path      = build_tweet_chart_image(coin, df_daily, df_4h, hourly, df_15m)
            twitter_success = upload_media_and_tweet(coin, tweet_text, chart_path)

    return discord_success or twitter_success


if __name__ == "__main__":
    print("=" * 60)
    print(f"🚀 CRYPTO INTEL BOT — {now_est.strftime('%I:%M %p %Z')}")
    print("=" * 60)

    github_event = os.getenv("GITHUB_EVENT_NAME", "")
    is_forced    = os.getenv("FORCE_FULL_REPORT") == "true"
    is_scheduled = github_event == "schedule"

    surge_only = os.getenv("RUN_MODE", "").strip().lower() == "surge"
    if surge_only:
        is_scheduled = False
        is_forced    = False

    if not github_event:
        scheduled_hours = config.get("scheduled_hours_est", DEFAULT_CONFIG["scheduled_hours_est"])
        is_scheduled = now_est.hour in scheduled_hours or (now_est.hour - 1) % 24 in scheduled_hours

    coins_to_process = ["XRP"]

    print(f"GITHUB_EVENT={github_event!r} | mode={'surge' if surge_only else 'report'} | forced={is_forced} | scheduled={is_scheduled}")
    print(f"coins={coins_to_process} (count={len(coins_to_process)})")
    print("-" * 60)

    _ensure(state, "news_posted", default={})

    for coin in coins_to_process:
        print(f"\n[{coin}]")
        try:
            if surge_only:
                minute_df = fetch_histominute(coin, aggregate=1, limit=60)
                small = _cc_to_ohlcv_df(_fetch_histohour_raw(coin, limit=3)) if minute_df is None else None
                hourly = small.set_index("timestamp") if (small is not None and not small.empty) else None
                if minute_df is None and hourly is None:
                    print(f"⚠️  {coin}: No data, skipping")
                    continue
                is_surge, surge_pct, price, direction = check_surge(coin, hourly, minute_df)
                if is_surge:
                    send_surge_alert(coin, surge_pct, price, direction)
                else:
                    print(f"  {coin}: {surge_pct:+.2f}% (1h)")
                continue

            hourly, df_4h, df_daily = fetch_data(coin)
            if hourly is None or hourly.empty:
                print(f"⚠️  {coin}: No data, skipping")
                continue

            is_surge, surge_pct, price, direction = check_surge(coin, hourly)
            if is_surge:
                send_surge_alert(coin, surge_pct, price, direction)

            if is_forced or is_scheduled:
                send_report(coin, hourly, df_4h, df_daily)

        except Exception as e:
            print(f"❌ {coin}: Unexpected error → {e}")
            import traceback
            traceback.print_exc()

    if (is_forced or is_scheduled) and not surge_only:
        print("\n[NEWS]")
        try:
            articles = fetch_news(coins_to_process)
            print(f"✓ News: fetched {len(articles)} articles (after domain filter + dedup)")
            post_news_to_discord(articles)
        except Exception as e:
            print(f"❌ News fetch/post failed → {e}")
            import traceback
            traceback.print_exc()

    try:
        with open(SURGE_STATE_FILE, "w") as f:
            json.dump(surge_state, f, indent=2, sort_keys=True)
        if not surge_only:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        print("\n✓ State saved successfully")
    except Exception as e:
        print(f"⚠️  Failed to save state: {e}")

    print("✓ Run complete — Empire Status: ONLINE")
