import socket
import re
import time
import math
import zmq
import json
from .dsk import Dsk


class Manager(object):
    def __init__(self, snowmix_address):
        self.snowmix_address = snowmix_address
        self.snowmix = self.connect_to_snowmix(snowmix_address)

        context = zmq.Context()
        socket = context.socket(zmq.REP)
        socket.bind("tcp://*:5555")
        self.server_socket = socket

        socket = context.socket(zmq.PUB)
        socket.bind("tcp://*:5556")
        self.publisher_socket = socket

        self.framerate = 30.
        self.preview = 1
        self.program = 2
        self.feeds_count = 8
        self.dsks = [Dsk(feed_id) for feed_id in [9, 10, 11, 12]]
        self.hide_all_dsks()
        self.update_main_bus()

        self.feed_types = {}
        self.feeds = [None] * 12

    def register_feed_type(self, feed_class, name, play_after_create=True):
        self.feed_types[name] = {
            'class': feed_class,
            'play_after_create': play_after_create
        }

    def create_feed(self, index, feed_type, *args):
        snowmix_id = 'feed{}'.format(index + 1)
        FeedClass = self.feed_types[feed_type]['class']
        feed = FeedClass(snowmix_id, *args, 1280, 720, '30/1')
        self.feeds[index] = feed

        if self.feed_types[feed_type]['play_after_create']:
            feed.play()

        return feed


    def start(self):
        keep_running = True

        while keep_running:
            message = self.server_socket.recv_json()
            print('message:', message)

            if 'action' in message:
                action = message['action']

                if action == 'transition':
                    self.transition()
                elif action == 'take':
                    self.take()
                elif action == 'sync':
                    self.sync()
                elif action == 'set_program':
                    self.set_program(message['feed'])
                elif action == 'set_preview':
                    self.set_preview(message['feed'])
                elif action == 'toggle_dsk':
                    self.toggle_dsk(message['dsk_id'])
                elif action == 'quit':
                    keep_running = False
                    self.publish_json({'action': 'quit'})

            self.server_socket.send_json({'response': 'ok'})

    def sync(self):
        self.notify('preview', self.preview)
        self.notify('program', self.program)
        self.notify('feeds_count', self.feeds_count)
        self.notify('active_dsks', self.get_active_dsk_ids())

    def publish_json(self, obj):
        message = bytes(json.dumps(obj), 'utf-8')
        self.publisher_socket.send_multipart([b'main', message])

    def connect_to_snowmix(self, address):
        snowmix = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            snowmix.connect(address)
        except:
            print("Unable to connect to Snowmix at {0}:{1}".format(*address))

        snowmix.recv(4096)  # Clear out version string
        return snowmix

    def hide_all_dsks(self):
        for dsk_feed in self.dsks:
            self.send_command('vfeed alpha {} 0'.format(dsk_feed.feed_id))

        self.active_dsks = []

    def transition_in_dsk(self, dsk):
        duration = dsk.transition_duration

        if duration > 0:
            frames = math.ceil(duration * self.framerate)
            delta = 1. / frames
            self.send_command('vfeed move alpha {0} {1} {2}'.format(
                              dsk.feed_id, delta, frames))
        else:
            self.send_command('vfeed alpha {} 1'.format(dsk.feed_id))

        dsk.active = True
        self.notify('active_dsks', self.get_active_dsk_ids())

    def transition_out_dsk(self, dsk):
        duration = dsk.transition_duration

        if duration > 0:
            frames = math.ceil(duration * self.framerate)
            delta = 1. / frames
            self.send_command('vfeed move alpha {0} {1} {2}'.format(
                              dsk.feed_id, -delta, frames))
        else:
            self.send_command('vfeed alpha {} 0'.format(dsk.feed_id))

        dsk.active = False
        self.notify('active_dsks', self.get_active_dsk_ids())

    def get_active_dsk_ids(self):
        return [feed.id for feed in self.dsks if feed.active]

    def build_dsk_feeds_list(self):
        return " ".join([str(feed.feed_id) for feed in self.dsks])

    def toggle_dsk(self, dsk_id):
        dsk = self.dsks[dsk_id]

        if self.dsks[dsk_id].active:
            self.transition_out_dsk(dsk)
        else:
            self.transition_in_dsk(dsk)

    def subscribe(self, callback):
        self.callback = callback

    def notify(self, target, value):
        self.publish_json({'update': target, 'value': value})

    def set_preview(self, feed):
        self.preview = feed
        self.update_main_bus()

    def set_program(self, feed):
        self.program = feed
        self.update_main_bus()

    def take(self):
        self.program, self.preview = self.preview, self.program
        self.update_main_bus()

    def update_main_bus(self):
        self.send_command('vfeed alpha {0} 0'.format(self.preview))
        self.send_command('vfeed alpha {0} 1'.format(self.program))
        self.send_command('tcl eval SetFeedToOverlay {0} {1} {2}'.format(
                          self.program,
                          self.preview,
                          self.build_dsk_feeds_list()
                          ))

        self.notify('preview', self.preview)
        self.notify('program', self.program)

    def transition(self, duration=0.25):
        frames = math.ceil(duration * self.framerate)
        delta = 1. / frames
        c = 'vfeed move alpha {0} {1} {2}'.format(self.preview, delta, frames)
        self.send_command(c)

        time.sleep(duration)
        self.take()

    def send_command(self, command, responds=False):
        self.snowmix.send(bytearray(command + '\n','utf-8'))

        if responds:
            return self.receive_all()
        else:
            return None

    def get_feed_ids(self):
        self.snowmix.send(b'feed list\n')
        response = self.receive_all()
        feed_pattern = re.compile(r"Feed ID ([\d]+)", re.MULTILINE)
        matches = feed_pattern.findall(response)
        return [int(string_id) for string_id in matches]

    def receive_all(self):
        result = ''

        while 1:
            data = self.snowmix.recv(4096)

            if len(data) == 0:
                break

            result += data.decode('utf-8')

            if result.endswith(('STAT: \n', 'MSG: \n')):
                break

        return result
