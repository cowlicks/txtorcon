import datetime
import os
import stat
import types
import warnings

from twisted.python import log
from twisted.internet import defer
from twisted.internet.endpoints import TCP4ClientEndpoint, UNIXClientEndpoint
from twisted.internet.interfaces import IReactorCore, IStreamClientEndpoint
from zope.interface import implements

from txtorcon import TorProtocolFactory
from txtorcon.stream import Stream
from txtorcon.circuit import Circuit
from txtorcon.router import Router, hashFromHexId
from txtorcon.addrmap import AddrMap
from txtorcon.torcontrolprotocol import parse_keywords
from txtorcon.log import txtorlog
from txtorcon.torcontrolprotocol import TorProtocolError

from txtorcon.interface import ITorControlProtocol, IRouterContainer, ICircuitListener
from txtorcon.interface import ICircuitContainer, IStreamListener, IStreamAttacher
from spaghetti import FSM, State, Transition


def _build_state(proto):
    state = TorState(proto)
    return state.post_bootstrap


def _wait_for_proto(proto):
    return proto.post_bootstrap


def build_tor_connection(connection, build_state=True, wait_for_proto=True,
                         password_function=lambda: None):
    """
    This is used to build a valid TorState (which has .protocol for
    the TorControlProtocol). For example::

        from twisted.internet import reactor
        from twisted.internet.endpoints import TCP4ClientEndpoint
        import txtorcon

        def example(state):
            print "Fully bootstrapped state:",state
            print "   with bootstrapped protocol:",state.protocol

        d = txtorcon.build_tor_connection(TCP4ClientEndpoint(reactor,
                                                             "localhost",
                                                             9051))
        d.addCallback(example)
        reactor.run()

    :param password_function:
        See :class:`txtorcon.TorControlProtocol`

    :param build_state:
        If True (the default) a TorState object will be
        built as well. If False, just a TorControlProtocol will be
        returned via the Deferred.

    :return:
        a Deferred that fires with a TorControlProtocol or, if you
        specified build_state=True, a TorState. In both cases, the
        object has finished bootstrapping
        (i.e. TorControlProtocol.post_bootstrap or
        TorState.post_bootstap has fired, as needed)
    """

    if IStreamClientEndpoint.providedBy(connection):
        endpoint = connection

    elif isinstance(connection, tuple):
        if len(connection) == 2:
            reactor, socket = connection
            if (os.path.exists(socket) and
                os.stat(socket).st_mode & (stat.S_IRGRP | stat.S_IRUSR |
                                           stat.S_IROTH)):
                endpoint = UNIXClientEndpoint(reactor, socket)
            else:
                raise ValueError('Can\'t use "%s" as a socket' % (socket, ))
        elif len(connection) == 3:
            endpoint = TCP4ClientEndpoint(*connection)
        else:
            raise TypeError('Expected either a (reactor, socket)- or a '
                            '(reactor, host, port)-tuple for argument '
                            '"connection", got %s' % (connection, ))
    else:
        raise TypeError('Expected a (reactor, socket)- or a (reactor, host, '
                        'port)-tuple or an object implementing IStreamClient'
                        'Endpoint for argument "connection", got %s' %
                        (connection, ))

    d = endpoint.connect(TorProtocolFactory(password_function=password_function))
    if build_state:
        d.addCallback(build_state if callable(build_state) else _build_state)
    elif wait_for_proto:
        d.addCallback(wait_for_proto if callable(wait_for_proto) else
                      _wait_for_proto)
    return d


def build_local_tor_connection(reactor, host='127.0.0.1', port=9051,
                               socket='/var/run/tor/control', *args, **kwargs):
    """
    This builds a connection to a local Tor, either via 127.0.0.1:9051
    (which is tried first) or /var/run/tor/control (by default). See
    also :meth:`build_tor_connection
    <txtorcon.torstate.build_tor_connection>` for other key-word
    arguments that are accepted here also.

    :param host:
        An IP address to find Tor at. Corresponds to the
        ControlListenAddress torrc option.

    :param port:
        The port to use with the address when trying to contact
        Tor. This corresponds to the ControlPort option in torrc
        (default is 9051).
    """

    try:
        return build_tor_connection((reactor, socket), *args, **kwargs)
    except:
        return build_tor_connection((reactor, host, port), *args, **kwargs)


def flags_from_dict(kw):
    """
    This turns a dict with keys that are flags (e.g. for CLOSECIRCUIT,
    CLOSESTREAM) only if the values are true.
    """

    if len(kw) == 0:
        return ''

    flags = ''
    for (k, v) in kw.iteritems():
        if v:
            flags += ' ' + str(k)
    # note that we want the leading space if there's at least one
    # flag.
    return flags


class TorState(object):
    """
    This tracks the current state of Tor using a TorControlProtocol.

    On setup it first queries the initial state of streams and
    circuits. It then asks for updates via the listeners. It requires
    an ITorControlProtocol instance. The control protocol doesn't need
    to be bootstrapped yet. The Deferred .post_boostrap is driggered
    when the TorState instance is fully ready to go.  The easiest way
    is to use the helper method
    :func:`txtorcon.build_tor_connection`. For details, see the
    implementation of that.

    You may add an :class:`txtorcon.interface.IStreamAttacher` to
    provide a custom mapping for Strams to Circuits (by default Tor
    picks by itself).

    This is also a good example of the various listeners, and acts as
    an :class:`txtorcon.interface.ICircuitContainer` and
    :class:`txtorcon.interface.IRouterContainer`.
    """

    implements(ICircuitListener, ICircuitContainer, IRouterContainer,
               IStreamListener)

    def __init__(self, protocol, bootstrap=True, write_state_diagram=False):
        self.protocol = ITorControlProtocol(protocol)
        ## fixme could use protocol.on_disconnect to re-connect; see issue #3

        ## could override these to get your own Circuit/Stream subclasses
        ## to track these things
        self.circuit_factory = Circuit
        self.stream_factory = Stream

        self.attacher = None
        """If set, provides
        :class:`txtorcon.interface.IStreamAttacher` to attach new
        streams we hear about."""

        self.tor_binary = 'tor'

        self.circuit_listeners = []
        self.stream_listeners = []

        self.addrmap = AddrMap()
        self.circuits = {}               # keys on id (integer)
        self.streams = {}                # keys on id (integer)

        self.all_routers = set()         # list of unique routers
        self.routers = {}                # keys by hexid (string) and by unique names
        self.routers_by_name = {}        # keys on name, value always list (many duplicate "Unnamed" routers, for example)
        self.routers_by_hash = {}        # keys by hexid (string)
        self.guards = {}                 # potentially-usable as entry guards, I think? (any router with 'Guard' flag)
        self.entry_guards = {}           # from GETINFO entry-guards, our current entry guards
        self.unusable_entry_guards = []  # list of entry guards we didn't parse out
        self.authorities = {}            # keys by name

        self.cleanup = None              # see set_attacher

        class die(object):
            __name__ = 'die'             # FIXME? just to ease spagetti.py:82's pain

            def __init__(self, msg):
                self.msg = msg

            def __call__(self, *args):
                raise RuntimeError(self.msg % tuple(args))

        def nothing(*args):
            pass

        waiting_r = State("waiting_r")
        waiting_w = State("waiting_w")
        waiting_p = State("waiting_p")
        waiting_s = State("waiting_s")

        def ignorable_line(x):
            return x.strip() == '.' or x.strip() == 'OK' or x[:3] == 'ns/' or x.strip() == ''

        waiting_r.add_transition(Transition(waiting_r, ignorable_line, nothing))
        waiting_r.add_transition(Transition(waiting_s, lambda x: x[:2] == 'r ', self._router_begin))
        ## FIXME use better method/func than die!!
        waiting_r.add_transition(Transition(waiting_r, lambda x: x[:2] != 'r ', die('Expected "r " while parsing routers not "%s"')))

        waiting_s.add_transition(Transition(waiting_w, lambda x: x[:2] == 's ', self._router_flags))
        waiting_s.add_transition(Transition(waiting_s, lambda x: x[:2] == 'a ', self._router_address))
        waiting_s.add_transition(Transition(waiting_r, ignorable_line, nothing))
        waiting_s.add_transition(Transition(waiting_r, lambda x: x[:2] != 's ' and x[:2] != 'a ', die('Expected "s " while parsing routers not "%s"')))
        waiting_s.add_transition(Transition(waiting_r, lambda x: x.strip() == '.', nothing))

        waiting_w.add_transition(Transition(waiting_p, lambda x: x[:2] == 'w ', self._router_bandwidth))
        waiting_w.add_transition(Transition(waiting_r, ignorable_line, nothing))
        waiting_w.add_transition(Transition(waiting_s, lambda x: x[:2] == 'r ', self._router_begin))  # "w" lines are optional
        waiting_w.add_transition(Transition(waiting_r, lambda x: x[:2] != 'w ', die('Expected "w " while parsing routers not "%s"')))
        waiting_w.add_transition(Transition(waiting_r, lambda x: x.strip() == '.', nothing))

        waiting_p.add_transition(Transition(waiting_r, lambda x: x[:2] == 'p ', self._router_policy))
        waiting_p.add_transition(Transition(waiting_r, ignorable_line, nothing))
        waiting_p.add_transition(Transition(waiting_s, lambda x: x[:2] == 'r ', self._router_begin))  # "p" lines are optional
        waiting_p.add_transition(Transition(waiting_r, lambda x: x[:2] != 'p ', die('Expected "p " while parsing routers not "%s"')))
        waiting_p.add_transition(Transition(waiting_r, lambda x: x.strip() == '.', nothing))

        self._network_status_parser = FSM([waiting_r, waiting_s, waiting_w, waiting_p])
        if write_state_diagram:
            with open('routerfsm.dot', 'w') as fsmfile:
                fsmfile.write(self._network_status_parser.dotty())

        self.post_bootstrap = defer.Deferred()
        if bootstrap:
            self.protocol.post_bootstrap.addCallback(self._bootstrap).addErrback(self.post_bootstrap.errback)

    def _router_begin(self, data):
        args = data.split()
        self._router = Router(self.protocol)
        self._router.from_consensus = True
        self._router.update(args[1],         # nickname
                            args[2],         # idhash
                            args[3],         # orhash
                            datetime.datetime.strptime(args[4] + args[5], '%Y-%m-%f%H:%M:%S'),
                            args[6],         # ip address
                            args[7],         # ORPort
                            args[8])         # DirPort

        if self._router.id_hex in self.routers:
            ## FIXME should I do an update() on this one??
            self._router = self.routers[self._router.id_hex]
            return

        if self._router.name in self.routers_by_name:
            self.routers_by_name[self._router.name].append(self._router)

        else:
            self.routers_by_name[self._router.name] = [self._router]

        if self._router.name in self.routers:
            self.routers[self._router.name] = None

        else:
            self.routers[self._router.name] = self._router
        self.routers[self._router.id_hex] = self._router
        self.routers_by_hash[self._router.id_hex] = self._router
        self.all_routers.add(self._router)

    def _router_flags(self, data):
        args = data.split()
        self._router.flags = args[1:]
        if 'guard' in self._router.flags:
            self.guards[self._router.id_hex] = self._router
        if 'authority' in self._router.flags:
            self.authorities[self._router.name] = self._router

    def _router_address(self, data):
        """only for IPv6 addresses"""
        self._router.ip_v6.append(data.split()[1].strip())

    def _router_bandwidth(self, data):
        args = data.split()
        self._router.bandwidth = int(args[1].split('=')[1])

    def _router_policy(self, data):
        args = data.split()
        self._router.policy = args[1:]
        self._router = None

    @defer.inlineCallbacks
    def _bootstrap(self, arg=None):
        "This takes an arg so we can use it as a callback (see __init__)."

        # update list of routers (must be before we do the
        # circuit-status) note that we're feeding each line
        # incrementally to a state-machine called
        # _network_status_parser, set up in constructor. "ns" should
        # be the empty string, but we call _update_network_status for
        # the de-duplication of named routers

        ns = yield self.protocol.get_info_incremental('ns/all',
                                                      self._network_status_parser.process)
        self._update_network_status(ns)

        # update list of existing circuits
        cs = yield self.protocol.get_info_raw('circuit-status')
        self._circuit_status(cs)

        # update list of streams
        ss = yield self.protocol.get_info_raw('stream-status')
        self._stream_status(ss)

        # update list of existing address-maps
        key = 'address-mappings/all'
        am = yield self.protocol.get_info_raw(key)
        # strip addressmappsings/all= and OK\n from raw data
        am = am[len(key) + 1:]
        for line in am.split('\n'):
            if len(line.strip()) == 0:
                continue            # FIXME
            self.addrmap.update(line)

        self._add_events()

        entries = yield self.protocol.get_info_raw("entry-guards")
        for line in entries.split('\n')[1:]:
            if len(line.strip()) == 0 or line.strip() == 'OK':
                # XXX does this ever really happen?
                continue
            args = line.split()
            (name, status) = args[:2]
            name = name[:41]

            # this is sometimes redundant, as a missing entry guard
            # usually means it won't be in our list of routers right
            # now, but just being on the safe side
            if status.lower() != 'up':
                self.unusable_entry_guards.append(line)
                continue

            try:
                self.entry_guards[name] = self.router_from_id(name)
            except KeyError:
                self.unusable_entry_guards.append(line)

        # in case process/pid doesn't exist and we don't know the PID
        # because we own it, we just leave it as 0 (previously
        # guessed using psutil, but that only works if there's
        # exactly one tor running anyway)
        try:
            pid = yield self.protocol.get_info_raw("process/pid")
        except TorProtocolError:
            pid = None
        self.tor_pid = 0
        if pid:
            try:
                pid = parse_keywords(pid)['process/pid']
                self.tor_pid = int(pid)
            except:
                self.tor_pid = 0
        if not self.tor_pid and self.protocol.is_owned:
            self.tor_pid = self.protocol.is_owned

        self.post_bootstrap.callback(self)
        self.post_boostrap = None

    def undo_attacher(self):
        """
        Shouldn't Tor handle this by turning this back to 0 if the
        controller that twiddled it disconnects?
        """

        return self.protocol.set_conf("__LeaveStreamsUnattached", 0)

    def set_attacher(self, attacher, myreactor):
        """
        Provide an :class:`txtorcon.interface.IStreamAttacher` to
        associate streams to circuits. This won't get turned on until
        after bootstrapping is completed. ('__LeaveStreamsUnattached'
        needs to be set to '1' and the existing circuits list needs to
        be populated).
        """

        react = IReactorCore(myreactor)
        if attacher:
            self.attacher = IStreamAttacher(attacher)
        else:
            self.attacher = None

        if self.attacher is None:
            self.undo_attacher()
            if self.cleanup:
                react.removeSystemEventTrigger(self.cleanup)
                self.cleanup = None

        else:
            self.protocol.set_conf("__LeaveStreamsUnattached", "1")
            self.cleanup = react.addSystemEventTrigger('before', 'shutdown',
                                                       self.undo_attacher)
        return None

    stream_close_reasons = {
        'REASON_MISC': 1,               # (catch-all for unlisted reasons)
        'REASON_RESOLVEFAILED': 2,      # (couldn't look up hostname)
        'REASON_CONNECTREFUSED': 3,     # (remote host refused connection) [*]
        'REASON_EXITPOLICY': 4,         # (OR refuses to connect to host or port)
        'REASON_DESTROY': 5,            # (Circuit is being destroyed)
        'REASON_DONE': 6,               # (Anonymized TCP connection was closed)
        'REASON_TIMEOUT': 7,            # (Connection timed out, or OR timed out while connecting)
        'REASON_NOROUTE': 8,            # (Routing error while attempting to contact destination)
        'REASON_HIBERNATING': 9,        # (OR is temporarily hibernating)
        'REASON_INTERNAL': 10,          # (Internal error at the OR)
        'REASON_RESOURCELIMIT': 11,     # (OR has no resources to fulfill request)
        'REASON_CONNRESET': 12,         # (Connection was unexpectedly reset)
        'REASON_TORPROTOCOL': 13,       # (Sent when closing connection because of Tor protocol violations.)
        'REASON_NOTDIRECTORY': 14}      # (Client sent RELAY_BEGIN_DIR to a non-directory relay.)

    def close_stream(self, stream, reason='REASON_MISC', **kwargs):
        """
        This sends a STREAMCLOSE command, using the specified reason
        (either an int or one of the 14 strings in section 6.3 of
        tor-spec.txt if the argument is a string). Any kwards are
        passed through as flags if they evaluated to true
        (e.g. "SomeFlag=True"). Currently there are none that Tor accepts.
        """

        if type(stream) != int:
            # assume it's a Stream instance
            stream = stream.id
        try:
            reason = int(reason)
        except ValueError:
            try:
                reason = TorState.stream_close_reasons[reason]
            except KeyError:
                raise ValueError('Unknown stream close reason "%s"' % str(reason))

        flags = flags_from_dict(kwargs)

        # stream is now an ID no matter what we passed in
        cmd = 'CLOSESTREAM %d %d%s' % (stream, reason, flags)
        return self.protocol.queue_command(cmd)

    def close_circuit(self, circid, **kwargs):
        """
        This sends a CLOSECIRCUIT command, using any keyword arguments
        passed as the Flags (currently, that is just 'IfUnused' which
        means to only close the circuit when it is no longer used by
        any streams).

        :param circid:
            Either a circuit-id (int) or a Circuit instance

        :return:
            a Deferred which callbacks with the result of queuing the
            command to Tor (usually "OK"). If you want to instead know
            when the circuit is actually-gone, see
            :meth:`Circuit.close <txtorcon.circuit.Circuit.close>`
        """

        if type(circid) != int:
            ## assume it's a Circuit instance
            circid = circid.id
        flags = flags_from_dict(kwargs)
        return self.protocol.queue_command('CLOSECIRCUIT %s%s' % (circid, flags))

    def add_circuit_listener(self, icircuitlistener):
        listen = ICircuitListener(icircuitlistener)
        for circ in self.circuits.values():
            circ.listen(listen)
        self.circuit_listeners.append(listen)

    def add_stream_listener(self, istreamlistener):
        listen = IStreamListener(istreamlistener)
        for stream in self.streams.values():
            stream.listen(listen)
        self.stream_listeners.append(listen)

    def _find_circuit_after_extend(self, x):
        ex, circ_id = x.split()
        if ex != 'EXTENDED':
            raise RuntimeError('Expected EXTENDED, got "%s"' % x)
        circ_id = int(circ_id)
        circ = self._maybe_create_circuit(circ_id)
        circ.update([str(circ_id), 'EXTENDED'])
        return circ

    def build_circuit(self, routers=None, using_guards=True):
        """
        Builds a circuit consisting of exactly the routers specified,
        in order.  This issues an EXTENDCIRCUIT call to Tor with all
        the routers specified.

        :param routers: a list of Router instances which is the path
            desired. To allow Tor to choose the routers itself, pass
            None (the default) for routers.

        :param using_guards: A warning is issued if the first router
            isn't in self.entry_guards.

        :return:
            A Deferred that will callback with a Circuit instance
            (with the .id member being valid, and probably nothing
            else).
        """

        if routers is None or routers == []:
            cmd = "EXTENDCIRCUIT 0"

        else:
            if using_guards and routers[0] not in self.entry_guards.values():
                warnings.warn("Building a circuit not starting with a guard: %s" % (str(routers),), RuntimeWarning)
            cmd = "EXTENDCIRCUIT 0 "
            first = True
            for router in routers:
                if first:
                    first = False
                else:
                    cmd += ','
                if isinstance(router, types.StringType) and len(router) == 40 and hashFromHexId(router):
                    cmd += router
                else:
                    cmd += router.id_hex[1:]
        d = self.protocol.queue_command(cmd)
        d.addCallback(self._find_circuit_after_extend)
        return d

    DO_NOT_ATTACH = object()

    def _maybe_attach(self, stream):
        """
        If we've got a custom stream-attachment instance (see
        set_attacher) this will ask it for the appropriate
        circuit. Note that we ignore .exit URIs and let Tor deal with
        those (by passing circuit ID 0).

        The stream attacher is allowed to return a Deferred which will
        callback with the desired circuit.

        You may return the special object DO_NOT_ATTACH which will
        cause the circuit attacher to simply ignore the stream
        (neither attaching it, nor telling Tor to attach it).
        """

        if self.attacher:
            if stream.target_host is not None and '.exit' in stream.target_host:
                ## we want to totally ignore .exit URIs as these are
                ## used to specify a particular exit node, and trying
                ## to do STREAMATTACH on them will fail with an error
                ## from Tor anyway.
                txtorlog.msg("ignore attacher:", stream)
                return

            circ = IStreamAttacher(self.attacher).attach_stream(stream, self.circuits)
            if circ is self.DO_NOT_ATTACH:
                return

            if circ is None:
                self.protocol.queue_command("ATTACHSTREAM %d 0" % stream.id)

            else:
                if isinstance(circ, defer.Deferred):
                    class IssueStreamAttach:
                        def __init__(self, state, streamid):
                            self.stream_id = streamid
                            self.state = state

                        def __call__(self, arg):
                            circid = arg.id
                            self.state.protocol.queue_command("ATTACHSTREAM %d %d" % (self.stream_id, circid))

                    circ.addCallback(IssueStreamAttach(self, stream.id)).addErrback(log.err)

                else:
                    if circ.id not in self.circuits:
                        raise RuntimeError("Attacher returned a circuit unknown to me.")
                    if circ.state != 'BUILT':
                        raise RuntimeError("Can only attach to BUILT circuits; %d is in %s." % (circ.id, circ.state))
                    self.protocol.queue_command("ATTACHSTREAM %d %d" % (stream.id, circ.id))

    def _circuit_status(self, data):
        """Used internally as a callback for updating Circuit information"""

        data = data[len('circuit-status='):].split('\n')
        ## sometimes there's a newline after circuit-status= and
        ## sometimes not, so we get rid of it
        if len(data) and len(data[0].strip()) == 0:
            data = data[1:]

        for line in data:
            self._circuit_update(line)

    def _stream_status(self, data):
        "Used internally as a callback for updating Stream information"
        # there's a slight issue with a single-stream vs >= 2 streams,
        # in that in the latter case we have a line by itself with
        # "stream-status=" on it followed by the streams EXCEPT in the
        # single-stream case which has "stream-status=123 blahblah"
        # (i.e. the key + value on one line)

        lines = data.split('\n')
        if len(lines) == 1:
            d = lines[0][len('stream-status='):]
            # if there are actually 0 streams, then there's nothing
            # left to parse
            if len(d):
                self._stream_update(d)
        else:
            [self._stream_update(line) for line in lines[1:]]

    def _update_network_status(self, data):
        """
        Used internally as a callback for updating Router information
        from NS and NEWCONSENSUS events.
        """

        self.all_routers = set()
        for line in data.split('\n'):
            self._network_status_parser.process(line)

        txtorlog.msg(len(self.routers_by_name), "named routers found.")
        # remove any names we added that turned out to have dups
        for (k, v) in self.routers.items():
            if v is None:
                txtorlog.msg(len(self.routers_by_name[k]), "dups:", k)
                del self.routers[k]

        txtorlog.msg(len(self.guards), "GUARDs")

    def _maybe_create_circuit(self, circ_id):
        if circ_id not in self.circuits:
            c = self.circuit_factory(self)
            c.listen(self)
            [c.listen(x) for x in self.circuit_listeners]

        else:
            c = self.circuits[circ_id]
        return c

    def _circuit_update(self, line):
        """
        Used internally as a callback to update Circuit information
        from CIRC events.
        """

        #print "circuit_update",line
        args = line.split()
        circ_id = int(args[0])

        c = self._maybe_create_circuit(circ_id)
        c.update(args)

    def _stream_update(self, line):
        """
        Used internally as a callback to update Stream information
        from STREAM events.
        """

        #print "stream_update",line
        if line.strip() == 'stream-status=':
            ## this happens if there are no active streams
            return

        args = line.split()
        assert len(args) >= 3

        stream_id = int(args[0])
        wasnew = False
        if stream_id not in self.streams:
            stream = self.stream_factory(self)
            self.streams[stream_id] = stream
            stream.listen(self)
            [stream.listen(x) for x in self.stream_listeners]
            wasnew = True
        self.streams[stream_id].update(args)

        ## if the update closed the stream, it won't be in our list
        ## anymore. FIXME: how can we ever hit such a case as the
        ## first update being a CLOSE?
        if wasnew and stream_id in self.streams:
            self._maybe_attach(self.streams[stream_id])

    def _addr_map(self, addr):
        "Internal callback to update DNS cache. Listens to ADDRMAP."
        txtorlog.msg(" --> addr_map", addr)
        self.addrmap.update(addr)

    event_map = {'STREAM': _stream_update,
                 'CIRC': _circuit_update,
                 'NS': _update_network_status,
                 'NEWCONSENSUS': _update_network_status,
                 'ADDRMAP': _addr_map}
    """event_map used by add_events to map event_name -> unbound method"""
    @defer.inlineCallbacks
    def _add_events(self):
        """
        Add listeners for all the events the controller is interested in.
        """

        for (event, func) in self.event_map.items():
            ## the map contains unbound methods, so we bind them
            ## to self so they call the right thing
            yield self.protocol.add_event_listener(event, types.MethodType(func, self, TorState))

    ## ICircuitContainer

    def find_circuit(self, circid):
        "ICircuitContainer API"
        return self.circuits[circid]

    ## IRouterContainer

    def router_from_id(self, routerid):
        """IRouterContainer API"""

        try:
            return self.routers[routerid[:41]]

        except KeyError:
            if routerid[0] != '$':
                raise                   # just re-raise the KeyError

            router = Router(self.protocol)
            idhash = routerid[1:41]
            nick = ''
            is_named = False
            if len(routerid) > 41:
                nick = routerid[42:]
                is_named = routerid[41] == '='
            router.update(nick, hashFromHexId(idhash), '0' * 27, 'unknown',
                          'unknown', '0', '0')
            router.name_is_unique = is_named
            self.routers[router.id_hex] = router
            return router

    ## implement IStreamListener

    def stream_new(self, stream):
        "IStreamListener: a new stream has been created"
        txtorlog.msg("stream_new", stream)

    def stream_succeeded(self, stream):
        "IStreamListener: stream has succeeded"
        txtorlog.msg("stream_succeeded", stream)

    def stream_attach(self, stream, circuit):
        """
        IStreamListener: the stream has been attached to a circuit. It
        seems you get an attach to None followed by an attach to real
        circuit fairly frequently. Perhaps related to __LeaveStreamsUnattached?
        """
        txtorlog.msg("stream_attach", stream.id,
                     stream.target_host, " -> ", circuit)

    def stream_detach(self, stream, **kw):
        """
        IStreamListener
        """
        txtorlog.msg("stream_detach", stream.id)

    def stream_closed(self, stream, **kw):
        """
        IStreamListener: stream has been closed (won't be in
        controller's list anymore)
        """

        txtorlog.msg("stream_closed", stream.id)
        del self.streams[stream.id]

    def stream_failed(self, stream, **kw):
        """
        IStreamListener: stream failed for some reason (won't be in
        controller's list anymore)
        """

        txtorlog.msg("stream_failed", stream.id)
        del self.streams[stream.id]

    ## implement ICircuitListener

    def circuit_launched(self, circuit):
        "ICircuitListener API"
        txtorlog.msg("circuit_launched", circuit)
        self.circuits[circuit.id] = circuit

    def circuit_extend(self, circuit, router):
        "ICircuitListener API"
        txtorlog.msg("circuit_extend:", circuit.id, router)

    def circuit_built(self, circuit):
        "ICircuitListener API"
        txtorlog.msg("circuit_built:", circuit.id,
                     "->".join("%s.%s" % (x.name, x.location.countrycode) for x in circuit.path),
                     circuit.streams)

    def circuit_new(self, circuit):
        "ICircuitListener API"
        txtorlog.msg("circuit_new:", circuit.id)
        self.circuits[circuit.id] = circuit

    def circuit_destroy(self, circuit):
        "Used by circuit_closed and circuit_failed (below)"
        txtorlog.msg("circuit_destroy:", circuit.id)
        del self.circuits[circuit.id]

    def circuit_closed(self, circuit, **kw):
        "ICircuitListener API"
        txtorlog.msg("circuit_closed", circuit)
        self.circuit_destroy(circuit)

    def circuit_failed(self, circuit, **kw):
        "ICircuitListener API"
        txtorlog.msg("circuit_failed", circuit, str(kw))
        self.circuit_destroy(circuit)
