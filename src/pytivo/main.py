#!/usr/bin/env python3

import logging
import os
import platform
import sys
import time
from typing import Type
from types import TracebackType

if sys.version_info[0] != 3 or sys.version_info[1] < 6:
    print("ERROR: pyTivo requires Python >= 3.6.\n")
    sys.exit(1)

try:
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
except:
    pass

from pytivo.beacon import Beacon
from pytivo.config import (
    config_init,
    init_logging,
    getPort,
    getShares,
    getBeaconAddresses,
)
from pytivo.httpserver import TivoHTTPServer, TivoHTTPHandler


def exceptionLogger(
    type_: Type[BaseException], value: BaseException, traceback: TracebackType
) -> None:
    sys.excepthook = sys.__excepthook__
    logging.getLogger("pyTivo").error(
        "Exception in pyTivo", exc_info=(type_, value, traceback)
    )


def last_date() -> str:
    lasttime = -1.0
    path = os.path.dirname(__file__)
    if not path:
        path = "."
    for root, dirs, files in os.walk(path):
        for name in files:
            if name.endswith(".py"):
                tm = os.path.getmtime(os.path.join(root, name))
                if tm > lasttime:
                    lasttime = tm

    return time.asctime(time.localtime(lasttime))


def setup(in_service: bool = False) -> TivoHTTPServer:
    config_init(sys.argv[1:])
    init_logging()
    sys.excepthook = exceptionLogger

    port = getPort()

    httpd = TivoHTTPServer(("", int(port)), TivoHTTPHandler)
    print("after TivoHTTPServer, before logging.getLogger in main.py")
    logger = logging.getLogger("pyTivo")
    logger.info("Last modified: " + last_date())
    logger.info("Python: " + platform.python_version())
    logger.info("System: " + platform.platform())

    for section, settings in getShares():
        httpd.add_container(section, settings)

    b = Beacon()
    b.add_service(b"TiVoMediaServer:%d/http" % int(port))
    b.start()
    if "listen" in getBeaconAddresses():
        b.listen()

    httpd.set_beacon(b)
    httpd.set_service_status(in_service)

    logger.info("pyTivo is ready.")
    return httpd


def serve(httpd: TivoHTTPServer) -> None:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def mainloop() -> bool:
    httpd = setup()
    serve(httpd)
    if httpd.beacon is not None:
        httpd.beacon.stop()
    return httpd.restart


def cli():
    while mainloop():
        time.sleep(5)


if __name__ == "__main__":
    cli()
