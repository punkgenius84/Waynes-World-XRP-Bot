#!/usr/bin/env python3
"""
scalper_5m.py
Fast 5-minute scalper: EMA9/21 micro-entries, uses 1H bias if provided.
Returns a dict with 'signal' and diagnostic fields.
"""
import math
from datetime import datetime, timezone

def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def run_scalper_5m(df_5m, bias=None):
    try:
        df = df_5m.copy()
        if len(df) < 5:
            return {"signal": "No Data", "bias": bias}
        df['ema9'] = _ema(df['close'], 9)
        df['ema21'] = _ema(df['close'], 21)

        prev = df.iloc[-2]
        last = df.iloc[-1]

        signal = "No Trade"

        # Use bias if provided to prefer direction
        bias_pref = (bias or "").upper()

        # bullish reclaim
        if prev['close'] < prev['ema9'] and last['close'] > last['ema9']:
            if bias_pref in ("BULLISH","") or bias_pref == "NEUTRAL":
                signal = "LONG 🟢 EMA9 reclaim"

        # bearish rejection
        if prev['close'] > prev['ema9'] and last['close'] < last['ema9']:
            if bias_pref in ("BEARISH","") or bias_pref == "NEUTRAL":
                signal = "SHORT 🔴 EMA9 rejection"

        return {
            "timeframe": "5m",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": signal,
            "bias": bias
        }
    except Exception as e:
        return {"signal": "Error", "error": str(e)}
