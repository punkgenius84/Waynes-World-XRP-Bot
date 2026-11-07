import unittest

import pandas as pd

from combined_xrp_intel_report import (
    _format_swing_pair, _last_swing_dates, _range_width_pct, _recent_daily_range,
    _structural_validation_lines, _uniq_levels, build_invalidation_status,
    calculate_market_confidence, calculate_scalper_confidence, scalper_outlook,
    position_outlook, validate_4h_candle_integrity, volume_analysis,
)


class KeyLevelTests(unittest.TestCase):
    def test_recent_daily_range_uses_latest_20_bars_not_old_extremes(self):
        index = pd.date_range("2026-01-01", periods=30, freq="D")
        highs = [1.50] * 30
        lows = [1.20] * 30
        highs[2] = 2.00
        lows[3] = 0.80
        highs[-1] = 1.40
        lows[-1] = 1.25
        df = pd.DataFrame({"high": highs, "low": lows}, index=index)

        self.assertEqual(_recent_daily_range(df, lookback=20), (1.5, 1.2))

    def test_recent_daily_range_handles_missing_data(self):
        self.assertEqual(_recent_daily_range(None), (None, None))

    def test_range_width_is_percentage_of_midpoint(self):
        self.assertAlmostEqual(_range_width_pct(1.3982, 1.1980), 15.42, places=2)

    def test_range_width_rejects_invalid_bounds(self):
        self.assertEqual(_range_width_pct(1.2, 1.4), 0.0)

    def test_bullish_daily_structure_uses_lower_pivot_as_invalidation(self):
        self.assertEqual(
            _structural_validation_lines("Bullish (HH+HL)", 1.65, 1.24),
            ["Bullish Daily structure invalidated: 4H close below Daily swing low $1.2400"],
        )

    def test_bearish_daily_structure_uses_upper_pivot_as_invalidation(self):
        self.assertEqual(
            _structural_validation_lines("Bearish (LH+LL)", 1.65, 1.24),
            ["Bearish Daily structure invalidated: 4H close above Daily swing high $1.6500"],
        )

    def test_ranging_daily_structure_describes_two_range_breaks_not_invalidation(self):
        self.assertEqual(
            _structural_validation_lines("Ranging/Choppy — Sideways", 1.65, 1.24),
            ["Daily swing-structure boundary: 4H close below Daily swing low $1.2400 breaks lower / above Daily swing high $1.6500 breaks higher"],
        )

    def test_resistance_is_sorted_nearest_first(self):
        self.assertEqual(
            _uniq_levels([1.50, 1.40, 1.35, 1.20], price=1.33, side="res"),
            [1.35, 1.40],
        )

    def test_support_is_sorted_nearest_first(self):
        self.assertEqual(
            _uniq_levels([1.20, 1.30, 1.25, 1.40], price=1.33, side="sup"),
            [1.30, 1.25],
        )

    def test_swing_dates_are_reported_for_confirmed_pivots(self):
        index = pd.date_range("2026-01-01", periods=90, freq="D")
        highs = [10.0 + i * 0.001 for i in range(90)]
        lows = [8.0 + i * 0.001 for i in range(90)]
        highs[40] = 20.0
        lows[60] = 1.0
        df = pd.DataFrame(
            {"high": highs, "low": lows},
            index=index,
        )

        high_date, low_date = _last_swing_dates(df, "Daily")

        self.assertEqual(high_date, "2026-02-10")
        self.assertEqual(low_date, "2026-03-02")

    def test_swing_pivots_pair_prices_with_dates(self):
        self.assertEqual(
            _format_swing_pair(1.6581, "2026-09-23", 1.2460, "2026-09-16"),
            "high $1.6581 (2026-09-23) / low $1.2460 (2026-09-16)",
        )

    def test_swing_pivot_handles_missing_date(self):
        self.assertEqual(
            _format_swing_pair(1.3982, None, 1.2530, "2026-10-24"),
            "high $1.3982 (date unavailable) / low $1.2530 (2026-10-24)",
        )

    def test_swing_dates_gracefully_handle_missing_data(self):
        self.assertEqual(_last_swing_dates(None, "Daily"), (None, None))




class InvalidationHistoryTests(unittest.TestCase):
    @staticmethod
    def _frame(close=1.30, periods=45, freq="4h"):
        index = pd.date_range("2026-10-01", periods=periods, freq=freq, tz="UTC")
        highs = [1.40] * periods
        lows = [1.20] * periods
        # Confirmed swing points well inside the series, with enough bars on
        # either side for the centered pivot window.
        highs[20] = 1.60
        lows[25] = 1.10
        closes = [1.30] * periods
        closes[-1] = close
        return pd.DataFrame(
            {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1.0] * periods},
            index=index,
        )

    def test_bullish_invalidation_requires_close_below_previous_boundary(self):
        state = {"invalidation_history": {"4H": {
            "direction": "BULLISH", "active_boundary": 1.20,
            "last_checked_candle": "old",
        }}}
        frame = self._frame(close=1.19)
        text = build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Bearish (LH+LL)", "Bearish (LH+LL)",
            {"bias": "Bullish"}, {"bias": "Bullish"}, {"bias": "Bullish"},
        )
        event = state["invalidation_history"]["4H"]["last_invalidation"]
        self.assertIsNotNone(event)
        self.assertEqual(event["boundary"], 1.20)
        self.assertEqual(event["close"], 1.19)
        self.assertIn("last confirmed invalidation", text)

    def test_wick_below_boundary_does_not_invalidate_when_close_holds(self):
        state = {"invalidation_history": {"4H": {
            "direction": "BULLISH", "active_boundary": 1.20,
            "last_checked_candle": "old",
        }}}
        frame = self._frame(close=1.21)
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Bullish (HH+HL)", "Bullish (HH+HL)",
            {"bias": "Bullish"}, {"bias": "Bullish"}, {"bias": "Bullish"},
        )
        self.assertIsNone(state["invalidation_history"]["4H"]["last_invalidation"])

    def test_missing_frame_preserves_previous_structural_state(self):
        previous = {"direction": "BULLISH", "active_boundary": 1.20,
                    "last_invalidation": {"at": "2026-10-01 00:00 UTC"}}
        state = {"invalidation_history": {"Daily": previous.copy()}}
        text = build_invalidation_status(
            state, None, None, None, None, None,
            "No Data", "No Data", {"bias": "No Data"},
            {"bias": "No Data"}, {"bias": "No Data"},
        )
        self.assertEqual(state["invalidation_history"]["Daily"], previous)
        self.assertIn("previous history preserved", text)

    def test_invalidation_status_separates_each_timeframe_with_blank_lines(self):
        state = {}
        frame = self._frame(close=1.30)
        text = build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Bearish (LH+LL)",
            {"bias": "BEARISH"}, {"bias": "NEUTRAL"}, {"bias": "BULLISH"},
        )
        self.assertIn("\n\n4H ", text)
        self.assertIn("\n\n1H ", text)
        self.assertIn("\n\n15m ", text)
        self.assertIn("\n\n5m ", text)

    def test_daily_range_downside_break_is_recorded_on_completed_4h_close(self):
        frame = self._frame(close=1.19)
        state = {"invalidation_history": {"Daily": {
            "direction": "RANGE", "active_boundary": None,
            "active_range_low": 1.20, "active_range_high": 1.60,
            "last_checked_candle": "old",
        }}}
        text = build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Bearish (LH+LL)",
            {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"},
        )
        event = state["invalidation_history"]["Daily"]["last_range_break"]
        self.assertEqual(event["direction"], "DOWNSIDE")
        self.assertEqual(event["boundary"], 1.20)
        self.assertEqual(event["close"], 1.19)
        self.assertIn("last close-confirmed swing-range break: DOWNSIDE", text)
        self.assertIsNone(state["invalidation_history"]["Daily"]["last_invalidation"])

    def test_four_hour_range_downside_break_is_recorded_on_completed_4h_close(self):
        frame = InvalidationHistoryTests._frame(close=1.09)
        state = {"invalidation_history": {"4H": {
            "direction": "RANGE", "active_boundary": None,
            "last_checked_candle": "old",
        }}}
        text = build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Ranging/Choppy — Sideways",
            {"bias": "BEARISH"}, {"bias": "BEARISH"}, {"bias": "NEUTRAL"},
        )
        event = state["invalidation_history"]["4H"]["last_range_break"]
        self.assertEqual(event["direction"], "DOWNSIDE")
        self.assertEqual(event["boundary"], 1.10)
        self.assertEqual(event["close"], 1.09)
        self.assertIn("4H pivot pattern: RANGE/CHOPPY; directional break status: ACTIVE DOWNSIDE BREAK", text)
        self.assertIn("price remains below broken boundary", text)

    def test_daily_range_upside_break_is_recorded_on_completed_4h_close(self):
        frame = self._frame(close=1.61)
        state = {"invalidation_history": {"Daily": {
            "direction": "RANGE", "active_boundary": None,
            "active_range_low": 1.20, "active_range_high": 1.60,
            "last_checked_candle": "old",
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Bearish (LH+LL)",
            {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"},
        )
        event = state["invalidation_history"]["Daily"]["last_range_break"]
        self.assertEqual(event["direction"], "UPSIDE")
        self.assertEqual(event["boundary"], 1.60)
        self.assertEqual(event["close"], 1.61)

    def test_daily_range_same_broken_boundary_is_not_relogged(self):
        previous = {
            "direction": "DOWNSIDE", "boundary": 1.20, "close": 1.19,
            "at": "2026-10-07 00:00 UTC", "confirmation": "4H candle close",
        }
        frame = self._frame(close=1.18)
        state = {"invalidation_history": {"Daily": {
            "direction": "RANGE", "active_boundary": None,
            "active_range_low": 1.20, "active_range_high": 1.60,
            "last_range_break": previous, "last_checked_candle": "older candle",
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Bearish (LH+LL)",
            {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"},
        )
        self.assertEqual(state["invalidation_history"]["Daily"]["last_range_break"], previous)

    def test_daily_range_wick_breach_does_not_record_break_when_close_holds(self):
        frame = self._frame(close=1.21)
        frame.iloc[-1, frame.columns.get_loc("low")] = 1.10
        state = {"invalidation_history": {"Daily": {
            "direction": "RANGE", "active_boundary": None,
            "active_range_low": 1.20, "active_range_high": 1.60,
            "last_checked_candle": "old",
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Bearish (LH+LL)",
            {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"},
        )
        self.assertIsNone(state["invalidation_history"]["Daily"]["last_range_break"])

    def test_daily_range_break_is_not_confirmed_by_forming_4h_candle(self):
        frame = self._frame(close=1.21)
        current_bucket = pd.Timestamp.now(tz="UTC").floor("4h")
        frame.index = frame.index[:-1].append(pd.DatetimeIndex([current_bucket]))
        frame.iloc[-1, frame.columns.get_loc("close")] = 1.19
        state = {"invalidation_history": {"Daily": {
            "direction": "RANGE", "active_boundary": None,
            "active_range_low": 1.20, "active_range_high": 1.60,
            "last_checked_candle": "old",
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Bearish (LH+LL)",
            {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"},
        )
        self.assertIsNone(state["invalidation_history"]["Daily"]["last_range_break"])

    def test_ema_shift_is_recorded_separately_from_structure(self):
        state = {"invalidation_history": {"1H": {
            "direction": "BEARISH", "last_checked_candle": "old",
        }}}
        frame = self._frame(close=1.31, freq="1h")
        text = build_invalidation_status(
            state, None, None, frame, None, None,
            "No Data", "No Data", {"bias": "Bullish"},
            {"bias": "No Data"}, {"bias": "No Data"},
        )
        self.assertEqual(state["invalidation_history"]["1H"]["last_bias_shift"]["to"], "BULLISH")
        self.assertIn("not a structural invalidation", text)

    def test_same_candle_does_not_create_duplicate_invalidation(self):
        frame = self._frame(close=1.19)
        stamp = frame.index[-1].strftime("%Y-%m-%d %H:%M UTC")
        state = {"invalidation_history": {"4H": {
            "direction": "BULLISH", "active_boundary": 1.20,
            "last_checked_candle": stamp,
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Bearish (LH+LL)", "Bearish (LH+LL)",
            {"bias": "Bullish"}, {"bias": "Bullish"}, {"bias": "Bullish"},
        )
        self.assertIsNone(state["invalidation_history"]["4H"]["last_invalidation"])

    def test_forming_four_hour_candle_cannot_confirm_invalidation(self):
        frame = self._frame(close=1.21)
        current_bucket = pd.Timestamp.now(tz="UTC").floor("4h")
        frame.index = frame.index[:-1].append(pd.DatetimeIndex([current_bucket]))
        frame.iloc[-2, frame.columns.get_loc("close")] = 1.21
        frame.iloc[-1, frame.columns.get_loc("close")] = 1.19
        state = {"invalidation_history": {"4H": {
            "direction": "BULLISH", "active_boundary": 1.20,
            "last_checked_candle": "old",
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Bearish (LH+LL)", "Bearish (LH+LL)",
            {"bias": "Bullish"}, {"bias": "Bullish"}, {"bias": "Bullish"},
        )
        self.assertIsNone(state["invalidation_history"]["4H"]["last_invalidation"])


    def test_same_broken_boundary_is_not_relogged_on_later_candles(self):
        frame = self._frame(close=1.19)
        previous_event = {
            "from": "BULLISH", "to": "BEARISH", "boundary": 1.20,
            "close": 1.18, "at": "2026-10-07 00:00 UTC",
            "confirmation": "4H candle close",
        }
        state = {"invalidation_history": {"4H": {
            "direction": "BULLISH", "active_boundary": 1.20,
            "last_invalidation": previous_event,
            "last_checked_candle": "older candle",
        }}}
        build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Bearish (LH+LL)", "Bearish (LH+LL)",
            {"bias": "Bullish"}, {"bias": "Bullish"}, {"bias": "Bullish"},
        )
        self.assertEqual(
            state["invalidation_history"]["4H"]["last_invalidation"],
            previous_event,
        )

    def test_scalper_confidence_rewards_bearish_alignment(self):
        score, note = calculate_scalper_confidence(
            {"bias": "BEARISH"}, {"bias": "BEARISH"}, {"bias": "BEARISH"},
            38, {"dist_pct": 43.5}, True, True,
        )
        self.assertEqual(score, 90)
        self.assertEqual(note, "")

    def test_scalper_confidence_is_direction_symmetric(self):
        bullish, _ = calculate_scalper_confidence(
            {"bias": "BULLISH"}, {"bias": "BULLISH"}, {"bias": "BULLISH"},
            60, {"dist_pct": 43.5}, True, True,
        )
        bearish, _ = calculate_scalper_confidence(
            {"bias": "BEARISH"}, {"bias": "BEARISH"}, {"bias": "BEARISH"},
            40, {"dist_pct": 43.5}, True, True,
        )
        self.assertEqual(bullish, bearish)

    def test_scalper_confidence_penalizes_missing_lower_timeframe_data(self):
        full, _ = calculate_scalper_confidence(
            {"bias": "BEARISH"}, {"bias": "BEARISH"}, {"bias": "BEARISH"},
            38, {"dist_pct": 43.5}, True, True,
        )
        partial, note = calculate_scalper_confidence(
            {"bias": "BEARISH"}, {"bias": "No Data"}, {"bias": "No Data"},
            38, {"dist_pct": 43.5}, False, False,
        )
        self.assertLess(partial, full)
        self.assertIn("data unavailable", note)


    def test_mixed_scalper_labels_five_minute_bullish_as_countertrend_to_bearish_hourly(self):
        result = scalper_outlook(
            {"bias": "BEARISH"}, {"bias": "NEUTRAL"}, {"bias": "BULLISH"},
            45, {"dist_pct": 43.5}, 35,
        )
        self.assertEqual(result["headline"], "MIXED — SHORT-TERM")
        self.assertEqual(result["five"], "Bullish — countertrend to 1H")

    def test_mixed_scalper_labels_five_minute_bearish_as_countertrend_to_bullish_hourly(self):
        result = scalper_outlook(
            {"bias": "BULLISH"}, {"bias": "NEUTRAL"}, {"bias": "BEARISH"},
            55, {"dist_pct": 43.5}, 35,
        )
        self.assertEqual(result["five"], "Bearish — countertrend to 1H")

    def test_mixed_higher_timeframe_structure_has_moderate_strength(self):
        score = calculate_market_confidence(
            {"dist_pct": 43.5}, 54, "Ranging/Choppy — Sideways", "Bearish (LH+LL) — Downtrend",
        )
        self.assertEqual(score, 58)

    def test_market_confidence_rewards_aligned_structure_in_either_direction(self):
        bullish = calculate_market_confidence(
            {"dist_pct": 43.5}, 60, "Bullish (HH+HL)", "Bullish (HH+HL)",
        )
        bearish = calculate_market_confidence(
            {"dist_pct": 43.5}, 40, "Bearish (LH+LL)", "Bearish (LH+LL)",
        )
        self.assertEqual(bullish, bearish)


class CandleIntegrityTests(unittest.TestCase):
    @staticmethod
    def _hourly_frame():
        index = pd.date_range("2026-01-01", periods=48, freq="1h", tz="UTC")
        frame = pd.DataFrame({
            "open": [1.00] * 48,
            "high": [1.01] * 48,
            "low": [0.99] * 48,
            "close": [1.00] * 48,
            "volume": [10.0] * 48,
        }, index=index)
        return frame

    def test_four_hour_ohlcv_matches_four_hourly_source_candles(self):
        hourly = self._hourly_frame()
        four_hour = hourly.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        result = validate_4h_candle_integrity(hourly, four_hour)
        self.assertEqual(result["status"], "OK")
        self.assertIn("matches four hourly candles", result["note"])

    def test_large_four_hour_move_is_flagged_not_silently_discarded(self):
        hourly = self._hourly_frame()
        hourly.iloc[-4:, hourly.columns.get_loc("close")] = 0.94
        hourly.iloc[-4:, hourly.columns.get_loc("low")] = 0.93
        four_hour = hourly.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        result = validate_4h_candle_integrity(hourly, four_hour, volume_ratio=0.11)
        self.assertEqual(result["status"], "FLAG")
        self.assertAlmostEqual(result["move_pct"], -6.0, places=2)
        self.assertIn("0.11x average", result["note"])
        self.assertIn("check source data", result["note"])

    def test_missing_hourly_source_candle_marks_four_hour_bar_unverified(self):
        hourly = self._hourly_frame().drop(index=pd.Timestamp("2026-01-02 21:00", tz="UTC"))
        four_hour = hourly.resample("4h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        result = validate_4h_candle_integrity(hourly, four_hour)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("source coverage incomplete", result["note"])

    def test_four_hour_volume_uses_last_completed_candle(self):
        now_bucket = pd.Timestamp.now(tz="UTC").floor("4h")
        index = pd.date_range(now_bucket - pd.Timedelta(hours=84), periods=22, freq="4h")
        frame = pd.DataFrame({
            "open": [1.0] * 22, "high": [1.1] * 22,
            "low": [0.9] * 22, "close": [1.0] * 22,
            "volume": [10.0] * 21 + [1.0],
        }, index=index)
        result = volume_analysis(frame, interval_minutes=240)
        self.assertEqual(result["ratio"], 1.0)
        self.assertEqual(result["last"], 10.0)
        self.assertEqual(result["basis"], "last completed candle")

    def test_unverified_4h_data_does_not_update_daily_or_4h_break_history(self):
        frame = self._frame(close=1.09)
        state = {"invalidation_history": {
            "Daily": {"direction": "RANGE", "active_range_low": 1.20,
                      "active_range_high": 1.60, "last_checked_candle": "old"},
            "4H": {"direction": "RANGE", "active_range_low": 1.10,
                   "active_range_high": 1.60, "last_checked_candle": "old"},
        }}
        text = build_invalidation_status(
            state, frame, frame, frame, frame, frame,
            "Ranging/Choppy — Sideways", "Ranging/Choppy — Sideways",
            {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"}, {"bias": "NEUTRAL"},
            data_integrity={"status": "UNVERIFIED", "note": "hourly source mismatch"},
        )
        self.assertNotIn("last_range_break", state["invalidation_history"]["Daily"])
        self.assertNotIn("last_range_break", state["invalidation_history"]["4H"])
        self.assertEqual(state["invalidation_history"]["Daily"]["last_checked_candle"], "old")
        self.assertIn("candle integrity UNVERIFIED", text)

    def test_neutral_position_outlook_labels_active_break_and_extension(self):
        history = {
            "Daily": {"last_range_break": {"direction": "DOWNSIDE", "boundary": 1.24}},
            "4H": {"last_range_break": {"direction": "DOWNSIDE", "boundary": 1.23}},
        }
        result = position_outlook(
            "Ranging/Choppy — Sideways", "Ranging/Choppy — Sideways",
            12, {"dist_pct": 12.7}, 50, price=1.15,
            invalidation_history=history,
        )
        self.assertEqual(
            result["headline"],
            "NEUTRAL BIAS — ACTIVE DAILY DOWNSIDE BREAK / 4H DOWNSIDE BREAK — EXTENDED",
        )


if __name__ == "__main__":
    unittest.main()
