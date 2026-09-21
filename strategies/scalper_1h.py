#!/usr/bin/env python3
"""
scalper_1h.py
1-hour trend gate (VWAP + EMA9/21 + simple structure). Returns bias + signal diagnostics.
"""
from datetime import datetime, timezone
import pandas as pd

def _ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def _vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    pv = (tp * df['volume']).cumsum()
    vol = df['volume'].cumsum()
    return pv / vol

def _market_structure_simple(df):
    highs = df['high'].tail(4)
    lows = df['low'].tail(4)
    try:
        if len(highs) >= 3 and len(lows) >= 3:
            if highs.iloc[-1] > highs.iloc[-2] > highs.iloc[-3] and lows.iloc[-1] > lows.iloc[-2] > lows.iloc[-3]:
                return "Bullish"
            if highs.iloc[-1] < highs.iloc[-2] < highs.iloc[-3] and lows.iloc[-1] < lows.iloc[-2] < lows.iloc[-3]:
                return "Bearish"
    except Exception:
        pass
    return "Ranging"

def run_scalper_1h(df_1h):
    try:
        df = df_1h.copy()
        if len(df) < 10:
            return {"bias": "No Data", "signal": "No Data"}

        df['ema9'] = _ema(df['close'], 9)
        df['ema21'] = _ema(df['close'], 21)
        df['vwap'] = _vwap(df)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        structure = _market_structure_simple(df)

        bias = "NEUTRAL"
        signal = "No Trade"

        # Bullish bias
        if last['close'] > last['vwap'] and last['ema9'] > last['ema21'] and "Bullish" in structure:
            bias = "Bullish"
            # entry signal if ema just flipped
            if prev['ema9'] <= prev['ema21'] and last['ema9'] > last['ema21']:
                signal = "LONG 🟢 EMA flip above VWAP"

        # Bearish bias
        if last['close'] < last['vwap'] and last['ema9'] < last['ema21'] and "Bearish" in structure:
            bias = "Bearish"
            if prev['ema9'] >= prev['ema21'] and last['ema9'] < last['ema21']:
                signal = "SHORT 🔴 EMA flip below VWAP"

        return {
            "timeframe": "1h",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bias": bias,
            "structure": structure,
            "signal": signal
        }
    except Exception as e:
        return {"bias": "Error", "signal": str(e)}
