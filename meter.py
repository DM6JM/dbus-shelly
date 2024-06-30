import asyncio
import logging
from asyncio.exceptions import TimeoutError # Deprecated in 3.11

from dbus_next.aio import MessageBus

from __main__ import VERSION
from __main__ import __file__ as MAIN_FILE

from aiovelib.service import Service, IntegerItem, DoubleItem, TextItem
from aiovelib.service import TextArrayItem
from aiovelib.client import Monitor, ServiceHandler
from aiovelib.localsettings import SettingsService, Setting, SETTINGS_SERVICE

logger = logging.getLogger(__name__)

class LocalSettings(SettingsService, ServiceHandler):
	pass
		
# Text formatters
unit_watt = lambda v: "{:.0f}W".format(v)
unit_volt = lambda v: "{:.1f}V".format(v)
unit_amp = lambda v: "{:.1f}A".format(v)
unit_kwh = lambda v: "{:.2f}kWh".format(v)
unit_productid = lambda v: "0x{:X}".format(v)

alias_instance = lambda v: "instance_" + str(v)
alias_position = lambda v: "position_" + str(v)
alias_phase = lambda v: "phase_" + str(v)

class SingleMeter(object):
	def __init__(self, bus_type, meterid):
		self.bus_type = bus_type
		self.monitor = None
		self.service = None
		self.position = None
		self.destroyed = False
		self.meterid = meterid
		self.phaseposition = 1 # Which phase the EM is connected to

	async def wait_for_settings(self):
			""" Attempt a connection to localsettings. If it does not show
				up within 5 seconds, return None. """
			try:
				return await asyncio.wait_for(
					self.monitor.wait_for_service(SETTINGS_SERVICE), 5)
			except TimeoutError:
				pass

			return None
		
	def get_settings(self):
		""" Non-async version of the above. Return the settings object
			if known. Otherwise return None. """
		return self.monitor.get_service(SETTINGS_SERVICE)

	async def start(self, host, port, data):
		try:
			mac = data['result']['mac']
			fw = data['result']['fw_id']
			model = data['result']['model']
			app = data['result']['app']						
		except KeyError:
			return False

		try:
			name = data['result']['name'] + "_" + str(self.meterid)
		except:
			name = None	

		# Check model and usage
		if not (model == 'SPEM-002CEBEU50' and app == 'ProEM'):
			return False # Unsupported model connected

		# Connect to dbus, localsettings
		bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(bus, self.settings_changed)

		logger.info("Waiting for localsettings")
		settings = await self.wait_for_settings()
		if settings is None:
			logger.error("Failed to connect to localsettings")
			return False

		logger.info("Connected to localsettings")

		settingprefix = '/Settings/Devices/shelly_' + mac + "_" + str(self.meterid)		
		
		await settings.add_settings(
				Setting(settingprefix + "/ClassAndVrmInstance", "grid:40", 0, 0, alias="instance"),
				Setting(settingprefix + '/Position', 0, 0, 2, alias="position"),
				Setting(settingprefix + '/Phase', 1, 1, 3, alias="phaseposition")
			)
		# Determine role and instance
		role, instance = self.role_instance(
			settings.get_value(settings.alias("instance")))
		# Determine phase position
		self.phaseposition = settings.get_value(settings.alias("phaseposition"))

		# Set up the service
		self.service = await Service.create(bus, "com.victronenergy.{}.shelly_{}_{}".format(role, mac, str(meterid)))

		self.service.add_item(TextItem('/Mgmt/ProcessName', MAIN_FILE))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.service.add_item(TextItem('/Mgmt/Connection', f"WebSocket {host}:{port}"))
		self.service.add_item(IntegerItem('/DeviceInstance', instance))
		self.service.add_item(IntegerItem('/ProductId', 0xB034, text=unit_productid))
		self.service.add_item(TextItem('/ProductName', "Shelly energy meter"))
		if name is not None:
			self.service.add_item(TextItem('/CustomName', name))
		self.service.add_item(TextItem('/FirmwareVersion', fw))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(IntegerItem('/RefreshTime', 100))

		# Role
		self.service.add_item(TextArrayItem('/AllowedRoles',
			['grid', 'pvinverter', 'genset', 'acload']))
		self.service.add_item(TextItem('/Role', role, writeable=True,
			onchange=self.role_changed))

		self.service.add_item(TextItem('/Phase', self.phaseposition, writeable=True,
			onchange=self.phase_changed))

		# Position for pvinverter
		if role == 'pvinverter':
			self.service.add_item(IntegerItem('/Position',
				settings.get_value(settings.alias("position")),
				writeable=True, onchange=self.position_changed))

		prefix = "/Ac/L" + str(self.phaseposition) 

		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Power', None, text=unit_watt))
		
		self.service.add_item(DoubleItem(prefix + '/Voltage', None, text=unit_volt))
		self.service.add_item(DoubleItem(prefix + '/Current', None, text=unit_amp))
		self.service.add_item(DoubleItem(prefix + '/Power', None, text=unit_watt))
		self.service.add_item(DoubleItem(prefix + '/Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem(prefix + '/Energy/Reverse', None, text=unit_kwh))

		return True


	def destroy(self):
		if self.service is not None:
			self.service.__del__()
		self.service = None
		self.settings = None
		self.destroyed = True
		
	async def update(self, data):		
		# Check if incoming is history or live data
		prefix = "/Ac/L" + str(self.phaseposition)
		try:
			helpertag = data['helpertag']
			if helpertag.startswith("em:"):
				try:
					with self.service as s:
						s[prefix + '/Voltage'] = data["voltage"]						
						s[prefix + '/Current'] = data["current"]						
						s[prefix + '/Power'] = data["act_power"]

						s['/Ac/Power'] = data["act_power"]
				except:
					pass
			elif helpertag.startswith("emdata:"):
				try:						
					with self.service as s:
						s["/Ac/Energy/Forward"] = round(data["total_act_energy"]/1000, 1)
						s["/Ac/Energy/Reverse"] = round(data["total_act_ret_energy"]/1000, 1)
						s[prefix + "/Energy/Forward"] = round(data["total_act_energy"]/1000, 1)
						s[prefix + "/Energy/Reverse"] = round(data["total_act_ret_energy"]/1000, 1)
				except:
					pass				
		except:
			pass

		####
		if self.service and data.get('method') == 'NotifyStatus':
			try:
				d = data['params']['em:0']
			except KeyError:
				pass
			else:
				with self.service as s:
					s['/Ac/L1/Voltage'] = d["a_voltage"]
					s['/Ac/L2/Voltage'] = d["b_voltage"]
					s['/Ac/L3/Voltage'] = d["c_voltage"]
					s['/Ac/L1/Current'] = d["a_current"]
					s['/Ac/L2/Current'] = d["b_current"]
					s['/Ac/L3/Current'] = d["c_current"]
					s['/Ac/L1/Power'] = d["a_act_power"]
					s['/Ac/L2/Power'] = d["b_act_power"]
					s['/Ac/L3/Power'] = d["c_act_power"]

					s['/Ac/Power'] = d["a_act_power"] + d["b_act_power"] + d["c_act_power"]

			try:
				d = data['params']['emdata:0']
			except KeyError:
				pass
			else:
				with self.service as s:
					s["/Ac/Energy/Forward"] = round(d["total_act"]/1000, 1)
					s["/Ac/Energy/Reverse"] = round(d["total_act_ret"]/1000, 1)
					s["/Ac/L1/Energy/Forward"] = round(d["a_total_act_energy"]/1000, 1)
					s["/Ac/L1/Energy/Reverse"] = round(d["a_total_act_ret_energy"]/1000, 1)
					s["/Ac/L2/Energy/Forward"] = round(d["b_total_act_energy"]/1000, 1)
					s["/Ac/L2/Energy/Reverse"] = round(d["b_total_act_ret_energy"]/1000, 1)
					s["/Ac/L3/Energy/Forward"] = round(d["c_total_act_energy"]/1000, 1)
					s["/Ac/L3/Energy/Reverse"] = round(d["c_total_act_ret_energy"]/1000, 1)

	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	def settings_changed(self, service, values):
		# Kill service, driver will restart us soon
		if service.alias("instance") in values:
			self.destroy()

	def role_changed(self, val):
		if val not in ['grid', 'pvinverter', 'genset', 'acload']:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		p = settings.alias("instance")
		role, instance = self.role_instance(settings.get_value(p))
		settings.set_value(p, "{}:{}".format(val, instance))

		self.destroy() # restart
		return True

	def position_changed(self, val):
		if not 0 <= val <= 2:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		settings.set_value(settings.alias("position"), val)
		return True


	def phase_changed(self, val):
		if not 1 <= val <= 3:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		settings.set_value(settings.alias("phaseposition"), val)
		self.phaseposition = val

		return True

class ThreePhaseMeter(object):
	def __init__(self, bus_type):
		self.bus_type = bus_type
		self.monitor = None
		self.service = None
		self.position = None
		self.destroyed = False
		self.phase1position = 1 # Which phase shall be presented as phase 1 when 3EM is connected

	async def wait_for_settings(self):
		""" Attempt a connection to localsettings. If it does not show
		    up within 5 seconds, return None. """
		try:
			return await asyncio.wait_for(
				self.monitor.wait_for_service(SETTINGS_SERVICE), 5)
		except TimeoutError:
			pass

		return None
	
	def get_settings(self):
		""" Non-async version of the above. Return the settings object
		    if known. Otherwise return None. """
		return self.monitor.get_service(SETTINGS_SERVICE)

	async def start(self, host, port, data):
		try:
			mac = data['result']['mac']
			fw = data['result']['fw_id']
			model = data['result']['model']
			app = data['result']['app']						
		except KeyError:
			return False

		try:
			name = data['result']['name']
		except:
			name = None	

		# Check model and usage
		if model == 'SPEM-003CEBEU' and app == 'Pro3EM':
			pass
		else:
			return False # Unsupported model connected

		# Connect to dbus, localsettings
		bus = await MessageBus(bus_type=self.bus_type).connect()
		self.monitor = await Monitor.create(bus, self.settings_changed)

		logger.info("Waiting for localsettings")
		settings = await self.wait_for_settings()
		if settings is None:
			logger.error("Failed to connect to localsettings")
			return False

		logger.info("Connected to localsettings")

		settingprefix = '/Settings/Devices/shelly_' + mac		
		## Begin of: HandlePro 3EM in 3phase setup

		await settings.add_settings(
				Setting(settingprefix + "/ClassAndVrmInstance", "grid:40", 0, 0, alias="instance"),
				Setting(settingprefix + '/Position', 0, 0, 2, alias="position"),
				Setting(settingprefix + '/Phase1Position', 1, 1, 3, alias="phase1position")
			)
		# Determine role and instance
		role, instance = self.role_instance(
			settings.get_value(settings.alias("instance")))
		# Determine phase 1 position
		self.phase1position = settings.get_value(settings.alias("phase1position"))

		# Set up the service
		self.service = await Service.create(bus, "com.victronenergy.{}.shelly_{}".format(role, mac))

		self.service.add_item(TextItem('/Mgmt/ProcessName', MAIN_FILE))
		self.service.add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		self.service.add_item(TextItem('/Mgmt/Connection', f"WebSocket {host}:{port}"))
		self.service.add_item(IntegerItem('/DeviceInstance', instance))
		self.service.add_item(IntegerItem('/ProductId', 0xB034, text=unit_productid))
		self.service.add_item(TextItem('/ProductName', "Shelly energy meter"))
		if name is not None:
			self.service.add_item(TextItem('/CustomName', name))
		self.service.add_item(TextItem('/FirmwareVersion', fw))
		self.service.add_item(IntegerItem('/Connected', 1))
		self.service.add_item(IntegerItem('/RefreshTime', 100))

		# Role
		self.service.add_item(TextArrayItem('/AllowedRoles',
			['grid', 'pvinverter', 'genset', 'acload']))
		self.service.add_item(TextItem('/Role', role, writeable=True,
			onchange=self.role_changed))

		self.service.add_item(TextItem('/Phase', self.phase1position, writeable=True,
			onchange=self.phase_changed))

		# Position for pvinverter
		if role == 'pvinverter':
			self.service.add_item(IntegerItem('/Position',
				settings.get_value(settings.alias("position")),
				writeable=True, onchange=self.position_changed))

		# Meter paths
		self.service.add_item(DoubleItem('/Ac/Energy/Forward', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Energy/Reverse', None, text=unit_kwh))
		self.service.add_item(DoubleItem('/Ac/Power', None, text=unit_watt))
		for prefix in (f"/Ac/L{x}" for x in range(1, 4)):
			self.service.add_item(DoubleItem(prefix + '/Voltage', None, text=unit_volt))
			self.service.add_item(DoubleItem(prefix + '/Current', None, text=unit_amp))
			self.service.add_item(DoubleItem(prefix + '/Power', None, text=unit_watt))
			self.service.add_item(DoubleItem(prefix + '/Energy/Forward', None, text=unit_kwh))
			self.service.add_item(DoubleItem(prefix + '/Energy/Reverse', None, text=unit_kwh))

		return True
	
	def destroy(self):
		if self.service is not None:
			self.service.__del__()
		self.service = None
		self.settings = None
		self.destroyed = True
	
	async def update(self, data):
		# NotifyStatus has power, current, voltage and energy values
		
		# Shift phases according to setup
		phaseorder = ["", "a","b","c","a","b"]

		# Check if incoming is history or live data
		try:
			helpertag = data['helpertag']
			if helpertag.startswith("em:"):
				try:
					with self.service as s:
						s['/Ac/L1/Voltage'] = data[phaseorder[self.phase1position] + "_voltage"]
						s['/Ac/L2/Voltage'] = data[phaseorder[self.phase1position + 1] + "_voltage"]
						s['/Ac/L3/Voltage'] = data[phaseorder[self.phase1position + 2] + "_voltage"]
						s['/Ac/L1/Current'] = data[phaseorder[self.phase1position] + "_current"]
						s['/Ac/L2/Current'] = data[phaseorder[self.phase1position + 1] + "_current"]
						s['/Ac/L3/Current'] = data[phaseorder[self.phase1position + 2] + "_current"]
						s['/Ac/L1/Power'] = data[phaseorder[self.phase1position] + "_act_power"]
						s['/Ac/L2/Power'] = data[phaseorder[self.phase1position + 1] + "_act_power"]
						s['/Ac/L3/Power'] = data[phaseorder[self.phase1position + 2] + "_act_power"]

						s['/Ac/Power'] = data["a_act_power"] + data["b_act_power"] + data["c_act_power"]
				except:
					pass
			elif helpertag.startswith("emdata:"):
				try:						
					with self.service as s:
						s["/Ac/Energy/Forward"] = round(data["total_act"]/1000, 1)
						s["/Ac/Energy/Reverse"] = round(data["total_act_ret"]/1000, 1)
						s["/Ac/L1/Energy/Forward"] = round(data[phaseorder[self.phase1position] + "_total_act_energy"]/1000, 1)
						s["/Ac/L1/Energy/Reverse"] = round(data[phaseorder[self.phase1position] + "_total_act_ret_energy"]/1000, 1)
						s["/Ac/L2/Energy/Forward"] = round(data[phaseorder[self.phase1position + 1] + "_total_act_energy"]/1000, 1)
						s["/Ac/L2/Energy/Reverse"] = round(data[phaseorder[self.phase1position + 1] + "_total_act_ret_energy"]/1000, 1)
						s["/Ac/L3/Energy/Forward"] = round(data[phaseorder[self.phase1position + 2] + "_total_act_energy"]/1000, 1)
						s["/Ac/L3/Energy/Reverse"] = round(data[phaseorder[self.phase1position + 2] + "_total_act_ret_energy"]/1000, 1)
				except:
					pass				
		except:
			pass


	def role_instance(self, value):
		val = value.split(':')
		return val[0], int(val[1])

	def settings_changed(self, service, values):
		# Kill service, driver will restart us soon
		if service.alias("instance") in values:
			self.destroy()

	def role_changed(self, val):
		if val not in ['grid', 'pvinverter', 'genset', 'acload']:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		p = settings.alias("instance")
		role, instance = self.role_instance(settings.get_value(p))
		settings.set_value(p, "{}:{}".format(val, instance))

		self.destroy() # restart
		return True

	def position_changed(self, val):
		if not 0 <= val <= 2:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		settings.set_value(settings.alias("position"), val)
		return True

	def phase_changed(self, val):
		if not 1 <= val <= 3:
			return False

		settings = self.get_settings()
		if settings is None:
			return False

		settings.set_value(settings.alias("phase1position"), val)
		self.phase1position = val

		return True


class PhysicalMeter(object):
	def __init__(self, bus_type):
		self.bus_type = bus_type
		self.destroyed = False
		self.localmeters = {}	
		self.localmeterskeys = {}

	async def start(self, host, port, data):
		try:
			#mac = data['result']['mac']
			#fw = data['result']['fw_id']
			model = data['result']['model']
			app = data['result']['app']						
		except KeyError:
			return False

		try:
			name = data['result']['name']
		except:
			name = None	

		# Check model and usage
		if model == 'SPEM-002CEBEU50' and app == 'ProEM':
			for meter in range(2):
				self.localmeters[meter] = m = SingleMeter(self.bus_type, meter)
				self.localmeterskeys[meter] = {"em1:"+str(meter), "em1data:"+str(meter)}
				if not await m.start(host, port, data):
					return False # Something failed on startup
		elif model == 'SPEM-003CEBEU' and app == 'Pro3EM':
			try:
				profile = data['result']['profile']
			except KeyError:
				return False
			if profile == 'monophase':
				for meter in range(3):
					self.localmeters[meter] = m = SingleMeter(self.bus_type, meter)
					self.localmeterskeys[meter] = {"em1:"+str(meter), "em1data:"+str(meter)}
					if not await m.start(host, port, data):
						return False # Something failed on startup
			else:
				self.localmeters[0] = m = ThreePhaseMeter(self.bus_type)
				self.localmeterskeys[0] = {"em:0", "emdata:0"}
				if not await m.start(host, port, data):
					return False # Something failed on startup
		else:
			return False # Unsupported model connected

		# # Connect to dbus, localsettings
		# bus = await MessageBus(bus_type=self.bus_type).connect()
		# self.monitor = await Monitor.create(bus, self.settings_changed)

		
		# logger.info("Waiting for localsettings")
		# settings = await self.wait_for_settings()
		# if settings is None:
		# 	logger.error("Failed to connect to localsettings")
		# 	return False

		# logger.info("Connected to localsettings")

		# settingprefix = '/Settings/Devices/shelly_' + mac

		# if self.phase3 == False:
		# 	## Begin of: Handle Pro EM and Pro 3EM in monophase setup

		# 	for cur_instance in range(self.phases):
		# 		settingsprefix_cur = settingprefix + "_em" + str(cur_instance)
		# 		await settings.add_settings(
		# 			Setting(settingsprefix_cur + "/ClassAndVrmInstance", "grid:40", 0, 0, alias=alias_instance(cur_instance)),
		# 			Setting(settingsprefix_cur + '/Position', 0, 0, 2, alias=alias_position(cur_instance)),
		# 			Setting(settingsprefix_cur + '/Phase', 1, 3, 1, alias=alias_phase(cur_instance))
		# 		)

		# 		# Determine role and instance
		# 		role, instance = self.role_instance(
		# 			settings.get_value(settings.alias(alias_instance(cur_instance))))
		# 		# Determine phase 
		# 		phase = settings.get_value(settings.alias(alias_phase(cur_instance)))

		# 		# Set up the service for this instance
		# 		self.services[cur_instance] = await Service.create(bus, "com.victronenergy.{}.shelly_{}_em{}".format(role, mac, cur_instance))

		# 		self.services[cur_instance].add_item(TextItem('/Mgmt/ProcessName', MAIN_FILE))
		# 		self.services[cur_instance].add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		# 		self.services[cur_instance].add_item(TextItem('/Mgmt/Connection', f"WebSocket {host}:{port}"))
		# 		self.services[cur_instance].add_item(IntegerItem('/DeviceInstance', instance))
		# 		self.services[cur_instance].add_item(IntegerItem('/ProductId', 0xB034, text=unit_productid))
		# 		self.services[cur_instance].add_item(TextItem('/ProductName', "Shelly energy meter"))
		# 		if name is not None:
		# 			self.services[cur_instance].add_item(TextItem('/CustomName', name))
		# 		self.services[cur_instance].add_item(TextItem('/FirmwareVersion', fw))
		# 		self.services[cur_instance].add_item(IntegerItem('/Connected', 1))
		# 		self.services[cur_instance].add_item(IntegerItem('/RefreshTime', 100))

		# 		# Role
		# 		self.services[cur_instance].add_item(TextArrayItem('/AllowedRoles',
		# 			['grid', 'pvinverter', 'genset', 'acload']))
		# 		self.services[cur_instance].add_item(TextItem('/Role', role, writeable=True,
		# 			onchange=self.role_changed))

		# 		self.services[cur_instance].add_item(TextItem('/Phase', phase, writeable=True,
		# 			onchange=self.phase_changed))

		# 		# Position for pvinverter
		# 		if role == 'pvinverter':
		# 			self.services[cur_instance].add_item(IntegerItem('/Position',
		# 				settings.get_value(settings.alias("position")),
		# 				writeable=True, onchange=self.position_changed))

		# 		# Meter paths
		# 		prefix = "/Ac/L" + str(phase)				
		# 		self.services[cur_instance].add_item(DoubleItem(prefix + '/Voltage', None, text=unit_volt))
		# 		self.services[cur_instance].add_item(DoubleItem(prefix + '/Current', None, text=unit_amp))
		# 		self.services[cur_instance].add_item(DoubleItem(prefix + '/Power', None, text=unit_watt))
		# 		self.services[cur_instance].add_item(DoubleItem(prefix + '/Energy/Forward', None, text=unit_kwh))
		# 		self.services[cur_instance].add_item(DoubleItem(prefix + '/Energy/Reverse', None, text=unit_kwh))

		# 	return True

		# 	## End of: Handle Pro EM and Pro 3EM in monophase setup
		# else:
		# 	## Begin of: HandlePro 3EM in 3phase setup

		# 	await settings.add_settings(
		# 			Setting(settingprefix + "/ClassAndVrmInstance", "grid:40", 0, 0, alias="instance"),
		# 			Setting(settingprefix + '/Position', 0, 0, 2, alias="position"),
		# 			Setting(settingprefix + '/Phase1Position', 1, 1, 3, alias="phase1position")
		# 		)
		# 	# Determine role and instance
		# 	role, instance = self.role_instance(
		# 		settings.get_value(settings.alias("instance")))
		# 	# Determine phase 1 position
		# 	self.phase1position = settings.get_value(settings.alias("phase1position"))

		# 	# Set up the service
		# 	self.services[0] = await Service.create(bus, "com.victronenergy.{}.shelly_{}".format(role, mac))

		# 	self.services[0].add_item(TextItem('/Mgmt/ProcessName', MAIN_FILE))
		# 	self.services[0].add_item(TextItem('/Mgmt/ProcessVersion', VERSION))
		# 	self.services[0].add_item(TextItem('/Mgmt/Connection', f"WebSocket {host}:{port}"))
		# 	self.services[0].add_item(IntegerItem('/DeviceInstance', instance))
		# 	self.services[0].add_item(IntegerItem('/ProductId', 0xB034, text=unit_productid))
		# 	self.services[0].add_item(TextItem('/ProductName', "Shelly energy meter"))
		# 	if name is not None:
		# 			self.services[0].add_item(TextItem('/CustomName', name))
		# 	self.services[0].add_item(TextItem('/FirmwareVersion', fw))
		# 	self.services[0].add_item(IntegerItem('/Connected', 1))
		# 	self.services[0].add_item(IntegerItem('/RefreshTime', 100))

		# 	# Role
		# 	self.services[0].add_item(TextArrayItem('/AllowedRoles',
		# 		['grid', 'pvinverter', 'genset', 'acload']))
		# 	self.services[0].add_item(TextItem('/Role', role, writeable=True,
		# 		onchange=self.role_changed))

		# 	self.services[0].add_item(TextItem('/Phase', self.phase1position, writeable=True,
		# 		onchange=self.phase_changed))

		# 	# Position for pvinverter
		# 	if role == 'pvinverter':
		# 		self.services[0].add_item(IntegerItem('/Position',
		# 			settings.get_value(settings.alias("position")),
		# 			writeable=True, onchange=self.position_changed))

		# 	# Meter paths
		# 	self.services[0].add_item(DoubleItem('/Ac/Energy/Forward', None, text=unit_kwh))
		# 	self.services[0].add_item(DoubleItem('/Ac/Energy/Reverse', None, text=unit_kwh))
		# 	self.services[0].add_item(DoubleItem('/Ac/Power', None, text=unit_watt))
		# 	for prefix in (f"/Ac/L{x}" for x in range(1, 4)):
		# 		self.services[0].add_item(DoubleItem(prefix + '/Voltage', None, text=unit_volt))
		# 		self.services[0].add_item(DoubleItem(prefix + '/Current', None, text=unit_amp))
		# 		self.services[0].add_item(DoubleItem(prefix + '/Power', None, text=unit_watt))
		# 		self.services[0].add_item(DoubleItem(prefix + '/Energy/Forward', None, text=unit_kwh))
		# 		self.services[0].add_item(DoubleItem(prefix + '/Energy/Reverse', None, text=unit_kwh))

		# 	return True

		# 	## End of: HandlePro 3EM in 3phase setup

	def destroy(self):
		if self.localmeters is not None:
			for meter in self.localmeters:
				if not meter.destroyed:
					meter.destroy()
		self.destroyed = True
	
	async def update(self, data):

		# Check if meters maybe killed themselves to enforce restart
		for meter in self.localmeters:
			if meter.destroyed:
				self.destroy()	#We kill ourselfs if one of the submeters needs a restart

		# NotifyStatus has power, current, voltage and energy values		
		try:
			if (self.localmeters is not None) and data.get('method') == 'NotifyStatus':
				# Forward data to the meters
				try:
					d = data['params']
				except KeyError:
					pass
				else:
					# See which data came in and pass it
					for meter in self.localmeters:
						for key in self.localmeterskeys[meter]:
							try:
								emdata = d[key]
							except KeyError:
								pass
							else:
								# Tag to be identifyable
								emdata['helpertag'] = key
								await self.localmeters[meter].update(emdata)					
		except:
			pass

			# try:
			# 	d = data['params']['emdata:0']
			# except KeyError:
			# 	pass
			# else:
			# 	with self.service as s:
			# 		s["/Ac/Energy/Forward"] = round(d["total_act"]/1000, 1)
			# 		s["/Ac/Energy/Reverse"] = round(d["total_act_ret"]/1000, 1)
			# 		s["/Ac/L1/Energy/Forward"] = round(d["a_total_act_energy"]/1000, 1)
			# 		s["/Ac/L1/Energy/Reverse"] = round(d["a_total_act_ret_energy"]/1000, 1)
			# 		s["/Ac/L2/Energy/Forward"] = round(d["b_total_act_energy"]/1000, 1)
			# 		s["/Ac/L2/Energy/Reverse"] = round(d["b_total_act_ret_energy"]/1000, 1)
			# 		s["/Ac/L3/Energy/Forward"] = round(d["c_total_act_energy"]/1000, 1)
			# 		s["/Ac/L3/Energy/Reverse"] = round(d["c_total_act_ret_energy"]/1000, 1)


			#		if (self.localmeters is not None) and data.get('method') == 'NotifyStatus':
			# # Forward data to the meters
			# try:
			# 	d = data['params']['em:0']
			# except KeyError:
			# 	pass
			# else:
			# 	with self.service as s:
			# 		s['/Ac/L1/Voltage'] = d["a_voltage"]
			# 		s['/Ac/L2/Voltage'] = d["b_voltage"]
			# 		s['/Ac/L3/Voltage'] = d["c_voltage"]
			# 		s['/Ac/L1/Current'] = d["a_current"]
			# 		s['/Ac/L2/Current'] = d["b_current"]
			# 		s['/Ac/L3/Current'] = d["c_current"]
			# 		s['/Ac/L1/Power'] = d["a_act_power"]
			# 		s['/Ac/L2/Power'] = d["b_act_power"]
			# 		s['/Ac/L3/Power'] = d["c_act_power"]

			# 		s['/Ac/Power'] = d["a_act_power"] + d["b_act_power"] + d["c_act_power"]

			# try:
			# 	d = data['params']['emdata:0']
			# except KeyError:
			# 	pass
			# else:
			# 	with self.service as s:
			# 		s["/Ac/Energy/Forward"] = round(d["total_act"]/1000, 1)
			# 		s["/Ac/Energy/Reverse"] = round(d["total_act_ret"]/1000, 1)
			# 		s["/Ac/L1/Energy/Forward"] = round(d["a_total_act_energy"]/1000, 1)
			# 		s["/Ac/L1/Energy/Reverse"] = round(d["a_total_act_ret_energy"]/1000, 1)
			# 		s["/Ac/L2/Energy/Forward"] = round(d["b_total_act_energy"]/1000, 1)
			# 		s["/Ac/L2/Energy/Reverse"] = round(d["b_total_act_ret_energy"]/1000, 1)
			# 		s["/Ac/L3/Energy/Forward"] = round(d["c_total_act_energy"]/1000, 1)
			# 		s["/Ac/L3/Energy/Reverse"] = round(d["c_total_act_ret_energy"]/1000, 1)

# 	def role_instance(self, value):
# 		val = value.split(':')
# 		return val[0], int(val[1])

# 	def settings_changed(self, service, values):
# 		# Kill service, driver will restart us soon

# 		#Setting(settingprefix + "/ClassAndVrmInstance", "grid:40", 0, 0, alias="instance"),
# 		#Setting(settingsprefix_cur + "/ClassAndVrmInstance", "grid:40", 0, 0, alias=alias_instance(cur_instance)),
# 		#settingsprefix_cur = settingprefix + "_em" + str(cur_instance)
# 		if service.alias("instance") in values:
# 			self.destroy()

# ###################################### How to find proper instance in monophase?
# 	def role_changed(self, val):
# 		if val not in ['grid', 'pvinverter', 'genset', 'acload']:
# 			return False

# 		settings = self.get_settings()
# 		if settings is None:
# 			return False

# 		p = settings.alias("instance")
# 		role, instance = self.role_instance(settings.get_value(p))
# 		settings.set_value(p, "{}:{}".format(val, instance))

# 		self.destroy() # restart
# 		return True

# ###################################### How to find proper instance in monophase?
# 	def position_changed(self, val):
# 		if not 0 <= val <= 2:
# 			return False

# 		settings = self.get_settings()
# 		if settings is None:
# 			return False

# 		settings.set_value(settings.alias("position"), val)
# 		return True

# ###################################### How to find proper instance in monophase?
# 	def phase_changed(self, val):
# 		if not 1 <= val <= 3:
# 			return False

# 		settings = self.get_settings()
# 		if settings is None:
# 			return False

# 		settings.set_value(settings.alias("phase"), val)

# 		# In case a single phase meter has changed connected phase, we must restart
# 		if self.phase3 == False:
# 			self.destroy() # restart
# 		return True
