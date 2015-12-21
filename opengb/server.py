"""
OpenGB server.

This is the core of openGB. It creates a printer object to run in a separate
process and communicates with it via queues.
"""

import os
import sys
import json
import multiprocessing
from pkg_resources import Requirement, resource_filename

import tornado.httpserver
import tornado.ioloop
import tornado.log
import tornado.websocket
from tornado.options import options
from tornado.web import Application, RequestHandler, StaticFileHandler
from jsonrpc import JSONRPCResponseManager, Dispatcher

import opengb.config
import opengb.printer
import opengb.database as ODB

# TODO: use rotated file logging.
LOGGER = tornado.log.app_log

# Websocket clients.
CLIENTS = []

# Local cache of printer state.
PRINTER = {
    'state':    opengb.printer.State.DISCONNECTED,
    'temp':     {
        'bed':      0,
        'nozzle1':  0,
        'nozzle2':  0,
    },
    'progress': {
        'current':  0,
        'total':    0,
    },
}


class MessageHandler(object):
    """
    Handles JSON-RPC calls received via websocket.

    :param to_printer: A queue whose messages will be sent to the printer.
    :type to_printer: :class:`multiuprocessing.Queue`
    """

    def __init__(self, to_printer):
        self._to_printer = to_printer

    def set_temp(self, bed=None, nozzle1=None, nozzle2=None):
        """
        Set printer target temperatures.

        Unspecified target temperatures will remain unchanged.

        :param bed: Bed target temperature.
        :type bed: :class:`float`
        :param nozzle1: Nozzle1 target temperature.
        :type nozzle1: :class:`float`
        :param nozzle2: Nozzle2 target temperature.
        :type nozzle2: :class:`float`
        """
        self._to_printer.put(json.dumps({
            'method':   'set_temp',
            'params': {
                'bed':      bed,
                'nozzle1':  nozzle1,
                'nozzle2':  nozzle2,
            }
        }))
        return True

    def move_head(self, x=0, y=0, z=0):
        """
        Move the print head along one or more axes.

        :param x: Millimeters to move along the X axis.
        :type x: :class:`float`
        :param y: Millimeters to move along the Y axis.
        :type y: :class:`float`
        :param z: Millimeters to move along the Z axis.
        :type z: :class:`float`
        """
        self._to_printer.put(json.dumps({
            'method':   'move_head',
            'params': {
                'x':    x,
                'y':    y,
                'z':    z,
            }
        }))
        return True

    def home_head(self, x=True, y=True, z=True):
        """
        Home the print head along one or more axes.

        :param x: Whether or not to home the X axis.
        :type x: :class:`bool`
        :param y: Whether or not to home the Y axis.
        :type y: :class:`bool`
        :param z: Whether or not to home the Z axis.
        :type z: :class:`bool`
        """
        self._to_printer.put(json.dumps({
            'method':   'home_head',
            'params': {
                'x':    x,
                'y':    y,
                'z':    z,
            }
        }))
        return True

    def put_gcode_file(self, payload, name):
        """
        Upload a gcode file.

        :param payload: Gcode file as ASCII text.
        :type payload: :class:`str`
        :param name: Gcode file name.
        :type name: :class:`str`
        """
        # TODO: Validate gcode. Could use gctools for this if it is
        # ever uploaded to PyPI https://github.com/thegaragelab/gctools
        payload_bytes = payload.encode()
        payload_size = len(payload_bytes)
        gcode_file = ODB.GCodeFile.create(name=name, size=payload_size)
        destination = os.path.join(options.gcode_dir, str(gcode_file.id))
        with open(destination, "wb") as gcode_file_out:
            try:
                gcode_file_out.write(payload_bytes)
            except IOError as e:
                LOGGER.error('Error writing gcode file {0}: '
                             '{1}'.format(destination, e))
                raise IOError('Unable to save gcode file.')
        return {'id': gcode_file.id, 'name': name, 'size': payload_size}

    def get_gcode_file(self, id, content=False):
        """
        Get details of a single gcode file with the given id.

        Optionally include the gcode file content.

        :param content: Include the gcode file content in the results.
        :type content: :class:`bool` (default False)
        """
        try:
            result = ODB.GCodeFile.get(ODB.GCodeFile.id == id)
            gcode_file = {
                'id':   result.id,
                'name': result.name,
                'size': result.size,
            }
        except ODB.GCodeFile.DoesNotExist:
            raise IndexError('No gcode file found with id {0}'.format(id))
        if content:
            destination = os.path.join(options.gcode_dir, str(id))
            if not os.path.isfile(destination):
                LOGGER.error('Error reading gcode file '
                             '{0}'.format(destination))
                raise IndexError('No gcode file found with id {0}'.format(id))
            with open(destination, "r") as gcode_file_in:
                try:
                    gcode = gcode_file_in.read()
                except IOError:
                    LOGGER.error('Error reading gcode file '
                                 '{0}'.format(destination))
                    raise IndexError('Error reading gcode file')
            gcode_file['content'] = gcode
        return gcode_file

    def get_gcode_files(self):
        """
        Get details of all gcode files.
        """
        return {'gcode_files': [
            {
                'id': g.id,
                'name': g.name,
                'size': g.size,
            }
            for g in ODB.GCodeFile.select()]}

    def get_counters(self):
        """
        Get printer counter values.

        Counters are listed in :data:`opengb.database.COUNTERS`.
        """
        return {'counters': {c.name: c.count for c in ODB.Counter.select()}}


class WebSocketHandler(tornado.websocket.WebSocketHandler):
    """
    Handles all websocket communication with clients.

    Uses `json-rpc <https://pypi.python.org/pypi/json-rpc/>`_ to map messages
    to methods and generate valid JSON-RPC 2.0 responses.
    """

    def __init__(self, *args, **kwargs):
        message_handler = MessageHandler(kwargs.pop('to_printer'))
        self.dispatcher = Dispatcher(message_handler)
        super().__init__(*args, **kwargs)

    def check_origin(self, origin):
        return True

    def open(self):
        LOGGER.info('New connection from {0}'.format(self.request.remote_ip))
        CLIENTS.append(self)
        self.write_message(json.dumps(
            {'cmd': 'STATE', 'new': PRINTER['state']},
            cls=opengb.printer.StateEncoder))

    def on_close(self):
        LOGGER.info('Connection closed to {0}'.format(self.request.remote_ip))
        CLIENTS.remove(self)

    def on_message(self, message):
        """
        Passes an incoming JSON-RPC message to the dispatcher for processing.
        """
        LOGGER.debug('Message received from {0}: {1}'.format(
            self.request.remote_ip, message))
        response = JSONRPCResponseManager.handle(message, self.dispatcher)
        LOGGER.debug('Sending response to {0}: {1}'.format(
            self.request.remote_ip, response._data))
        self.write_message(response.json)


class StatusHandler(RequestHandler):
    def get(self):
        self.write(json.dumps(PRINTER, cls=opengb.printer.StateEncoder))
        self.set_header("Content-Type", "application/json")


def broadcast_message(message):
    """
    Broadcast message to websocket clients.
    """
    for each in CLIENTS:
        each.write_message(message)


def process_event(event):
    """
    Process an event from the printer.
    """
    global PRINTER
    try:
        if event['event'] == 'state_change':
            # TODO: if state changes from printing to ready, reset progress.
            PRINTER['state'] = opengb.printer.State(event['params']['new'])
        elif event['event'] == 'temp_update':
            PRINTER['temp'] = event['params']
        elif event['event'] == 'print_progress':
            PRINTER['progress'] = event['params']
        elif event['event'] == 'z_change':
            # TODO: trigger update camera image.
            pass
    except KeyError:
        LOGGER.error('Malformed event from printer: {0}'.format(event))


def process_printer_events(from_printer):
    """
    Process events from printer.

    Runs via a :class:`tornado.ioloop.PeriodicCallback`.

    :param from_printer: A queue which will be populated with messages sent
        from the printer.
    :type from_printer: :class:`multiuprocessing.Queue`
    """
    if not from_printer.empty():
        try:
            event = json.loads(from_printer.get())
            if event['event'] == 'log':
                LOGGER.log(event['params']['level'], event['params']['msg'])
            else:
                broadcast_message(event)
                process_event(event)
        except TypeError as e:
            LOGGER.exception(e)


def update_counters(count=1):
    """
    Increment all printer counters.

    Runs via a :class:`tornado.ioloop.PeriodicCallback`.

    :param count: Value by which to increment counters.
    :type count: :class:`int`
    """

    LOGGER.debug('Incrementing printer counters')
    query = ODB.Counter.update(count=ODB.Counter.count+1).where(
        ODB.Counter.name.contains('uptime'))
    query.execute()


def main():
    # Load config.
    options.parse_config_file(opengb.config.CONFIG_FILE)

    # Initialize database.
    ODB.initialize(options.db_file)

    # Initialize printer queues.
    to_printer = multiprocessing.Queue()
    from_printer = multiprocessing.Queue()

    # Initialize printer using queue callbacks.
    printer_callbacks = opengb.printer.QueuedPrinterCallbacks(from_printer)
    printer_type = getattr(opengb.printer, options.printer)
    printer = printer_type(to_printer, printer_callbacks,
                           baud_rate=options.baud_rate,
                           port=options.serial_port)
    printer.daemon = True
    printer.start()

    # Initialize web server.
    install_dir = resource_filename(Requirement.parse('openGB'), 'opengb')
    static_dir = os.path.join(install_dir, 'static')
    handlers = [
        (r"/ws", WebSocketHandler,
            {"to_printer": to_printer}),
        # TODO: get rid of this?
        (r"/api/status", StatusHandler),
        (r"/fonts/(.*)", StaticFileHandler,
            {"path": os.path.join(static_dir, "fonts")}),
        (r"/views/(.*)", StaticFileHandler,
            {"path": os.path.join(static_dir, "views")}),
        (r"/images/(.*)", StaticFileHandler,
            {"path": os.path.join(static_dir, "images")}),
        (r"/scripts/(.*)", StaticFileHandler,
            {"path": os.path.join(static_dir, "scripts")}),
        (r"/styles/(.*)", StaticFileHandler,
            {"path": os.path.join(static_dir, "styles")}),
        (r"/(.*)", StaticFileHandler,
            {"path": os.path.join(static_dir, "index.html")}),
    ]
    app = Application(handlers=handlers, debug=options.debug)
    httpServer = tornado.httpserver.HTTPServer(app)
    httpServer.listen(options.http_port)

    # Create event loop and periodic callbacks
    main_loop = tornado.ioloop.IOLoop.instance()
    printer_event_processor = tornado.ioloop.PeriodicCallback(
        lambda: process_printer_events(from_printer), 10, io_loop=main_loop)
    counter_updater = tornado.ioloop.PeriodicCallback(
        lambda: update_counters(), 60000)
    # TODO: ioloop for watchdog
    # TODO: ioloop for camera

    # Rock and roll.
    printer_event_processor.start()
    counter_updater.start()
    main_loop.start()

    return(os.EX_OK)

if __name__ == '__main__':
    sys.exit(main())
