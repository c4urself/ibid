from time import sleep

import ibid
from ibid.plugins import Processor, match

class Delay(Processor):

    @match(r'^delay\s+(\d+\.?\d*)$')
    def handler(self, event, delay):
        sleep(float(delay))
        event.addresponse(True)

class Authorise(Processor):

    @match(r'^authorise\s+(\S+)$')
    def handler(self, event, permission):
        if ibid.auth.authorise(event, permission):
            event.addresponse(u'Yes')
        else:
            event.addresponse(u'No')

class Email(Processor):

    @match(r'^email\s+(.+)$')
    def email(self, event, address):
        event.addresponse(u'Test message', source='email', target=unicode(address))
        event.addresponse(u"I've emailed %s", address)

class Except(Processor):

    @match(r'^raise\s+exception$')
    def handler(self, event):
        raise Exception("Ow, that hurt.")

class Topic(Processor):
    @match(r'^topic\s+(.+)$')
    def handler(self, event, topic):
        event.addresponse(topic, topic=True, address=False)

# vi: set et sta sw=4 ts=4:
