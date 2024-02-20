import argparse
import logging
import sys
import socket
import time
import uuid
import pooltool as pt
import json
import threading
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum

from modules import msgutil
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

class Server:
    def __init__(self, addr: Address, backlog = 3):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setblocking(False)
        self._sock.bind(addr.astuple)
        self._sock.listen(backlog) 
        self._addr = Address(*self._sock.getsockname())
        self._connections: Dict[uuid.UUID, socket.socket] = {}

    @property
    def address(self):
        return self._addr

    def check_connection(self, conn_uuid: uuid.UUID):
        return conn_uuid in self._connections.keys()

    def accept_connection(self) -> Optional[uuid.UUID]:
        try:
            conn, cli_addr = self._sock.accept()
            logging.debug(f'Client connected from address {str(Address(*cli_addr))}.')
            conn_uuid = uuid.uuid4()
            self._connections[conn_uuid] = conn
            return conn_uuid
        except BlockingIOError:
            return None

    def close_connection(self, conn_uuid: uuid.UUID):
        self._connections[conn_uuid].close()
        del self._connections[conn_uuid]
    
    def send_msg_to(self, conn_uuid: uuid.UUID, msg: msgutil.Message, timeout: Optional[float] = None):
        msgutil.send_msg(self._connections[conn_uuid], msg, timeout=timeout)

    def receive_msg_from(self, conn_uuid: uuid.UUID, timeout: Optional[float] = None) -> msgutil.Message:
        return msgutil.receive_msg(self._connections[conn_uuid], timeout=timeout)

    def shutdown(self):
        for conn_uuid in self._connections:
            self._connections[conn_uuid].close()
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, traceback):
        self.shutdown()

@dataclass
class PlayerClient:
    name: str
    conn_uuid: uuid.UUID
    connected: bool = True

class GameState:
    WaitingForPlayers = 1
    ReadyForNextMove = 2
    WaitingForNextMove = 3
    MatchOver = 4

class MatchServer:
    def __init__(self, addr: Address, max_score: int = 10):
        self._addr = addr
        self._player_names = ['P1', 'P2']
        self._match = poolgame.PoolMatch(pt.GameType.NINEBALL, self._player_names, max_score)
        self._available_names = self._player_names.copy()
        self._player_identities: Dict[str, str] = {}       #name to secret
        self._player_clients: Dict[str, PlayerClient] = {} #secret to client
        self._game_state = GameState.WaitingForPlayers
        self._player_transmission_lock = threading.Lock()
        self._update_lock = threading.Lock()

    def _full(self) -> bool:
        return len(self._available_names) == 0

    def _connected(self, name: str) -> bool:
        if name in self._player_identities.keys():
            secret = self._player_identities[name]
            client = self._player_clients[secret]
            return client.connected
        else:
            return False

    def _disconnect_player(self, server: Server, name: str):
        if name in self._player_identities.keys():
            secret = self._player_identities[name]
            client = self._player_clients[secret]
            client.connected = False
            if server.check_connection(client.conn_uuid):
                server.close_connection(client.conn_uuid)

    def _get_player_conn_uuid(self, name: str):
        secret = self._player_identities[name]
        return self._player_clients[secret].conn_uuid

    def _auth_connection(self, server: Server, conn_uuid: uuid.UUID, secret: str):
        if secret in self._player_identities.values():
            if server.check_connection(self._player_clients[secret].conn_uuid):
                server.close_connection(self._player_clients[secret].conn_uuid)
            self._player_clients[secret].conn_uuid = conn_uuid
            self._player_clients[secret].connected = True
            server.send_msg_to(conn_uuid, msgutil.LoginSuccessMessage(self._player_clients[secret].name, secret))
            logging.info(f'{self._player_clients[secret].name} reconnected.')
            if (self._game_state == GameState.WaitingForNextMove) and (self._player_clients[secret].name == self._match.active_player_name()):
                self._game_state = GameState.ReadyForNextMove
        else:
            server.send_msg_to(conn_uuid, msgutil.LoginFailedMessage('Invalid login!'))
            server.close_connection(conn_uuid)

    def _register_player(self, server: Server, conn_uuid: uuid.UUID):
        if not self._full():
            name = self._available_names.pop(0)
            secret = conn_uuid.hex
            self._player_identities[name] = secret
            self._player_clients[secret] = PlayerClient(name, conn_uuid)
            server.send_msg_to(conn_uuid, msgutil.LoginSuccessMessage(name, secret))
            logging.info(f'{name} logged in.')
        else:
            server.send_msg_to(conn_uuid, msgutil.LoginFailedMessage('Server full!'))
            server.close_connection(conn_uuid)

    def _handle_login_request(self, server: Server, conn_uuid: uuid.UUID, msg: msgutil.LoginMessage):
        with self._update_lock:
            if len(msg.secret) > 0:
                self._auth_connection(server, conn_uuid, msg.secret)
            else:
                self._register_player(server, conn_uuid)

    def _handle_connection(self, server: Server, conn_uuid: uuid.UUID):
        try:
            msg = server.receive_msg_from(conn_uuid, timeout = 60)
            if isinstance(msg, msgutil.LoginMessage):
                self._handle_login_request(server, conn_uuid, msg)
            else:
                server.close_connection(conn_uuid)
        except socket.timeout:
            logging.warning('Connection timed out! Dropping connection!')
            server.close_connection(conn_uuid)
        except msgutil.InvalidMessageError:
            logging.error('Failed to decode message!')
            server.close_connection(conn_uuid)
        if len(server._connections) > 2:
            logging.error('Connection leaked!')

    def _send_move_request(self, server: Server):
        conn_uuid = self._get_player_conn_uuid(self._match.active_player_name())
        msg = msgutil.YourTurnMessage(self._match.get_system(),
                                      self._match.get_shot_constraints(),
                                      self._match.is_break())
        server.send_msg_to(conn_uuid, msg)

    def _attempt_player_move(self, server):
        try:
            player_name = self._match.active_player_name()
            conn_uuid = self._get_player_conn_uuid(player_name)
            msg = server.receive_msg_from(conn_uuid, timeout = 0.1)
            if isinstance(msg, msgutil.ConnectionClosedMessage):
                self._disconnect_player(server, player_name)
                logging.info(f'{player_name} disconnected!')
                self._game_state = GameState.WaitingForPlayers
            elif isinstance(msg, msgutil.MakeShotMessage):
                #TODO validate shot request
                if self._match.is_break():
                    logging.info(f'Startin new game! Current standing: {self._match._scores}')
                    logging.info(f'{self._match.active_player_name()} breaks.')
                self._match.make_shot(msg.cue, msg.cue_ball_pos, msg.shot_call)
                if self._match.is_match_over():
                    self._game_state = GameState.MatchOver
                else:
                    self._game_state = GameState.ReadyForNextMove
            else:
                logging.warining('Unexpected message!')
        except msgutil.InvalidMessageError:
            logging.error('Failed to decode message!')
        except socket.timeout:
            return

    def _update(self, server: Server):
        with self._update_lock:
            #Waiting for players
            if self._game_state == GameState.WaitingForPlayers:
                connected = True
                for name in self._player_names:
                    connected = connected and self._connected(name)
                if self._full() and connected:
                    self._game_state = GameState.ReadyForNextMove
            #Ready for next move
            elif self._game_state == GameState.ReadyForNextMove:
                self._send_move_request(server)
                self._game_state = GameState.WaitingForNextMove
            #Waiting for next move
            elif self._game_state == GameState.WaitingForNextMove:
                self._attempt_player_move(server)
            #Game over
            elif self._game_state == GameState.MatchOver:
                logging.info(f'Match is over! {self._match.match_winner()} won! Scores: {str(self._match._scores)}')
                for name in self._player_names:
                    conn_uuid = self._get_player_conn_uuid(name)
                    if self._connected(name):
                        server.send_msg_to(conn_uuid, msgutil.GameOverMessage(self._match.match_winner(), self._match._scores))
                        self._disconnect_player(server, name)
                raise KeyboardInterrupt #exit
                
    def _serve_forever(self, server: Server, update_freq: int = 10):
        while True:
            conn_uuid = server.accept_connection()
            if conn_uuid is not None:
                connection_handler = threading.Thread(target=self._handle_connection, args=(server, conn_uuid))
                connection_handler.start()
            self._update(server)
            time.sleep(1/update_freq)
            
    def main_loop(self):
        try:
            with Server(self._addr) as server:
                logging.info(f'Listening on port {server.address.port} . . .')
                self._serve_forever(server)
        except KeyboardInterrupt:
            logging.info('Interrupted signal received! Shutting down . . .')
        
def main(args):
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', level = args.log_level)

    addr = Address(args.address, args.port)
    m = MatchServer(addr, args.race_to)
    m.main_loop()

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

    args = parser.parse_args()
    main(args)
