from time import sleep
import logging

import dcwords

from twisted.protocols.basic import LineReceiver
from twisted.internet import reactor
from twisted.internet import protocol
from twisted.application import internet
from pkg_resources import resource_exists, resource_string

import ibid
from ibid.config import Option, IntOption, BoolOption, FloatOption
from ibid.source import IbidSourceFactory
from ibid.event import Event

class DCBot(dcwords.DCClient):
    version = resource_exists(__name__, '../.version') and resource_string(__name__, '../.version') or 'dev'
    client = 'Ibid' 

    _ping_deferred = None
    _reconnect_deferred = None

    def connectionMade(self):
        self.my_nickname = self.factory.nick
        self.my_password = self.factory.password
        self.my_interest = self.factory.interest
        self.my_speed = self.factory.speed
        self.my_email = self.factory.email
        self.my_sharesize = self.factory.sharesize
        self.my_slots = self.factory.slots
        dcwords.DCClient.connectionMade(self)
        self.factory.resetDelay()
        self.factory.send = self.send
        self.factory.proto = self
        self.auth_callbacks = {}
        self._ping_deferred = reactor.callLater(self.factory.ping_interval, self._idle_ping)
        self.factory.log.info(u"Connected")

    def connectionLost(self, reason):
        self.factory.log.info(u"Disconnected (%s)", reason)
        dcwords.DCClient.connectionLost(self, reason)

    def _idle_ping(self):
        self.factory.log.log(logging.DEBUG - 5, u'Sending idle PING')
        self._ping_deferred = None
        self._reconnect_deferred = reactor.callLater(self.factory.pong_timeout, self._timeout_reconnect)
        self.sendLine('$GetNickList')

    def _timeout_reconnect(self):
        self.factory.log.info(u'Ping-Pong timeout. Reconnecting')
        self.transport.loseConnection()

    def dc_NickList(self, params):
        dcwords.DCClient.dc_NickList(self, params)
        if self._reconnect_deferred is not None:
            self.factory.log.log(logging.DEBUG - 5, u'Received PONG')
            self._reconnect_deferred.cancel()
            self._reconnect_deferred = None
            self._ping_deferred = reactor.callLater(self.factory.ping_interval, self._idle_ping)
        
    def lineReceived(self, data):
        dcwords.DCClient.lineReceived(self, data)
        if self._ping_deferred is not None:
            self._ping_deferred.reset(self.factory.ping_interval)

    def sendLine(self, line):
        dcwords.DCClient.sendLine(self, line)
        if self._ping_deferred is not None:
            self._ping_deferred.reset(self.factory.ping_interval)

    def signedOn(self):
        names = ibid.config.plugins['core']['names']
        if self.my_nickname not in names:
            self.factory.log.info(u'Adding "%s" to plugins.core.names', self.my_nickname)
            names.append(self.my_nickname)
            ibid.config.plugins['core']['names'] = names
            ibid.reloader.reload_config()
        self.factory.log.info(u"Signed on")

    def _create_event(self, type, user):
        event = Event(self.factory.name, type)
        event.sender['connection'] = user
        event.sender['id'] = user
        event.sender['nick'] = event.sender['id']
        event.channel = u'$public'
        event.public = True
        event.source = self.factory.name
        return event

    def _state_event(self, user, action):
        event = self._create_event(u'state', user)
        event.state = action
        ibid.dispatcher.dispatch(event).addCallback(self.respond)

    def _message_event(self, msgtype, user, private, msg):
        event = self._create_event(msgtype, user)
        event.message = msg
        self.factory.log.debug(u'Received %s from %s in %s: %s',
                msgtype, event.sender['id'], private and u'private' or u'public', event.message)

        if private:
            event.addressed = True
            event.public = False
            event.channel = event.sender['nick']
        else:
            event.public = True

        ibid.dispatcher.dispatch(event).addCallback(self.respond)

    def privmsg(self, user, private, msg):
        self._message_event(u'message', user, private, msg)

    def userJoined(self, user):
        self._state_event(user, u'online')

    def userQuit(self, user):
        self._state_event(user, u'offline')

    def respond(self, event):
        for response in event.responses:
            self.send(response)

    def send(self, response):
        message = response['reply'].replace('\n', ' ')[:490]

        if message[0] in self.factory.banned_prefixes:
            self.factory.log.info(u'Suppressed banned response: %s', message)
            return

        target = response['target']
        if target == '$public':
            target = None

        if 'action' in response and response['action']:
            if self.factory.action_prefix and target is None:
                self.say(target, u'%s %s' % (self.factory.action_prefix, message))
            else:
                self.say(target, u'* %s %s' % (self.my_nickname, message))

            self.factory.log.debug(u"Sent action to %s: %s", target, message)
        else:
            self.say(target, message)
            self.factory.log.debug(u"Sent privmsg to %s: %s", target, message)

    def authenticate(self, nick, callback):
        self.auth_callbacks[nick] = callback
        self.sendLine('$GetNickList')

    def dc_OpList(self, params):
        dcwords.DCClient.dc_OpList(self, params)
        done = []
        for nick, callback in self.auth_callbacks.iteritems():
            if nick in self.hub_users and self.hub_users[nick].op is True:
                callback(nick, True)
            else:
                callback(nick, False)
            done.append(nick)
        for nick in done:
            del self.auth_callbacks[nick]

class SourceFactory(protocol.ReconnectingClientFactory, IbidSourceFactory):
    protocol = DCBot

    auth = ['hub', 'op']

    port = IntOption('port', 'Server port number', 411)
    server = Option('server', 'Server hostname')
    nick = Option('nick', 'DC nick', ibid.config['botname'])
    password = Option('password', 'Password', None)
    interest = Option('interest', 'User Description', '')
    speed = Option('speed', 'Bandwidth', '1kbps')
    email = Option('email', 'eMail Address', 'http://ibid.omnia.za.net/')
    sharesize = IntOption('sharesize', 'DC Share Size (bytes)', 0)
    slots = IntOption('slots', 'DC Open Slots', 0)
    trust_hubauth = BoolOption('trust_hubauth', 'Trust the server to authenticate users', False)
    action_prefix = Option('action_prefix', 'Command for actions (i.e. +me)', None)
    banned_prefixes = Option('banned_prefixes', 'Prefixes not allowed in bot responses, i.e. !', '')
    ping_interval = FloatOption('ping_interval', 'Seconds idle before sending a PING', 60)
    pong_timeout = FloatOption('pong_timeout', 'Seconds to wait for PONG', 300)
    # ReconnectingClient uses this:
    maxDelay = IntOption('max_delay', 'Max seconds to wait inbetween reconnects', 120)

    def __init__(self, name):
        IbidSourceFactory.__init__(self, name)
        self.log = logging.getLogger('source.%s' % self.name)
        self._auth = {}
        if not self.trust_hubauth:
            self.auth.remove('hub')

    def setServiceParent(self, service):
        if service:
            internet.TCPClient(self.server, self.port, self).setServiceParent(service)
        else:
            reactor.connectTCP(self.server, self.port, self)

    def connect(self):
        return self.setServiceParent(None)

    def disconnect(self):
        self.stopTrying()
        self.stopFactory()
        self.proto.transport.loseConnection()
        return True

    def auth_hub(self, event, credential):
        return True

    def _irc_auth_callback(self, nick, result):
        self._auth[nick] = result

    def auth_op(self, event, credential):
        nick = event.sender['nick']
        if nick in self.proto.hub_users and self.proto.hub_users[nick].op in (True, False):
            return self.proto.hub_users[nick].op

        reactor.callFromThread(self.proto.authenticate, nick, self._irc_auth_callback)
        for i in xrange(150):
            if nick in self._auth:
                break
            sleep(0.1)

        if nick in self._auth:
            result = self._auth[nick]
            del self._auth[nick]
            return result

    def url(self):
        return u'dc://%s@%s:%s' % (self.nick, self.server, self.port)

# vi: set et sta sw=4 ts=4:
