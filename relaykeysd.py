# -*- coding: utf-8 -*-

#
# The control daemon for handling BLE HID
# communication with http-JSON-RPC
#


# To debug - without the correct hardware attached:
#   Run demoSerial.py 
#   Call this code with relayKeys.py --noserial 

import os, serial
from time import sleep
from sys import exit
import sys
import serial.tools.list_ports
from hashlib import sha256
from threading import Thread
from queue import Queue, Empty as QueueEmpty
import array as arr

if os.name =='posix': # unix based OS
  from daemon import DaemonContext
  from lockfile.pidlockfile import PIDLockFile

# util modules
import logging
import argparse
from configparser import ConfigParser
import traceback

# rpc modules
from werkzeug.wrappers import Request, Response
from werkzeug.serving import make_server, BaseRequestHandler
from jsonrpc import JSONRPCResponseManager, Dispatcher
from jsonrpc.jsonrpc2 import JSONRPC20Response

from blehid import blehid_send_keyboardcode, blehid_init_serial

# from pygame.locals import *
# from pygame.compat import as_bytes
# BytesIO = pygame.compat.get_BytesIO()

#Use AT+BAUDRATE=115200 but make sure hardware flow control CTS/RTS works
BAUD = 115200
#Doesnt do much currently
#OS = 'ios' 
RETRY_TIMEOUT=10

parser = argparse.ArgumentParser(description='Relay keys daemon, BLEHID controller.')
parser.add_argument('--noserial', dest='noserial', action='store_const',
                    const=True, default=False,
                    help='debug option to run the daemon with no hardware (with help of demoSerial.py')
parser.add_argument('--debug', dest='debug', action='store_const',
                    const=True, default=False,
                    help='set logger to debug level')
parser.add_argument('--daemon', '-d', dest='daemon', action='store_const',
                    const=True, default=False,
                    help='Run as daemon, posfix specific')
parser.add_argument('--pidfile', dest='pidfile', default=None,
                    help='file to hold pid of daemon, posfix specific')
parser.add_argument('--logfile', dest='logfile', default=None,
                    help='file to hold pid of daemon, posfix specific')
parser.add_argument('--config', '-c', dest='config',
                    default=None, help='Path to config file')

class RPCServExitException (BaseException):
  pass

class RequestHandler (BaseRequestHandler):
  def log(self, type, message, *args):
    # treat `info` logs as `debug
    lvl = logging.DEBUG
    if type == 'error':
      lvl = logging.ERROR
    logging.log(lvl, '%s - - [%s] %s\n' % (self.address_string(),
                                           self.log_date_time_string(),
                                           message % args))

def rpc_server_worker(host, port, username, password, queue):
  dispatcher = Dispatcher()
  srv = None
  # prehash password
  password = int(sha256(bytes(password, "utf8")).hexdigest(), 16)
  
  @dispatcher.add_method
  def keyevent (args):
    key, modifiers, down = args
    respqueue = Queue(1)
    queue.put(("keyevent", respqueue, key, modifiers or [], down), True)
    try:
      return respqueue.get(True, 5)
    except QueueEmpty:
      return "TIMEOUT"
  
  @dispatcher.add_method
  def exit (args):
    respqueue = Queue(1)
    queue.put(("exit", respqueue), True)
    try:
      respqueue.get(True, 5)
    except:
      pass
    raise RPCServExitException()

  @Request.application
  def app (request):
    # auth with simplified version of equal op (timing attack secure)
    if (username != "" or password != "") and \
       (getattr(request.authorization,"username","") != username or \
        int(sha256(bytes(getattr(request.authorization,"password",""), "utf8")).hexdigest(), 16) - \
        password != 0):
      json = JSONRPC20Response(error={"code":403, "message": "Invalid username or password!"}).json
      return Response(json, 403, mimetype='application/json')
    response = JSONRPCResponseManager.handle(request.data, dispatcher)
    return Response(response.json, mimetype='application/json')

  try:
    # response queue is used to notify result of attempt to run the server
    respqueue = queue.get()
    srv = make_server(host, port, app, request_handler=RequestHandler)
    logging.info("http-jsonrpc listening at {}:{}".format(host, port))
    queue.task_done() # let run_rpc_server return
    respqueue.put("SUCCESS")
    srv.serve_forever()
  except RPCServExitException:
    logging.info("Exit exception raised!")
  except:
    queue.task_done()
    respqueue.put("FAIL")
    logging.error(traceback.format_exc())

# run rpc server on another thread
def run_rpc_server (host, port, username, password):
  queue = Queue(10)
  respqueue = Queue(1)
  queue.put(respqueue)
  t = Thread(target=rpc_server_worker, args=(host, port, username, password,
                                             queue))
  t.daemon = True
  t.start()
  if respqueue.get(True) == "FAIL":
    return None
  return queue


def find_device_path (noserial):
  dev = None
  if noserial: 
    if os.name =='posix':
      serialdemofile = os.path.join(os.path.dirname(os.path.realpath(__file__)), '.serialDemo')
      if os.path.isfile(serialdemofile):
        with open(serialdemofile, 'r') as f:
          dev = f.read()
      else:
        logging.critical('no-serial is set to true.. Please make sure you have already run \'python resources\demoSerial.py\' from a different shell')
        exit(-1)
    elif (os.name=='nt'):
      dev = 'COM7'
  else:
    # Default names
    if (os.name=='posix'):
      dev = '/dev/ttyUSB0'
    else:
      dev = 'COM6'
      # Look for Adafruit CP2104 break out board or Feather nRF52. Use the first 
      # one found. Default is /dev/ttyUSB0 Or COM6 (Windows)
      # tty for Bluetooth device with baud
      # NB: Could be p.device with a suitable name we are looking for. Noticed some variation around this
    for p in serial.tools.list_ports.comports():
      if "CP2104" in p.description:
        logging.debug('serial desc:'+ str(p))
        dev = p.device
        break
      elif "nRF52" in p.description:
        logging.debug('serial desc:'+ str(p))
        dev = p.device
        break
  return dev

def do_main (args, config):
  # actions queue
  queue = run_rpc_server(config.get("host", "localhost"),
                         config.getint("port", 5383),
                         config.get("username", ""),
                         config.get("password", ""))
  try: # remove password from memory
    del config["password"]
  except:
    pass
  if queue is None:
    logging.critical("Could not start rpc server")
    return -1 # exit the process
  while True:
    command = None
    try:
      devicepath = find_device_path(args.noserial)
      with serial.Serial(devicepath, BAUD, rtscts=1) as ser:
        logging.info("serial device opened: {}".format(devicepath))
        blehid_init_serial(ser)
        # Six keys for USB keyboard HID report
        # uint8_t keys[6] = {0,0,0,0,0,0}
        keys = arr.array('B', [0, 0, 0, 0, 0, 0])
        while True:
          #try:
          #  command = queue.get(True, 1) # with timeout
          #except QueueEmpty:
          #  pass
          command = queue.get(True) 
          if command[0] == "keyevent":
            key = command[2]
            modifiers = command[3]
            down = command[4]
            blehid_send_keyboardcode(ser, key, modifiers, down, keys)
            ser.flushInput()
          queue.task_done()
          # response queue given by command
          command[1].put("SUCCESS")
          if command[0] == "exit":
            return 0
          command = None
    except (KeyboardInterrupt,SystemExit):
      if command != None:
        command[1].put("FAIL")
      break # exit
    except:
      if command != None:
        command[1].put("FAIL")
      logging.error(traceback.format_exc())
      print("Will retry in {} seconds".format(RETRY_TIMEOUT))
      sleep(RETRY_TIMEOUT)
  return 0

def init_logger (isdaemon, args, config):
  # init logger
  loggerformatter = logging.Formatter('%(asctime)-15s: %(message)s')
  logger = logging.getLogger()
  # logging stdout handler
  if not isdaemon:
    handler = logging.StreamHandler()
    handler.setFormatter(loggerformatter)
    logger.addHandler(handler)
  # logging file handler
  logfile = config.get("logfile", None) if args.logfile is None else args.logfile
  if logfile is not None:
    handler = logging.FileHandler(logfile)
    handler.setFormatter(loggerformatter)
    logger.addHandler(handler)
  logger.setLevel(logging.INFO)
  if args.debug:
    logger.setLevel(logging.DEBUG)
  if logfile is None and isdaemon: # disable logger
    logging.disable(sys.maxsize)
  
def main ():
  args = parser.parse_args()
  config = ConfigParser()
  config.read([os.path.expanduser('~/.relaykeys.cfg') if args.config is None else args.config])
  if "server" not in config.sections():
    config["server"] = {}
  serverconfig = config["server"]
  isdaemon = args.daemon or serverconfig.getboolean("daemon", False) if os.name =='posix' else False
  if isdaemon:
    pidfile = os.path.realpath(args.pidfile or serverconfig.get("pidfile", None))
    with DaemonContext(working_directory=os.getcwd(),
                       pidfile=PIDLockFile(pidfile)):
      init_logger(True, args, serverconfig)
      return do_main(args, serverconfig)
  else:
    init_logger(False, args, serverconfig)
    return do_main(args, serverconfig)

  
# MAIN
if __name__ == '__main__':
  if os.name == 'nt':
    # win32 service impl
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    import socket
    class AppServerSvc (win32serviceutil.ServiceFramework):
      _svc_name_ = "RelayKeysDameon"
      _svc_display_name_ = "Relay Keys Daemon"

      def __init__(self,args):
        win32serviceutil.ServiceFramework.__init__(self,args)
        self.hWaitStop = win32event.CreateEvent(None,0,0,None)
        socket.setdefaulttimeout(60)

      def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

      def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_,''))
        self.main()

      def main(self):
        main()

    win32serviceutil.HandleCommandLine(AppServerSvc)
  else:
    ret = main()
    exit(ret)
    
