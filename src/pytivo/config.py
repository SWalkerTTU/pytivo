import configparser
import getopt
import logging
import logging.config
import os
import re
import socket
import sys
import uuid
from configparser import NoOptionError
from functools import reduce
from typing import Dict, List, Optional, Tuple

from pytivo.pytivo_types import Bdict, Settings

TIVOS: Dict[str, Settings] = {}
GUID = uuid.uuid4()
CONFIG_FILES: List[str] = []
TIVOS_FOUND = False
BIN_PATHS: Dict[str, str] = {}
CONFIG = configparser.ConfigParser()
CONFIGS_FOUND: List[str] = []


def config_init(argv: List[str]) -> None:
    global TIVOS
    global GUID
    global CONFIG_FILES
    global TIVOS_FOUND

    TIVOS = {}
    GUID = uuid.uuid4()
    TIVOS_FOUND = False

    p = os.getcwd()
    CONFIG_FILES = ["/etc/pyTivo.conf", os.path.join(p, "pyTivo.conf")]

    try:
        opts, _ = getopt.getopt(argv, "c:e:", ["config=", "extraconf="])
    except getopt.GetoptError as msg:
        print(msg)

    for opt, value in opts:
        if opt in ("-c", "--config"):
            CONFIG_FILES = [value]
        elif opt in ("-e", "--extraconf"):
            CONFIG_FILES.append(value)

    config_reset()


def config_reset() -> None:
    global BIN_PATHS
    global CONFIG
    global CONFIGS_FOUND
    global TIVOS_FOUND

    BIN_PATHS = {}

    CONFIG = configparser.ConfigParser()
    CONFIGS_FOUND = CONFIG.read(CONFIG_FILES)
    if not CONFIGS_FOUND:
        print(("WARNING: pyTivo.conf does not exist.\n" + "Assuming default values."))
        CONFIGS_FOUND = CONFIG_FILES[-1:]

    for section in CONFIG.sections():
        if section.startswith("_tivo_"):
            tsn = section[6:]
            if tsn.upper() not in ["SD", "HD", "4K"]:
                TIVOS_FOUND = True
                TIVOS[tsn] = Bdict(CONFIG.items(section))

    for section in ["Server", "_tivo_SD", "_tivo_HD"]:
        if not CONFIG.has_section(section):
            CONFIG.add_section(section)


def config_write() -> None:
    f = open(CONFIGS_FOUND[-1], "w")
    CONFIG.write(f)
    f.close()


def tivos_by_ip(tivoIP: str) -> str:
    for key, value in list(TIVOS.items()):
        if value["address"] == tivoIP:
            return key
    return ""


def get_server(name: str, default: str) -> str:
    if CONFIG.has_option("Server", name):
        return CONFIG.get("Server", name)
    else:
        return default


def getGUID() -> str:
    return str(GUID)


def get_ip(tsn: Optional[str] = None) -> str:
    if tsn is not None:
        dest_ip = TIVOS[tsn]["address"]
    else:
        dest_ip = "4.2.2.1"

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((dest_ip, 123))
    return s.getsockname()[0]


def get_zc() -> bool:
    opt = get_server("zeroconf", "auto").lower()

    if opt == "auto":
        for section in CONFIG.sections():
            if section.startswith("_tivo_"):
                if CONFIG.has_option(section, "shares"):
                    logger = logging.getLogger("pyTivo.config")
                    logger.info("Shares security in use -- zeroconf disabled")
                    return False
    elif opt in ["false", "no", "off"]:
        return False

    return True


def getBeaconAddresses() -> str:
    return get_server("beacon", "255.255.255.255")


def getPort() -> str:
    return get_server("port", "9032")


def get169Blacklist(tsn: str) -> bool:  # tivo does not pad 16:9 video
    return bool(tsn) and not isHDtivo(tsn) and not get169Letterbox(tsn)
    # verified Blacklist Tivo's are ('130', '240', '540')
    # It is assumed all remaining non-HD and non-Letterbox tivos are Blacklist


def get169Letterbox(tsn: str) -> bool:  # tivo pads 16:9 video for 4:3 display
    return bool(tsn) and tsn[:3] in ["649"]


def get169Setting(tsn: str) -> bool:
    if not tsn:
        return True

    tsnsect = "_tivo_" + tsn
    if CONFIG.has_section(tsnsect):
        if CONFIG.has_option(tsnsect, "aspect169"):
            try:
                return CONFIG.getboolean(tsnsect, "aspect169")
            except ValueError:
                pass

    if get169Blacklist(tsn) or get169Letterbox(tsn):
        return False

    return True


def getAllowedClients() -> List[str]:
    return get_server("allowedips", "").split()


def getIsExternal(tsn: str) -> bool:
    tsnsect = "_tivo_" + tsn
    if tsnsect in CONFIG.sections():
        if CONFIG.has_option(tsnsect, "external"):
            try:
                return CONFIG.getboolean(tsnsect, "external")
            except ValueError:
                pass

    return False


def isTsnInConfig(tsn: str) -> bool:
    return ("_tivo_" + tsn) in CONFIG.sections()


def getShares(tsn: str = "") -> List[Tuple[str, Settings]]:
    shares = [
        (section, Bdict(CONFIG.items(section)))
        for section in CONFIG.sections()
        if not (
            section.startswith(("_tivo_", "logger_", "handler_", "formatter_"))
            or section in ("Server", "loggers", "handlers", "formatters")
        )
    ]

    tsnsect = "_tivo_" + tsn
    if CONFIG.has_section(tsnsect) and CONFIG.has_option(tsnsect, "shares"):
        # clean up leading and trailing spaces & make sure ref is valid
        tsnshares = []
        for x in CONFIG.get(tsnsect, "shares").split(","):
            y = x.strip()
            if CONFIG.has_section(y):
                tsnshares.append((y, Bdict(CONFIG.items(y))))
        shares = tsnshares

    shares.sort()

    if get_server("nosettings", "false").lower() in ["false", "no", "off"]:
        shares.append(("Settings", Bdict({"type": "settings"})))
    if get_server("tivo_mak", "") and get_server("togo_path", ""):
        shares.append(("ToGo", Bdict({"type": "togo"})))

    return shares


def getDebug() -> bool:
    try:
        return CONFIG.getboolean("Server", "debug")
    except:
        return False


def getOptres(tsn: str) -> bool:
    try:
        return CONFIG.getboolean("_tivo_" + tsn, "optres")
    except:
        try:
            return CONFIG.getboolean(get_section(tsn), "optres")
        except:
            try:
                return CONFIG.getboolean("Server", "optres")
            except:
                return False


def get_bin(fname: str) -> Optional[str]:
    global BIN_PATHS

    logger = logging.getLogger("pyTivo.config")

    if fname in BIN_PATHS:
        return BIN_PATHS[fname]

    if CONFIG.has_option("Server", fname):
        fpath = CONFIG.get("Server", fname)
        if os.path.exists(fpath) and os.path.isfile(fpath):
            BIN_PATHS[fname] = fpath
            return fpath
        else:
            logger.error("Bad %s path: %s" % (fname, fpath))

    if sys.platform == "win32":
        fext = ".exe"
    else:
        fext = ""

    sys_path = os.getenv("PATH")
    if sys_path is not None:
        sys_path_list = sys_path.split(os.pathsep)
    else:
        sys_path_list = []
    for path in [os.path.join(os.path.dirname(__file__), "bin")] + sys_path_list:
        fpath = os.path.join(path, fname + fext)
        if os.path.exists(fpath) and os.path.isfile(fpath):
            BIN_PATHS[fname] = fpath
            return fpath

    logger.warn("%s not found" % fname)
    return None


def getFFmpegWait() -> int:
    if CONFIG.has_option("Server", "ffmpeg_wait"):
        return max(int(float(CONFIG.get("Server", "ffmpeg_wait"))), 1)
    else:
        return 0


def getFFmpegPrams(tsn: str) -> Optional[str]:
    return get_tsn("ffmpeg_pram", tsn, True)


def isHDtivo(tsn: str) -> bool:  # TSNs of High Definition TiVos
    return bool(tsn and tsn[0] >= "6" and tsn[:3] != "649")


def get_ts_flag() -> str:
    return get_server("ts", "auto").lower()


def is_ts_capable(tsn: str) -> bool:  # tsn's of Tivos that support transport streams
    return bool(tsn and (tsn[0] >= "7" or tsn.startswith("663")))


def getValidWidths() -> List[int]:
    return [1920, 1440, 1280, 720, 704, 544, 480, 352]


def getValidHeights() -> List[int]:
    return [1080, 720, 480]  # Technically 240 is also supported


# Return the number in list that is nearest to x
# if two values are equidistant, return the larger
def nearest(x: int, list_: List[int]) -> int:
    return reduce(lambda a, b: closest(x, a, b), list_)


def closest(x: int, a: int, b: int) -> int:
    da = abs(x - a)
    db = abs(x - b)
    if da < db or (da == db and a > b):
        return a
    else:
        return b


def nearestTivoHeight(height: int) -> int:
    return nearest(height, getValidHeights())


def nearestTivoWidth(width: int) -> int:
    return nearest(width, getValidWidths())


def getTivoHeight(tsn: str) -> int:
    return [480, 1080][isHDtivo(tsn)]


def getTivoWidth(tsn: str) -> int:
    return [544, 1920][isHDtivo(tsn)]


def _trunc64(i: str) -> int:
    return max(strtod(i) // 64000, 1) * 64


def getAudioBR(tsn: str) -> str:
    rate = get_tsn("audio_br", tsn)
    if not rate:
        rate = "448k"
    # convert to non-zero multiple of 64 to ensure ffmpeg compatibility
    # compare audio_br to max_audio_br and return lowest
    return str(min(_trunc64(rate), getMaxAudioBR(tsn))) + "k"


def _k(i: str) -> str:
    return str(strtod(i) // 1000) + "k"


def getVideoBR(tsn: str) -> str:
    rate = get_tsn("video_br", tsn)
    if rate:
        return _k(rate)
    return ["4096K", "16384K"][isHDtivo(tsn)]


def getMaxVideoBR(tsn: str) -> str:
    rate = get_tsn("max_video_br", tsn)
    if rate:
        return _k(rate)
    return "30000k"


def getBuffSize(tsn: str) -> str:
    size = get_tsn("bufsize", tsn)
    if size:
        return _k(size)
    return ["1024k", "4096k"][isHDtivo(tsn)]


def getMaxAudioBR(tsn: str) -> int:
    rate = get_tsn("max_audio_br", tsn)
    # convert to non-zero multiple of 64 for ffmpeg compatibility
    if rate:
        return _trunc64(rate)
    return 448


def get_section(tsn: str) -> str:
    return ["_tivo_SD", "_tivo_HD"][isHDtivo(tsn)]


def get_tsn(name: str, tsn: str, raw: bool = False) -> Optional[str]:
    try:
        return CONFIG.get("_tivo_" + tsn, name, raw=raw)
    except:
        try:
            return CONFIG.get(get_section(tsn), name, raw=raw)
        except:
            try:
                return CONFIG.get("Server", name, raw=raw)
            except:
                return None


# Parse a bitrate using the SI/IEEE suffix values as if by ffmpeg
# For example, 2K==2000, 2Ki==2048, 2MB==16000000, 2MiB==16777216
# Algorithm: http://svn.mplayerhq.hu/ffmpeg/trunk/libavcodec/eval.c
def strtod(value_str: str) -> int:
    prefixes = {
        "y": -24,
        "z": -21,
        "a": -18,
        "f": -15,
        "p": -12,
        "n": -9,
        "u": -6,
        "m": -3,
        "c": -2,
        "d": -1,
        "h": 2,
        "k": 3,
        "K": 3,
        "M": 6,
        "G": 9,
        "T": 12,
        "P": 15,
        "E": 18,
        "Z": 21,
        "Y": 24,
    }
    p = re.compile(r"^(\d+)(?:([yzafpnumcdhkKMGTPEZY])(i)?)?([Bb])?$")
    m = p.match(value_str)
    if not m:
        raise SyntaxError("Invalid bit value syntax")
    (coef, prefix, power, byte) = m.groups()
    if prefix is None:
        value = float(coef)
    else:
        exponent = float(prefixes[prefix])
        if power == "i":
            # Use powers of 2
            value = float(coef) * pow(2.0, exponent / 0.3)
        else:
            # Use powers of 10
            value = float(coef) * pow(10.0, exponent)
    if byte == "B":  # B == Byte, b == bit
        value *= 8
    return int(value)


def init_logging() -> None:
    if (
        CONFIG.has_section("loggers")
        and CONFIG.has_section("handlers")
        and CONFIG.has_section("formatters")
    ):
        logging.config.fileConfig(CONFIG)

    elif getDebug():
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
