import cgi
import http.cookiejar
import logging
import os
import subprocess
import sys
import _thread
import time
from typing import TYPE_CHECKING, Any, Dict, List, BinaryIO
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import quote, unquote
from xml.dom import minidom  # type: ignore

from Cheetah.Template import Template  # type: ignore

import pytivo.config
from pytivo.config import (
    getShares,
    get_bin,
    get_server,
    get_tsn,
    is_ts_capable,
    tivos_by_ip,
)
from pytivo.metadata import dump, from_container, from_details, human_size, tag_data
from pytivo.plugin import Plugin
from pytivo.pytivo_types import Query

if TYPE_CHECKING:
    from pytivo.httpserver import TivoHTTPHandler

LOGGER = logging.getLogger(__name__)

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = "ToGo"

# Characters to remove from filenames
BADCHAR = {
    "\\": "-",
    "/": "-",
    ":": " -",
    ";": ",",
    "*": ".",
    "?": ".",
    "!": ".",
    '"': "'",
    "<": "(",
    ">": ")",
    "|": " ",
}

# Default top-level share path
DEFPATH = "/TiVoConnect?Command=QueryContainer&Container=/NowPlaying"

# Some error/status message templates
MISSING = """<h3>Missing Data</h3> <p>You must set both "tivo_mak" and
 "togo_path" before using this function.</p>"""

TRANS_QUEUE = """<h3>Queued for Transfer</h3> <p>%s</p> <p>queued for
 transfer to:</p> <p>%s</p>"""

TRANS_STOP = """<h3>Transfer Stopped</h3> <p>Your transfer of:</p>
 <p>%s</p> <p>has been stopped.</p>"""

UNQUEUE = """<h3>Removed from Queue</h3> <p>%s</p> <p>has been removed
 from the queue.</p>"""

UNABLE = """<h3>Unable to Connect to TiVo</h3> <p>pyTivo was unable to
 connect to the TiVo at %s.</p> <p>This is most likely caused by an
 incorrect Media Access Key. Please return to the Settings page and
 double check your <b>tivo_mak</b> setting.</p> <pre>%s</pre>"""

# Compile the templates
NPL_TCLASS = Template.compile(file=os.path.join(SCRIPTDIR, "templates", "npl.tmpl"))

MSWINDOWS = sys.platform == "win32"

STATUS: Dict[str, Dict[str, Any]] = {}  # Global variable to control download threads
TIVO_CACHE: Dict[str, Dict[str, Any]] = {}  # Cache of TiVo NPL
QUEUE: Dict[str, List[str]] = {}  # Recordings to download -- list per TiVo
BASIC_META: Dict[
    str, Dict[str, Any]
] = {}  # Data from NPL, parsed, indexed by progam URL
DETAILS_URLS: Dict[str, str] = {}  # URLs for extended data, indexed by main URL


def null_cookie(name: str, value: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        0,
        name,
        value,
        None,
        False,
        "",
        False,
        False,
        "",
        False,
        False,
        None,
        False,
        None,
        None,
        {},
    )


def getint(thing: Any) -> int:
    try:
        result = int(thing)
    except:
        result = 0
    return result


AUTH_HANDLER = urllib.request.HTTPPasswordMgrWithDefaultRealm()
cj = http.cookiejar.CookieJar()
cj.set_cookie(null_cookie("sid", "ADEADDA7EDEBAC1E"))
TIVO_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.HTTPBasicAuthHandler(AUTH_HANDLER),
    urllib.request.HTTPDigestAuthHandler(AUTH_HANDLER),
)


tsn = get_server("togo_tsn", "")
if tsn:
    TIVO_OPENER.addheaders.append(("TSN", tsn))


class ToGo(Plugin):
    CONTENT_TYPE = "text/html"

    def tivo_open(self, url: str) -> BinaryIO:
        # Loop just in case we get a server busy message
        while True:
            try:
                # Open the URL using our authentication/cookie opener
                return TIVO_OPENER.open(url)

            # Do a retry if the TiVo responds that the server is busy
            except urllib.error.HTTPError as e:
                if e.code == 503:
                    time.sleep(5)
                    continue

                # Log and throw the error otherwise
                LOGGER.error(e)
                raise

    def NPL(self, handler: "TivoHTTPHandler", query: Query) -> None:
        global BASIC_META
        global DETAILS_URLS
        shows_per_page = 50  # Change this to alter the number of shows returned
        folder = ""
        FirstAnchor = ""
        has_tivodecode = bool(get_bin("tivodecode"))

        if "TiVo" in query:
            tivoIP = query["TiVo"][0]
            tsn = tivos_by_ip(tivoIP)
            attrs = pytivo.config.TIVOS[tsn]
            tivo_name = attrs.get("name", tivoIP)
            tivo_mak = get_tsn("tivo_mak", tsn)
            if tivo_mak is None:
                handler.redir("Unable to find Tivo.", 10)
                return

            protocol = attrs.get("protocol", "https")
            ip_port = "%s:%d" % (tivoIP, attrs.get("port", 443))
            path = attrs.get("path", DEFPATH)
            baseurl = "%s://%s%s" % (protocol, ip_port, path)
            theurl = baseurl
            if "Folder" in query:
                folder = query["Folder"][0]
                theurl = urllib.parse.urljoin(theurl, folder)
            theurl += "&ItemCount=%d" % shows_per_page
            if "AnchorItem" in query:
                theurl += "&AnchorItem=" + quote(query["AnchorItem"][0])
            if "AnchorOffset" in query:
                theurl += "&AnchorOffset=" + query["AnchorOffset"][0]

            if (
                theurl not in TIVO_CACHE
                or (time.time() - TIVO_CACHE[theurl]["thepage_time"]) >= 60
            ):
                # if page is not cached or old then retreive it
                AUTH_HANDLER.add_password("TiVo DVR", ip_port, "tivo", tivo_mak)
                try:
                    page = self.tivo_open(theurl)
                except IOError as e:
                    handler.redir(UNABLE % (tivoIP, cgi.escape(str(e))), 10)
                    return
                TIVO_CACHE[theurl] = {
                    "thepage": minidom.parse(page),
                    "thepage_time": time.time(),
                }
                page.close()

            xmldoc = TIVO_CACHE[theurl]["thepage"]
            items = xmldoc.getElementsByTagName("Item")
            TotalItems = int(tag_data(xmldoc, "TiVoContainer/Details/TotalItems"))
            ItemStart = int(tag_data(xmldoc, "TiVoContainer/ItemStart"))
            ItemCount = int(tag_data(xmldoc, "TiVoContainer/ItemCount"))
            title = tag_data(xmldoc, "TiVoContainer/Details/Title")
            if items:
                FirstAnchor = tag_data(items[0], "Links/Content/Url")

            data = []
            for item in items:
                entry = {}
                for tag in ("CopyProtected", "ContentType"):
                    value = tag_data(item, "Details/" + tag)
                    if value:
                        entry[tag] = value
                if entry["ContentType"].startswith("x-tivo-container"):
                    entry["Url"] = tag_data(item, "Links/Content/Url")
                    entry["Title"] = tag_data(item, "Details/Title")
                    entry["TotalItems"] = tag_data(item, "Details/TotalItems")
                    lc = tag_data(item, "Details/LastCaptureDate")
                    if not lc:
                        lc = tag_data(item, "Details/LastChangeDate")
                    entry["LastChangeDate"] = time.strftime(
                        "%b %d, %Y", time.localtime(int(lc, 16))
                    )
                else:
                    keys = {
                        "Icon": "Links/CustomIcon/Url",
                        "Url": "Links/Content/Url",
                        "Details": "Links/TiVoVideoDetails/Url",
                        "SourceSize": "Details/SourceSize",
                        "Duration": "Details/Duration",
                        "CaptureDate": "Details/CaptureDate",
                    }
                    for key in keys:
                        value = tag_data(item, keys[key])
                        if value:
                            entry[key] = value

                    if "SourceSize" in entry:
                        rawsize = entry["SourceSize"]
                        entry["SourceSize"] = human_size(rawsize)

                    if "Duration" in entry:
                        dur = getint(entry["Duration"]) / 1000
                        entry["Duration"] = "%d:%02d:%02d" % (
                            dur / 3600,
                            (dur % 3600) / 60,
                            dur % 60,
                        )

                    if "CaptureDate" in entry:
                        entry["CaptureDate"] = time.strftime(
                            "%b %d, %Y", time.localtime(int(entry["CaptureDate"], 16))
                        )

                    url = urllib.parse.urljoin(baseurl, entry["Url"])
                    entry["Url"] = url
                    if url in BASIC_META:
                        entry.update(BASIC_META[url])
                    else:
                        basic_data = from_container(item)
                        entry.update(basic_data)
                        BASIC_META[url] = basic_data
                        if "Details" in entry:
                            DETAILS_URLS[url] = entry["Details"]

                data.append(entry)
        else:
            data = []
            tivoIP = ""
            TotalItems = 0
            ItemStart = 0
            ItemCount = 0
            title = ""

        t = NPL_TCLASS()
        t.quote = quote
        t.folder = folder
        t.status = STATUS
        if tivoIP in QUEUE:
            t.queue = QUEUE[tivoIP]
        t.has_tivodecode = has_tivodecode
        t.togo_mpegts = is_ts_capable(tsn)
        t.tname = tivo_name
        t.tivoIP = tivoIP
        t.container = handler.cname
        t.data = data
        t.len = len
        t.TotalItems = getint(TotalItems)
        t.ItemStart = getint(ItemStart)
        t.ItemCount = getint(ItemCount)
        t.FirstAnchor = quote(FirstAnchor)
        t.shows_per_page = shows_per_page
        t.title = title
        handler.send_html(str(t), refresh="300")

    def get_tivo_file(self, tivoIP: str, url: str, mak: str, togo_path: str) -> None:
        # global STATUS
        STATUS[url].update({"running": True, "queued": False})

        parse_url = urllib.parse.urlparse(url)

        name_list = unquote(parse_url[2]).split("/")[-1].split(".")
        try:
            id = unquote(parse_url[4]).split("id=")[1]
            name_list.insert(-1, " - " + id)
        except:
            pass
        ts = STATUS[url]["ts_format"]
        if STATUS[url]["decode"]:
            if ts:
                name_list[-1] = "ts"
            else:
                name_list[-1] = "mpg"
        else:
            if ts:
                name_list.insert(-1, " (TS)")
            else:
                name_list.insert(-1, " (PS)")
        name_list.insert(-1, ".")
        name: str = "".join(name)
        for ch in BADCHAR:
            name = name.replace(ch, BADCHAR[ch])
        outfile = os.path.join(togo_path, name)

        if STATUS[url]["save"]:
            meta = BASIC_META[url]
            try:
                handle = self.tivo_open(DETAILS_URLS[url])
                meta.update(from_details(handle.read().decode("utf-8")))
                handle.close()
            except:
                pass
            metafile = open(outfile + ".txt", "w")
            dump(metafile, meta)
            metafile.close()

        AUTH_HANDLER.add_password("TiVo DVR", url, "tivo", mak)
        try:
            if STATUS[url]["ts_format"]:
                handle = self.tivo_open(url + "&Format=video/x-tivo-mpeg-ts")
            else:
                handle = self.tivo_open(url)
        except Exception as msg:
            STATUS[url]["running"] = False
            STATUS[url]["error"] = str(msg)
            return

        tivo_name = pytivo.config.TIVOS[tivos_by_ip(tivoIP)].get("name", tivoIP)

        LOGGER.info(
            '[%s] Start getting "%s" from %s'
            % (time.strftime("%d/%b/%Y %H:%M:%S"), outfile, tivo_name)
        )

        if STATUS[url]["decode"]:
            tivodecode_path = get_bin("tivodecode")
            if tivodecode_path is None:
                STATUS[url]["running"] = False
                STATUS[url]["error"] = "Can't find binary 'tivodecode'."
                return

            tcmd = [tivodecode_path, "-m", mak, "-o", outfile, "-"]
            tivodecode = subprocess.Popen(
                tcmd, stdin=subprocess.PIPE, bufsize=(512 * 1024)
            )
            f = tivodecode.stdin
        else:
            f = open(outfile, "wb")
        length = 0
        start_time = time.time()
        last_interval = start_time
        now = start_time
        try:
            while STATUS[url]["running"]:
                output = handle.read(1024000)
                if not output:
                    break
                length += len(output)
                f.write(output)
                now = time.time()
                elapsed = now - last_interval
                if elapsed >= 5:
                    STATUS[url]["rate"] = "%.2f Mb/s" % (
                        length * 8.0 / (elapsed * 1024 * 1024)
                    )
                    STATUS[url]["size"] += length
                    length = 0
                    last_interval = now
            if STATUS[url]["running"]:
                STATUS[url]["finished"] = True
        except Exception as msg:
            STATUS[url]["running"] = False
            LOGGER.info(msg)
        handle.close()
        f.close()
        STATUS[url]["size"] += length
        if STATUS[url]["running"]:
            mega_elapsed = (now - start_time) * 1024 * 1024
            if mega_elapsed < 1:
                mega_elapsed = 1
            size = STATUS[url]["size"]
            rate = size * 8.0 / mega_elapsed
            LOGGER.info(
                '[%s] Done getting "%s" from %s, %d bytes, %.2f Mb/s'
                % (time.strftime("%d/%b/%Y %H:%M:%S"), outfile, tivo_name, size, rate)
            )
            STATUS[url]["running"] = False
        else:
            os.remove(outfile)
            if STATUS[url]["save"]:
                os.remove(outfile + ".txt")
            LOGGER.info(
                '[%s] Transfer of "%s" from %s aborted'
                % (time.strftime("%d/%b/%Y %H:%M:%S"), outfile, tivo_name)
            )
            del STATUS[url]

    def process_queue(self, tivoIP: str, mak: str, togo_path: str) -> None:
        while QUEUE[tivoIP]:
            time.sleep(5)
            url = QUEUE[tivoIP][0]
            self.get_tivo_file(tivoIP, url, mak, togo_path)
            QUEUE[tivoIP].pop(0)
        del QUEUE[tivoIP]

    def ToGo(self, handler: "TivoHTTPHandler", query: Query) -> None:
        togo_path = get_server("togo_path", "")
        for name, data in getShares():
            if togo_path == name:
                togo_path = str(data.get("path"))
        if togo_path:
            tivoIP = query["TiVo"][0]
            tsn = tivos_by_ip(tivoIP)
            tivo_mak = get_tsn("tivo_mak", tsn)
            urls = query.get("Url", [])
            decode = "decode" in query
            save = "save" in query
            ts_format = "ts_format" in query
            for theurl in urls:
                STATUS[theurl] = {
                    "running": False,
                    "error": "",
                    "rate": "",
                    "queued": True,
                    "size": 0,
                    "finished": False,
                    "decode": decode,
                    "save": save,
                    "ts_format": ts_format,
                }
                if tivoIP in QUEUE:
                    QUEUE[tivoIP].append(theurl)
                else:
                    QUEUE[tivoIP] = [theurl]
                    _thread.start_new_thread(
                        ToGo.process_queue, (self, tivoIP, tivo_mak, togo_path)
                    )
                LOGGER.info(
                    '[%s] Queued "%s" for transfer to %s'
                    % (time.strftime("%d/%b/%Y %H:%M:%S"), unquote(theurl), togo_path)
                )
            urlstring = "<br>".join([unquote(x) for x in urls])
            message = TRANS_QUEUE % (urlstring, togo_path)
        else:
            message = MISSING
        handler.redir(message, 5)

    def ToGoStop(self, handler: "TivoHTTPHandler", query: Query) -> None:
        theurl = query["Url"][0]
        STATUS[theurl]["running"] = False
        handler.redir(TRANS_STOP % unquote(theurl))

    def Unqueue(self, handler: "TivoHTTPHandler", query: Query) -> None:
        theurl = query["Url"][0]
        tivoIP = query["TiVo"][0]
        del STATUS[theurl]
        QUEUE[tivoIP].remove(theurl)
        LOGGER.info(
            '[%s] Removed "%s" from queue'
            % (time.strftime("%d/%b/%Y %H:%M:%S"), unquote(theurl))
        )
        handler.redir(UNQUEUE % unquote(theurl))
