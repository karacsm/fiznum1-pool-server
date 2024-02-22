import socket
import argparse
import simplepbr
import logging
import sys
import uuid
from modules import msgutil
from enum import Enum

import pooltool as pt
import pooltool.ani as ani
from direct.showbase.ShowBase import ShowBase
from pooltool.system.render import SystemController, PlaybackMode
from pooltool.ani.environment import Environment

def get_initial_system(game_type: pt.GameType) -> pt.System:
    table = pt.Table.from_game_type(game_type)
    balls = pt.get_rack(game_type = game_type, table = table, params=None, ballset=None, spacing_factor=1e-3)
    cue = pt.Cue(theta=0)
    system = pt.System(cue, table, balls)
    return system

class ViewerState(Enum):
    WaitingForConnection = 0
    ConnectionPending = 1
    Viewing = 2

class Viewer(ShowBase):
    def __init__(self, address, name, secret):
        super().__init__()
        simplepbr.init(enable_shadows=ani.settings["graphics"]["shadows"], max_lights=13)
        self.address = address
        self.system = get_initial_system(pt.GameType.NINEBALL)
        self.system.strike(V0 = 3, phi=pt.aim.at_ball(self.system, '1'))
        self.render.attach_new_node('scene')
        self.controller = SystemController()
        self.controller.attach_system(self.system)
        self.controller.buildup()
        self.env = Environment()
        self.env.init(self.system.table)
        self.camLens.set_near(0.1)
        self.camLens.set_fov(53)
        self.cam.set_pos((self.system.table.w/4, self.system.table.l / 2, 2.2))
        self.cam.look_at((*self.system.table.center, 0))
        self.update_time = 0.01
        self.task_mgr.doMethodLater(self.update_time, self.update, 'update')
        self.state = ViewerState.WaitingForConnection
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(False)
        self.buffer: msgutil.MessageBuffer
        self.name = name
        if secret is not None:
            self.secret = uuid.UUID(hex = secret)
        else:
            self.secret = None
        self.exitFunc = self.exit

    def update(self, task):
        #waiting for connection
        if self.state == ViewerState.WaitingForConnection:
            try:
                self.sock.connect(self.address)
                self.buffer = msgutil.MessageBuffer(self.sock, run=False)
                self.buffer.push_msg(msgutil.LoginMessage(self.name, secret=self.secret, conn_type=msgutil.ConnectionType.VIEWER))
                self.state = ViewerState.ConnectionPending
            except BlockingIOError:
                pass
        #connection pending
        elif self.state == ViewerState.ConnectionPending:
            self.buffer.update()
            msg = self.buffer.pop_msg()
            if msg is not None:
                if isinstance(msg, msgutil.ConnectionClosedMessage):
                    logging.info('Server disconnected!')
                    return task.done
                elif isinstance(msg, msgutil.LoginSuccessMessage):
                    logging.info(f'Connected! Secret: {msg.secret}')
                    self.state = ViewerState.Viewing
                elif isinstance(msg, msgutil.LoginFailedMessage):
                    logging.info(f'Failed to connect! {msg.reason}')
                    return task.done
                else:
                    logging.warning('Unexpected message!')
        #viewing
        elif self.state == ViewerState.Viewing:
            self.buffer.update()
            msg = self.buffer.pop_msg()
            if msg is not None:
                if isinstance(msg, msgutil.ConnectionClosedMessage):
                    logging.info('Server disconnected!')
                    return task.done
                elif isinstance(msg, msgutil.BroadcastMessage):
                    del self.system
                    logging.info(msg.shot_info)
                    logging.info(msg.scores)
                    self.system = msg.system
                    self.controller.attach_system(self.system)
                    self.controller.buildup()
                    self.controller.build_shot_animation()
                    self.controller.animate()
                    self.controller.advance_to_end_of_stroke()
                else:
                    logging.warning('Unexpected message!')
        else:
            raise NotImplementedError('Unkown state!')
        return task.again

    def exit(self):
        self.sock.close()

def main(args):
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', level = args.log_level)
    viewer = Viewer((args.address, args.port), args.name, args.secret)
    viewer.run()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--log-level',
                        default = 'INFO',
                        choices = ['DEBUG', 'INFO', 'WARNING'],
                        type    = str,
                        dest    = 'log_level',
                        help    = 'Set logging level. Default setting is INFO.')

    parser.add_argument('-a', '--address',
                        metavar  = 'X.X.X.X',
                        type     = str,
                        dest     = 'address',
                        required = True,
                        help     = 'Set remote server address (IPv4). Required.')

    parser.add_argument('-p', '--port',
                        metavar  = 'PORT',
                        type     = int,
                        dest     = 'port',
                        required = True,
                        help     = 'Set remote server port. Required.')

    parser.add_argument('-n', '--name',
                        metavar  = 'NAME',
                        type     = str,
                        dest     = 'name',
                        required = True,
                        help     = 'Set user name. Required.')

    parser.add_argument('-s', '--secret',
                        default = None,
                        metavar = 'UUID',
                        type    = str,
                        dest    = 'secret',
                        help    = 'Login secret for authentication.')

    args = parser.parse_args()
    main(args)