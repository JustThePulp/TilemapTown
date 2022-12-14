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

import asyncio, datetime, random, websockets, json, os.path, hashlib
from .buildglobal import *

# Make a command to send
def makeCommand(commandType, commandParams):
	if commandParams != None:
		return commandType + " " + json.dumps(commandParams)
	else:
		return commandType

userCounter = 1

class Client(object):
	def __init__(self,websocket):
		global userCounter
		self.ws = websocket
		self.name = 'Guest '+ str(userCounter)
		self.x = 0
		self.y = 0
		self.map = None
		self.map_id = -1
		self.pic = [0, 2, 25]
		self.id = userCounter
		self.db_id = None        # database key
		self.ping_timer = 180
		self.idle_timer = 0
		self.ip = None           # for IP ban purposes
		userCounter += 1

		self.map_allow = 0       # Cache map allows and map denys to avoid excessive SQL queries
		self.map_deny = 0
		self.oper_override = False

		# other user info
		self.ignore_list = set()
		self.watch_list = set()
		self.tags = {}    # description, species, gender and other things
		self.away = False # true, or a string if person is away
		self.home = None
		self.client_settings = ""

		# temporary information
		self.requests = {} # indexed by username, array with [timer, type]
		# valid types are "tpa", "tpahere", "carry"
		self.tp_history = []

		# allow cleaning up BotWatch info
		self.listening_maps = set() # tuples of (category, map)

		# riding information
		self.vehicle = None     # user being ridden
		self.passengers = set() # users being carried
		self.is_following = False # if true, follow behind instead of being carried

		# account stuff
		self.username = None
		self.password = None # actually the password hash

	def send(self, commandType, commandParams):
		""" Send a command to the client """
		if self.ws == None:
			return
		asyncio.ensure_future(self.ws.send(makeCommand(commandType, commandParams)))

	def testServerBanned(self):
		""" Test for and take action on IP bans """
		# Look for IP bans
		if self.ip != '':
			c = Database.cursor()
			split = self.ip.split('.')
			if len(split) == 4:
				c.execute("""SELECT id, expiry, reason FROM Server_Ban WHERE
                          (ip1=? or ip1='*') and
                          (ip2=? or ip2='*') and
                          (ip3=? or ip3='*') and
                          (ip4=? or ip4='*')""", (split[0], split[1], split[2], split[3]))
			else:
				c.execute('SELECT id, expiry, reason FROM Server_Ban WHERE ip=?', (self.ip,))

			# If there is a result, check to see if it's expired or not
			result = c.fetchone()
			if result != None:
				if result[1] != None and datetime.datetime.now() > result[1]:
					print("ban expired")
					c.execute('DELETE FROM Server_Ban WHERE id=?', (result[0],))
				else:
					print("stopped banned user %s" % self.ip)
					self.disconnect('Banned from the server until %s (%s)' % (str(result[1]), result[2]))
					return True
		return False

	def updateMapPermissions(self):
		""" Update map_allow and map_deny for the current map """
		self.map_allow = 0
		self.map_deny = 0

		# If a guest, don't bother looking up any queries
		if self.db_id == None:
			return
		c = Database.cursor()
		c.execute('SELECT allow, deny FROM Map_Permission WHERE mid=? AND uid=?', (self.map_id, self.db_id,))
		result = c.fetchone()
		if result != None:
			self.map_allow = result[0]
			self.map_deny = result[1]

		# Turn on all permissions
		for row in c.execute('SELECT p.allow FROM Group_Map_Permission p, Group_Member m\
			WHERE m.uid=? AND p.gid=m.gid AND p.mid=?', (self.db_id, self.map_id)):
			self.map_allow |= row[0]

	def permissionByName(self, perm):
		perm = perm.lower()
		if perm in permission:
			return permission[perm]
		self.send("ERR", {'text': 'Permission "%s" doesn\t exist' % perm})
		return None

	def failedToFind(self, username):
		if username == None or len(username) == 0:
			self.send("ERR", {'text': 'No username given'})
		else:
			self.send("ERR", {'text': 'Player '+username+' not found'})

	def inBanList(self, banlist, action):
		if self.username == None and '!guests' in banlist:
			self.send("ERR", {'text': 'Guests may not %s' % action})
			return True
		if self.username in banlist:
			self.send("ERR", {'text': 'You may not %s' % action})
			return True
		return False

	def ride(self, other):
		# cannot ride yourself
		if self == other:
			return
		# remove the old ride before getting a new one
		if self.vehicle != None:
			self.dismount()
		# let's not deal with trees of passengers first
		if len(self.passengers):
			self.send("MSG", {'text': 'You let out all your passengers first'})
			temp = set(self.passengers)
			for u in temp:
				u.dismount()

		self.send("MSG", {'text': 'You get on %s (/hopoff to get off)' % other.nameAndUsername()})
		other.send("MSG", {'text': 'You carry %s' % self.nameAndUsername()})

		self.vehicle = other
		other.passengers.add(self)

		self.map.broadcast("WHO", {'add': self.who()}, remote_category=botwatch_type['move'])
		other.map.broadcast("WHO", {'add': other.who()}, remote_category=botwatch_type['move'])

		self.switch_map(other.map_id, new_pos=[other.x, other.y])

	def dismount(self):
		if self.vehicle == None:
			self.send("ERR", {'text': 'You\'re not being carried'})
		else:
			self.send("MSG", {'text': 'You get off %s' % self.vehicle.nameAndUsername()})
			self.vehicle.send("MSG", {'text': '%s gets off of you' % self.nameAndUsername()})

			other = self.vehicle

			self.vehicle.passengers.remove(self)
			self.vehicle = None

			self.map.broadcast("WHO", {'add': self.who()}, remote_category=botwatch_type['move'])
			other.map.broadcast("WHO", {'add': other.who()}, remote_category=botwatch_type['move'])

	def mustBeServerAdmin(self, giveError=True):
		if self.username in Config["Server"]["Admins"]:
			return True
		elif giveError:
			self.send("ERR", {'text': 'You don\'t have permission to do that'})
		return False

	def mustBeOwner(self, adminOkay, giveError=True):
		if self.map.owner == self.db_id or self.oper_override or (adminOkay and self.map.has_permission(self, permission['admin'], False)):
			return True
		elif giveError:
			self.send("ERR", {'text': 'You don\'t have permission to do that'})
		return False

	def moveTo(self, x, y):
		# keep the old position, for following
		oldx = self.x
		oldy = self.y
		# set new position
		self.x = x
		self.y = y
		for u in self.passengers:
			if u.is_following:
				u.moveTo(oldx, oldy)
			else:
				u.moveTo(x, y)
			u.map.broadcast("MOV", {'id': u.id, 'to': [u.x, u.y]}, remote_category=botwatch_type['move'])

	def who(self):
		""" A dictionary of information for the WHO command """
		return {
			'name': self.name,
			'pic': self.pic,
			'x': self.x,
			'y': self.y,
			'id': self.id,
			'username': self.username,
			'passengers': [passenger.id for passenger in self.passengers],
			'vehicle': self.vehicle.id if self.vehicle else None,
			'is_following': self.is_following
		}

	def disconnect(self, text=None):
		if text != None:
			# Does not actually seem to go through, might need some refactoring
			self.send("ERR", {'text': text})
		asyncio.ensure_future(self.ws.close())

	def usernameOrId(self):
		return self.username or str(self.id)

	def nameAndUsername(self):
		return '%s (%s)' % (self.name, self.usernameOrId())

	def set_tag(self, name, value):
		self.tags[name] = value

	def get_tag(self, name, default=None):
		if name in self.tags:
			return self.tags[name]
		return default

	def save(self):
		""" Save user information to the database """
		c = Database.cursor()

		# Create new user if user doesn't already exist
		if findDBIdByUsername(self.username) == None:
			c.execute("INSERT INTO User (regtime, username) VALUES (?, ?)", (datetime.datetime.now(), self.username,))
		# Update database ID in RAM with the possibly newly created row
		self.db_id = findDBIdByUsername(self.username)

		# Update the user
		values = (self.password, "sha512", self.name, json.dumps(self.pic), self.map_id, self.x, self.y, json.dumps(self.home), json.dumps(list(self.watch_list)), json.dumps(list(self.ignore_list)), self.client_settings, json.dumps(self.tags), datetime.datetime.now(), self.db_id)
		c.execute("UPDATE User SET passhash=?, passalgo=?, name=?, pic=?, mid=?, map_x=?, map_y=?, home=?, watch=?, ignore=?, client_settings=?, tags=?, lastseen=? WHERE uid=?", values)
		Database.commit()

	def switch_map(self, map_id, new_pos=None, goto_spawn=True, update_history=True):
		""" Teleport the user to another map """
		if update_history and self.map_id >= 0:
			# Add a new teleport history entry if new map
			if self.map_id != map_id:
				self.tp_history.append([self.map_id, self.x, self.y])
			if len(self.tp_history) > 20:
				self.tp_history.pop(0)

		if not self.map or (self.map and self.map.id != map_id):
			# First check if you can even go to that map
			new_map = getMapById(map_id)
			if not new_map.has_permission(self, permission['entry'], True):
				self.send("ERR", {'text': 'You don\'t have permission to go to map %d' % map_id})
				return False

			if self.map:
				# Remove the user for everyone on the map
				self.map.users.remove(self)
				self.map.broadcast("WHO", {'remove': self.id}, remote_category=botwatch_type['entry'])

			# Get the new map and send it to the client
			self.map_id = map_id
			self.map = new_map
			self.updateMapPermissions()

			self.send("MAI", self.map.map_info())
			self.send("MAP", self.map.map_section(0, 0, self.map.width-1, self.map.height-1))
			self.map.users.add(self)
			self.send("WHO", {'list': self.map.who(), 'you': self.id})

			# Tell everyone on the new map the user arrived
			self.map.broadcast("WHO", {'add': self.who()}, remote_category=botwatch_type['entry'])

			# Warn about chat listeners, if present
			if map_id in BotWatch[botwatch_type['chat']]:
				self.send("MSG", {'text': 'A bot has access to messages sent here ([command]listeners[/command])'})

		# Move player's X and Y coordinates if needed
		if new_pos != None:
			self.moveTo(new_pos[0], new_pos[1])
			self.map.broadcast("MOV", {'id': self.id, 'to': [self.x, self.y]}, remote_category=botwatch_type['move'])
		elif goto_spawn:
			self.moveTo(self.map.start_pos[0], self.map.start_pos[1])
			self.map.broadcast("MOV", {'id': self.id, 'to': [self.x, self.y]}, remote_category=botwatch_type['move'])

		# Move any passengers too
		for u in self.passengers:
			u.switch_map(map_id, new_pos=[self.x, self.y])
		return True

	def send_home(self):
		""" If player has a home, send them there. If not, to map zero """
		if self.home != None:
			self.switch_map(self.home[0], new_pos=[self.home[1], self.home[2]])
		else:
			self.switch_map(0)

	def cleanup(self):
		self.ws = None
		temp = set(self.passengers)
		for u in temp:
			u.dismount()
		if self.vehicle:
			self.dismount()
		for p in self.listening_maps:
			BotWatch[p[0]][p[1]].remove(self)

	def login(self, username, password):
		""" Attempt to log the client into an account """
		username = filterUsername(username)
		result = self.load(username, password)
		if result == True:
			print("login: \"%s\" from %s" % (self.username, self.ip))

			self.switch_map(self.map_id, goto_spawn=False)
			self.map.broadcast("MSG", {'text': self.name+" has logged in ("+self.username+")"})
			self.map.broadcast("WHO", {'add': self.who()}, remote_category=botwatch_type['entry']) # update client view

			# send the client their inventory
			c = Database.cursor()
			inventory = []
			for row in c.execute('SELECT aid, name, desc, type, flags, folder, data FROM Asset_Info WHERE owner=?', (self.db_id,)):
				item = {'id': row[0], 'name': row[1], 'desc': row[2], 'type': row[3], 'flags': row[4], 'folder': row[5], 'data': row[6]}
				inventory.append(item)
			self.send("BAG", {'list': inventory})

			# send the client their mail
			mail = []
			for row in c.execute('SELECT id, sender, recipients, subject, contents, flags FROM Mail WHERE uid=?', (self.db_id,)):
				item = {'id': row[0], 'from': findUsernameByDBId(row[1]),
				'to': [findUsernameByDBId(int(x)) for x in row[2].split(',')],
				'subject': row[3], 'contents': row[4], 'flags': row[5]}
				mail.append(item)
			if len(mail):
				self.send("EML", {'list': mail})

			return True
		elif result == False:
			self.send("ERR", {'text': 'Login fail, bad password for account'})
		else:
			self.send("ERR", {'text': 'Login fail, nonexistent account'})
		return False

	def changepass(self, password):
		# Generate a random salt and append it to the password
		salt = str(random.random())
		combined = password+salt
		self.password = "%s:%s" % (salt, hashlib.sha512(combined.encode()).hexdigest())
		self.save()

	def register(self, username, password):
		username = str(filterUsername(username))
		# User can't already exist
		if findDBIdByUsername(username) != None:
			return False
		self.username = username
		self.changepass(password)
		# db_id updated by changepass
		return True

	def load(self, username, password):
		""" Load an account from the database """
		c = Database.cursor()
		
		c.execute('SELECT uid, passhash, passalgo, username, name, pic, mid, map_x, map_y, home, watch, ignore, client_settings, tags FROM User WHERE username=?', (username,))
		result = c.fetchone()
		if result == None:
			return None

		passalgo = result[2] # Algorithm used, allows more options later
		passhash = result[1] # Hash that may be formatted "hash" or "salt:hash"

		if passalgo == "sha512":
			# Start with a default for no salt
			salt = ""
			comparewith = passhash

			# Is there a salt?
			split = passhash.split(':')
			if len(split) == 2:
				salt = split[0]
				comparewith = split[1]

			# Verify the password
			if hashlib.sha512((password+salt).encode()).hexdigest() != comparewith:
				return False
			self.password = passhash

		self.db_id = result[0]
		self.username = result[3]
		self.name = result[4]
		self.pic = json.loads(result[5])
		self.map_id = result[6]
		self.x = result[7]
		self.y = result[8]
		self.home = json.loads(result[9] or "null")
		self.watch_list = set(json.loads(result[10]))
		self.ignore_list = set(json.loads(result[11]))
		self.client_settings = result[12]
		self.tags = json.loads(result[13])

		return True
