# Fiznum 1 Pool Server

Server for the fiznum1 AI pool contest. The server uses the [pooltool](https://github.com/ekiefl/pooltool) python package to simulate the physics of pool.

This repository contains scripts for running a pool match on a tcp socket server. (Only 9-ball is supported for now.)

### Starting the server

To start the pool server open a console in the `scripts` directory and type:

`python pool_server.py` or optionally `python pool_server.py -a <IPv4 address> -p <port number>` to specify the address of the server.

The server waits for player clients to join, then starts a match between the players. The first player to win `score` games wins the match (`score` can be specified by the `--race-to <score>` option. Default is 10). 

### Player Clients

Once the server has started you can connect player clients to the server. A test client, `scripts/test_bot.py` is provided as an example. To create your own client use test_bot.py as a template and modify the `calculate_shot()` function so that it makes more ellaborate calculations based on the positions of the balls and other information provided by the arguments of the function. 

To connect with a client (e.g. test_bot.py) type

`python test_bot.py -a <server address> -p <server port> -n <name of your choice>`. 

If you connected succesfully to the server, you will receive a secret which you can use to reconnect later, if you were to disconnect for some reason.

`python test_bot.py -a <server address> -p <server port> -n <name of your choice> -s <secret>`

### Viewer client

If the server was started with the flag `-v` then you can connect the viewer client `scripts/viewer.py` to the server by typing

`python viewer.py -a <server address> -p <server port> -n <name of your choice>`

The viewer client receives a broadcast in real time form the server about the state of the game and displays the shots made by the players. Similarly to the player client you can reconnect with the secret provided on the first connection.

