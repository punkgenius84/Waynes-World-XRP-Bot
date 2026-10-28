import unittest

import pandas as pd

from combined_xrp_intel_report import _format_swing_pair, _last_swing_dates, _uniq_levels


class KeyLevelTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
