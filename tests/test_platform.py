from __future__ import annotations

import unittest

from galgame_mcp.platform import _bgrx_to_rgba


class PlatformBufferTests(unittest.TestCase):
    def test_bgrx_to_rgba_swaps_channels_and_uses_opaque_alpha(self) -> None:
        bgrx = bytes((3, 2, 1, 0, 40, 50, 60, 17))
        self.assertEqual(_bgrx_to_rgba(bgrx), bytearray((1, 2, 3, 255, 60, 50, 40, 255)))


if __name__ == "__main__":
    unittest.main()
