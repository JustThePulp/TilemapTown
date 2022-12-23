# Tilemap Town
# Copyright (C) 2017-2019 NovaSquirrel
#
# This program is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio, datetime, random, websockets, json, sys, traceback
from .buildglobal import *
from .buildmap import *
from .buildclient import *
if Config["Database"]["Setup"]:
	from .database_setup import *

# Timer that runs and performs background tasks
def mainTimer():
	global ServerShutdown
	global loop

	# Disconnect pinged-out users
	for c in AllClients:
		# Remove requests that time out
		remove_requests = set()
		for k,v in c.requests.items():
			v[0] -= 1 # remove 1 from timer
			if v[0] < 0:
				remove_requests.add(k)
		for r in remove_requests:
			del c.requests[r]

		c.idle_timer += 1

		# Remove users that time out
		c.ping_timer -= 1
		if c.ping_timer == 60 or c.ping_timer == 30:
			c.send("PIN", None)
		elif c.ping_timer < 0:
			c.disconnect()

	# Unload unused maps
	unloaded = set()
	for k,m in AllMaps.items():
		if (m.id not in Config["Server"]["AlwaysLoadedMaps"]) and (len(m.users) < 1):
			print("Unloading map "+str(k))
			m.save()
			m.clean_up()
			unloaded.add(k)
	for m in unloaded:
		del AllMaps[m]

	# Run server shutdown timer, if it's running
	if ServerShutdown[0] > 0:
		ServerShutdown[0] -= 1
		if ServerShutdown[0] == 1:
			broadcastToAll("Server is going down!")
			for u in AllClients:
				u.disconnect()
			for k,m in AllMaps.items():
				m.save()
		elif ServerShutdown[0] == 0:
			loop.stop()
	if ServerShutdown[0] != 0:
		loop.call_later(1, mainTimer)

# Websocket connection handler
async def clientHandler(websocket, path):
	client = Client(websocket)
	client.ip = websocket.remote_address[0]

	# If the local and remote addresses are the same, it's trusted
	# and the server should look for the forwarded IP address
	if websocket.local_address[0] == websocket.remote_address[0]:
		if 'X-Real-IP' in websocket.request_headers:
			client.ip = websocket.request_headers['X-Real-IP']
		else:
			client.ip = ''

	if client.testServerBanned():
		return

	print("connected: %s %s" % (path, client.ip))
	AllClients.add(client)

	try:
		while True:
			# Read a message, make sure it's not too short
			message = await websocket.recv()
			if len(message) < 3:
				continue
            # Split it into parts
			command = message[0:3]
			arg = None
			if len(message) > 4:
				arg = json.loads(message[4:])

			# Identify the user and put them on a map
			if command == "IDN":
				result = False
				if arg != None:
					result = client.login(filterUsername(arg["username"]), arg["password"])
				if result != True: # default to map 0 if can't log in
					client.switch_map(0)
				if len(Config["Server"]["MOTD"]):
					client.send("MSG", {'text': Config["Server"]["MOTD"]})
				client.send("MSG", {'text': 'Users connected: %d' % len(AllClients)})
			elif command == "PIN":
				client.ping_timer = 300

			# Don't allow the user to go any further if they're not on a map
			if client.map_id == -1:
				continue
			# Send the command through to the map
			if command not in ["IDN", "PIN"]:
				if "remote_map" in arg:
					if arg["remote_map"] in AllMaps:
						map = AllMaps[arg["remote_map"]]
						if map.has_permission(client, permission['map_bot'], False):
							map.receive_command(client, command, arg)
						else:
							client.send("ERR", {'text': 'You do not have [tt]map_bot[/tt] permission on map %d' % arg["remote_map"]})
					else:
						client.send("ERR", {'text': 'Map %d is not loaded' % arg["remote_map"]})
				else:
					client.map.receive_command(client, command, arg)

	except websockets.ConnectionClosed:
		print("disconnected: %s (%s, \"%s\")" % (client.ip, client.username or "?", client.name))
	except:
		print("Unexpected error:", sys.exc_info()[0])
		print(sys.exc_info()[1])
		traceback.print_tb(sys.exc_info()[2])
#		raise

	client.cleanup()
	if client.username:
		client.save()

	# remove the user from all clients' views
	if client.map != None:
		client.map.users.remove(client)
		client.map.broadcast("WHO", {'remove': client.id})
	AllClients.remove(client)

global loop

def main():
	global loop
	start_server = websockets.serve(clientHandler, None, Config["Server"]["Port"], max_size=Config["Server"]["WSMaxSize"], max_queue=Config["Server"]["WSMaxQueue"])

	# Start the event loop
	loop = asyncio.get_event_loop()
	loop.call_soon(mainTimer)
	loop.run_until_complete(start_server)
	print("Server started!")
	loop.run_forever()
	Database.close()

if __name__ == "__main__":
	main()
