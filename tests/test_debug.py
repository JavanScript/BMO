import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.games.debug import run_debug
from bmo_web.games.hokm import HokmFactory
from bmo_web.games.wordle import WordleFactory


class RunDebugTest(unittest.TestCase):
    def test_hokm_plays_to_completion(self) -> None:
        players = [f"@debug-{i}" for i in range(4)]

        log = run_debug(HokmFactory(), players)

        # A full Hokm match is hundreds of plays; the old spectator-view bug
        # stalled it after ~2 steps because per-player fields were empty.
        self.assertGreater(len(log), 50)
        self.assertTrue(log[-1].get("ended"))

    def test_wordle_plays_to_completion(self) -> None:
        log = run_debug(WordleFactory(), ["@debug-0"])

        self.assertGreater(len(log), 1)
        self.assertTrue(log[-1].get("ended"))


if __name__ == "__main__":
    unittest.main()
