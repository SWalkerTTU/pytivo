import calendar
from functools import lru_cache
import logging
import os
import struct
import time
import zlib
from collections.abc import MutableMapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional, List, Dict, Any, Iterator
from xml.sax.saxutils import escape

from Cheetah.Template import Template  # type: ignore

import pytivo.config
from pytivo.config import (
    getDebug,
    getGUID,
    getMaxAudioBR,
    getTivoHeight,
    getTivoWidth,
    get_bin,
    get_server,
    get_ts_flag,
    is_ts_capable,
)
import pytivo.metadata
from pytivo.metadata import (
    basic,
    from_mscore,
    from_tivo,
    get_color,
    get_mpaa,
    get_stars,
    get_tv,
    human_size,
    video_info,
)
from pytivo.plugins.video.transcode import (
    is_resumable,
    resume_transfer,
    select_videostr,
    supported_format,
    tivo_compatible,
    transcode,
    transcode_settings,
)
from pytivo.plugin import Plugin, quote
from pytivo.pytivo_types import Query

if TYPE_CHECKING:
    from pytivo.httpserver import TivoHTTPHandler

LOGGER = logging.getLogger(__name__)

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = "Video"

# Compile the templates
VIDEO_CONTAINER_XML_TCLASS = Template.compile(
    file=os.path.join(SCRIPTDIR, "templates", "container_xml.tmpl")
)
TVBUS_TCLASS = Template.compile(file=os.path.join(SCRIPTDIR, "templates", "TvBus.tmpl"))

EXTENSIONS = """.tivo .mpg .avi .wmv .mov .flv .f4v .vob .mp4 .m4v .mkv
.ts .tp .trp .3g2 .3gp .3gp2 .3gpp .amv .asf .avs .bik .bix .box .bsf
.dat .dif .divx .dmb .dpg .dv .dvr-ms .evo .eye .flc .fli .flx .gvi .ivf
.m1v .m21 .m2t .m2ts .m2v .m2p .m4e .mjp .mjpeg .mod .moov .movie .mp21
.mpe .mpeg .mpv .mpv2 .mqv .mts .mvb .nsv .nuv .nut .ogm .qt .rm .rmvb
.rts .scm .smv .ssm .svi .vdo .vfw .vid .viv .vivo .vp6 .vp7 .vro .webm
.wm .wmd .wtv .yuv""".split()

LIKELYTS = """.ts .tp .trp .3g2 .3gp .3gp2 .3gpp .m2t .m2ts .mts .mp4
.m4v .flv .mkv .mov .wtv .dvr-ms .webm""".split()

use_extensions = True
try:
    assert get_bin("ffmpeg")
except:
    use_extensions = False


def uniso(iso: str) -> time.struct_time:
    return time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")


def isodt(iso: str) -> datetime:
    return datetime(*uniso(iso)[:6])


def isogm(iso: str) -> int:
    return int(calendar.timegm(uniso(iso)))


def pad(length: int, align: int) -> int:
    extra = length % align
    if extra:
        extra = align - extra
    return extra


# TODO 20191125: can probably be replaced with NamedTuple
# dict, but with specific defaults for certain keys
class VideoDetails(MutableMapping):
    def __init__(self, d: Optional[Dict[str, Any]] = None):
        self.d: Dict[str, Any]
        if d is not None:
            self.d = d
        else:
            self.d = {}

    def __getitem__(self, key: str) -> Any:
        if key not in self.d:
            self.d[key] = self.default(key)
        return self.d[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.d[key] = value

    def __delitem__(self, key: str) -> None:
        del self.d[key]

    def __iter__(self) -> Iterator[Any]:
        return self.d.__iter__()

    def __len__(self) -> int:
        return len(self.d)

    def default(self, key: str) -> Any:
        defaults = {
            "showingBits": "0",
            "displayMajorNumber": "0",
            "displayMinorNumber": "0",
            "isEpisode": "true",
            "colorCode": "4",
            "showType": ("SERIES", "5"),
        }
        if key in defaults:
            return defaults[key]
        elif key.startswith("v"):
            return []
        else:
            return ""


class Video(Plugin):
    CONTENT_TYPE = "x-container/tivo-videos"

    def video_file_filter(self, full_path: str, type: Optional[str] = None) -> bool:
        if os.path.isdir(full_path):
            return True
        if use_extensions:
            return os.path.splitext(full_path)[1].lower() in EXTENSIONS
        else:
            return supported_format(full_path)

    def send_file(self, handler: "TivoHTTPHandler", path: str, query: Query) -> None:
        mime = "video/x-tivo-mpeg"
        tsn = handler.headers.get("tsn", "")
        try:
            assert tsn
            tivo_name = pytivo.config.TIVOS[tsn].get("name", tsn)
        except:
            tivo_name = handler.address_string()

        is_tivo_file = path[-5:].lower() == ".tivo"

        if "Format" in query:
            mime = query["Format"][0]

        needs_tivodecode = is_tivo_file and mime == "video/mpeg"
        compatible = not needs_tivodecode and tivo_compatible(path, tsn, mime)[0]

        try:  # "bytes=XXX-"
            offset = int(handler.headers.get("Range")[6:-1])
        except:
            offset = 0

        if needs_tivodecode:
            valid = bool(get_bin("tivodecode") and get_server("tivo_mak", ""))
        else:
            valid = True

        if valid and offset:
            valid = (compatible and offset < os.path.getsize(path)) or (
                not compatible and is_resumable(path, offset)
            )

        # faking = (mime in ['video/x-tivo-mpeg-ts', 'video/x-tivo-mpeg'] and
        faking = mime == "video/x-tivo-mpeg" and not (is_tivo_file and compatible)
        thead = b""
        if faking:
            thead = self.tivo_header(tsn, path, mime)
        if compatible:
            size = os.path.getsize(path) + len(thead)
            handler.send_response(200)
            handler.send_header("Content-Length", str(size - offset))
            handler.send_header(
                "Content-Range", "bytes %d-%d/%d" % (offset, size - offset - 1, size)
            )
        else:
            handler.send_response(206)
            handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Content-Type", mime)
        handler.end_headers()

        LOGGER.info(
            '[%s] Start sending "%s" to %s'
            % (time.strftime("%d/%b/%Y %H:%M:%S"), path, tivo_name)
        )
        start = time.time()
        count = 0

        if valid:
            if compatible:
                if faking and not offset:
                    handler.wfile.write(thead)
                LOGGER.debug('"%s" is tivo compatible' % path)
                f = open(path, "rb")
                try:
                    if offset:
                        offset -= len(thead)
                        f.seek(offset)
                    while True:
                        block = f.read(512 * 1024)
                        if not block:
                            break
                        handler.wfile.write(block)
                        count += len(block)
                except Exception as msg:
                    LOGGER.info(msg)
                f.close()
            else:
                LOGGER.debug('"%s" is not tivo compatible' % path)
                if offset:
                    count = resume_transfer(path, handler.wfile, offset)
                else:
                    count = transcode(path, handler.wfile, tsn, mime, thead)
        try:
            if not compatible:
                handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except Exception as msg:
            LOGGER.info(msg)

        mega_elapsed = (time.time() - start) * 1024 * 1024
        if mega_elapsed < 1:
            mega_elapsed = 1
        rate = count * 8.0 / mega_elapsed
        LOGGER.info(
            '[%s] Done sending "%s" to %s, %d bytes, %.2f Mb/s'
            % (time.strftime("%d/%b/%Y %H:%M:%S"), path, tivo_name, count, rate)
        )

    def __duration(self, full_path: str) -> Optional[float]:
        return video_info(full_path).millisecs

    def __total_items(self, full_path: str) -> int:
        count = 0
        try:
            for f in os.listdir(full_path):
                if f.startswith("."):
                    continue
                f = os.path.join(full_path, f)
                if os.path.isdir(f):
                    count += 1
                elif use_extensions:
                    if os.path.splitext(f)[1].lower() in EXTENSIONS:
                        count += 1
                elif f in pytivo.metadata.INFO_CACHE:
                    if supported_format(f):
                        count += 1
        except:
            pass
        return count

    def __est_size(self, full_path: str, tsn: str = "", mime: str = "") -> int:
        # Size is estimated by taking audio and video bit rate adding 2%

        if tivo_compatible(full_path, tsn, mime)[0]:
            return os.path.getsize(full_path)
        else:
            # Must be re-encoded
            audioBPS = getMaxAudioBR(tsn) * 1000
            # audioBPS = config.strtod(config.getAudioBR(tsn))
            videoBPS = select_videostr(full_path, tsn)
            bitrate = audioBPS + videoBPS
            duration = self.__duration(full_path)
            if duration is None:
                LOGGER.error("self.__duration(%s) is None", full_path)
                duration = 0
            return int((duration / 1000) * (bitrate * 1.02 / 8))

    def metadata_full(
        self,
        full_path: str,
        tsn: str = "",
        mime: str = "",
        mtime: Optional[float] = None,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        vInfo = video_info(full_path)

        if vInfo.vHeight is None or vInfo.vWidth is None:
            LOGGER.error("vInfo.vHeight or vInfo.vWidth is None")
            return data

        if (vInfo.vHeight >= 720 and getTivoHeight(tsn) >= 720) or (
            vInfo.vWidth >= 1280 and getTivoWidth(tsn) >= 1280
        ):
            data["showingBits"] = "4096"

        data.update(basic(full_path, mtime))
        if full_path[-5:].lower() == ".tivo":
            data.update(from_tivo(full_path))
        if full_path[-4:].lower() == ".wtv":
            data.update(from_mscore(vInfo.rawmeta))

        if "episodeNumber" in data:
            try:
                ep = int(data["episodeNumber"])
            except:
                ep = 0
            data["episodeNumber"] = str(ep)

        if getDebug() and "vHost" not in data:
            compatible, reason = tivo_compatible(full_path, tsn, mime)
            if compatible:
                transcode_options: List[str] = []
            else:
                transcode_options = transcode_settings(True, full_path, tsn, mime)
            data["vHost"] = (
                ["TRANSCODE=%s, %s" % (["YES", "NO"][compatible], reason)]
                + ["SOURCE INFO: "]
                + [
                    "%s=%s" % (k, v)
                    for k, v in sorted(list(vInfo._asdict().items()), reverse=True)
                ]
                + ["TRANSCODE OPTIONS: "]
                + transcode_options
                + ["SOURCE FILE: ", os.path.basename(full_path)]
            )

        now = datetime.utcnow()
        if "time" in data:
            if data["time"].lower() == "file":
                if not mtime:
                    mtime = os.path.getmtime(full_path)
                try:
                    now = datetime.utcfromtimestamp(mtime)
                except:
                    LOGGER.warning("Bad file time on " + full_path)
            elif data["time"].lower() == "oad":
                now = isodt(data["originalAirDate"])
            else:
                try:
                    now = isodt(data["time"])
                except:
                    LOGGER.warning(
                        "Bad time format: " + data["time"] + " , using current time"
                    )

        duration = self.__duration(full_path)
        if duration is None:
            LOGGER.error("duration is None")
            return data

        duration_delta = timedelta(milliseconds=duration)
        min = duration_delta.seconds / 60
        sec = duration_delta.seconds % 60
        hours = min / 60
        min = min % 60

        data.update(
            {
                "time": now.isoformat(),
                "startTime": now.isoformat(),
                "stopTime": (now + duration_delta).isoformat(),
                "size": self.__est_size(full_path, tsn, mime),
                "duration": duration,
                "iso_duration": (
                    "P%sDT%sH%sM%sS" % (duration_delta.days, hours, min, sec)
                ),
            }
        )

        return data

    def QueryContainer(self, handler: "TivoHTTPHandler", query: Query) -> None:
        tsn = handler.headers.get("tsn", "")
        subcname = query["Container"][0]
        # e.g. Filter =
        #   "x-tivo-container/tivo-videos,x-tivo-container/folder,video/x-tivo-mpeg,video/*"

        if not self.get_local_path(handler, query):
            handler.send_error(404)
            return

        container = handler.container
        force_alpha = container.getboolean("force_alpha")
        ar = container.get("allow_recurse", "auto").lower()
        if ar == "auto":
            allow_recurse = not tsn or tsn[0] < "7"
        else:
            allow_recurse = ar in ("1", "yes", "true", "on")

        files, total, start = self.get_files(
            handler, query, self.video_file_filter, force_alpha, allow_recurse
        )

        videos = []
        local_base_path = self.get_local_base_path(handler, query)
        start_time = time.time()
        # This loop takes the most amount of time in the first query
        for f in files:
            video = VideoDetails()
            mtime = f.mdate
            try:
                ltime = time.localtime(mtime)
            except:
                LOGGER.warning("Bad file time on " + str(f.name, "utf-8"))
                mtime = time.time()
                ltime = time.localtime(mtime)
            video["captureDate"] = hex(int(mtime))
            video["textDate"] = time.strftime("%b %d, %Y", ltime)
            video["name"] = os.path.basename(f.name)
            video["path"] = f.name
            video["part_path"] = f.name.replace(local_base_path, "", 1)
            if not video["part_path"].startswith(os.path.sep):
                video["part_path"] = os.path.sep + video["part_path"]
            video["title"] = os.path.basename(f.name)
            video["is_dir"] = f.isdir
            if video["is_dir"]:
                video["small_path"] = subcname + "/" + video["name"]
                video["total_items"] = self.__total_items(f.name)
            else:
                if len(files) == 1 or f.name in pytivo.metadata.INFO_CACHE:
                    video["valid"] = supported_format(f.name)
                    if video["valid"]:
                        video.update(self.metadata_full(f.name, tsn, mtime=mtime))
                        if len(files) == 1:
                            video["captureDate"] = hex(isogm(video["time"]))
                else:
                    video["valid"] = True
                    video.update(basic(f.name, mtime))

                if self.use_ts(tsn, f.name):
                    video["mime"] = "video/x-tivo-mpeg-ts"
                else:
                    video["mime"] = "video/x-tivo-mpeg"

                video["textSize"] = human_size(f.size)

            videos.append(video)
        end_time_ms = (time.time() - start_time) * 1000
        LOGGER.info(f"for f in files: {end_time_ms:.1f}ms")

        def crc_str(in_str: str) -> int:
            return zlib.crc32(in_str.encode("utf-8"))

        t = VIDEO_CONTAINER_XML_TCLASS()
        t.container = handler.cname
        t.name = subcname
        t.total = total
        t.start = start
        t.videos = videos
        t.quote = quote
        t.escape = escape
        t.crc = crc_str  # applied to (guid + name) and (guid + video.name)
        t.guid = getGUID()
        t.tivos = pytivo.config.TIVOS
        handler.send_xml(str(t))

    def use_ts(self, tsn: str, file_path: str) -> bool:
        if is_ts_capable(tsn):
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".tivo":
                try:
                    with open(file_path, "rb") as flag_fh:
                        flag = flag_fh.read(8)
                except:
                    return False
                if flag[7] & 0x20:
                    return True
            else:
                opt = get_ts_flag()
                if (opt == "auto" and ext in LIKELYTS) or (
                    opt in ["true", "yes", "on"]
                ):
                    return True

        return False

    @lru_cache(maxsize=1)
    def get_details_xml(self, tsn: str, file_path: str) -> str:
        details: str
        file_info = VideoDetails()
        file_info["valid"] = supported_format(file_path)
        if file_info["valid"]:
            file_info.update(self.metadata_full(file_path, tsn))

        t = TVBUS_TCLASS()
        t.video = file_info
        t.escape = escape
        t.get_tv = get_tv
        t.get_mpaa = get_mpaa
        t.get_stars = get_stars
        t.get_color = get_color
        details = str(t)
        return details

    def tivo_header(self, tsn: str, path: str, mime: str) -> bytes:
        if mime == "video/x-tivo-mpeg-ts":
            flag = 45
        else:
            flag = 13
        details = self.get_details_xml(tsn, path).encode("utf-8")
        ld = len(details)
        chunk = details + b"\0" * (pad(ld, 4) + 4)
        lc = len(chunk)
        blocklen = lc * 2 + 40
        padding = pad(blocklen, 1024)

        return b"".join(
            [
                b"TiVo",
                struct.pack(">HHHLH", 4, flag, 0, padding + blocklen, 2),
                struct.pack(">LLHH", lc + 12, ld, 1, 0),
                chunk,
                struct.pack(">LLHH", lc + 12, ld, 2, 0),
                chunk,
                b"\0" * padding,
            ]
        )

    def TVBusQuery(self, handler: "TivoHTTPHandler", query: Query) -> None:
        tsn = handler.headers.get("tsn", "")
        f = query["File"][0]
        path = self.get_local_path(handler, query)
        file_path = os.path.normpath(path + "/" + f)

        details = self.get_details_xml(tsn, file_path)

        handler.send_xml(details)
