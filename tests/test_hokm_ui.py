import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.app import _hokm_html


class HokmHtmlTest(unittest.TestCase):
    def test_active_hokm_ui_renders_last_trick_fallback(self) -> None:
        html = _hokm_html("session")

        self.assertIn("function visibleTrick", html)
        self.assertIn("data.last_trick", html)
        self.assertIn(".play.winner", html)
        self.assertIn('" winner"', html)


if __name__ == "__main__":
    unittest.main()
