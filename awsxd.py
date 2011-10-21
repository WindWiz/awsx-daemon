#! /usr/bin/env python
# Copyright (c) 2010-2011 Magnus Olsson (magnus@minimum.se)
# See LICENSE for details

"""awsxd - AWS-X GSM weather station daemon
This application implements an server for reception of AWS-X GSM weather 
station UDP packets. For each received packet, the contents will be decoded,
verified and inserted into the given MySQL database.

For details on the sensor hardware, see the DSP Promatic webpage at:
  http://www.dps-promatic.com/

For details on the GPRS functionality, check:
  http://www.dpspro.com/aws_gprs.htmlhttp://www.dpspro.com/aws_gprs.html
  
usage: awsxd [options]

options:
-p <port>		Server listen port number (default 9999)
-h <host>		Server listen address (defaults to localhost)
-v			Verbose output (may be used multiple times)
-s <str>		Simulate <str> input, process and exit
-c <path>		Launch <path> for each processed packet (station name passed as arg)
-f <path>		Config file (defaults to 'awsxd.conf')
-r <ip[:port]>		Replicate valid packets to given host and port (UDP)
-i <pidfile>		Process ID file (defaults to '/tmp/awsxd.pid')
"""

import SocketServer
import getopt
import sys
import MySQLdb
import os.path
import subprocess
import ConfigParser
import socket

global verbose
global callback
global config
global fwhost
global fwport

# Fancy name -> Part array index enum
class Pkt:
	CMD	= 0
	DATE	= 1
	TIME	= 2
	ID	= 3	
	SMSC	= 4
	SI	= 5
	AVGWND	= 6
	PRESS	= 7
	MINWND	= 8
	MAXWND	= 9
	DGUST	= 10
	LEAF	= 11
	WNDDIR	= 12
	WNDSTAB	= 13
	SUNRAD	= 14
	AIRTEMP	= 15
	DMINTMP	= 16
	DMAXTMP	= 17
	SOILT	= 18
	RAIN	= 19
	DRAIN	= 20
	SOILM	= 21
	DEWPT	= 22
	RH	= 23
	DMINRH	= 24
	DMAXRH	= 25
	PWR	= 26
	VBAT	= 27
	CSUM	= 28

def usage(*args):
        sys.stdout = sys.stderr
        print __doc__
        for msg in args: print msg
        sys.exit(2)

class AWSHandler(SocketServer.BaseRequestHandler):
	def handle(self):
		data = self.request[0].strip()
		socket = self.request[1]
		dbg("Received %d bytes from %s: " % (len(data), self.client_address[0]))
		dbg(data)
		process(data)		

def log(str):
	if (verbose > 0):
		print str
		
def dbg(str):
	if (verbose > 1):
		print str

""" Process a AWS-X packet. Recognized format (always on a SINGLE line):

$DPTAW,				# Command, always $DPTAW
<date>,				# Ex: 2010/03/21
<time>,				# Ex: 04:48
<station_name>,			# Unique name set by SMS for each station
<sms count>,			# SMS count, may be used for sequencing
<interval>,			# Current sample interval, in minutes (1-60)
<avg_windspeed>,		# Windspeed, in km/h
<air_pressure>,			# Atmospheric air pressure, in millibar
<wind_min>,			# Max windspeed during interval, in km/h
<wind_max>,			# Min windspeed during interval, in km/h
<dailygust>,			# Daily maxwindspeed, in km/h
<leaf>,				# Leaf wetness, 0=wet to 15=dry
<wdir>,				# Average wind direction during interval, in degs.
<wdir_stability>,		# Wind dir. variability, 0=steady to 15=unstable
<sun_radiation>,		# Solar radiation, in W/Mq
<avg_airtemp>,			# Average air temperature, in celcius degs.
<mindailytemp>,			# Daily min temperature, in celcius degs.
<maxdailytemp>,			# Daily max temperature, in celcius degs.
<soil_temp>,			# Soil temperature, in celcius degs
<rainfall>,			# Rainfall during interval, in mm (??)
<daily_rainfall>,		# Daily rainfall
<soil_moisture>,		# Soil moisture, 0=wet to 200=dry (Kpa)
<dewpoint>,			# Dewpoint (extrapolated)
<humidity>,			# Relative humidity
<daily minhumidity>,		# Daily minimum relative humidity
<daily maxhumidity>,		# Daily maximum relative humidity
<pwrtype>,			# Power supply (E=External, B=Battery)
<battery_voltage>		# Current battery voltage
<checksum>			# NMEA 0183-style checksum (8-bit XOR between $ and * delimiters)

Example:
	$DPTAW,2003/03/19,04:48,AWSTEST,0027,10,53,1199,47,61,83, \
	0,244,1,1531,16.0,15.0,17.0,102.0,0.0,0.0,0,13.0,84.9,82.0, \ 
	99.0,E,13.1,*56

"""
def process(str):	
	# Calculate checksum
	left = str.find("$")
	if (left == -1):
		log("Invalid packet (no $ present): %s" % str)
		return

	right = str.find("*")
	if (right == -1):
		log("Invalid packet (no * present): %s" % str)
		return

	payload = str[left+1:right]
	actual_checksum = 0
	for c in payload:
		actual_checksum = actual_checksum ^ ord(c)

	dbg("Payload checksum is %.2X" % actual_checksum)

	p = str.split(',')

	# Sanity checks
	if (len(p) != 29):
		log("Invalid packet (partcount): %s" % str)
		return
		
	if (p[Pkt.CMD] != "$DPTAW"):
		log("Invalid packet (id): %s" % str)
		return

	checksum = int(p[Pkt.CSUM][1:3], 16)
	if (actual_checksum != checksum):
		print "Checksum failed for '%s'. Calculated=0x%.2X Expected=0x%.2X" % (str, actual_checksum, checksum)
		return

	# Debug packet 
	dbg("%s, last %d minutes (sms #%d)" % (p[Pkt.ID], int(p[Pkt.SI]), int(p[Pkt.SMSC])))
	dbg("==============================")
	
	dbg("-- Date %s %s" % (p[Pkt.DATE], p[Pkt.TIME]))
	
	dbg("-- Wind current %d km/h, min %d km/h, max %d km/h, daily max %d km/h" % 
		(int(p[Pkt.AVGWND]), int(p[Pkt.MINWND]), int(p[Pkt.MAXWND]), int(p[Pkt.DGUST])))

	stability = (1 - (float(p[Pkt.WNDSTAB]) / 15))*100
	dbg("-- Wind direction %d degrees, stability %d percent" % 
		(int(p[Pkt.WNDDIR]), int(stability)))		
		
	dbg("-- Temp current %d C, daily min %d C, daily max %d C" % 
		(float(p[Pkt.AIRTEMP]), float(p[Pkt.DMINTMP]), float(p[Pkt.DMAXTMP])))

	# Insert into DB
	db = MySQLdb.connect(host=config.get('mysql', 'dbhost'), 
						 user=config.get('mysql', 'dbuser'),
						 passwd=config.get('mysql', 'dbpass'), 
						 db=config.get('mysql', 'dbname'))
	cur = db.cursor()
	
	# TODO: Clean this mess up; Needs proper prepared queries and injection protection
	q = """INSERT INTO awsx (tstamp, station, sms_counter, sample_interval, wind_avg, 
				 wind_min, wind_max, wind_daily_max, wind_dir, wind_stability, 
				 air_pressure, leaf_wetness, sun_radiation, temp_avg, 
				 temp_daily_min, temp_daily_max, soil_temp, rainfall,
				 rainfall_daily, soil_moisture, dewpoint, humidity,
				 humidity_daily_min, humidity_daily_max, power_supply,
				 battery_voltage) VALUES ('%s', '%s', '%s', '%s', '%s', '%s', '%s',
				 '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s', '%s',
				 '%s', '%s', '%s')""" % (p[Pkt.DATE]+" "+p[Pkt.TIME], p[Pkt.ID], p[Pkt.SMSC], p[Pkt.SI],
				 p[Pkt.AVGWND], p[Pkt.MINWND], p[Pkt.MAXWND], p[Pkt.DGUST], p[Pkt.WNDDIR],
				 p[Pkt.WNDSTAB], p[Pkt.PRESS], p[Pkt.LEAF], p[Pkt.SUNRAD], p[Pkt.AIRTEMP],
				 p[Pkt.DMINTMP], p[Pkt.DMAXTMP], p[Pkt.SOILT], p[Pkt.RAIN], p[Pkt.DRAIN],
				 p[Pkt.SOILM], p[Pkt.DEWPT], p[Pkt.RH], p[Pkt.DMINRH], p[Pkt.DMAXRH],
				 p[Pkt.PWR], p[Pkt.VBAT])
	dbg(q)
	if (not cur.execute(q)):
		log("Failed to insert record: '%s'" % q)
		return

	if (fwhost):
		sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		sock.sendto(str, (fwhost, fwport))
		dbg("Packet replicated for %s:%d." % (fwhost, fwport))
		sock.close()

	# Run callback (if any)
	if (callback):
		ret = subprocess.call([callback, p[Pkt.ID]])
		if (ret):
			print "Callback '%s' failed with retcode %d" % (callback, ret)

if __name__ == "__main__":
	config = ConfigParser.RawConfigParser({'dbhost': 'localhost',
										   'dbpass': '',
										   'dbuser': 'awsxd',
										   'dbname': 'awsxd'})

	pidfile = "/tmp/awsxd.pid"
	host = "localhost"
	port = 9999
	verbose = 0
	callback = False
	cfgfile = "awsxd.conf"
	simstr = False
	fwhost = None
	fwport = None
			
	try:
		opts, args = getopt.getopt(sys.argv[1:], 'p:h:vs:c:f:r:i:')
	except getopt.error, msg:
		usage(msg)

	for o, a in opts:
		if o == '-p': port = int(a)
		if o == '-v': verbose = verbose + 1
		if o == '-h': host = a
		if o == '-f': cfgfile = a
		if o == '-s': simstr = a
		if o == '-i': pidfile = a
		if o == '-r':
			hostport = a.split(':')
			if len(hostport) > 2:
				print "Invalid replication (-x) host '%s', aborting." % a
				sys.exit(1)

			fwhost = hostport[0]
			if len(hostport) == 2:
				fwport = int(hostport[1])

		if o == '-c':
			if (not os.path.isfile(a)):
				print "No such callback file '%s', aborting." % a 
				sys.exit(1)			
			if (not os.access(a, os.X_OK)):
				print "Specified callback file '%s' is not an executable." % a
				sys.exit(1)
			callback = a;

	log("Using config '%s'" % cfgfile)
	config.read(cfgfile)

	if (fwhost != None):
		if (fwport == None):
			fwport = port
		log("Replicating packets to %s:%d." % (fwhost, fwport))

	if (simstr):
		log("Simulating input: %s" % simstr)
		process(simstr)
	else:
		pid = str(os.getpid())
	
		if os.path.isfile(pidfile):
			print "%s already exists, exiting." % pidfile
			sys.exit(1)

	   	file(pidfile, 'w').write(pid)

		log("Listening at %s:%d" % (host, port))
		try:
			server = SocketServer.UDPServer((host, port), AWSHandler)
			server.serve_forever()
		except:
			os.unlink(pidfile)
			raise

