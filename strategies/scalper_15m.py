#!/usr/bin/env python3
"""
scalper_15m.py
15-minute confirmation scalper: EMA9/21 momentum check, optionally uses 1H bias.
"""
from datetime import datetime, timezone

def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def run_scalper_15m(df_15m, bias=None):
    try:
        df = df_15m.copy()
        if len(df) < 6:
            return {"signal": "No Data", "bias": bias}
        df['ema9'] = _ema(df['close'], 9)
        df['ema21'] = _ema(df['close'], 21)

        prev = df.iloc[-2]
        last = df.iloc[-1]

        signal = "No Trade"
        bias_pref = (bias or "").upper()

        # 15m cross confirmation for entries
        if prev['ema9'] <= prev['ema21'] and last['ema9'] > last['ema21']:
            if bias_pref in ("BULLISH","") or bias_pref == "NEUTRAL":
                signal = "LONG CONFIRM ✅ 15m 9>21"

        if prev['ema9'] >= prev['ema21'] and last['ema9'] < last['ema21']:
            if bias_pref in ("BEARISH","") or bias_pref == "NEUTRAL":
                signal = "SHORT CONFIRM ✅ 15m 9<21"

        return {
            "timeframe": "15m",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": signal,
            "bias": bias
        }
    except Exception as e:
        return {"signal": "Error", "error": str(e)}
