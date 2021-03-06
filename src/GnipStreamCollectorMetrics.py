#!/usr/bin/env python
__author__ = 'scott hendrickson'

import os
import time
import logging
import logging.handlers
import ssl
import base64
import zlib
import sys
import socket
import signal
if sys.version_info.major == 3:
    import http.client as httplib
    import configparser as ConfigParser
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError,URLError 
elif sys.version_info.major == 2:
    import httplib
    import ConfigParser
    from urllib2 import HTTPError,URLError,Request,urlopen

CHUNK_SIZE = 2**17        # decrease for v. low volume streams, > max record size
GNIP_KEEP_ALIVE = 30      # 30 sec gnip timeout
MAX_BUF_SIZE = 2**22      # bytes records to hold in memory
MAX_ROLL_SIZE = 2**30     # force time-period to roll forward 
DELAY_FACTOR = 1.5        # grow by DELAY_FACTOR - 1 % with each failed connection
DELAY_MAX = 150           # maximum delay in seconds
DELAY_MIN = 0.1           # minimum delay in seconds
DELAY_RESET = 60*10       # Connected for the long, then reset the delay to min
NEW_LINE = '\r\n'

class GnipStreamClient(object):
    def __init__(self, _streamURL, _streamName, _userName, _password,
            _filePath, _rollDuration, _procThread, compressed=True):
        logr.info('GnipStreamClient started')
        self.compressed = compressed
        logr.info('Stream compressed: %s'%str(self.compressed))
        self.rollDuration = _rollDuration
        self.streamName = _streamName
        self.streamURL = _streamURL
        self.filePath = _filePath
        # this is a list
        self.procThread = _procThread
        if sys.version_info.major == 3:
            byte_user_and_pwd = ('%s:%s'%(_userName, _password)).encode('utf-8')
            encoded_u_and_p = base64.encodestring(byte_user_and_pwd).strip()
            string_auth = 'Basic ' + encoded_u_and_p.decode('utf-8')
        elif sys.version_info.major == 2:
            string_auth = 'Basic %s'%base64.encodestring('%s:%s'%(_userName, _password)).strip()
        self.headers = { 'Accept': 'application/json',
            'Connection': 'Keep-Alive',
            'Accept-Encoding' : 'gzip',
            'Authorization' : string_auth
            }
    
    def run(self, **kwargs):
        self.time_roll_start = time.time()
        delay_reset = time.time()
        delay = DELAY_MIN
        while True:
            try:
                self.getStream(**kwargs)
                logr.error("Forced disconnect")
                delay = DELAY_MIN
            except ssl.SSLError as e:
                delay = delay*DELAY_FACTOR if delay < DELAY_MAX else DELAY_MAX
                logr.error("Connection failed: %s (delay %2.1f s)"%(e, delay))
            except httplib.IncompleteRead as e:
                logr.error("Streaming chunked-read error (data chunk lost): %s"%e)
                # no delay increase here, just reconnect
            except HTTPError as e:
                logr.error("HTTP error: %s"%e)
                # no delay increase here, just reconnect
            except URLError as e:
                delay = delay*DELAY_FACTOR if delay < DELAY_MAX else DELAY_MAX
                logr.error("URL error: %s (delay %2.1f s)"%(e, delay))
            except socket.error as e:
                # Likely reset by peer (why?)
                delay = delay*DELAY_FACTOR if delay < DELAY_MAX else DELAY_MAX
                logr.error("Socket error: %s (delay %2.1f s)"%(e, delay))
            if time.time() - delay_reset > DELAY_RESET:
                # if we have been connected for a long time before this error,
                # then reset the delay
                delay = DELAY_MIN
            delay_reset = time.time()
            time.sleep(delay)

    def getStream(self, **kwargs):
        logr.info("Connecting")
        req = Request(self.streamURL, headers=self.headers)
        response = urlopen(req, timeout=(1+GNIP_KEEP_ALIVE))
        # sometimes there is a delay closing the connection, can go directly to the socket to control this
        # and in python 3 this looks a little different
        if sys.version_info.major == 3:
            realsock = response.fp.raw._sock
        elif sys.version_info.major == 2:
            realsock = response.fp._sock.fp._sock
        try:
            decompressor = zlib.decompressobj(16+zlib.MAX_WBITS)
            self.string_buffer = ''
            roll_size = 0
            while True:
                if self.compressed:
                    chunk = decompressor.decompress(response.read(CHUNK_SIZE))
                else:
                    chunk = response.read(CHUNK_SIZE)
                # if chunk is zero length, no longer connected to gnip
                if chunk == '':
                    return
                self.string_buffer += chunk.decode('utf-8')
                test_time = time.time()
                test_roll_size = roll_size + len(self.string_buffer)
                if self.triggerProcess(test_time, test_roll_size):
                    if test_roll_size == 0:
                        logr.info("No data collected this period (testTime=%s)"%test_time)
                    # occasionally new lines are missing
                    self.string_buffer.replace("}{", "}%s{"%NEW_LINE)
                    # only splits on new lines
                    [records, self.string_buffer] = self.string_buffer.rsplit(NEW_LINE,1)
                    timeSpan = test_time - self.time_roll_start
                    logr.debug("recsize=%d, %s, %s, ts=%d, dur=%d"%
                            (len(records), self.streamName, self.filePath, 
                                test_time, timeSpan))
                    for p in self.procThread:
                        #print(p)
                        p(records, 
                          self.streamName, 
                          self.filePath,
                          logr, 
                          self.time_roll_start, 
                          timeSpan,
                          **kwargs).start()
                    if self.rollForward(test_time, test_roll_size):
                        self.time_roll_start = test_time
                        roll_size = 0
                    else:
                        roll_size += len(records)
        except Exception as e:
            logr.error("Buffer processing error (%s) - restarting connection"%e)
            realsock.close() 
            response.close()
            raise e

    def rollForward(self, ttime, tsize):
        # these trigger both processing and roll forward
        if ttime - self.time_roll_start >= self.rollDuration:
            logr.debug("Roll: duration (%d>=%d)"%
                    (ttime-self.time_roll_start, self.rollDuration))
            return True
        if tsize >= MAX_ROLL_SIZE:
            logr.debug("Roll: size (%d>=%d)"%
                    (tsize, MAX_ROLL_SIZE))
            return True
        return False

    def triggerProcess(self, ttime, tsize):
        # trigger process?
        # no trigger if no NEW_LINE
        if NEW_LINE not in self.string_buffer:
            return False
        if len(self.string_buffer) > MAX_BUF_SIZE:
            logr.debug("Trigger: buffer size (%d>%d)"%
                    (len(self.string_buffer), MAX_BUF_SIZE))
            return True
        return self.rollForward(ttime, tsize)

if __name__ == '__main__':
    # try grabbing the config file from sys.argv first
    # this makes it easier to launch multiple processes to get multiple streams 
    # (like FH partitions)
    try: 
        config_file_name = sys.argv[1] 
    except IndexError:
        if 'GNIP_CONFIG_FILE' in os.environ:
            config_file_name = os.environ['GNIP_CONFIG_FILE']
        else:
            config_file_name = "./gnip.cfg"
            if not os.path.exists(config_file_name):
                print("No configuration file found.")
                sys.exit()
    config = ConfigParser.ConfigParser()
    config.read(config_file_name)
    streamname = config.get('stream', 'streamname')
    # logger 
    logfilepath = config.get('sys','logfilepath')
    logr = logging.getLogger('GnipStreamLogger')
    rotating_handler = logging.handlers.RotatingFileHandler(
            filename=logfilepath + "/%s-log"%streamname,
            mode='a', maxBytes=2**24, backupCount=5)
    rotating_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(funcName)s %(message)s"))
    #logr.setLevel(logging.DEBUG)
    logr.setLevel(logging.INFO) 
    #logr.setLevel(logging.ERROR)
    logr.addHandler(rotating_handler)
    # set up authentication
    if config.has_section('auth'):
        username = config.get('auth','username')
        password = config.get('auth','password')
    elif config.has_section('creds'):
        username = config.get('creds','username')
        password = config.get('creds','password')
    else:
        logr.error("No credentials found")
        sys.exit()
    # stream
    streamurl = config.get('stream', 'streamurl')
    filepath = config.get('stream', 'filepath')
    # set db up authentication (optional)
    kwargs = {}
    if config.has_section('db'):
        kwargs["sql_user_name"] = config.get('db','sql_user_name')
        kwargs["sql_password"] = config.get('db','sql_password')
        kwargs["sql_instance"] = config.get('db','sql_instance')
        kwargs["sql_db"] = config.get('db','sql_db')
    if config.has_section('gnacs'):
        kwargs["options"] = config.get('gnacs','options')
        kwargs["delim"] = config.get('gnacs','delim')
    try:
        compressed = config.getboolean('stream', 'compressed')
    except ConfigParser.NoOptionError:
        compressed = True
    logr.info("Collection starting for stream %s"%(streamurl))
    logr.info("Storing data in path %s"%(filepath))
    
    # Determine processing method
    rollduration = int(config.get('proc', 'rollduration'))
    processtypes = config.get('proc', 'processtype').split(",")
    logr.info("processing strategy/strategies %s"%processtypes)
    
    proc = []
    for processtype in processtypes:
        if processtype == "latency":
            from Latency import Latency
            proc.append(Latency)
        elif processtype == "files":
            from SaveThread import SaveThread
            proc.append(SaveThread)
        elif processtype == "files-gnacs":
            if sys.version_info.major == 3:
                logr.error("The processtype 'files-gnacs' is not availble for python 3") 
            if sys.version_info.major == 2:
                from SaveThreadGnacs import SaveThreadGnacs
                proc.append(SaveThreadGnacs)
        elif processtype == "rules":
            from CountTwitterRules import CountTwitterRules
            proc.append(CountTwitterRules)
        elif processtype == "redis":
            from Redis import Redis
            proc.append(Redis)
        elif processtype == "fileandmetrics":
            from SaveThread import SaveThread
            from Metrics import Metrics
            if "sql_db" not in kwargs:
                logr.error("No database configured.")
                sys.exit()
            proc.append(SaveThread)
            proc.append(Metrics)
        else:
            import CustomOutput
            if processtype in dir(CustomOutput):
                proc.append(getattr(CustomOutput, processtype))
    if proc == []: 
        logr.error("No valid processing strategy selected (%s), aborting"%processtype)
        sys.exit(-1)
    # ok, do it
    client = GnipStreamClient(streamurl, 
                              streamname, 
                              username, 
                              password, 
                              filepath, 
                              rollduration, 
                              proc, 
                              compressed=compressed,
                              )
    client.run(**kwargs)
