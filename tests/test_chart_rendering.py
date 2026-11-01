import os
import tempfile
import unittest

import numpy as np
import pandas as pd
from PIL import Image

from combined_xrp_intel_report import render_single_timeframe_chart


class ChartRenderingTests(unittest.TestCase):
    def test_chart_dependencies_import(self):
        import mplfinance  # noqa: F401
        from PIL import Image as PillowImage  # noqa: F401

    def test_single_timeframe_chart_is_rendered_as_png(self):
        index = pd.date_range("2026-01-01", periods=90, freq="D", tz="UTC")
        trend = np.linspace(1.0, 1.35, len(index))
        wave = np.sin(np.arange(len(index)) / 4.0) * 0.015
        close = trend + wave
        open_ = close - 0.004
        high = np.maximum(open_, close) + 0.012
        low = np.minimum(open_, close) - 0.012
        frame = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": np.linspace(1000, 2500, len(index)),
            },
            index=index,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "xrp_daily.png")
            rendered = render_single_timeframe_chart(frame, "XRP — Daily", output)

            self.assertEqual(rendered, output)
            self.assertTrue(os.path.isfile(output))
            self.assertGreater(os.path.getsize(output), 10_000)
            with Image.open(output) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreater(image.width, 500)
                self.assertGreater(image.height, 300)


if __name__ == "__main__":
    unittest.main()
