import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import quote

from Cheetah.Template import Template  # type: ignore

from . import buildhelp
import pytivo.config
from pytivo.config import config_reset, config_write
from pytivo.plugin import Plugin
from pytivo.pytivo_types import Query

if TYPE_CHECKING:
    from pytivo.httpserver import TivoHTTPHandler

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = "Settings"

# Some error/status message templates

RESET_MSG = """<h3>Soft Reset</h3> <p>pyTivo has reloaded the
 pyTivo.conf file and all changes should now be in effect.</p>"""

RESTART_MSG = """<h3>Restart</h3> <p>pyTivo will now restart.</p>"""

GOODBYE_MSG = "Goodbye.\n"

SETTINGS_MSG = """<h3>Settings Saved</h3> <p>Your settings have been
 saved to the pyTivo.conf file. However you may need to do a <b>Soft
 Reset</b> or <b>Restart</b> before these changes will take effect.</p>"""

# Preload the templates
SETTINGS_TCLASS = Template.compile(
    file=os.path.join(SCRIPTDIR, "templates", "settings.tmpl")
)


class Settings(Plugin):
    CONTENT_TYPE = "text/html"

    def Quit(self, handler: "TivoHTTPHandler", query: Query) -> None:
        if hasattr(handler.server, "shutdown"):
            handler.send_fixed(GOODBYE_MSG.encode("utf-8"), "text/plain")
            if handler.server.in_service:
                handler.server.stop = True
            else:
                handler.server.shutdown()
            handler.server.socket.close()
        else:
            handler.send_error(501)

    def Restart(self, handler: "TivoHTTPHandler", query: Query) -> None:
        if hasattr(handler.server, "shutdown"):
            handler.redir(RESTART_MSG, 10)
            handler.server.restart = True
            if handler.server.in_service:
                handler.server.stop = True
            else:
                handler.server.shutdown()
            handler.server.socket.close()
        else:
            handler.send_error(501)

    def Reset(self, handler: "TivoHTTPHandler", query: Query) -> None:
        config_reset()
        handler.server.reset()
        handler.redir(RESET_MSG, 3)
        logging.getLogger("pyTivo.settings").info("pyTivo has been soft reset.")

    def Settings(self, handler: "TivoHTTPHandler", query: Query) -> None:
        # Read config file new each time in case there was any outside edits
        config_reset()

        shares_data = []
        for section in pytivo.config.CONFIG.sections():
            if not section.startswith(("_tivo_", "Server")):
                if not (
                    pytivo.config.CONFIG.has_option(section, "type")
                ) or pytivo.config.CONFIG.get(section, "type").lower() not in [
                    "settings",
                    "togo",
                ]:
                    shares_data.append(
                        (section, dict(pytivo.config.CONFIG.items(section, raw=True)))
                    )

        t = SETTINGS_TCLASS()
        t.mode = buildhelp.mode
        t.options = buildhelp.options
        t.container = handler.cname
        t.quote = quote
        t.server_data = dict(pytivo.config.CONFIG.items("Server", raw=True))
        t.server_known = buildhelp.getknown("server")
        t.hd_tivos_data = dict(pytivo.config.CONFIG.items("_tivo_HD", raw=True))
        t.hd_tivos_known = buildhelp.getknown("hd_tivos")
        t.sd_tivos_data = dict(pytivo.config.CONFIG.items("_tivo_SD", raw=True))
        t.sd_tivos_known = buildhelp.getknown("sd_tivos")
        t.shares_data = shares_data
        t.shares_known = buildhelp.getknown("shares")
        t.tivos_data = [
            (section, dict(pytivo.config.CONFIG.items(section, raw=True)))
            for section in pytivo.config.CONFIG.sections()
            if section.startswith("_tivo_")
            and not section.startswith(("_tivo_SD", "_tivo_HD"))
        ]
        t.tivos_known = buildhelp.getknown("tivos")
        t.help_list = buildhelp.gethelp()
        t.has_shutdown = hasattr(handler.server, "shutdown")
        handler.send_html(str(t))

    def each_section(self, query: Query, label: str, section: str) -> None:
        new_setting = new_value = " "
        if pytivo.config.CONFIG.has_section(section):
            pytivo.config.CONFIG.remove_section(section)
        pytivo.config.CONFIG.add_section(section)
        for key, value_list in list(query.items()):
            key = key.replace("opts.", "", 1)
            if key.startswith(label + "."):
                _, option = key.split(".")
                default = buildhelp.default.get(option, " ")
                value = value_list[0]
                if not pytivo.config.CONFIG.has_section(section):
                    pytivo.config.CONFIG.add_section(section)
                if option == "new__setting":
                    new_setting = value
                elif option == "new__value":
                    new_value = value
                elif value not in (" ", default):
                    pytivo.config.CONFIG.set(section, option, value)
        if not (new_setting == " " and new_value == " "):
            pytivo.config.CONFIG.set(section, new_setting, new_value)

    def UpdateSettings(self, handler: "TivoHTTPHandler", query: Query) -> None:
        config_reset()
        for section in ["Server", "_tivo_SD", "_tivo_HD"]:
            self.each_section(query, section, section)

        sections = query["Section_Map"][0].split("]")[:-1]
        for section in sections:
            ID, name = section.split("|")
            if query[ID][0] == "Delete_Me":
                pytivo.config.CONFIG.remove_section(name)
                continue
            if query[ID][0] != name:
                pytivo.config.CONFIG.remove_section(name)
                pytivo.config.CONFIG.add_section(query[ID][0])
            self.each_section(query, ID, query[ID][0])

        if query["new_Section"][0] != " ":
            pytivo.config.CONFIG.add_section(query["new_Section"][0])
        config_write()

        handler.redir(SETTINGS_MSG, 5)
