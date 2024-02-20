import argparse
import logging
import socket
import sys
import time
import numpy as np
import pooltool as pt
from pooltool import System
from pooltool.game.ruleset.datatypes import ShotConstraints

from modules import msgutil
from modules.poolgame import BallPosition, ShotCall

#make random shot for testing
#modify this function to create your own pool playing bot
def calculate_shot(system: System, shot_constraints: ShotConstraints, break_shot: bool = False):
    cue = pt.Cue()
    ball_id = np.random.choice(shot_constraints.hittable)
    V0 = np.random.uniform(1, 2)
    a = np.random.uniform(-0.3, 0.3)
    b = np.random.uniform(-0.3, 0.3)
    theta = np.random.uniform(0, 45)
    cue.set_state(V0 = V0, a = a, b = b, theta = theta)
    cut = np.random.uniform(-45, 45)
    if break_shot: #deal with break
        cue.set_state(V0 = 4)
        noise = np.random.normal(0, 1.0)
        cue.set_state(phi = pt.aim.at_pos(system, np.array([system.table.w / 2, system.table.l * 3 / 4, 0])) + noise)
    else:
        cue.set_state(phi = pt.aim.at_ball(system, ball_id, cut = cut))
    cue_ball_pos: Optional[BallPosition] = None #TODO deal with ball-in-hand
    shot_call: Optional[ShotCall] = None #TODO call shots for eightball
    return cue, cue_ball_pos, shot_call

def handle_msg(conn: socket.socket, msg: msgutil.Message):
    if isinstance(msg, msgutil.ConnectionClosedMessage):
        raise KeyboardInterrupt #exit
    elif isinstance(msg, msgutil.YourTurnMessage):
        cue, cue_ball_pos, shot_call = calculate_shot(msg.system, msg.shot_constraints, msg.break_shot)
        logging.debug(cue)
        msgutil.send_msg(conn, msgutil.MakeShotMessage(cue, cue_ball_pos=cue_ball_pos, shot_call=shot_call))
    elif isinstance(msg, msgutil.GameOverMessage):
        logging.info('Match over!')
        logging.info(f'Winner: {msg.winner}. Scores: {str(msg.scores)}.')
        raise KeyboardInterrupt
    else:
        logging.error('Unexpected message!')

def main_loop(conn: socket.socket, update_freq: int = 10):
    while True:
        try:
            msg = msgutil.receive_msg(conn, timeout=0.1)
            handle_msg(conn, msg)
        except msgutil.InvalidMessageError:
            logging.error('Invaild message!')
        except socket.timeout:
            pass
        time.sleep(1/update_freq)

def main(args):
    logging.basicConfig(stream=sys.stdout,
                        format='[%(asctime)s] %(levelname)s: %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', level = args.log_level)
    try:
        conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        conn.connect((args.address, args.port))
        msgutil.send_msg(conn, msgutil.LoginMessage(args.secret))
        msg = msgutil.receive_msg(conn)
        if isinstance(msg, msgutil.LoginSuccessMessage):
            logging.info(f'Connected as {msg.player_id}. Secret: {msg.secret}. Use this secret to reconnect!')
            main_loop(conn)
        elif isinstance(msg, msgutil.LoginFailedMessage):
            logging.info(f'Failed to login! {msg.reason}')
    except KeyboardInterrupt:
        logging.info('Interrupted! Shutting down . . .')
    finally:
        conn.close()

if __name__ == '__main__':
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

    parser.add_argument('-s', '--secret',
                        default = '',
                        metavar = 'UUID',
                        type    = str,
                        dest    = 'secret',
                        help    = 'Login secret for authentication.')

    args = parser.parse_args()
    main(args)
