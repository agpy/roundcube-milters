#!/usr/bin/env python2

# Search inside a shared database to check if the message is coming
# from a known email address stored in a user's address book.
# I use http://bmsi.com/python/milter.html
# This is actually compatible with Python2 / Debian Stretch.
# Since the package python3-milter is included in Debian Buster,
# a new version will be written for Python 3.

# Andre Rodier <andre@rodier.me>
# Licence: GPL v2

# Make sure pylint knows about the print function
from __future__ import print_function

import StringIO
import os
import ConfigParser
from socket import AF_INET6
from multiprocessing import Process as Thread, Queue
import mysql.connector

import Milter
from Milter.utils import parse_addr

# setup utf-8
import sys
reload(sys)
sys.setdefaultencoding('utf-8')

# Constants related to the systemd service
#SOCKET_PATH = "/var/spool/postfix/private/milter-rc-abook.socket"
PID_FILE_PATH = "/var/run/milter-rc-abook/main.pid"

GlobalLogQueue = Queue(maxsize=100) # type: Queue[str]

configParser = ConfigParser.RawConfigParser()
configFilePath = '/etc/roundcube/milters.conf'
configParser.read(configFilePath)

def searchInRoundCube(fromAddress, recipients, debug, dbConnection):
    """Search for an address in the RoundCube database."""
    if debug:
        print(
            "Search for an address in the RoundCube database. from: {}, recipients: {}".format(fromAddress, recipients))

    try:
        # Nothing found
        if not dbConnection:
            GlobalLogQueue.put("Could not connect to the database")
            return []

        sources = []

        for rec in recipients:
            parts = parse_addr(rec[0])
            uid = parts[0]
            email = '@'.join(parse_addr(rec[0]))
            email = email.lower()

            # First, get all the address books from this user
            tablesCursor = dbConnection.cursor()
            abQuery = ("select cg.name from contacts as c"
                       " join contactgroupmembers as cgm"
                       " on cgm.contact_id = c.contact_id"
                       " join contactgroups as cg"
                       " on cg.contactgroup_id=cgm.contactgroup_id"
                       " join users as u"
                       " on u.user_id = c.user_id"
                       " where u.username='{}' and"
                       " c.vcard like '%{}%' and"
                       " c.del = 0 and cg.del = 0"
                       .format(email, fromAddress))
            if debug:
                GlobalLogQueue.put("Query: '{}'".format(abQuery))

            tablesCursor.execute(abQuery)

            abooks = tablesCursor.fetchall()

            if debug:
                GlobalLogQueue.put("Total records found: {}".format(len(abooks)))

            # End to search in this recipient
            tablesCursor.close()

            # Store the address book name, or "default" when no name
            for abResult in abooks:
                if debug:
                    GlobalLogQueue.put("abResult: '{}'".format(abResult))

                # Get the first cell as a result
                abName = abResult[0]

                if abName:
                    source = "Roundcube:{}".format(abName)
                else:
                    source = "Roundcube:default"

                # Insert if not already inside.
                if not source in sources:
                    sources.append(source)

        if debug:
            GlobalLogQueue.put("Searched address {} for user {}: {} result(s)".format(
                fromAddress, uid, len(sources)))

    # Make sure to not prevent the message to pass if something happen,
    # but log the error
    except Exception as error:
        GlobalLogQueue.put("Error when searching in address database: {}".format(error.message))

    return sources

class MarkAddressBookMilter(Milter.Base):
    """Milter to search the sender address in RoundCube recipient's address books."""
 
# A new instance with each new connection.
    def __init__(self):

        # Integer incremented with each call.
        self.id = Milter.uniqueID()

        # mysql
        user = configParser.get('mysql', 'user')
        password = configParser.get('mysql', 'password')
        dbName = configParser.get('mysql', 'dbName')
        connectUrl = "mysql://{}:{}@localhost/{}"
        config = {
            'user': user,
            'password': password,
            'database': dbName,
            'host': '127.0.0.1'
        }
        self.dbConnection = mysql.connector.connect(**config)

        self.debug = configParser.getboolean('main', 'debug')

        if self.debug:
            self.queueLogMessage("Running in debug mode")

        if not self.dbConnection:
            print("Cannot open RoundCube database")
            raise "Cannot open RoundCube database"

    # Should be executed at the end of a message parsing
    def __exit__(self, exc_type, exc_val, exc_tb):

        if self.debug:
            self.queueLogMessage("Exit from milter address book")

        if self.dbConnection:
            self.dbConnection.close()

    # Each connection runs in its own thread and has its own
    # MarkAddressBookMilter instance.
    # Python code must be thread safe. This is trivial if only stuff
    # in MarkAddressBookMilter instances is referenced.
    @Milter.noreply
    def connect(self, hostname, family, hostaddr):
        self.IP = hostaddr[0]
        self.port = hostaddr[1]
        if family == AF_INET6:
            self.flow = hostaddr[2]
            self.scope = hostaddr[3]
        else:
            self.flow = None
            self.scope = None
            self.IPname = hostname    # Name from a reverse IP lookup
            self.H = None
            self.fp = None
            self.receiver = self.getsymval('j')

            if self.debug:
                self.queueLogMessage("connect from {} at {}".format(hostname, hostaddr))

        return Milter.CONTINUE

    def envfrom(self, fromAddress, *extra):
        self.mailFrom = '@'.join(parse_addr(fromAddress))
        self.recipients = []
        self.fromparms = Milter.dictfromlist(extra) # ESMTP parms
        self.user = self.getsymval('{auth_authen}') # authenticated user
        self.fp = StringIO.StringIO()
        return Milter.CONTINUE

    @Milter.noreply
    def envrcpt(self, to, *extra):
        rcptinfo = to, Milter.dictfromlist(extra)
        self.recipients.append(rcptinfo)
        return Milter.CONTINUE

    @Milter.noreply
    def header(self, field, value):
        self.fp.write("{}: {}\n".format(field, value))
        return Milter.CONTINUE

    @Milter.noreply
    def eoh(self):
        self.fp.write("\n")
        return Milter.CONTINUE

    @Milter.noreply
    def body(self, blk):
        self.fp.write(blk)
        return Milter.CONTINUE

    # Add the headers at the eom (End of Message) function.
    # This should work when the recipient is in any of To, CC or BCC headers
    def eom(self):

        # Need to be at the beginning to add headers
        self.fp.seek(0)

        # Include all the sources in the same header, joined by coma
        sources = searchInRoundCube(self.mailFrom, self.recipients, self.debug, self.dbConnection)

         if sources:
            sourceList = ','.join(sources)
            print("Source list: '{}'".format(sourceList))
            self.addheader("X-AddressBook", sourceList)

        return Milter.ACCEPT

    def close(self):
        # always called, even when abort is called. Clean up
        # any external resources here.
        return Milter.CONTINUE

    def abort(self):
        # client disconnected prematurely
        return Milter.CONTINUE

    def queueLogMessage(self, msg):
        """Add a message to the log queue"""
        GlobalLogQueue.put(msg)


# Background logging thread function
def loggingThread():
    """Display the messages in the log queue for systemd"""
    while True:
        entry = GlobalLogQueue.get()
        if entry:
            print(entry)
            sys.stdout.flush()

def main():
    """Main entry point, run the milter and start the background logging daemon"""

    try:
        debug = configParser.getboolean('main', 'debug')

        # Exit if the main thread have been already created
        if os.path.exists(PID_FILE_PATH):
            print("pid file {} already exists, exiting".format(PID_FILE_PATH))
            os.exit(-1)

        lgThread = Thread(target=loggingThread)
        lgThread.start()
        timeout = 600

        # Register to have the Milter factory create new instances
        print("Register to have the Milter factory create new instances")
        Milter.factory = MarkAddressBookMilter

        # For this milter, we only add headers
        print("For this milter, we only add headers")
        flags = Milter.ADDHDRS
        Milter.set_flags(flags)

        # Get the parent process ID and remember it
        print("Get the parent process ID and remember it")
        pid = os.getpid()
        with open(PID_FILE_PATH, "w") as pidFile:
            pidFile.write(str(pid))
            pidFile.close()

        print("Started RoundCube address book search and tag milter (pid={}, debug={})".format(pid, debug))
        sys.stdout.flush()

        # Start the background thread
        print("Start the background thread")
        socketPath = configParser.get('main', 'socketPath')
        Milter.runmilter("milter-rc-abook", SOCKET_PATH, timeout)
        GlobalLogQueue.put(None)

        #  Wait until the logging thread terminates
        print("Wait until the logging thread terminates")
        lgThread.join()

        # Log the end of process
        print("Stopped RoundCube address book search and tag milter (pid={})".format(pid))

    except Exception as error:
        print("Exception when running the milter: {}".format(error.message))

    # Make sure to remove the pid file even if an error occurs
    # And close the database connection if opened
    finally:
        if os.path.exists(PID_FILE_PATH):
            os.remove(PID_FILE_PATH)

if __name__ == "__main__":
    main()
