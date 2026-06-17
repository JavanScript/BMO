from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from random import SystemRandom
from typing import Any, Iterable

from .base import GameInfo, GameReply, JsonDict


WORD_LENGTH = 5
MAX_GUESSES = 6

DEFAULT_ANSWERS = (
    "adieu",
    "brave",
    "cider",
    "delta",
    "ember",
    "fjord",
    "glade",
    "honey",
    "ivory",
    "jolly",
    "karma",
    "lemon",
    "mango",
    "noble",
    "ocean",
    "piano",
    "quilt",
    "raven",
    "solar",
    "tango",
    "umbra",
    "vivid",
    "waltz",
    "xenon",
    "yacht",
    "zesty",
)


class Mark(Enum):
    EXACT = "!"
    PRESENT = "?"
    ABSENT = "."


@dataclass(frozen=True)
class GuessResult:
    guess: str
    marks: tuple[Mark, ...]

    def render(self) -> str:
        return " ".join(
            f"{letter.upper()}{mark.value}" for letter, mark in zip(self.guess, self.marks)
        )

    @property
    def solved(self) -> bool:
        return all(mark is Mark.EXACT for mark in self.marks)

    def to_state(self) -> JsonDict:
        return {
            "guess": self.guess,
            "marks": [mark.name for mark in self.marks],
        }

    @classmethod
    def from_state(cls, state: JsonDict) -> "GuessResult":
        return cls(
            guess=str(state["guess"]),
            marks=tuple(Mark[str(mark)] for mark in state["marks"]),
        )


def score_guess(answer: str, guess: str) -> GuessResult:
    answer = answer.lower()
    guess = guess.lower()
    marks = [Mark.ABSENT] * len(answer)
    unmatched = Counter()

    for index, answer_letter in enumerate(answer):
        if guess[index] == answer_letter:
            marks[index] = Mark.EXACT
        else:
            unmatched[answer_letter] += 1

    for index, guess_letter in enumerate(guess):
        if marks[index] is Mark.EXACT:
            continue
        if unmatched[guess_letter] > 0:
            marks[index] = Mark.PRESENT
            unmatched[guess_letter] -= 1

    return GuessResult(guess=guess, marks=tuple(marks))


class WordleFactory:
    info = GameInfo(
        key="wordle",
        title="Wordle",
        description="Guess a five-letter word in six tries.",
        min_players=1,
    )

    def __init__(self, answers: Iterable[str] = DEFAULT_ANSWERS) -> None:
        self._answers = tuple(_normalize_word(word) for word in answers)
        if not self._answers:
            raise ValueError("Wordle requires at least one answer.")

    def create(self, players: list[str] | None = None) -> "WordleGame":
        del players
        return WordleGame(answer=SystemRandom().choice(self._answers))

    def load(self, state: JsonDict) -> "WordleGame":
        return WordleGame.from_state(state)


@dataclass
class WordleGame:
    answer: str
    guesses: list[GuessResult] | None = None
    ended: bool = False
    key: str = "wordle"

    def __post_init__(self) -> None:
        self.answer = _normalize_word(self.answer)
        self.guesses = list(self.guesses or [])

    @property
    def board(self) -> str:
        return "\n".join(result.render() for result in self.guesses)

    def handle_action(self, player_id: str, action: str, payload: JsonDict) -> GameReply:
        if action != "guess":
            raise ValueError(f"Unsupported Wordle action: {action}")
        return self.guess(player_id=player_id, text=str(payload.get("guess", "")))

    @property
    def solved(self) -> bool:
        guesses = self.guesses or []
        return bool(guesses) and guesses[-1].solved

    def serialize_public(self, player_id: str | None = None) -> JsonDict:
        del player_id
        guesses = self.guesses or []
        return {
            "board": self.board,
            "rows": [
                {
                    "guess": result.guess.upper(),
                    "marks": [mark.name for mark in result.marks],
                }
                for result in guesses
            ],
            "guess_count": len(guesses),
            "max_guesses": MAX_GUESSES,
            "word_length": WORD_LENGTH,
            "ended": self.ended,
            "solved": self.solved,
            "answer": self.answer.upper() if self.ended else None,
        }

    def to_state(self) -> JsonDict:
        return {
            "answer": self.answer,
            "guesses": [guess.to_state() for guess in self.guesses or []],
            "ended": self.ended,
        }

    @classmethod
    def from_state(cls, state: JsonDict) -> "WordleGame":
        return cls(
            answer=str(state["answer"]),
            guesses=[
                GuessResult.from_state(_ensure_dict(guess))
                for guess in state.get("guesses", [])
            ],
            ended=bool(state.get("ended", False)),
        )

    def guess(self, player_id: str, text: str) -> GameReply:
        del player_id
        if self.ended:
            return GameReply("This game is already over.", ended=True)

        cleaned = text.lower().strip()
        if not _is_valid_guess(cleaned):
            raise ValueError("Guesses must be exactly five alphabetic letters.")

        result = score_guess(self.answer, cleaned)
        self.guesses = self.guesses or []
        self.guesses.append(result)

        if result.solved:
            self.ended = True
            return GameReply(f"Solved in {len(self.guesses)}/{MAX_GUESSES}!", ended=True)

        if len(self.guesses) >= MAX_GUESSES:
            self.ended = True
            return GameReply(f"Game over. The word was {self.answer.upper()}.", ended=True)

        return GameReply(f"{len(self.guesses)}/{MAX_GUESSES} guesses used.")


def _normalize_word(word: str) -> str:
    normalized = word.lower().strip()
    if not _is_valid_guess(normalized):
        raise ValueError(f"Invalid Wordle word: {word!r}")
    return normalized


def _is_valid_guess(word: str) -> bool:
    return len(word) == WORD_LENGTH and word.isalpha() and word.isascii()


def _ensure_dict(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        raise ValueError("Invalid Wordle state")
    return value
