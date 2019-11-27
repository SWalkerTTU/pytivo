import http.server
import socketserver
import cgi
import gzip
import logging
import mimetypes
import os
import shutil
import socket
import time
from io import StringIO, BytesIO
from email.utils import formatdate
from urllib.parse import unquote_plus, quote, parse_qs
from xml.sax.saxutils import escape
from typing import Dict, Any, Optional, List, Tuple

from Cheetah.Template import Template  # type: ignore

import config
from plugin import GetPlugin
from beacon import Beacon
from pytivo_types import Query, Settings, Bdict

SCRIPTDIR = os.path.dirname(__file__)

SERVER_INFO = """<?xml version="1.0" encoding="utf-8"?>
<TiVoServer>
<Version>1.6</Version>
<InternalName>pyTivo</InternalName>
<InternalVersion>1.0</InternalVersion>
<Organization>pyTivo Developers</Organization>
<Comment>http://pytivo.sf.net/</Comment>
</TiVoServer>"""

VIDEO_FORMATS = """<?xml version="1.0" encoding="utf-8"?>
<TiVoFormats>
<Format><ContentType>video/x-tivo-mpeg</ContentType><Description/></Format>
</TiVoFormats>"""

VIDEO_FORMATS_TS = """<?xml version="1.0" encoding="utf-8"?>
<TiVoFormats>
<Format><ContentType>video/x-tivo-mpeg</ContentType><Description/></Format>
<Format><ContentType>video/x-tivo-mpeg-ts</ContentType><Description/></Format>
</TiVoFormats>"""

BASE_HTML = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN"
"http://www.w3.org/TR/html4/strict.dtd">
<html> <head><title>pyTivo</title>
<link rel="stylesheet" type="text/css" href="/main.css">
</head> <body> %s </body> </html>"""

RELOAD = '<p>The <a href="%s">page</a> will reload in %d seconds.</p>'
UNSUP = "<h3>Unsupported Command</h3> <p>Query:</p> <ul>%s</ul>"


class TivoHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    def __init__(
        self, server_address: Tuple[str, int], RequestHandlerClass: type
    ) -> None:
        self.containers: Dict[str, Settings] = {}
        self.beacon = Beacon()  # TODO 20191123 think about: set empty beacon to start
        self.stop = False
        self.restart = False
        self.logger = logging.getLogger("pyTivo")
        http.server.HTTPServer.__init__(self, server_address, RequestHandlerClass)
        self.daemon_threads = True

    def add_container(self, name: str, settings: Settings) -> None:
        if name in self.containers or name == "TiVoConnect":
            raise Exception("Container Name in use")
        try:
            self.containers[name] = settings
        except KeyError:
            self.logger.error("Unable to add container " + name)

    def reset(self) -> None:
        self.containers.clear()
        for section, settings in config.getShares():
            self.add_container(section, settings)

    def handle_error(self, request: bytes, client_address: Tuple[str, int]) -> None:
        self.logger.exception("Exception during request from %s" % (client_address,))

    def set_beacon(self, beacon: Beacon) -> None:
        self.beacon = beacon

    def set_service_status(self, status: bool) -> None:
        self.in_service = status


class TivoHTTPHandler(http.server.BaseHTTPRequestHandler):
    def __init__(
        self, request: bytes, client_address: Tuple[str, int], server: TivoHTTPServer
    ) -> None:
        self.container: Settings = Bdict({})
        self.wbufsize = 0x10000
        self.server_version = "pyTivo/1.0"
        self.protocol_version = "HTTP/1.1"
        self.sys_version = ""
        self.server: TivoHTTPServer
        http.server.BaseHTTPRequestHandler.__init__(
            self, request, client_address, server
        )

    def address_string(self) -> str:
        host, port = self.client_address[:2]
        return host

    def version_string(self) -> str:
        """ Override version_string() so it doesn't include the Python 
            version.

        """
        return self.server_version

    def do_GET(self) -> None:
        tsn = self.headers.get("TiVo_TCD_ID", self.headers.get("tsn", ""))
        if not self.authorize(tsn):
            return
        if tsn and (not config.tivos_found or tsn in config.tivos):
            attr = config.tivos.get(tsn, Bdict({}))
            if "address" not in attr:
                attr["address"] = self.address_string()
            if "name" not in attr:
                attr["name"] = self.server.beacon.get_name(attr["address"])
            config.tivos[tsn] = attr

        if "?" in self.path:
            path, opts = self.path.split("?", 1)
            query = parse_qs(opts)
        else:
            path = self.path
            query = {}

        if path == "/TiVoConnect":
            self.handle_query(query, tsn)
        else:
            ## Get File
            splitpath = [x for x in unquote_plus(path).split("/") if x]
            if splitpath:
                self.handle_file(query, splitpath)
            else:
                ## Not a file not a TiVo command
                self.infopage()

    def do_POST(self) -> None:
        tsn = self.headers.get("TiVo_TCD_ID", self.headers.get("tsn", ""))
        if not self.authorize(tsn):
            return
        ctype, pdict = cgi.parse_header(self.headers.get("content-type"))
        pdict_bytes = {key: val.encode("utf-8") for (key, val) in pdict.items()}
        if ctype == "multipart/form-data":
            query = cgi.parse_multipart(self.rfile, pdict_bytes)
        else:
            length = int(self.headers.get("content-length"))
            qs = self.rfile.read(length).decode("utf-8")
            query = parse_qs(qs, keep_blank_values=True)
        self.handle_query(query, tsn)

    def do_command(self, query: Query, command: str, target: str, tsn: str) -> bool:
        for name, container in config.getShares(tsn):
            if target == name:
                plugin = GetPlugin(container["type"])
                if hasattr(plugin, command):
                    self.cname = name
                    self.container = container
                    method = getattr(plugin, command)
                    method(self, query)
                    return True
                else:
                    break
        return False

    def handle_query(self, query: Query, tsn: str) -> None:
        mname = False
        if "Command" in query and len(query["Command"]) >= 1:

            command = query["Command"][0]

            # If we are looking at the root container
            if command == "QueryContainer" and (
                not "Container" in query or query["Container"][0] == "/"
            ):
                self.root_container()
                return

            if "Container" in query:
                # Dispatch to the container plugin
                basepath = query["Container"][0].split("/")[0]
                if self.do_command(query, command, basepath, tsn):
                    return

            elif command == "QueryItem":
                path = query.get("Url", [""])[0]
                splitpath = [x for x in unquote_plus(path).split("/") if x]
                if splitpath and not ".." in splitpath:
                    if self.do_command(query, command, splitpath[0], tsn):
                        return

            elif (
                command == "QueryFormats"
                and "SourceFormat" in query
                and query["SourceFormat"][0].startswith("video")
            ):
                if config.is_ts_capable(tsn):
                    self.send_xml(VIDEO_FORMATS_TS)
                else:
                    self.send_xml(VIDEO_FORMATS)
                return

            elif command == "QueryServer":
                self.send_xml(SERVER_INFO)
                return

            elif command in ("FlushServer", "ResetServer"):
                # Does nothing -- included for completeness
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                self.wfile.flush()
                return

        # If we made it here it means we couldn't match the request to
        # anything.
        self.unsupported(query)

    def send_content_file(self, path: str) -> None:
        lmdate = os.path.getmtime(path)
        try:
            handle = open(path, "rb")
        except:
            self.send_error(404)
            return

        # Send the header
        mime = mimetypes.guess_type(path)[0]
        self.send_response(200)
        if mime:
            self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Last-Modified", formatdate(lmdate))
        self.end_headers()

        # Send the body of the file
        try:
            shutil.copyfileobj(handle, self.wfile)
        except:
            pass
        handle.close()
        self.wfile.flush()

    def handle_file(self, query: Query, splitpath: List[str]) -> None:
        if ".." not in splitpath:  # Protect against path exploits
            ## Pass it off to a plugin?
            for name, container in list(self.server.containers.items()):
                if splitpath[0] == name:
                    self.cname = name
                    self.container = container
                    base = os.path.normpath(container["path"])
                    path = os.path.join(base, *splitpath[1:])
                    plugin = GetPlugin(container["type"])
                    # plugin could be Error, with no send_file method
                    try:
                        plugin.send_file(self, path, query)  # type: ignore
                    except AttributeError:
                        pass
                    return

            ## Serve it from a "content" directory?
            base = os.path.join(SCRIPTDIR, *splitpath[:-1])
            path = os.path.join(base, "content", splitpath[-1])

            if os.path.isfile(path):
                self.send_content_file(path)
                return

        ## Give up
        self.send_error(404)

    def authorize(self, tsn: Optional[str] = None) -> bool:
        # if allowed_clients is empty, we are completely open
        allowed_clients = config.getAllowedClients()
        if not allowed_clients or (tsn and config.isTsnInConfig(tsn)):
            return True
        client_ip = self.client_address[0]
        for allowedip in allowed_clients:
            if client_ip.startswith(allowedip):
                return True

        self.send_fixed(b"Unauthorized.", "text/plain", 403)
        return False

    # TODO: typing for *args ??
    def log_message(self, format: str, *args) -> None:
        self.server.logger.info(
            "%s [%s] %s"
            % (self.address_string(), self.log_date_time_string(), format % args)
        )

    def send_fixed(
        self, data: bytes, mime: str, code: int = 200, refresh: str = ""
    ) -> None:
        squeeze = (
            len(data) > 256
            and mime.startswith("text")
            and "gzip" in self.headers.get("Accept-Encoding", "")
        )
        if squeeze:
            out = BytesIO()
            gzip.GzipFile(mode="wb", fileobj=out).write(data)
            data = out.getvalue()
            out.close()
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if squeeze:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Expires", "0")
        if refresh:
            self.send_header("Refresh", refresh)
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def send_xml(self, page: str) -> None:
        # use page: str because Cheetah outputs unicode str
        self.send_fixed(page.encode("utf-8"), "text/xml")

    def send_html(self, page: str, code: int = 200, refresh: str = "") -> None:
        # use page: str because Cheetah outputs unicode str
        self.send_fixed(page.encode("utf-8"), "text/html; charset=utf-8", code, refresh)

    def root_container(self) -> None:
        tsn = self.headers.get("TiVo_TCD_ID", "")
        tsnshares = config.getShares(tsn)
        tsncontainers = []
        for section, settings in tsnshares:
            try:
                mime = GetPlugin(settings["type"]).CONTENT_TYPE
                if mime.split("/")[1] in ("tivo-videos", "tivo-music", "tivo-photos"):
                    settings["content_type"] = mime
                    tsncontainers.append((section, settings))
            except Exception as msg:
                self.server.logger.error(section + " - " + str(msg), exc_info=True)
        t = Template(file=os.path.join(SCRIPTDIR, "templates", "root_container.tmpl"))
        if self.server.beacon.bd:
            t.renamed = self.server.beacon.bd.renamed
        else:
            t.renamed = {}
        t.containers = tsncontainers
        t.hostname = socket.gethostname()
        t.escape = escape
        t.quote = quote
        self.send_xml(str(t))

    def infopage(self) -> None:
        t = Template(file=os.path.join(SCRIPTDIR, "templates", "info_page.tmpl"))
        t.admin = ""

        if config.get_server("tivo_mak", "") and config.get_server("togo_path", ""):
            t.togo = "<br>Pull from TiVos:<br>"
        else:
            t.togo = ""

        for section, settings in config.getShares():
            plugin_type = settings.get("type")
            if plugin_type == "settings":
                t.admin += (
                    '<a href="/TiVoConnect?Command=Settings&amp;'
                    + "Container="
                    + quote(section)
                    + '">Settings</a><br>'
                )
            elif plugin_type == "togo" and t.togo:
                for tsn in config.tivos:
                    if tsn and "address" in config.tivos[tsn]:
                        t.togo += (
                            '<a href="/TiVoConnect?'
                            + "Command=NPL&amp;Container="
                            + quote(section)
                            + "&amp;TiVo="
                            + config.tivos[tsn]["address"]
                            + '">'
                            + config.tivos[tsn]["name"]
                            + "</a><br>"
                        )

        self.send_html(str(t))

    def unsupported(self, query: Query) -> None:
        message = UNSUP % "\n".join(
            [
                "<li>%s: %s</li>" % (key, repr(value))
                for key, value in list(query.items())
            ]
        )
        text = BASE_HTML % message
        self.send_html(text, code=404)

    def redir(self, message: str, seconds: int = 2) -> None:
        url = self.headers.get("Referer")
        if url:
            message += RELOAD % (url, seconds)
            refresh = "%d; url=%s" % (seconds, url)
        else:
            refresh = ""
        text = BASE_HTML % message
        self.send_html(text, refresh=refresh)
