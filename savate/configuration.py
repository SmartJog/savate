# -*- coding: utf-8 -*-

import collections
import itertools
import urlparse
import socket
import sys
import re


SIZE_REGEXP = re.compile(r'^\d+k?$')


class BadConfig(Exception):
    pass


def convert_burst_size(size):
    if size is None:
        return

    if isinstance(size, int):
        if size >= 0:
            return size
        else:
            raise BadConfig('Burst size must be a positive int.')

    size_str = str(size)
    if SIZE_REGEXP.match(size_str):
        size = int(size_str.replace('k', ''))
        if 'k' in size_str:
            size *= 2 ** 10

        return size

    raise BadConfig('Bad format for burst size.')


class ServerConfiguration(object):

    def __init__(self, server, config_dict):
        self.server = server
        self.config_dict = config_dict

        self.modules_loaded = set()  # keep trace of the modules we load

    def __getitem__(self, key):
        return self.config_dict[key]

    def configure(self):
        self.configure_stats()
        self.configure_authorization()
        self.configure_status()
        self.configure_relays()
        self.configure_limits()

    def reconfigure(self, config_dict):
        self.config_dict = config_dict
        # authorization, status and statistics handlers may have a close method
        for handler in itertools.chain(self.server.auth_handlers,
                                       self.server.status_handlers,
                                       self.server.statistics_handlers):
            if callable(getattr(handler, 'close', None)):
                handler.close()
        # make sure modules will be reloaded
        for module_name in self.modules_loaded:
            sys.modules.pop(module_name, None)
        self.modules_loaded = set()
        # Drop authorization, status and statistics handlers, they will be
        # properly re-created anyway
        self.server.auth_handlers = []
        self.server.status_handlers = {}
        self.server.statistics_handlers = []
        self.configure_authorization()
        self.configure_status()
        self.configure_stats()

        # Here comes the tricky part: identifying which relays we need
        # to drop
        tmp_relays = self.server.relays
        self.server.relays = {}

        # use a dict to index all relays configurations
        # values are tuples ( burstsizes or None, keepalive or None)
        # if a relay is represented in the index, it means it exists
        relay_index = dict((
            (url, mount['path']),
            (
                convert_burst_size(
                    mount.get(
                        'burst_size',
                        self.config_dict.get('burst_size'),
                    ),
                ),
                mount.get('keepalive', self.config_dict.get('keepalive')),
            ),
        ) for mount in self.config_dict.get(
            'mounts', []) for url in mount.get('source_urls', []))
        # source index same as relays but with source instances as values
        source_index = dict((
            source.sock,
            source,
        ) for sources in self.server.sources.itervalues() for source in sources)

        for relay in tmp_relays.values():
            relay_params = relay_index.get((relay.url, relay.path), None)
            if relay_params is not None:
                # update relay burst size
                relay.burst_size = relay_params[0]

                # update keepalive info
                try:
                    relay.keepalive = int(relay_params[1])
                except (ValueError, TypeError):
                    relay.keepalive = None

                # update sources burst size
                source = source_index.get(relay.sock)
                if source is not None:
                    source.update_burst_size(relay.burst_size)
                    source.keepalive = relay.keepalive

                self.server.relays[relay.sock] = relay
            else:
                source = source_index.get(relay.sock)
                if source is not None:
                    self.server.logger.info('Dropping source %s since it has '
                                            'been removed from configuration',
                                            source)
                    source.close()
                else:
                    # This relay has not been yet added as a source
                    relay.close()

        # Any relay marked to be restarted must be checked as well
        tmp_relays = self.server.relays_to_restart
        self.server.relays_to_restart = collections.deque()

        for timeout, relay in tmp_relays:
            if (relay.url, relay.path) in relay_index:
                self.server.relays_to_restart.append((timeout, relay))

        # Take new configuration into account
        self.configure_relays()
        self.configure_limits()

    def configure_relays(self):
        conf = self.config_dict
        server = self.server
        global_burst_size = conf.get('burst_size', None)
        global_on_demand = conf.get('on_demand', False)
        global_keepalive = conf.get('keepalive', False)
        global_max_queue_size = conf.get('max_queue_size')

        net_resolve_all = conf.get('net_resolve_all', False)


        # index
        relay_index = dict((
            (relay.url, relay.path, relay.addr_info),
            relay,
        ) for relay in itertools.chain(
            self.server.relays.itervalues(),
            (relay for timeout, relay in self.server.relays_to_restart),
        ))

        for mount_conf in conf.get('mounts', {}):
            if 'source_urls' not in mount_conf:
                continue

            mount_burst_size = convert_burst_size(
                mount_conf.get('burst_size', global_burst_size))
            mount_on_demand = mount_conf.get('on_demand', global_on_demand)
            mount_keep_alive = mount_conf.get('keepalive', global_keepalive)
            mount_max_queue_size = mount_conf.get('max_queue_size', global_max_queue_size)
            path = mount_conf['path']
            for source_url in mount_conf['source_urls']:
                parsed_url = urlparse.urlparse(source_url)
                if parsed_url.scheme in ('udp', 'multicast'):
                    if (source_url, path, None) not in relay_index:
                        server.logger.info('Trying to relay %s', source_url)
                        server.add_relay(source_url, path,
                                         burst_size=mount_burst_size)
                else:
                    if mount_conf.get('net_resolve_all', net_resolve_all):
                        for address_info in socket.getaddrinfo(
                            parsed_url.hostname,
                            parsed_url.port,
                            socket.AF_UNSPEC,
                            socket.SOCK_STREAM,
                            socket.IPPROTO_TCP):
                            if (source_url, path,
                                address_info) not in relay_index:
                                server.logger.info('Trying to relay %s from %s:%s', source_url,
                                            address_info[4][0], address_info[4][1])
                                server.add_relay(source_url, path, address_info,
                                                 mount_burst_size,
                                                 mount_on_demand,
                                                 mount_keep_alive,
                                                 mount_max_queue_size)
                    else:
                        if (source_url, path, None) not in relay_index:
                            server.logger.info('Trying to relay %s', source_url)
                            server.add_relay(source_url, path,
                                             burst_size=mount_burst_size,
                                             on_demand=mount_on_demand,
                                             keepalive=mount_keep_alive,
                                             max_queue_size=mount_max_queue_size)

    def configure_authorization(self):
        conf = self.config_dict
        server = self.server
        for auth_handler in conf.get('auth', []):
            handler_name = auth_handler['handler']
            handler_module, handler_class = handler_name.rsplit('.', 1)
            self.modules_loaded.add(handler_module)
            handler_module = __import__(handler_module, {}, {}, [''])
            handler_class = getattr(handler_module, handler_class)
            handler_instance = handler_class(server, conf, **auth_handler)
            server.add_auth_handler(handler_instance)

    def configure_status(self):
        conf = self.config_dict
        server = self.server
        for handler_path, status_handler in conf.get('status', {}).items():
            handler_name = status_handler['handler']
            handler_module, handler_class = handler_name.rsplit('.', 1)
            self.modules_loaded.add(handler_module)
            handler_module = __import__(handler_module, {}, {}, [''])
            handler_class = getattr(handler_module, handler_class)
            handler_instance = handler_class(server, conf, **status_handler)
            server.add_status_handler(handler_path, handler_instance)

    def configure_stats(self):
        conf = self.config_dict
        server = self.server
        for stat_handler in conf.get('statistics', {}):
            handler_name = stat_handler['handler']
            handler_module, handler_class = handler_name.rsplit('.', 1)
            self.modules_loaded.add(handler_module)
            handler_module = __import__(handler_module, {}, {}, [''])
            handler_class = getattr(handler_module, handler_class)
            handler_instance = handler_class(server, **stat_handler)
            self.server.add_stats_handler(handler_instance)

    def configure_limits(self):
        # set limits for maximum simultaneous clients
        try:
            self.server.clients_limit = int(self.config_dict.get('clients_limit'))
            self.server.logger.info('Set client limit to %d', self.server.clients_limit)
        except (ValueError, TypeError):
            self.server.clients_limit = None
