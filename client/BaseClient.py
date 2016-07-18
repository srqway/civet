import logging, logging.handlers
from JobGetter import JobGetter
from JobRunner import JobRunner
from ServerUpdater import ServerUpdater
from InterruptHandler import InterruptHandler
import os, signal
import time
import traceback

import logging
logger = logging.getLogger("civet_client")

from threading import Thread
from Queue import Queue

def has_handler(handler_type):
  """
  Check to see if a handler is already installed.
  Normally this isn't a problem but when running tests it might be.
  """
  for h in logger.handlers:
    # Use type instead of isinstance since the types have
    # to match exactly
    if type(h) == handler_type:
      return True
  return False

def setup_logger(log_file=None):
  """
  Setup the "civet_client" logger.
  Input:
    log_file: If not None then a RotatingFileHandler is installed. Otherwise a logger to console is used.
  """
  formatter = logging.Formatter('%(asctime)-15s:%(levelname)s:%(message)s')
  fhandler = None
  if log_file:
    if has_handler(logging.handlers.RotatingFileHandler):
      return
    fhandler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1024*1024*5, backupCount=5)
  else:
    if has_handler(logging.StreamHandler):
      return
    fhandler = logging.StreamHandler()

  fhandler.setFormatter(formatter)
  logger.addHandler(fhandler)
  logger.setLevel(logging.DEBUG)

class ClientException(Exception):
  pass

class BaseClient(object):
  """
  This is the job server client. It polls the server
  for new jobs, requests one, and then runs it.
  While running a job it reports back with output
  from the job. During this operation the server
  can respond with commands to the the client. Mainly
  to cancel the job.
  """
  def __init__(self, client_info):
    self.client_info = client_info
    self.command_q = Queue()

    if self.client_info["log_file"]:
      self.set_log_file(self.client_info["log_file"])
    elif self.client_info["log_dir"]:
      self.set_log_dir(self.client_info["log_dir"])
    else:
      raise ClientException("log file not set")

    setup_logger(self.client_info["log_file"])

    try:
      self.cancel_signal = InterruptHandler(self.command_q, sig=[signal.SIGUSR1, signal.SIGINT])
      self.graceful_signal = InterruptHandler(self.command_q, sig=[signal.SIGUSR2])
    except:
      # On Windows, SIGUSR1, SIGUSR2 are not defined. Signals don't
      # work in general so this is the easiest way to disable
      # them but leave all the code in place.
      self.cancel_signal = InterruptHandler(self.command_q, sig=[])
      self.graceful_signal = InterruptHandler(self.command_q, sig=[])

    if self.client_info["ssl_cert"]:
      self.client_info["ssl_verify"] = self.client_info["ssl_cert"]

  def set_log_dir(self, log_dir):
    """
    Sets the log dir. If log_dir is set
    the log file name will have a set name of "civet_client_<name>_<pid>.log"
    raises Exception if the directory doesn't exist or isn't writable.
    """
    if not log_dir:
      return

    log_dir = os.path.abspath(log_dir)
    self.check_log_dir(log_dir)
    self.client_info["log_file"] = "%s/civet_client_%s.log" % (log_dir, self.client_info["client_name"])

  def check_log_dir(self, log_dir):
    """
    Makes sure the log directory exists and is writable
    Input:
      log_dir: The directory to check if we can write a log file
    Raises:
      ClientException if unable to write
    """
    if not os.path.isdir(log_dir):
      raise ClientException('Log directory (%s) does not exist!' % log_dir)

    if not os.access(log_dir, os.W_OK):
      raise ClientException('Log directory (%s) is not writeable!' % log_dir)

  def set_log_file(self, log_file):
    """
    Specify a log file to use.
    Input:
      log_file: The log file to write to
    Raises:
      ClientException if we can't write to the file
    """
    if not log_file:
      return

    log_file = os.path.abspath(log_file)

    log_dir = os.path.dirname(log_file)
    self.check_log_dir(log_dir)
    self.client_info["log_file"] = log_file

  def run_claimed_job(self, server, servers, claimed):
    job_info = claimed["job_info"]
    job_id = job_info["job_id"]
    message_q = Queue()
    runner = JobRunner(self.client_info, job_info, message_q, self.command_q)
    self.cancel_signal.set_message({"job_id": job_id, "command": "cancel"})

    control_q = Queue()
    updater = ServerUpdater(server, self.client_info, message_q, self.command_q, control_q)
    for entry in servers:
      if entry != server:
        control_q.put({"server": entry, "message": "Running job on another server"})
      else:
        control_q.put({"server": entry, "message": "Job {}: {}".format(job_id, job_info["recipe_name"])})

    updater_thread = Thread(target=ServerUpdater.run, args=(updater,))
    updater_thread.start();
    runner.run_job()
    if not runner.stopped and not runner.canceled:
      logger.info("Joining message_q")
      message_q.join()
    control_q.put({"command": "Quit"})
    logger.info("Joining ServerUpdater")
    updater_thread.join()
    self.command_q.queue.clear()

  def run(self):
    """
    Main client loop. Polls the server for jobs and runs them.
    """

    while True:
      do_poll = True
      try:
        getter = JobGetter(self.client_info)
        claimed = getter.find_job()
        if claimed:
          server = self.client_info["server"]
          self.run_claimed_job(server, [server], claimed)
          # finished the job, look for a new one immediately
          do_poll = False
      except Exception as e:
        logger.warning("Error: %s" % traceback.format_exc(e))

      if self.cancel_signal.triggered or self.graceful_signal.triggered:
        logger.info("Received signal...exiting")
        break
      if self.client_info["single_shot"]:
        break

      if do_poll:
        time.sleep(self.client_info["poll"])