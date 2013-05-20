#! /usr/bin/env python
# Copyright (c) 2010-2013 Magnus Olsson (magnus@minimum.se)
# See LICENSE for details

"""awsxd - AWS-X GSM weather station daemon
This application implements an server for reception of AWS-X GSM weather
station UDP packets. For each received packet, the contents will be decoded,
verified and inserted into the given MySQL database.

For details on the sensor hardware, see the DSP Promatic webpage at:
  http://www.dps-promatic.com/

For details on the GPRS functionality, check:
  http://www.dpspro.com/aws_gprs.html
  http://www.dps-promatic.com/tcs_meteo_packet.html
  
usage: awsxd [options]

options:
-p <port>       Server listen port number (default 9999)
-h <host>       Server listen address (defaults to localhost)
-v              Verbose output (may be used multiple times)
-s <str>        Simulate <str> input, process and exit
-c <path>       Launch <path> for each processed packet (station name passed as arg)
-f <path>       Config file (defaults to 'awsxd.conf')
-r <ip[:port]>  Replicate valid packets to given host and port (UDP)
-i <pidfile>    Process ID file (defaults to '/tmp/awsxd.pid')
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

class NMEAException(Exception):
    pass

class NMEASentence:
    def __init__(self, str):
        left = str.find("$")
        if (left == -1):
            raise NMEAException("Invalid packet (no $ present)")

        right = str.find("*")
        if (right == -1):
            raise NMEAException("Invalid packet (no * present)")

        self.payload = str[left+1:right]

        actual_csum = self.checksum()
        expected_csum = int(str[right+1:], 16)

        if (actual_csum != expected_csum):
            raise NMEAException("Checksum mismatch (0x%02X != 0x%02X)" % (actual_csum, expected_csum))

        self.fields = str[left+1:right].split(',')

    def checksum(self):
        actual_checksum = 0
        for c in self.payload:
            actual_checksum = actual_checksum ^ ord(c)

        return actual_checksum

    def encode(self):
        return "$%s*%02X" % (self.payload, self.checksum())

class AWSException(Exception):
    pass

class AWSPacket(NMEASentence):
    def _parseDec(value):
        return int(value, 10)

    def _parseString(value):
        return value

    def _parseDate(value):
        return value

    def _parseTime(value):
        return value

    def _parseFloat(value):
        return float(value)

    _awsFields = [
        # Packet header, always 'DPTAW'
        { 'tag': "header", 'f': _parseString },
        # Date in yyyy/mm/dd format
        { 'tag': "date", 'f': _parseDate },
        # Time in hh:mm (24h) format
        { 'tag': "time", 'f': _parseTime },
        # Station ID (10)
        { 'tag': "id", 'f': _parseString },
        # SMS serial number (SMS counter)
        { 'tag': "smsc", 'f': _parseDec },
        # Sample interval 
        { 'tag': "si", 'f': _parseFloat },
        # Wind average speed ('si' period, 1 sample/second)
        { 'tag': "was", 'f': _parseFloat },
        # Air pressure (millibars)
        { 'tag': "wssd", 'f': _parseFloat },
        # Minimum wind speed ('si' period, 1 sample/second)
        { 'tag': "wmins", 'f': _parseFloat },  
        # Max wind gust (3s gusts) ('si' period, 1 sample/second)
        { 'tag': "wgust", 'f': _parseFloat },  
        # Daily gust (maximum gust of the day)
        { 'tag': "dwgust", 'f': _parseFloat },
        # Leaf wetness
        { 'tag': "leaf_wetness", 'f': _parseDec },
        # Average wind direction ('si' period, 1 sample/second)
        { 'tag': "wdir", 'f': _parseDec },
        # Wind direction, standard deviation
        { 'tag': "wdsd", 'f': _parseDec },
        # Solar radiation
        { 'tag': "sun", 'f': _parseDec },
        # Average temperature ('si' period, 1 sample/second)
        { 'tag': "temp", 'f': _parseFloat },
        # Daily minimum temperature
        { 'tag': "dmintemp", 'f': _parseFloat },
        # Daily maximum temperature
        { 'tag': "dmaxtemp", 'f': _parseFloat },
        # Soil temperature
        { 'tag': "soilt", 'f': _parseFloat },
        # Rainfall
        { 'tag': "rf", 'f': _parseFloat },
        # Daily rainfall
        { 'tag': "drf", 'f': _parseFloat },
        # Soil water potential
        { 'tag': "soilw", 'f': _parseDec },
        # Dew point
        { 'tag': "dp", 'f': _parseFloat },
        # Relative humidity
        { 'tag': "rh", 'f': _parseFloat },
        # Daily minimum relative humidity
        { 'tag': "dminrh", 'f': _parseFloat },
        # Daily maximum relative humidity
        { 'tag': "dmaxrh", 'f': _parseFloat },
        # Power supply type (E=External/Solar, B=Battery)
        { 'tag': "pwtype", 'f': _parseString },
        # Battery voltage
        { 'tag': "battvolt", 'f': _parseFloat },
        # Dummy (trailing comma -- always blank)
        { 'tag': "blank", 'f': _parseString }
    ]

    def __init__(self, str):
        NMEASentence.__init__(self, str)

        if (len(self.fields) != len(self._awsFields)):
            raise AWSException("Invalid fieldcount (%d)" % len(self.fields))

        if (self.fields[0] != "DPTAW"):
            raise AWSException("Unknown packet type %s" % self.fields[0])

        self._awsValues = {}
        try:
            for idx, field in enumerate(self._awsFields):
                self._awsValues[field["tag"]] = field["f"](self.fields[idx])
        except Exception as x:
            raise AWSException("Parse error for %s: %s (%s)" % (field["tag"], self.fields[idx], x))

    def __str__(self):
        return self._awsValues.__str__()

    def get(self, field):
        if field in self._awsValues:
            return self_awsValues[field]
        else:
            return None

def usage(*args):
    sys.stdout = sys.stderr
    print __doc__
    for msg in args: print msg
    sys.exit(2)

def log(str):
    if (verbose > 0):
        print str
		
def dbg(str):
    if (verbose > 1):
        print str

def run_callback(packet):
    ret = subprocess.call([callback, packet.get('id')])
    if (ret):
        print "Callback '%s' failed with retcode %d" % (callback, ret)

def forward_packet(pkt):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(pkt.encode(), (fwhost, fwport))
    dbg("Packet replicated for %s:%d." % (fwhost, fwport))
    sock.close()


def insert_database(pkt):
    db = MySQLdb.connect(host=config.get('mysql', 'dbhost'),
                         user=config.get('mysql', 'dbuser'),
                         passwd=config.get('mysql', 'dbpass'),
                         db=config.get('mysql', 'dbname'))
    cur = db.cursor()

    q = """INSERT INTO awsx
(
    tstamp, station, sms_counter, sample_interval, wind_avg,
    wind_min, wind_max, wind_daily_max, wind_dir, wind_stability,
    air_pressure, leaf_wetness, sun_radiation, temp_avg, temp_daily_min,
    temp_daily_max, soil_temp, rainfall, rainfall_daily, soil_moisture,
    dewpoint, humidity, humidity_daily_min, humidity_daily_max, power_supply,
    battery_voltage
)
VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s
)"""

    values = (pkt.get('date') + " " + pkt.get('time'),
              pkt.get('id'),
              pkt.get('smsc'),
              pkt.get('si'),
              pkt.get('was'),
              pkt.get('wmins'),
              pkt.get('wgust'),
              pkt.get('dwgust'),
              pkt.get('wdir'),
              pkt.get('wdsd'),
              pkt.get('wssd'),
              pkt.get('leaf_wetness'),
              pkt.get('sun'),
              pkt.get('temp'),
              pkt.get('dmintemp'),
              pkt.get('dmaxtemp'),
              pkt.get('soilt'),
              pkt.get('rf'),
              pkt.get('drf'),
              pkt.get('soilw'),
              pkt.get('dp'),
              pkt.get('rh'),
              pkt.get('dminrh'),
              pkt.get('dmaxrh'),
              pkt.get('pwtype'),
              pkt.get('battvolt'))

    if (not cur.execute(q, values)):
        expanded_q = q % values
        log("Failed to insert record: '%s'" % expanded_q)
        return False

    return True

def process(str, source = None):
    if source is not None:
        dbg("Received from %s" % source)
    dbg("Processing %d bytes: %s" % (len(str), str))

    try:
        packet = AWSPacket(str)
        
        log(packet)
        insert_database(packet)

        if fwhost:
            forward_packet(packet)

        if callback:
            run_callback(packet)
    except Exception as e:
        print(e)

class AWSHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        data = self.request[0]
        ip = self.client_address[0]
        process(data, ip)     

if __name__ == "__main__":
    config = ConfigParser.RawConfigParser({'dbhost': 'localhost',
                                           'dbpass': '',
                                           'dbuser': 'awsxd',
                                           'dbname': 'awsxd'})

    pidfile = "/tmp/awsxd.pid"
    host = "localhost"
    port = 9999
    verbose = 0
    callback = None
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
