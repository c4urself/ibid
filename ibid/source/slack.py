"""
Slack source plugin for Ibid. Slack is different to other sources in a couple of ways:

    * It uses wss:// for listening to events and regular http:// for responding
    to them
    * It uses @username instead of the username: IRC convention

"""

# Copyright (c) 2014, Christian Verkerk
# Released under terms of the MIT/X/Expat Licence. See COPYING for details.

import logging
import re
import urllib

from twisted.internet import protocol, ssl
from twisted.web.client import getPage

from autobahn.twisted.websocket import (WebSocketClientFactory,
                                       WebSocketClientProtocol,
                                       connectWS)


import ibid
from ibid.config import Option, ListOption
from ibid.source import IbidSourceFactory
from ibid.event import Event

from ibid.compat import json


log = logging.getLogger('source.slack')


class WebSocketSlackProtocol(WebSocketClientProtocol):

    def onOpen(self):
        self.pingsReceived = 0
        self.pongsSent = 0
        self.messageNumber = 0

    def onPing(self, payload):
        self.pingsReceived += 1
        log.debug(u'Ping received from {} - {}'.format(self.peer, self.pingsReceived))
        self.sendPong(payload)
        self.pongsSent += 1
        log.debug(u'Pong sent to {} - {}'.format(self.peer, self.pongsSent))

    def handleError(self, data):
        log.error(u'WSS error code {} occurred, message was "{}"'.format(
            data['error'].get('code'),
            data['error'].get('message')
        ))

    def onMessage(self, payload, isBinary):
        """
        Handles an incoming message from Slack. This will always be either
        a utf8-encoded string or binary, check the latter via `isBinary`
        """
        log.debug(u'Received message via WSS')

        if isBinary:
            return

        payload = payload.decode('utf-8')
        log.info(u'Text message received: {}'.format(payload))

        try:
            data = json.loads(payload)
        except TypeError:
            log.exception(u'Error parsing JSON response')

        # Don't reply to acks
        if data.get('ok') and data.get('reply_to'):
            return

        if data['type'] == 'hello':
            log.info(u'Successfully connected to Slack via WSS')

        elif data['type'] == 'error':
            self.handleError(data)

        # Ignore other bots
        elif data['type'] == 'message' and data.get('subtype') != 'bot_message':

            # Handle annoying diff versions of Slack servers
            if 'message' in data:
                sender = self.factory.users[data['message']['user']]['name']
                text = data['message']['text']
                is_edited = 'edited' in data['message']
            else:
                sender = self.factory.users[data['user']]['name']
                text = data['text']
                is_edited = 'edited' in data

            # Ignore edits to sent messages
            if is_edited:
                log.info("Ignoring edited message")
                return

            channel_id = data['channel']
            raw_text = text
            event = Event(u'slack', u'message')
            event.message = self.factory.clean_slack_msg(raw_text)
            event.channel = channel_id
            event.sender['id'] = sender
            event.sender['connection'] = channel_id
            event.sender['nick'] = sender
            log.debug(u'Got raw message: {}'.format(event.message))
            if channel_id[0] == 'D':
                event.public = False
                event.addressed = True
            else:
                event.public = True
                if re.search(r'<@{}>'.format(self.factory.user_id), raw_text):
                    event.addressed = True
            ibid.dispatcher.dispatch(event).addCallback(self.parseResponse)

    def onClose(self, wasClean, code, reason):
        log.error(u'WebSocket connection closed: {0}'.format(reason))

    def parseResponse(self, event):
        self.messageNumber += 1
        log.debug(u'Responding to event via wss://')
        for response in event.responses:
            data = {
                u'id': self.messageNumber,
                u'type': u'message',
                u'channel': response['target'],
                u'text': response['reply'].encode('utf-8')
            }
            self.sendMessage(json.dumps(data), isBinary=False)

    def respondHTTP(self, event):
        log.debug(u'Responding to event via http://')
        for response in event.responses:
            self.factory.post_message(event, response)


class SlackBot(WebSocketClientFactory):

    protocol = WebSocketSlackProtocol

    def __init__(self, team, token, channels=None, groups=None):
        self.team = team
        self.token = token
        self.socket_url = ''
        self.authenticated = False
        self.channels_to_join = channels
        self.groups_to_join = groups
        self.channels = []
        self.groups = []
        self.users = {}
        self.user_id = None
        self.name = ''

    def clean_slack_msg(self, raw_msg):
        """ For no good reason slack uses <@`uid`> instead of
        `name`: so we have to clean it up a little
        """
        user_ids = re.findall(r'<@(U[\d\w]{8})>', raw_msg)
        msg = raw_msg
        if user_ids:
            for user_id in user_ids:
                try:
                    user_name = self.users[user_id]['name']
                    msg = re.sub('<@{}>'.format(user_id), u'{}'.format(user_name), msg)
                except:
                    log.error(user_id)
        links = re.findall(r'(\<http.*\|([^>]+)\>)', msg)
        if links:
            for link in links:
                try:
                    msg = msg.replace(link[0], link[1])
                except:
                    log.exception(u'Failed to clean link')
        log.debug(u'Cleaned message is: {}'.format(msg))
        return msg

    def connect(self):
        log.debug(u'Setting up WSS Client')

        WebSocketClientFactory.__init__(self, self.socket_url, debug=False)
        if self.isSecure:
            contextFactory = ssl.ClientContextFactory()
        else:
            contextFactory = None
        connectWS(self, contextFactory)

    def login(self):
        log.debug(u'Attempting to authenticate')
        self.call_API('rtm.start', cb=self.on_login)

    def on_login(self, response_string):
        log.debug(u'Parsing authentication response')
        data = json.loads(response_string)

        if data['ok']:
            self.socket_url = data['url']
            log.info(u'Received WSS URL: {url}'.format(url=self.socket_url))

            self.authenticated = True
            log.info(u'Authenticated successfully')

            self.team_id = data['team']['id']
            self.team_name = data['team']['name']
            self.team_domain = data['team']['domain']
            self.name = data['self']['name']
            self.user_id = data['self']['id']

            for user in data['users']:
                self.users[user['id']] = user
            self.users[self.user_id] = {'name': self.name}

            # TODO: If we're a restricted user skip all this
            # Leave/join channels that the admin has explicitly
            # told us to
            for channel in data['channels']:
                if channel.get('is_member'):
                    name, _id = channel['name'], channel['id']
                    if name not in self.channels_to_join:
                        log.debug(u'Leaving channel {}'.format(name))
                        self.leave(_id)
                    else:
                        self.channels.append(channel)
                        self.channels_to_join.remove(name)
            if self.channels_to_join:
                for channel in self.channels_to_join:
                    self.join(channel)

            # Ensure we leave groups the admin hasn't explicitly
            # told us to be part of (cannot join groups)
            log.debug(self.groups_to_join)
            for group in data['groups']:
                name, _id = group['name'], group['id']
                if name not in self.groups_to_join:
                    self.leave_group(_id)
                else:
                    self.groups.append(group)

            log.info(u'Logged in as {} ({})'.format(self.name, self.user_id))
            if self.channels:
                log.info(u'You are in the following channels: {}'.format(
                    ','.join(map(lambda c: '"{}"'.format(c['name']), self.channels))))
            else:
                log.info(u'You are not in any channels.')
            if self.groups:
                log.info(u'You are in the following private groups: {}'.format(
                    ','.join(map(lambda c: '"{}"'.format(c['name']), self.groups))))
            else:
                log.info(u'You are not in any private groups')

            event = Event(u'slack', u'source')
            event.status = u'connected'
            ibid.dispatcher.dispatch(event)

            self.connect()
        else:
            log.error(u'Authentication failed.')
            self.ebHandleAPIError('error')

    def handle_API_response(self, response_string):
        log.info(u'Received response: {}'.format(response_string))

    def handle_API_error(self):
        log.error(u'Errback')

    def send(self, response):
        self.post_message(None, response)

    def post_message(self, event, response):
        log.debug(event)

        params = {
            u'text': response['reply'].encode('utf-8'),
            u'channel': response['target'],
            u'username': ibid.config['botname'],
        }
        self.call_API('chat.postMessage', params=params)

    def join(self, name):
        log.info(u'Joining {}'.format(name))
        self.call_API('channels.join', cb=self.on_join_channel, params={'name': name})

    def on_join_channel(self, data):
        if data['ok']:
            channel = data['channel']
            self.channels[channel['id']] = channel
        else:
            self.handle_API_error(data['error'])

    def leave(self, _id):
        self.call_API('channels.leave', params={'channel': _id})
        del self.channels[_id]

    def leave_group(self, _id):
        self.call_API('groups.leave', params={'channel': _id})
        del self.groups[_id]

    def call_API(self, method, params=None, cb=None, eb=None):
        cb = cb or self.handle_API_response
        eb = eb or self.handle_API_error

        data = {
            'token': self.token
        }

        if params:
            data.update(params)

        post_data = urllib.urlencode(data)
        log.debug(post_data)

        api_url = 'https://{team}.slack.com/api/{method}'.format(
            team=self.team,
            method=method
        )

        headers = {
            'Content-Length': len(post_data),
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        log.debug(u'Fetching endpoint: {}'.format(api_url))
        getPage(api_url, method='POST', postdata=post_data, headers=headers) \
                .addCallback(cb) \
                .addErrback(eb)


class SourceFactory(IbidSourceFactory, protocol.ReconnectingClientFactory):

    auth = ('implicit',)
    supports = ('action', 'multiline', 'topic')

    token = Option('token', 'Slack Token')
    team = Option('team', 'Slack Team Name')
    channels = ListOption('channels', 'Channels (public) to join', [])
    groups = ListOption('groups', 'Groups (private) to join', [])

    def __init__(self, name):
        super(SourceFactory, self).__init__(name)
        self.client = SlackBot(self.team, self.token, channels=self.channels, groups=self.groups)

    def setServiceParent(self, service):
        self.client.login()

    def connect(self):
        self.setServiceParent(None);

    def disconnect(self):
        self.stopTrying()
        self.stopFactory()
        return True

    def url(self):
        return self.client.url

    def send(self, response):
        return self.client.send(response)

    def join(self, room_name):
        return self.client.join(room_name)

    def leave(self, room_name):
        return self.client.leave(room_name)

    def logging_name(self, identity):
        return identity if identity else u''

    def truncation_point(self, response, event=None):
        return None





# vi: set et sta sw=4 ts=4:
