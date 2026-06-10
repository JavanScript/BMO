import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bmo-web"))

from bmo_web.games.wordle import Mark, WordleGame, score_guess


class WordleScoringTest(unittest.TestCase):
    def test_scores_exact_letters(self) -> None:
        result = score_guess("cider", "cider")

        self.assertEqual(result.marks, (Mark.EXACT,) * 5)
        self.assertTrue(result.solved)

    def test_scores_present_letters(self) -> None:
        result = score_guess("cider", "crane")

        self.assertEqual(
            result.marks,
            (Mark.EXACT, Mark.PRESENT, Mark.ABSENT, Mark.ABSENT, Mark.PRESENT),
        )

    def test_handles_duplicate_letters(self) -> None:
        result = score_guess("cider", "eerie")

        self.assertEqual(
            result.marks,
            (Mark.PRESENT, Mark.ABSENT, Mark.PRESENT, Mark.PRESENT, Mark.ABSENT),
        )


class WordleGameTest(unittest.TestCase):
    def test_solving_ends_game(self) -> None:
        game = WordleGame(answer="cider")

        reply = game.guess("@ada:example.org", "cider")

        self.assertTrue(reply.ended)
        self.assertTrue(game.ended)
        self.assertIn("Solved", reply.message)

    def test_six_wrong_guesses_end_game(self) -> None:
        game = WordleGame(answer="cider")

        for guess in ("adieu", "brave", "fjord", "glade", "honey"):
            reply = game.guess("@ada:example.org", guess)
            self.assertFalse(reply.ended)

        reply = game.guess("@ada:example.org", "karma")

        self.assertTrue(reply.ended)
        self.assertIn("CIDER", reply.message)


if __name__ == "__main__":
    unittest.main()

