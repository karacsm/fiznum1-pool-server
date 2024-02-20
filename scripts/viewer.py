import pooltool as pt
from pooltool.ani import tasks
from pooltool.ani.camera.states import camera_states
from pooltool.ani.camera import cam
from pooltool.ani.mouse import mouse, MouseMode
from pooltool.ani.globals import Global
import socket
import argparse
import time

class ServerViewer(pt.ShotViewer):
    def __init__(self, address):
        super().__init__()
        tasks.add(self.set_camera, 'set_camera')
        self.address = address
    
    def show(self, shots, title, cam_state):
        self.cam_state = cam_state
        pt.ShotViewer.show(self, shots, title)
        
    def set_camera(self, task):
        cam.load_state(self.cam_state)
        return task.done

def get_initial_system(game_type: pt.GameType) -> pt.System:
    table = pt.Table.from_game_type(game_type)
    balls = pt.get_rack(game_type = game_type, table = table, params=None, ballset=None, spacing_factor=1e-3)
    cue = pt.Cue()
    system = pt.System(cue, table, balls)
    return system

def main(args):
    system = get_initial_system(pt.GameType.NINEBALL)
    print(system.balls['1'].params.u_sp)
    system.strike(V0 = 12.0, phi = pt.aim.at_ball(system, '1'), b = 0.0)
    pt.simulate(system, inplace=True)
    viewer = ServerViewer(('127.0.0.1', 0))
    viewer.show(system, "Test title", camera_states['7_foot_overhead_zoom'])

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
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
    args = parser.parse_args()
    main(args)