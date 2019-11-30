import logging
import re
import socket
import struct
import time
import uuid
from threading import Timer
from urllib.parse import quote
from typing import List, Dict, Optional

import zeroconf

import pytivo.config
from pytivo.config import (
    getBeaconAddresses,
    getGUID,
    getPort,
    getShares,
    get_ip,
    get_server,
    get_zc,
)
from pytivo.plugin import GetPlugin
from pytivo.pytivo_types import Bdict

LOGGER = logging.getLogger(__name__)

SHARE_TEMPLATE = "/TiVoConnect?Command=QueryContainer&Container=%s"
PLATFORM_MAIN = "pyTivo"
PLATFORM_VIDEO = "pc/pyTivo"  # For the nice icon


class ZCListener:
    def __init__(self, names: List[str]) -> None:
        self.names = names

    def remove_service(self, server: zeroconf.Zeroconf, type_: str, name: str) -> None:
        self.names.remove(name.replace("." + type_, ""))

    def add_service(self, server: zeroconf.Zeroconf, type_: str, name: str) -> None:
        self.names.append(name.replace("." + type_, ""))


class ZCBroadcast:
    def __init__(self) -> None:
        """ Announce our shares via Zeroconf. """
        self.share_names: List[str] = []
        self.share_info: List[zeroconf.ServiceInfo] = []
        self.rz = zeroconf.Zeroconf()
        self.renamed: Dict[str, str] = {}
        old_titles = self.scan()
        address = socket.inet_aton(get_ip())
        port = int(getPort())
        LOGGER.info("Announcing shares...")
        for section, settings in getShares():
            try:
                ct = GetPlugin(settings["type"]).CONTENT_TYPE
            except:
                continue
            if ct.startswith("x-container/"):
                if "video" in ct:
                    platform = PLATFORM_VIDEO
                else:
                    platform = PLATFORM_MAIN
                LOGGER.info("Registering: %s" % section)
                self.share_names.append(section)
                desc = {
                    "path": SHARE_TEMPLATE % quote(section),
                    "platform": platform,
                    "protocol": "http",
                    "tsn": "{%s}" % uuid.uuid4(),
                }
                tt = ct.split("/")[1]
                title = section
                count = 1
                while title in old_titles:
                    count += 1
                    title = "%s [%d]" % (section, count)
                    self.renamed[section] = title
                info = zeroconf.ServiceInfo(
                    "_%s._tcp.local." % tt,
                    "%s._%s._tcp.local." % (title, tt),
                    address,
                    port,
                    0,
                    0,
                    desc,
                )
                self.rz.register_service(info)
                self.share_info.append(info)

    def scan(self) -> List[str]:
        """ Look for TiVos using Zeroconf. """
        VIDS = "_tivo-videos._tcp.local."
        names: List[str] = []

        LOGGER.info("Scanning for TiVos...")

        # Get the names of servers offering TiVo videos
        _ = zeroconf.ServiceBrowser(self.rz, VIDS, listener=ZCListener(names))

        # Give them a second to respond
        time.sleep(1)

        # any results?
        if names:
            pytivo.config.TIVOS_FOUND = True

        # Now get the addresses -- this is the slow part
        for name in names:
            info = self.rz.get_service_info(VIDS, name + "." + VIDS)
            if info:
                tsn = tsn_from_service_info(info)
                if tsn is not None:
                    address = socket.inet_ntoa(info.address)
                    port = info.port
                    pytivo.config.TIVOS[tsn] = Bdict(
                        {"name": name, "address": address, "port": port}
                    )
                    pytivo.config.TIVOS[tsn].update(info.properties)
                    LOGGER.info(name)

        return names

    def shutdown(self) -> None:
        LOGGER.info("Unregistering: %s" % " ".join(self.share_names))
        for info in self.share_info:
            self.rz.unregister_service(info)
        self.rz.close()


class Beacon:
    def __init__(self) -> None:
        self.UDPSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.UDPSock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.services: List[bytes] = []
        self.bd: Optional[ZCBroadcast]

        self.platform = PLATFORM_VIDEO
        for section, settings in getShares():
            try:
                ct = GetPlugin(settings["type"]).CONTENT_TYPE
            except:
                continue
            if ct in ("x-container/tivo-music", "x-container/tivo-photos"):
                self.platform = PLATFORM_MAIN
                break

        if get_zc():
            try:
                self.bd = ZCBroadcast()
            except:
                LOGGER.error("Zeroconf failure", exc_info=True)
                self.bd = None
        else:
            self.bd = None

    def add_service(self, service: bytes) -> None:
        self.services.append(service)
        self.send_beacon()

    def format_services(self) -> bytes:
        return b";".join(self.services)

    def format_beacon(self, conntype: bytes, services: bool = True) -> bytes:
        beacon = [
            b"tivoconnect=1",
            b"method=%s" % conntype,
            b"identity={%s}" % bytes(getGUID(), "utf-8"),
            b"machine=%s" % bytes(socket.gethostname(), "utf-8"),
            b"platform=%s" % bytes(self.platform, "utf-8"),
        ]

        if services:
            beacon.append(b"services=" + self.format_services())
        else:
            beacon.append(b"services=TiVoMediaServer:0/http")

        return b"\n".join(beacon) + b"\n"

    def send_beacon(self) -> None:
        beacon_ips = getBeaconAddresses()
        beacon = self.format_beacon(b"broadcast")
        for beacon_ip in beacon_ips.split():
            if beacon_ip != "listen":
                try:
                    packet = beacon
                    while packet:
                        result = self.UDPSock.sendto(packet, (beacon_ip, 2190))
                        if result < 0:
                            break
                        packet = packet[result:]
                except Exception as e:
                    print(e)

    def start(self) -> None:
        self.send_beacon()
        self.timer = Timer(60, self.start)
        self.timer.start()

    def stop(self) -> None:
        self.timer.cancel()
        if self.bd:
            self.bd.shutdown()

    def recv_bytes(self, sock: socket.socket, length: int) -> bytes:
        block = b""
        while len(block) < length:
            add = sock.recv(length - len(block))
            if not add:
                break
            block += add
        return block

    def recv_packet(self, sock: socket.socket) -> bytes:
        length = struct.unpack("!I", self.recv_bytes(sock, 4))[0]
        return self.recv_bytes(sock, length)

    def send_packet(self, sock: socket.socket, packet: bytes) -> None:
        sock.sendall(struct.pack("!I", len(packet)) + packet)

    def listen(self) -> None:
        """ For the direct-connect, TCP-style beacon """
        import _thread

        def server() -> None:
            TCPSock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            TCPSock.bind(("", 2190))
            TCPSock.listen(5)

            while True:
                # Wait for a connection
                client, address = TCPSock.accept()

                # Accept (and discard) the client's beacon
                self.recv_packet(client)

                # Send ours
                self.send_packet(client, self.format_beacon(b"connected"))

                client.close()

        _thread.start_new_thread(server, ())

    def get_name(self, address: str) -> str:
        """ Exchange beacons, and extract the machine name. """
        our_beacon = self.format_beacon(b"connected", False)

        try:
            tsock = socket.socket()
            tsock.connect((address, 2190))
            self.send_packet(tsock, our_beacon)
            tivo_beacon = self.recv_packet(tsock)
            tsock.close()
        except:
            return address

        name_re = re.search(r"machine=(.*)\n", tivo_beacon.decode("utf-8"))
        if name_re:
            return name_re.groups()[0]
        else:
            return address


def tsn_from_service_info(info: zeroconf.ServiceInfo) -> Optional[str]:
    tsn = info.properties.get(b"TSN")
    if get_server("togo_all", ""):
        tsn = info.properties.get(b"tsn", tsn)

    if tsn is None:
        return None

    if isinstance(tsn, bytes):
        tsn_str = tsn.decode("utf-8")
    else:
        tsn_str = str(tsn)

    return tsn_str
