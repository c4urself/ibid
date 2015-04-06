# Copyright (c) 2008-2010, Michael Gorven, Stefano Rivera, Marco Gallotta
# Released under terms of the MIT/X/Expat Licence. See COPYING for details.
import json
import urllib

from twisted.web.client import getPage

from ibid.config import Option
from ibid.plugins import Processor, match

class Datadog(Processor):
    base_url = Option('base_url', 'Datadog base url', 'https://app.datadoghq.com')
    app_key = Option('application_key', 'Application key', '')
    api_key = Option('api_key', 'API key', '')
    channel = Option('channel', 'Channel to limit the listening to', '')

    @match(r'^dog\s+(.*)$')
    def handle_datadog_event(self, event, msg):
        params = urllib.urlencode({
            'application_key': self.app_key,
            'api_key': self.api_key
        })
        body = {
            'title': u"[Opslog] {}".format(event['sender']['nick']),
            'text': msg,
        }

        body_s = json.dumps(body)
        url = '{base}/api/v1/events?{params}'.format(
            base=self.base_url,
            params=params
        )

        #post_data = urllib.urlencode(body_s)
        #log.debug(post_data)

        headers = {
            'Content-Length': len(body_s),
            'Content-Type': 'application/json'
        }

        #log.debug(u'Sending Datadog event to: {}'.format(api_url))
        getPage(url, method='POST', postdata=body_s, headers=headers)
        event.addresponse(u'Event sent.')
