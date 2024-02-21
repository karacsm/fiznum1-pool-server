import socket
import time
import json
import pooltool as pt
import threading
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

class ConnectionClosedMessage(Message):
    def __init__(self):
        super().__init__(MessageCode.ConnectionClosed, {})

class LoginMessage(Message):
    def __init__(self, secret: str = ''):
        data = {'secret' : secret}
        super().__init__(MessageCode.Login, data)

    @property
    def secret(self) -> str:
        return self.data['secret']

class LoginSuccessMessage(Message):
    def __init__(self, player_id: str, secret: str):
        data = {'player_id' : player_id, 'secret' : secret}
        super().__init__(MessageCode.LoginSuccess, data)

    @property
    def player_id(self) -> str:
        return self.data['player_id']

    @property
    def secret(self) -> str:
        return self.data['secret']

class LoginFailedMessage(Message):
    def __init__(self, reason: str):
        data = {'reason' : reason}
        super().__init__(MessageCode.LoginFailed, data)

    @property
    def reason(self):
        return self.data['reason']

class YourTurnMessage(Message):
    def __init__(self, system: System, shot_constraints: ShotConstraints, break_shot: bool):
        data = {'system' : pt.serialize.conversion.converters['json'].unstructure(system),
                'shot_constraints' : pt.serialize.conversion.converters['json'].unstructure(shot_constraints),
                'break_shot' : break_shot}
        super().__init__(MessageCode.YourTurn, data)

    @property
    def system(self):
        raw = self.data['system']
        return pt.serialize.conversion.converters['json'].structure(raw, System)
    
    @property
    def shot_constraints(self):
        raw = self.data['shot_constraints']
        return pt.serialize.conversion.converters['json'].structure(raw, ShotConstraints)

    @property
    def break_shot(self):
        return self.data['break_shot']

class MakeShotMessage(Message):
    def __init__(self, cue: Cue, cue_ball_pos: BallPosition = None, shot_call: ShotCall = None):
        data = {'cue' : pt.serialize.conversion.converters['json'].unstructure(cue)}
        if cue_ball_pos is not None:
            data['cue_ball_pos'] = cue_ball_pos.__dict__
        if shot_call is not None:
            data['shot_call'] = shot_call.__dict__
        super().__init__(MessageCode.MakeShot, data)

    @property
    def cue(self):
        raw = self.data['cue']
        return pt.serialize.conversion.converters['json'].structure(raw, Cue)

    @property
    def cue_ball_pos(self):
        if 'cue_ball_pos' in self.data.keys():
            return BallPosition(**self.data['cue_ball_pos'])
        else:
            return None

    @property
    def shot_call(self):
        if 'shot_call' in self.data.keys():
            return ShotCall(**self.data['shot_call'])
        else:
            return None

class GameOverMessage(Message):
    def __init__(self, winner: str, scores: dict):
        super().__init__(MessageCode.GameOver, {'winner' : winner, 'scores' : scores})
    
    @property
    def winner(self):
        return self.data['winner']

    @property
    def scores(self):
        return self.data['scores']

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
        return dmsg
    except Exception: #catch almost everything to protect againts invalid messages
        raise InvalidMessageError(msg)

def send_msg(conn: socket.socket, msg: Message, timeout: Optional[float] = None):
    conn.settimeout(timeout)
    data = msg.encode()
    header = int.to_bytes(len(data), _MSG_HEADER_SIZE, byteorder=_MSG_HEADER_BYTEORDER)
    msg = header + data
    conn.sendall(msg)

def _receive_nbytes(conn: socket.socket, bytecount: int, maxbufsize) -> bytes:
    remaining_length = bytecount
    data = b''
    while remaining_length > 0:
        if remaining_length <= maxbufsize:
            bufsize = remaining_length
        else:
            bufsize = maxbufsize
        data_chunk = conn.recv(bufsize)
        if len(data_chunk) == 0:
            raise ConnectionClosedError('Connection closed before all bytes received!')
        remaining_length = remaining_length - len(data_chunk)
        data = data + data_chunk
    return data

def receive_msg(conn: socket.socket, maxbufsize: int = 1024, timeout: Optional[float] = None) -> Message:
    try:
        conn.settimeout(timeout)
        header = _receive_nbytes(conn, _MSG_HEADER_SIZE, maxbufsize)
        msg_len = int.from_bytes(bytes = header, byteorder=_MSG_HEADER_BYTEORDER)
        msg = _receive_nbytes(conn, msg_len, maxbufsize)
        return decode_msg(msg)
    except ConnectionClosedError:
        return ConnectionClosedMessage()

#performs async socket IO
class MessageBuffer:
    def __init__(self, conn: socket.socket, update_freq: int = 200):
        assert(conn.getblocking() == False)
        self._conn = conn
        self._rec_buffer: bytes = b''
        self._send_buffer: bytes = b''
        self._bufsize = 4096
        self._update_freq = update_freq
        self._access_lock = threading.Lock()
        self._exit_event = threading.Event()
        self._disconnected = threading.Event()
        self._thread = threading.Thread(target=self._run, args = ())
        self._thread.start()

    def _run(self):
        while not self._exit_event.is_set():
            with self._access_lock:
                try:
                    data = self._conn.recv(self._bufsize)
                    self._rec_buffer += data
                    if len(data) == 0:
                        self._disconnected.set()
                        break
                except BlockingIOError:
                    pass

                try:
                    if len(self._send_buffer):
                        sent = self._conn.send(self._send_buffer[:self._bufsize])
                        self._send_buffer = self._send_buffer[sent:]
                except BlockingIOError:
                    pass
            time.sleep(1/self._update_freq)

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

