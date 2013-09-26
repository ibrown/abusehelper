"""
PhishTank feed handler. Requires a PhishTank application key.

Maintainer: Jussi Eronen <exec@iki.fi>
"""

import re
import bz2
import urllib2
import urlparse
import collections
from datetime import datetime
import xml.etree.cElementTree as etree

import idiokit
from abusehelper.core import bot, events, utils


def _replace_non_xml_chars(unicode_obj, replacement=u"\uFFFD"):
    return _NON_XML.sub(replacement, unicode_obj)
_NON_XML = re.compile(u"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]", re.U)


class BZ2Reader(object):
    def __init__(self, fileobj):
        self._fileobj = fileobj
        self._bz2 = bz2.BZ2Decompressor()

        self._line_buffer = collections.deque([""])

        self._current_line = ""
        self._current_offset = 0

    def _read_raw(self, chunk_size=65536):
        while True:
            compressed = self._fileobj.read(chunk_size)
            if not compressed:
                return ""

            decompressed = self._bz2.decompress(compressed)
            if decompressed:
                return decompressed

    def _read_line(self):
        if not self._line_buffer:
            return ""

        while len(self._line_buffer) == 1:
            raw = self._read_raw()
            if not raw:
                return self._line_buffer.pop()

            last = self._line_buffer.pop()
            self._line_buffer.extend((last + raw).splitlines(True))

        return self._line_buffer.popleft()

    def _mangle_line(self, line, target="utf-8"):
        # Forcibly decode the bytes into an unicode object.
        try:
            decoded = line.decode("utf-8")
        except UnicodeDecodeError:
            decoded = line.decode("latin-1")

        # Remove characters that are not proper XML 1.0.
        sanitized = _replace_non_xml_chars(decoded)

        return sanitized.encode("utf-8")

    def _read(self, amount):
        while self._current_offset >= len(self._current_line):
            line = self._read_line()
            if not line:
                return ""

            self._current_line = self._mangle_line(line)
            self._current_offset = 0

        data = self._current_line[self._current_offset:self._current_offset + amount]
        self._current_offset += len(data)
        return data

    def read(self, amount):
        result = list()

        while amount > 0:
            data = self._read(amount)
            if not data:
                break
            amount -= len(data)
            result.append(data)

        return "".join(result)


class HeadRequest(urllib2.Request):
    def get_method(self):
        return "HEAD"


class PhishTankBot(bot.PollingBot):
    application_key = bot.Param("registered application key for PhishTank")
    feed_url = bot.Param(default="http://data.phishtank.com/data/%s/online-valid.xml.bz2")

    def __init__(self, *args, **keys):
        bot.PollingBot.__init__(self, *args, **keys)
        self.fileobj = None
        self.etag = None

    @idiokit.stream
    def _handle_entry(self, entry, sites):
        url = entry.find("url")
        if url is None:
            return
        if not isinstance(url, basestring):
            url = url.text

        verification = entry.find("verification")
        if verification is None:
            return

        verified = verification.find("verified")
        if verified is None or verified.text != "yes":
            return

        ts = verification.find("verification_time")
        if ts != None and ts.text:
            try:
                ts = datetime.strptime(ts.text, "%Y-%m-%dT%H:%M:%S+00:00")
                ts = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
            except ValueError:
                ts = None

        status = entry.find("status")
        if status is None:
            return

        online = status.find("online")
        if online is None or online.text != "yes":
            return

        details = entry.find("details")
        if details is None:
            return

        target = entry.find("target")

        for detail in details.findall("detail"):
            ip = detail.find("ip_address")
            if ip is None:
                continue

            announcer = detail.find("announcing_network")
            if announcer is None or announcer.text == None:
                continue

            ip = ip.text
            announcer = announcer.text

            url_data = sites.setdefault(url, set())
            if (ip, announcer) in url_data:
                continue
            url_data.add((ip, announcer))

            event = events.Event()
            event.add("feed", "phishtank")
            event.add("url", url)
            parsed = urlparse.urlparse(url)
            host = parsed.netloc
            event.add("host", host)
            event.add("ip", ip)
            event.add("asn", announcer)

            if ts:
                event.add("source time", ts)

            if target is not None and target.text is not None:
                event.add("target", target.text)

            yield idiokit.send(event)

    @idiokit.stream
    def poll(self):
        url = self.feed_url % self.application_key
        try:
            self.log.info("Checking if %r has new data" % url)
            info, _ = yield utils.fetch_url(HeadRequest(url))

            etag = info.get("etag", None)
            if etag is None or self.etag != etag:
                self.log.info("Downloading new data from %r", url)
                _, self.fileobj = yield utils.fetch_url(url)
            self.etag = etag
        except utils.FetchUrlFailed, error:
            self.log.error("Failed to download %r: %r", url, error)
            return

        self.fileobj.seek(0)
        reader = BZ2Reader(self.fileobj)
        try:
            depth = 0
            sites = dict()

            for event, element in etree.iterparse(reader, events=("start", "end")):
                if event == "start" and element.tag == "entry":
                    depth += 1

                if event == "end" and element.tag == "entry":
                    yield self._handle_entry(element, sites)
                    depth -= 1

                if event == "end" and depth == 0:
                    element.clear()
        except SyntaxError, error:
            self.log.error("Syntax error in report %r: %r", url, error)

if __name__ == "__main__":
    PhishTankBot.from_command_line().execute()
