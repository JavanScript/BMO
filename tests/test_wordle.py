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


class WordleSerializeTest(unittest.TestCase):
    def test_hides_answer_while_in_progress(self) -> None:
        game = WordleGame(answer="cider")
        game.guess("@ada:example.org", "crane")

        data = game.serialize_public()

        self.assertFalse(data["ended"])
        self.assertFalse(data["solved"])
        self.assertIsNone(data["answer"])
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["guess"], "CRANE")

    def test_reveals_answer_after_loss(self) -> None:
        game = WordleGame(answer="cider")
        for guess in ("adieu", "brave", "fjord", "glade", "honey", "karma"):
            game.guess("@ada:example.org", guess)

        data = game.serialize_public()

        self.assertTrue(data["ended"])
        self.assertFalse(data["solved"])
        self.assertEqual(data["answer"], "CIDER")

    def test_marks_solved_state(self) -> None:
        game = WordleGame(answer="cider")
        game.guess("@ada:example.org", "cider")

        data = game.serialize_public()

        self.assertTrue(data["solved"])
        self.assertEqual(data["answer"], "CIDER")


if __name__ == "__main__":
    unittest.main()

