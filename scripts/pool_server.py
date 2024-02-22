import argparse
import logging
import sys
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, List
from enum import Enum
import pooltool as pt

from modules import msgutil
from modules.msgutil import MessageBuffer
from modules import poolgame

@dataclass
class Address:
    ipv4: str
    port: int

    @property
    def astuple(self):
        return (self.ipv4, self.port)

    def __str__(self):
        return f'{self.ipv4}/{self.port}'

class _Connection:
    def __init__(self, sock: socket.socket, raddr: Address, update_freq: int = 200):
        self.name = ''
        self.type = msgutil.ConnectionType.UNKNOWN
        self.sock = sock
        self.raddr = raddr
        self.buffer = MessageBuffer(sock, update_freq)

    def close(self):
        self.buffer.stop()
        self.sock.close()

class ConnectionHandler:
    def __init__(self, addr: Address, view_mode = False, backlog = 3, max_players = 2, max_viewers = 1):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setblocking(False)
        self._sock.bind(addr.astuple)
        self._sock.listen(backlog)
        self._addr = Address(*self._sock.getsockname())
        self._registered_client_identities: Dict[str, uuid.UUID] = {}
        self._registered_client_types: Dict[str, msgutil.ConnectionType] = {}
        self._client_counts = {msgutil.ConnectionType.PLAYER : 0, msgutil.ConnectionType.VIEWER : 0}
        self._client_limits = {msgutil.ConnectionType.PLAYER : max_players, msgutil.ConnectionType.VIEWER : max_viewers if view_mode else 0}
        self._view_mode = view_mode

    @property
    def address(self):
        return self._addr

    def _auth_client(self, conn: _Connection, msg: msgutil.LoginMessage) -> Optional[_Connection]:
        if msg.player_id in self._registered_client_identities.keys():
            if (self._registered_client_identities[msg.player_id] == msg.secret) and (self._registered_client_types[msg.player_id] == msg.conn_type):
                conn.buffer.push_msg(msgutil.LoginSuccessMessage(msg.player_id, msg.secret))
                conn.name = msg.player_id
                conn.type = msg.conn_type
                return conn
        
        conn.buffer.push_msg(msgutil.LoginFailedMessage('Invalid login!'))
        conn.close()
        return None

    def _register_client(self, conn: _Connection, msg: msgutil.LoginMessage) -> Optional[_Connection]:
        if self._client_counts[msg.conn_type] < self._client_limits[msg.conn_type]:
            if not (msg.player_id in self._registered_client_identities.keys()):
                secret = uuid.uuid4()
                self._registered_client_identities[msg.player_id] = secret
                self._registered_client_types[msg.player_id] = msg.conn_type
                self._client_counts[msg.conn_type] += 1
                conn.buffer.push_msg(msgutil.LoginSuccessMessage(msg.player_id, secret))
                conn.name = msg.player_id
                conn.type = msg.conn_type
                return conn
            else:
                conn.buffer.push_msg(msgutil.LoginFailedMessage('Name already in use!'))
                conn.close()
                return None
        else:
            conn.buffer.push_msg(msgutil.LoginFailedMessage('Server full!'))
            conn.close()
            return None

    def _handle_login(self, conn: _Connection, msg: msgutil.LoginMessage) -> Optional[_Connection]:
        if msg.secret is not None:
            return self._auth_client(conn, msg)
        else:
            return self._register_client(conn, msg)

    def _handle_connection(self, conn: _Connection) -> Optional[_Connection]:
        try:
            msg = conn.buffer.await_msg()
        except TimeoutError:
            conn.close()
            return None

        if isinstance(msg, msgutil.ConnectionClosedMessage):
            conn.close()
            return None
        elif isinstance(msg, msgutil.LoginMessage):
            return self._handle_login(conn, msg)
        else:
            logging.info('Unexpected message from connection!')
            conn.close()
            return None

    def poll_connection(self) -> Optional[_Connection]:
        try:
            conn_sock, raddr = self._sock.accept()
        except BlockingIOError:
            return None
        conn_sock.setblocking(False)
        conn = _Connection(conn_sock, Address(*raddr))
        return self._handle_connection(conn)

    def shutdown(self):
        self._sock.close()
        
    def __enter__(self):
        return self

    def __exit__(self, exc_val, exc_type, traceback):
        self.shutdown()

class MatchState(Enum):
    WaitingForPlayers = 0
    ReadyForNextMove = 1
    WaitingForNextMove = 2
    MatchOver = 3

class MatchServer:
    def __init__(self, addr: Address, race_to: int = 10, view_mode: bool = False):
        self._game_count = 0
        self._addr = addr
        self._race_to = race_to
        self._match = None
        self._state = MatchState.WaitingForPlayers
        self._player_connections: Dict[str, _Connection] = {}
        self._viewer_connections: Dict[str, _Connection] = {}
        self._view_mode = view_mode

    def _remove_player_connection(self, name: str):
        self._player_connections[name].close()
        del self._player_connections[name]

    def _remove_viewer_connection(self, name: str):
        self._viewer_connections[name].close()
        del self._viewer_connections[name]

    def _add_player_connection(self, conn: _Connection):
        if conn.name in self._player_connections.keys():
            self._remove_player_connection(conn.name)
        logging.info(f'Player {conn.name} connected!')
        self._player_connections[conn.name] = conn
        assert(len(self._player_connections) <= 2)

    def _add_viewer_connection(self, conn: _Connection):
        if conn.name in self._viewer_connections.keys():
            self._remove_viewer_connection(conn.name)
        logging.info(f'Viewer {conn.name} connected!')
        self._viewer_connections[conn.name] = conn
        assert(len(self._viewer_connections) <= 2)

    def _accept_connection(self, conn):
        if conn is not None:
            if conn.type == msgutil.ConnectionType.PLAYER:
                self._add_player_connection(conn)
            elif conn.type == msgutil.ConnectionType.VIEWER:
                self._add_viewer_connection(conn)
            else:
                logging.info('Unexpected connection! Dropping connection!')
                conn.close()

    def _stage_waiting_for_players(self, handler: ConnectionHandler):
        conn = handler.poll_connection()
        self._accept_connection(conn)
        proceed = len(self._player_connections) == 2
        if self._view_mode:
            proceed = proceed and (len(self._viewer_connections) > 0)
        if proceed:
            if self._match is None:
                self._match = poolgame.PoolMatch(pt.GameType.NINEBALL,
                                                    list(self._player_connections.keys()),
                                                    self._race_to)
                names = list(self._player_connections.keys())
                logging.info(f'Starting match. {names[0]} vs. {names[1]}')
                logging.info(f'The first player to win {self._race_to} games wins the match.')
            self._state = MatchState.ReadyForNextMove

    def _stage_ready_for_next_move(self):
        player = self._match.active_player_name()
        self._player_connections[player].buffer.push_msg(msgutil.YourTurnMessage(self._match.get_system(),
                                                                                    self._match.get_shot_constraints(),
                                                                                    self._match.is_break()))
        self._state = MatchState.WaitingForNextMove

    def _send_broad_cast_message(self, shot_info, system, break_shot):
        for name in list(self._viewer_connections.keys()):
            conn = self._viewer_connections[name]
            msg = conn.buffer.pop_msg()
            if msg is not None:
                if isinstance(msg, msgutil.ConnectionClosedMessage):
                    self._remove_viewer_connection(name)
                    self._state = MatchState.WaitingForPlayers
                    logging.info(f'Viewer {name} disconnected.')
            else:
                conn.buffer.push_msg(msgutil.BroadcastMessage(system,
                                                              shot_info,
                                                              break_shot,
                                                              self._match._scores))
                if self._view_mode:
                    time.sleep(self._match.get_system().t + 2)

    def _stage_waiting_for_next_move(self):
        player = self._match.active_player_name()
        msg = self._player_connections[player].buffer.pop_msg()
        if msg is not None:
            if isinstance(msg, msgutil.ConnectionClosedMessage):
                logging.info(f'Player {player} disconnected!')
                self._remove_player_connection(player)
                self._state = MatchState.WaitingForPlayers
                logging.info('Waiting for players . . .')
            elif isinstance(msg, msgutil.MakeShotMessage):
                break_shot = self._match.is_break()
                shot_info, system = self._match.make_shot(msg.cue, msg.cue_ball_pos, msg.shot_call)
                logging.debug(shot_info)
                if self._match.is_match_over():
                    self._state = MatchState.MatchOver
                else:
                    if self._match.is_break():
                        self._game_count += 1
                        logging.info(f'Game {self._game_count} finished. Current standing: {self._match._scores}.')
                    self._state = MatchState.ReadyForNextMove
                self._send_broad_cast_message(shot_info, system, break_shot)
            else:
                logging.info('Unexpected message!')

    def _stage_match_over(self):
        logging.info('Match over!')
        logging.info(f'{self._match.match_winner()} won!. Final score: {self._match._scores}.')
        for name in self._player_connections.keys():
            winner = self._match.match_winner()
            self._player_connections[name].buffer.push_msg(msgutil.GameOverMessage(winner, self._match._scores))
            self._player_connections[name].close()
        raise KeyboardInterrupt

    def _update(self, handler: ConnectionHandler):
        #Waiting for players
        if self._state == MatchState.WaitingForPlayers:
            self._stage_waiting_for_players(handler)
        #Ready for next move
        elif self._state == MatchState.ReadyForNextMove:
            self._stage_ready_for_next_move()
        #Waiting for next move
        elif self._state == MatchState.WaitingForNextMove:
            self._stage_waiting_for_next_move()
        #Match over
        elif self._state == MatchState.MatchOver:
            self._stage_match_over()
        
    def main_loop(self, update_freq: int = 20):
        try:
            with ConnectionHandler(self._addr, self._view_mode) as handler:
                self._addr = handler.address
                logging.info(f'Listening on address {handler.address}.')
                logging.info('Waiting for players . . .')
                while True:
                    self._update(handler)
                    time.sleep(1/update_freq)
        except KeyboardInterrupt:
            logging.info('Shutting down . . .')

    def shutdown(self):
        for conn in self._player_connections.values():
            conn.close()
        for conn in self._viewer_connections.values():
            conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_val, exc_type, traceback):
        self.shutdown()

def main(args):
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', level = args.log_level)

    addr = Address(args.address, args.port)
    with MatchServer(addr, args.race_to, args.view_mode) as server:
        server.main_loop()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--log-level',
                        default = 'INFO',
                        choices = ['DEBUG', 'INFO', 'WARNING'],
                        type    = str,
                        dest    = 'log_level',
                        help    = 'Set logging level. Default setting is INFO.')

    parser.add_argument('-a', '--address',
                        default = '0.0.0.0',
                        metavar = 'X.X.X.X',
                        type    = str,
                        dest    = 'address',
                        help    = 'Set server address (IPv4). Defaults to 0.0.0.0 (INADDR_ANY).')

    parser.add_argument('-p', '--port',
                        default = 0,
                        metavar = 'PORT',
                        type    = int,
                        dest    = 'port',
                        help    = 'Set server port. Defaults to 0 (automatic port assignment).')
    
    parser.add_argument('--race-to',
                        default = 10,
                        metavar = 'SCORE',
                        type    = int,
                        dest    = 'race_to',
                        help    = 'The first player to reach this score wins the match. Default is 10.')

    parser.add_argument('-v',
                        default = False,
                        action  = 'store_true',
                        dest    = 'view_mode',
                        help    = 'Set view mode.')

    args = parser.parse_args()
    main(args)
