import random
import numpy as np
import pooltool as pt
from dataclasses import dataclass
from pooltool.game.ruleset.datatypes import ShotConstraints
from typing import Optional, List, Tuple

def get_initial_system(game_type: pt.GameType) -> pt.System:
    table = pt.Table.from_game_type(game_type)
    balls = pt.get_rack(game_type = game_type, table = table, params=None, ballset=None, spacing_factor=1e-3)
    cue = pt.Cue()
    system = pt.System(cue, table, balls)
    return system

@dataclass
class BallPosition:
    x: float
    y: float

@dataclass
class ShotCall:
    ball_id: str
    pocket_id: str

class PoolGame:
    def __init__(self, game_type: pt.GameType, P1_name: str, P2_name: str):
        assert(P1_name != P2_name)
        self._history = pt.MultiSystem()
        self._ruleset = pt.get_ruleset(game_type)([pt.Player(P1_name), pt.Player(P2_name)])
        self._system = get_initial_system(game_type)

    def active_player_name(self) -> str:
        return self._ruleset.active_player.name

    def make_shot(self, cue: pt.Cue, cue_ball_spot: Optional[BallPosition] = None, call: Optional[ShotCall] = None):
        if self.is_game_over():
            return
        self._system.cue = cue
        if (cue_ball_spot is not None) and ('cue' in self._ruleset.shot_constraints.movable):
            ball_in_hand = self._ruleset.shot_constraints.ball_in_hand
            if ball_in_hand == pt.game.ruleset.datatypes.BallInHandOptions.BEHIND_LINE:
                if cue_ball_spot.y <= self._system.table.l / 4:
                    pt.game.ruleset.utils.respot(self._system, 'cue', cue_ball_spot.x, cue_ball_spot.y)
            elif ball_in_hand == pt.game.ruleset.datatypes.BallInHandOptions.ANYWHERE:
                pt.game.ruleset.utils.respot(self._system, 'cue', cue_ball_spot.x, cue_ball_spot.y)

        if call is None:
            self._ruleset.shot_constraints.call_shot = False
        else:
            self._ruleset.shot_constraints.ball_call = call.ball_id
            self._ruleset.shot_constraints.pocket_call = call.pocket_id
        pt.simulate(self._system, inplace=True)
        self._ruleset.process_and_advance(self._system)
        self._history.append(self._system.copy())
        return self._ruleset.shot_info, self._system.copy()

    def get_shot_constraints(self) -> ShotConstraints:
        return self._ruleset.shot_constraints

    def get_system(self) -> pt.System:
        return self._system

    def is_game_over(self) -> bool:
        if len(self._history) == 0:
            return False
        else:
            return self._ruleset.shot_info.game_over

    def is_break(self) -> bool:
        return self._ruleset.shot_number == 0

    def winner(self) -> Optional[str]:
        if not self.is_game_over():
            return None
        return self._ruleset.shot_info.winner.name

    def save(self, path):
        self._history.save(path)

class PoolMatch:
    def __init__(self, game_type: pt.GameType, player_names: List[str], max_score: int = 10):
        assert(len(player_names) == 2)
        self._game_type = game_type
        self._player_names = player_names
        self._player_name_to_idx = {name : i for i, name in enumerate(player_names)}
        self._scores = {name : 0 for name in player_names}
        self._max_score = max_score
        self._last_winner = None
        self._current_game = PoolGame(game_type, *PoolMatch._random_break_assignment(player_names))
        self._match_over = False
        self._match_winner: Optional[str] = None

    def _other(self, player_name: str):
        player_idx = self._player_name_to_idx[player_name]
        other_idx = (player_idx + 1) % 2
        return self._player_names[other_idx]

    def _update_score(self, game: PoolGame):
        winner = game.winner()
        if winner is None:
            #tie, or game is not over
            return
        else:
            self._scores[winner] += 1

    def _random_break_assignment(player_names) -> Tuple[str, str]:
        return tuple(np.random.choice(player_names, len(player_names), replace = False))

    def _current_max_score(self) -> int:
        return np.max(list(self._scores.values()))

    def _start_new_game(self):
        self._last_winner = self._current_game.winner()
        winner = self._last_winner
        if winner is None:
            self._current_game = PoolGame(game_type, *PoolMatch._random_break_assignment(self._player_names))
        else:
            other = self._other(winner)
            self._current_game = PoolGame(self._game_type, winner, other)

    def _update(self):
        if self._current_game.is_game_over():
            self._update_score(self._current_game)
            if self._current_max_score() < self._max_score:
                self._start_new_game()
            else:
                self._match_over = True
                self._match_winner = self._current_game.winner()
    
    def active_player_name(self) -> str:
        return self._current_game.active_player_name()

    def is_match_over(self) -> bool:
        return self._match_over

    def match_winner(self) -> str:
        return self._match_winner

    def get_system(self) -> pt.System:
        return self._current_game.get_system()

    def get_shot_constraints(self) -> ShotConstraints:
        return self._current_game.get_shot_constraints()

    def is_break(self) -> bool:
        return self._current_game.is_break()

    def make_shot(self, cue: pt.Cue, cue_ball_spot: Optional[BallPosition] = None, call: Optional[ShotCall] = None):
        if not self._match_over:
            info, system = self._current_game.make_shot(cue, cue_ball_spot, call)
            self._update()
            return info, system
            

