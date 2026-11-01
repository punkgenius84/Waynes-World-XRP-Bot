import unittest

import pandas as pd

from combined_xrp_intel_report import (
    _format_swing_pair, _last_swing_dates, _range_width_pct, _recent_daily_range,
    _structural_validation_lines, _uniq_levels, build_invalidation_status,
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
            ["Daily range boundary: 4H close below $1.2400 breaks lower / above $1.6500 breaks higher"],
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


if __name__ == "__main__":
    unittest.main()


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
