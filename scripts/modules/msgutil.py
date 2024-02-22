import socket
import time
import json
import uuid
import pooltool as pt
import threading
import select
from pooltool import System, Cue
from pooltool.game.ruleset.datatypes import ShotConstraints
from .poolgame import ShotCall, BallPosition
from enum import IntEnum
from dataclasses import dataclass
from typing import Optional, List

_MSG_HEADER_SIZE = 4
_MSG_HEADER_BYTEORDER = 'little'

_MSG_CODE_SIZE = 1
_MSG_CODE_BYTEORDER = 'little'

class ConnectionClosedError(Exception):
    pass

class InvalidMessageError(Exception):
    def __init__(self, msg: bytes = b''):
        self.msg = msg
        super().__init__()

class MessageCode(IntEnum):
    ConnectionClosed = 0
    Login = 1
    LoginSuccess = 2
    LoginFailed = 3
    YourTurn = 4
    MakeShot = 5
    GameOver = 6

@dataclass
class Message:
    code: MessageCode
    data: dict

    def encode(self):
        encoded_msg_code = int.to_bytes(int(self.code), length=_MSG_CODE_SIZE, byteorder=_MSG_CODE_BYTEORDER)
        return encoded_msg_code + json.dumps(self.data).encode('utf-8')

    def _decode_data(self):
        raise NotImplementedError(f'{self} instance must implement _decode_data!')

class ConnectionClosedMessage(Message):
    def __init__(self):
        super().__init__(MessageCode.ConnectionClosed, None)

    def _decode_data(self):
        pass

class LoginMessage(Message):
    def __init__(self, player_id: str, secret: Optional[uuid.UUID] = None):
        self.player_id = player_id
        self.secret = secret
        data = {'player_id' : player_id}
        if secret is not None:
            data['secret'] = secret.hex
        super().__init__(MessageCode.Login, data)

    def _decode_data(self):
        self.player_id = self.data['player_id']
        if 'secret' in self.data.keys():
            self.secret = uuid.UUID(hex = self.data['secret'])
        else:
            self.secret = None

class LoginSuccessMessage(Message):
    def __init__(self, player_id: str, secret: uuid.UUID):
        self.player_id = player_id
        self.secret = secret
        data = {'player_id' : player_id, 'secret' : secret.hex}
        super().__init__(MessageCode.LoginSuccess, data)

    def _decode_data(self):
        self.player_id = self.data['player_id']
        self.secret = uuid.UUID(hex = self.data['secret'])

class LoginFailedMessage(Message):
    def __init__(self, reason: str):
        self.reason = reason
        data = {'reason' : reason}
        super().__init__(MessageCode.LoginFailed, data)

    def _decode_data(self):
        self.reason = self.data['reason']

class YourTurnMessage(Message):
    def __init__(self, system: System, shot_constraints: ShotConstraints, break_shot: bool):
        self.system = system
        self.shot_constraints = shot_constraints
        self.break_shot = break_shot
        data = {'system' : pt.serialize.conversion.converters['json'].unstructure(system),
                'shot_constraints' : pt.serialize.conversion.converters['json'].unstructure(shot_constraints),
                'break_shot' : break_shot}
        super().__init__(MessageCode.YourTurn, data)

    def _decode_data(self):
        raw_system = self.data['system']
        raw_shot_constraints = self.data['shot_constraints']
        self.system = pt.serialize.conversion.converters['json'].structure(raw_system, System)
        self.shot_constraints = pt.serialize.conversion.converters['json'].structure(raw_shot_constraints, ShotConstraints)
        self.break_shot = self.data['break_shot']

class MakeShotMessage(Message):
    def __init__(self, cue: Cue, cue_ball_pos: BallPosition = None, shot_call: ShotCall = None):
        self.cue = cue
        self.cue_ball_pos = cue_ball_pos
        self.shot_call = shot_call
        data = {'cue' : pt.serialize.conversion.converters['json'].unstructure(cue)}
        if cue_ball_pos is not None:
            data['cue_ball_pos'] = cue_ball_pos.__dict__
        if shot_call is not None:
            data['shot_call'] = shot_call.__dict__
        super().__init__(MessageCode.MakeShot, data)

    def _decode_data(self):
        raw_cue = self.data['cue']
        self.cue = pt.serialize.conversion.converters['json'].structure(raw_cue, Cue)
        if 'cue_ball_pos' in self.data.keys():
            self.cue_ball_pos = BallPosition(**self.data['cue_ball_pos'])
        else:
            self.cue_ball_pos = None
        if 'shot_call' in self.data.keys():
            self.shot_call = ShotCall(**self.data['shot_call'])
        else:
            self.shot_call = None

class GameOverMessage(Message):
    def __init__(self, winner: str, scores: dict):
        self.winner = winner
        self.scores = scores
        super().__init__(MessageCode.GameOver, {'winner' : winner, 'scores' : scores})

    def _decode_data(self):
        self.winner = self.data['winner']
        self.scores = self.data['scores']

_message_translation_dict = {
    MessageCode.ConnectionClosed : ConnectionClosedMessage,
    MessageCode.Login : LoginMessage,
    MessageCode.LoginSuccess : LoginSuccessMessage,
    MessageCode.LoginFailed : LoginFailedMessage,
    MessageCode.YourTurn : YourTurnMessage,
    MessageCode.MakeShot : MakeShotMessage,
    MessageCode.GameOver : GameOverMessage
}

def decode_msg(msg: bytes) -> Message:
    try:
        code = MessageCode(int.from_bytes(bytes = msg[:_MSG_CODE_SIZE], byteorder=_MSG_CODE_BYTEORDER))
        data = json.loads(msg[_MSG_CODE_SIZE:])
        dmsg = Message(code, data)
        dmsg.__class__ = _message_translation_dict[dmsg.code]
        dmsg._decode_data()
        return dmsg
    except Exception: #catch almost everything to protect againts invalid messages
        raise InvalidMessageError(msg)

class MessageBuffer:
    """
    Performs async IO on non-blocking socket.

    Retreive messages from buffer using peek_msg(), pop_msg() or await_msg().
    Send message with push_msg()
    Call stop() before closing the socket.
    """
    def __init__(self, conn: socket.socket, update_freq: int = 200, run: bool = True):
        assert(conn.getblocking() == False)
        self._conn = conn
        self._rec_buffer: bytes = b''
        self._send_buffer: bytes = b''
        self._bufsize = 4096
        self._update_freq = update_freq
        self._access_lock = threading.Lock()
        self._exit_event = threading.Event()
        self._disconnected = threading.Event()
        if run:
            self._thread = threading.Thread(target=self._run, args = ())
            self._thread.start()

    def _run(self):
        while True:
            rlst, wlst, _ = select.select([self._conn], [self._conn], [])
            with self._access_lock:
                if rlst:
                    data = rlst[0].recv(self._bufsize)
                    self._rec_buffer += data
                    if len(data) == 0:
                        self._disconnected.set()
                        break
                if wlst and len(self._send_buffer) > 0:
                    sent = wlst[0].send(self._send_buffer[:self._bufsize])
                    self._send_buffer = self._send_buffer[sent:]
                elif self._exit_event.is_set():
                    break
            time.sleep(1/self._update_freq)

    #manual buffer update
    def update(self):
        with self._access_lock:
            try:
                data = self._conn.recv(self._bufsize)
                self._rec_buffer += data
                if len(data) == 0:
                    self._disconnected.set()
            except BlockingIOError:
                pass

            if len(self._send_buffer) > 0:
                try:
                    sent = self._conn.send(self._send_buffer[:self._bufsize])
                    self._send_buffer = self._send_buffer[sent:]
                except BlockingIOError:
                    pass

    def _ret_no_available_msg(self) -> Optional[Message]:
        return ConnectionClosedMessage() if self._disconnected.is_set() else None

    def peek_msg(self) -> Optional[Message]:
        with self._access_lock:
            header = self._rec_buffer[:_MSG_HEADER_SIZE]
            if len(header) < _MSG_HEADER_SIZE:
                return self._ret_no_available_msg()
            msg_len = int.from_bytes(header, byteorder=_MSG_HEADER_BYTEORDER)
            msg = self._rec_buffer[_MSG_HEADER_SIZE:_MSG_HEADER_SIZE + msg_len]
            if msg_len > len(msg):
                return self._ret_no_available_msg()
            return decode_msg(msg)

    def pop_msg(self) -> Optional[Message]:
        with self._access_lock:
            header = self._rec_buffer[:_MSG_HEADER_SIZE]
            if len(header) < _MSG_HEADER_SIZE:
                return self._ret_no_available_msg()
            msg_len = int.from_bytes(header, byteorder=_MSG_HEADER_BYTEORDER)
            msg = self._rec_buffer[_MSG_HEADER_SIZE:_MSG_HEADER_SIZE + msg_len]
            if msg_len > len(msg):
                return self._ret_no_available_msg()
            self._rec_buffer = self._rec_buffer[_MSG_HEADER_SIZE + msg_len:]
            return decode_msg(msg)

    def await_msg(self, peek: bool = False, timeout: int = 60) -> Message:
        msg_func = self.peek_msg if peek else self.pop_msg
        start_time = time.time()
        while time.time() - start_time < timeout:
            msg = msg_func()
            if msg is not None:
                return msg
            time.sleep(1/self._update_freq)
        raise TimeoutError

    def push_msg(self, msg: Message):
        with self._access_lock:
            data = msg.encode()
            header = int.to_bytes(len(data), _MSG_HEADER_SIZE, byteorder=_MSG_HEADER_BYTEORDER)
            encoded_msg = header + data
            self._send_buffer += encoded_msg

    def stop(self):
        self._exit_event.set()
        self._thread.join()

