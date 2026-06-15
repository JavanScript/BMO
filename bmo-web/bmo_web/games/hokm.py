from __future__ import annotations

from dataclasses import dataclass
from random import SystemRandom
from typing import Iterable, Sequence

from .base import GameInfo, GameReply, JsonDict


SUITS = ("spades", "hearts", "diamonds", "clubs")
SUIT_SYMBOLS = {
    "spades": "♠",
    "hearts": "♥",
    "diamonds": "♦",
    "clubs": "♣",
}
RED_SUITS = {"hearts", "diamonds"}
RANKS = ("A", "K", "Q", "J", "10", "9", "8", "7", "6", "5", "4", "3", "2")
RANK_STRENGTH = {rank: len(RANKS) - index for index, rank in enumerate(RANKS)}
HAND_TRICKS_TO_WIN = 7
MATCH_POINTS_TO_WIN = 7
PLAYERS_REQUIRED = 4


@dataclass(frozen=True)
class Card:
    suit: str
    rank: str

    @property
    def card_id(self) -> str:
        return f"{self.suit}:{self.rank}"

    @property
    def label(self) -> str:
        return f"{self.rank}{SUIT_SYMBOLS[self.suit]}"

    def to_state(self) -> JsonDict:
        return {"suit": self.suit, "rank": self.rank}

    def serialize(self) -> JsonDict:
        return {
            "id": self.card_id,
            "suit": self.suit,
            "rank": self.rank,
            "symbol": SUIT_SYMBOLS[self.suit],
            "label": self.label,
            "color": "red" if self.suit in RED_SUITS else "black",
        }

    @classmethod
    def from_state(cls, state: JsonDict) -> "Card":
        suit = str(state["suit"])
        rank = str(state["rank"])
        if suit not in SUITS or rank not in RANKS:
            raise ValueError("Invalid Hokm card state")
        return cls(suit=suit, rank=rank)

    @classmethod
    def from_id(cls, card_id: str) -> "Card":
        try:
            suit, rank = card_id.split(":", 1)
        except ValueError as exc:
            raise ValueError("Invalid card id") from exc
        return cls.from_state({"suit": suit, "rank": rank})


@dataclass(frozen=True)
class PlayedCard:
    player_id: str
    card: Card

    def to_state(self) -> JsonDict:
        return {"player_id": self.player_id, "card": self.card.to_state()}

    def serialize(self) -> JsonDict:
        return {"player_id": self.player_id, "card": self.card.serialize()}

    @classmethod
    def from_state(cls, state: JsonDict) -> "PlayedCard":
        return cls(
            player_id=str(state["player_id"]),
            card=Card.from_state(_ensure_dict(state["card"])),
        )


class HokmFactory:
    info = GameInfo(
        key="hokm",
        title="Hokm / حکم",
        description="Four-player Iranian trick-taking with Hâkem and trump.",
        min_players=PLAYERS_REQUIRED,
        max_players=PLAYERS_REQUIRED,
    )

    def create(self, players: list[str] | None = None) -> "HokmGame":
        return HokmGame.new(players or [])

    def load(self, state: JsonDict) -> "HokmGame":
        return HokmGame.from_state(state)


@dataclass
class HokmGame:
    players: list[str]
    seats: list[str]
    team_of: dict[str, int]
    hakem: str
    hands: dict[str, list[Card]]
    undealt: list[Card]
    scores: dict[int, int]
    hand_tricks: dict[int, int]
    phase: str = "choose_trump"
    trump_suit: str | None = None
    current_turn: str | None = None
    current_trick: list[PlayedCard] | None = None
    last_trick: dict[str, object] | None = None
    last_hand: dict[str, object] | None = None
    winner_team: int | None = None
    hand_number: int = 0
    key: str = "hokm"

    @property
    def ended(self) -> bool:
        return self.phase == "finished"

    @classmethod
    def new(
        cls,
        players: Sequence[str],
        *,
        setup_deck: Iterable[Card] | None = None,
        deal_deck: Iterable[Card] | None = None,
    ) -> "HokmGame":
        clean_players = _validate_players(players)
        setup_cards = list(setup_deck or _shuffled_deck())
        hakem, partner = _select_hakem_partner(clean_players, setup_cards)
        seats = _seat_partners_opposite(clean_players, hakem=hakem, partner=partner)
        team_of = {
            seats[0]: 0,
            seats[2]: 0,
            seats[1]: 1,
            seats[3]: 1,
        }
        game = cls(
            players=clean_players,
            seats=seats,
            team_of=team_of,
            hakem=hakem,
            hands={player: [] for player in clean_players},
            undealt=[],
            scores={0: 0, 1: 0},
            hand_tricks={0: 0, 1: 0},
            current_trick=[],
        )
        game._start_hand(deck=list(deal_deck or _shuffled_deck()))
        return game

    def handle_action(self, player_id: str, action: str, payload: JsonDict) -> GameReply:
        if action == "choose_trump":
            return self.choose_trump(player_id, suit=str(payload.get("suit", "")))
        if action == "play_card":
            card_id = str(payload.get("card") or payload.get("card_id") or "")
            return self.play_card(player_id, card_id=card_id)
        raise ValueError(f"Unsupported Hokm action: {action}")

    def choose_trump(self, player_id: str, *, suit: str) -> GameReply:
        if self.phase != "choose_trump":
            raise ValueError("Trump has already been chosen.")
        if player_id != self.hakem:
            raise PermissionError("Only Hâkem can choose trump.")
        if suit not in SUITS:
            raise ValueError("Choose a valid trump suit.")

        self.trump_suit = suit
        self.phase = "play"
        self.current_turn = self.hakem

        for player in self.seats[1:]:
            self.hands[player].extend(_draw(self.undealt, 5))
        for _round in range(2):
            for player in self.seats:
                self.hands[player].extend(_draw(self.undealt, 4))

        return GameReply(f"Trump / حکم is {SUIT_SYMBOLS[suit]}.")

    def play_card(self, player_id: str, *, card_id: str) -> GameReply:
        if self.phase != "play":
            raise ValueError("Cards cannot be played right now.")
        if player_id != self.current_turn:
            raise PermissionError("It is not your turn.")

        card = Card.from_id(card_id)
        hand = self.hands.get(player_id)
        if hand is None or card not in hand:
            raise ValueError("That card is not in your hand.")

        led_suit = self._led_suit
        if led_suit and card.suit != led_suit and self._has_suit(player_id, led_suit):
            raise ValueError("You must follow suit.")

        hand.remove(card)
        self.current_trick = self.current_trick or []
        self.current_trick.append(PlayedCard(player_id=player_id, card=card))

        if len(self.current_trick) < PLAYERS_REQUIRED:
            self.current_turn = self._next_player(player_id)
            return GameReply(f"{card.label} played.")

        winner = self._resolve_trick_winner(self.current_trick)
        winning_team = self.team_of[winner]
        self.hand_tricks[winning_team] += 1
        self.last_trick = {
            "winner": winner,
            "team": winning_team,
            "cards": [play.serialize() for play in self.current_trick],
        }
        self.current_trick = []

        if self.hand_tricks[winning_team] >= HAND_TRICKS_TO_WIN:
            return self._finish_hand(winning_team)

        self.current_turn = winner
        return GameReply(f"{winner} won the trick.")

    def serialize_public(self, player_id: str | None = None) -> JsonDict:
        viewer_hand = self.hands.get(player_id or "", [])
        return {
            "phase": self.phase,
            "seats": self.seats,
            "teams": self._serialize_teams(),
            "hakem": self.hakem,
            "hakem_team": self.team_of[self.hakem],
            "trump_suit": self.trump_suit,
            "trump_symbol": SUIT_SYMBOLS.get(self.trump_suit or ""),
            "current_turn": self.current_turn,
            "current_trick": [
                play.serialize() for play in (self.current_trick or [])
            ],
            "last_trick": self.last_trick,
            "last_hand": self.last_hand,
            "hand_number": self.hand_number,
            "hand": [_card.serialize() for _card in _sorted_cards(viewer_hand)],
            "playable_card_ids": self._playable_card_ids(player_id),
            "can_choose_trump": self.phase == "choose_trump" and player_id == self.hakem,
            "trump_options": [
                {"suit": suit, "symbol": SUIT_SYMBOLS[suit]} for suit in SUITS
            ],
            "winner_team": self.winner_team,
            "ended": self.ended,
        }

    def to_state(self) -> JsonDict:
        return {
            "players": self.players,
            "seats": self.seats,
            "team_of": self.team_of,
            "hakem": self.hakem,
            "hands": {
                player: [card.to_state() for card in hand]
                for player, hand in self.hands.items()
            },
            "undealt": [card.to_state() for card in self.undealt],
            "scores": {str(team): score for team, score in self.scores.items()},
            "hand_tricks": {
                str(team): tricks for team, tricks in self.hand_tricks.items()
            },
            "phase": self.phase,
            "trump_suit": self.trump_suit,
            "current_turn": self.current_turn,
            "current_trick": [
                play.to_state() for play in (self.current_trick or [])
            ],
            "last_trick": self.last_trick,
            "last_hand": self.last_hand,
            "winner_team": self.winner_team,
            "hand_number": self.hand_number,
        }

    @classmethod
    def from_state(cls, state: JsonDict) -> "HokmGame":
        return cls(
            players=[str(player) for player in state["players"]],
            seats=[str(player) for player in state["seats"]],
            team_of={
                str(player): int(team)
                for player, team in _ensure_dict(state["team_of"]).items()
            },
            hakem=str(state["hakem"]),
            hands={
                str(player): [
                    Card.from_state(_ensure_dict(card)) for card in cards
                ]
                for player, cards in _ensure_dict(state["hands"]).items()
            },
            undealt=[
                Card.from_state(_ensure_dict(card))
                for card in state.get("undealt", [])
            ],
            scores=_team_totals(state.get("scores", {})),
            hand_tricks=_team_totals(state.get("hand_tricks", {})),
            phase=str(state.get("phase", "choose_trump")),
            trump_suit=state.get("trump_suit"),
            current_turn=state.get("current_turn"),
            current_trick=[
                PlayedCard.from_state(_ensure_dict(play))
                for play in state.get("current_trick", [])
            ],
            last_trick=_optional_dict(state.get("last_trick")),
            last_hand=_optional_dict(state.get("last_hand")),
            winner_team=_optional_team(state.get("winner_team")),
            hand_number=int(state.get("hand_number", 0)),
        )

    @property
    def _led_suit(self) -> str | None:
        if not self.current_trick:
            return None
        return self.current_trick[0].card.suit

    def _start_hand(
        self,
        *,
        deck: list[Card] | None = None,
        keep_last_trick: bool = False,
    ) -> None:
        self.hand_number += 1
        self.phase = "choose_trump"
        self.trump_suit = None
        self.current_turn = self.hakem
        self.current_trick = []
        if not keep_last_trick:
            self.last_trick = None
        self.hand_tricks = {0: 0, 1: 0}
        self.hands = {player: [] for player in self.players}
        self.undealt = list(deck or _shuffled_deck())
        self.hands[self.hakem].extend(_draw(self.undealt, 5))

    def _finish_hand(self, winning_team: int) -> GameReply:
        hakem_team = self.team_of[self.hakem]
        losing_team = _other_team(winning_team)
        losing_tricks = self.hand_tricks[losing_team]
        points = 1
        result = "normal"
        if losing_tricks == 0:
            if winning_team == hakem_team:
                points = 2
                result = "kot"
            else:
                points = 3
                result = "hakem_koti"

        previous_hakem = self.hakem
        self.scores[winning_team] += points
        self.last_hand = {
            "winner_team": winning_team,
            "hakem": previous_hakem,
            "hakem_team": hakem_team,
            "points": points,
            "result": result,
            "tricks": {str(team): tricks for team, tricks in self.hand_tricks.items()},
            "scores": {str(team): score for team, score in self.scores.items()},
        }

        if self.scores[winning_team] >= MATCH_POINTS_TO_WIN:
            self.phase = "finished"
            self.current_turn = None
            self.winner_team = winning_team
            return GameReply(f"Team {winning_team + 1} wins the match.", ended=True)

        if winning_team != hakem_team:
            self._advance_hakem()
        self._start_hand(keep_last_trick=True)
        return GameReply(
            f"Team {winning_team + 1} wins the hand for {points} point(s). "
            f"Next Hâkem / حاکم: {self.hakem}."
        )

    def _advance_hakem(self) -> None:
        new_hakem = self._next_player(self.hakem)
        while self.seats[0] != new_hakem:
            self.seats = [*self.seats[1:], self.seats[0]]
        self.hakem = new_hakem

    def _next_player(self, player_id: str) -> str:
        index = self.seats.index(player_id)
        return self.seats[(index + 1) % len(self.seats)]

    def _has_suit(self, player_id: str, suit: str) -> bool:
        return any(card.suit == suit for card in self.hands.get(player_id, []))

    def _resolve_trick_winner(self, plays: list[PlayedCard]) -> str:
        if not self.trump_suit:
            raise ValueError("Trump has not been chosen.")
        led_suit = plays[0].card.suit
        trump_plays = [play for play in plays if play.card.suit == self.trump_suit]
        candidates = trump_plays or [play for play in plays if play.card.suit == led_suit]
        winning_play = max(candidates, key=lambda play: RANK_STRENGTH[play.card.rank])
        return winning_play.player_id

    def _playable_card_ids(self, player_id: str | None) -> list[str]:
        if self.phase != "play" or player_id != self.current_turn:
            return []
        hand = self.hands.get(player_id or "", [])
        led_suit = self._led_suit
        if not led_suit or not self._has_suit(player_id or "", led_suit):
            return [card.card_id for card in hand]
        return [card.card_id for card in hand if card.suit == led_suit]

    def _serialize_teams(self) -> list[JsonDict]:
        return [
            {
                "id": team,
                "name": f"Team {team + 1}",
                "players": [
                    player for player in self.seats if self.team_of[player] == team
                ],
                "score": self.scores[team],
                "tricks": self.hand_tricks[team],
                "is_hakem_team": self.team_of[self.hakem] == team,
            }
            for team in (0, 1)
        ]


def _full_deck() -> list[Card]:
    return [Card(suit=suit, rank=rank) for suit in SUITS for rank in RANKS]


def _shuffled_deck() -> list[Card]:
    deck = _full_deck()
    SystemRandom().shuffle(deck)
    return deck


def _draw(deck: list[Card], count: int) -> list[Card]:
    if len(deck) < count:
        raise ValueError("The Hokm deck ran out of cards.")
    drawn = deck[:count]
    del deck[:count]
    return drawn


def _validate_players(players: Sequence[str]) -> list[str]:
    clean_players = [str(player) for player in players if str(player)]
    if len(clean_players) != len(set(clean_players)):
        raise ValueError("Hokm requires four unique players.")
    if len(clean_players) != PLAYERS_REQUIRED:
        raise ValueError("Hokm requires exactly four players.")
    return clean_players


def _select_hakem_partner(players: list[str], deck: list[Card]) -> tuple[str, str]:
    hakem: str | None = None
    player_index = 0
    card_index = 0
    while card_index < len(deck):
        player = players[player_index % len(players)]
        player_index += 1
        if hakem and player == hakem:
            continue

        card = deck[card_index]
        card_index += 1
        if card.rank != "A":
            continue
        if not hakem:
            hakem = player
            continue
        return hakem, player

    raise ValueError("Could not determine Hâkem and partner from the deck.")


def _seat_partners_opposite(
    players: list[str],
    *,
    hakem: str,
    partner: str,
) -> list[str]:
    after_hakem = _players_after(players, hakem)
    opponents = [player for player in after_hakem if player != partner]
    return [hakem, opponents[0], partner, opponents[1]]


def _players_after(players: list[str], player_id: str) -> list[str]:
    index = players.index(player_id)
    return [players[(index + offset) % len(players)] for offset in range(1, len(players))]


def _sorted_cards(cards: Iterable[Card]) -> list[Card]:
    suit_order = {suit: index for index, suit in enumerate(SUITS)}
    return sorted(
        cards,
        key=lambda card: (suit_order[card.suit], -RANK_STRENGTH[card.rank]),
    )


def _team_totals(value: object) -> dict[int, int]:
    data = _ensure_dict(value)
    return {0: int(data.get("0", data.get(0, 0))), 1: int(data.get("1", data.get(1, 0)))}


def _other_team(team: int) -> int:
    return 1 if team == 0 else 0


def _optional_team(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    return dict(_ensure_dict(value))


def _ensure_dict(value: object) -> JsonDict:
    if not isinstance(value, dict):
        raise ValueError("Invalid Hokm state")
    return value
