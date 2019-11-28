from datetime import datetime
from functools import lru_cache
import hashlib
import logging
import logging
import os
import os
import re
import shlex
import struct
import subprocess
import subprocess
import sys
import tempfile
import time
from typing import Dict, Any, Optional, NamedTuple, List, Tuple, TextIO
from xml.dom import minidom  # type: ignore
from xml.parsers import expat

try:
    import plistlib
except:
    pass

import mutagen  # type: ignore

from pytivo.config import get_bin, getFFmpegWait, get_server, init
from pytivo.lrucache import LRUCache
from pytivo.turing import Turing

INFO_CACHE = LRUCache(1000)
LOGGER = logging.getLogger("pyTivo.metadata")


# Something to strip
TRIBUNE_CR = " Copyright Tribune Media Services, Inc."
ROVI_CR = " Copyright Rovi, Inc."

TV_RATINGS = {
    "TV-Y7": 1,
    "TV-Y": 2,
    "TV-G": 3,
    "TV-PG": 4,
    "TV-14": 5,
    "TV-MA": 6,
    "TV-NR": 7,
    "TVY7": 1,
    "TVY": 2,
    "TVG": 3,
    "TVPG": 4,
    "TV14": 5,
    "TVMA": 6,
    "TVNR": 7,
    "Y7": 1,
    "Y": 2,
    "G": 3,
    "PG": 4,
    "14": 5,
    "MA": 6,
    "NR": 7,
    "UNRATED": 7,
    "X1": 1,
    "X2": 2,
    "X3": 3,
    "X4": 4,
    "X5": 5,
    "X6": 6,
    "X7": 7,
}

MPAA_RATINGS = {
    "G": 1,
    "PG": 2,
    "PG-13": 3,
    "PG13": 3,
    "R": 4,
    "X": 5,
    "NC-17": 6,
    "NC17": 6,
    "NR": 8,
    "UNRATED": 8,
    "G1": 1,
    "P2": 2,
    "P3": 3,
    "R4": 4,
    "X5": 5,
    "N6": 6,
    "N8": 8,
}

STAR_RATINGS = {
    "1": 1,
    "1.5": 2,
    "2": 3,
    "2.5": 4,
    "3": 5,
    "3.5": 6,
    "4": 7,
    "*": 1,
    "**": 3,
    "***": 5,
    "****": 7,
    "X1": 1,
    "X2": 2,
    "X3": 3,
    "X4": 4,
    "X5": 5,
    "X6": 6,
    "X7": 7,
}

HUMAN = {
    "mpaaRating": {1: "G", 2: "PG", 3: "PG-13", 4: "R", 5: "X", 6: "NC-17", 8: "NR"},
    "tvRating": {1: "Y7", 2: "Y", 3: "G", 4: "PG", 5: "14", 6: "MA", 7: "NR"},
    "starRating": {1: "1", 2: "1.5", 3: "2", 4: "2.5", 5: "3", 6: "3.5", 7: "4"},
    "colorCode": {1: "B & W", 2: "COLOR AND B & W", 3: "COLORIZED", 4: "COLOR"},
}

BOM = "\xef\xbb\xbf"

GB = 1024 ** 3
MB = 1024 ** 2
KB = 1024

mswindows = sys.platform == "win32"


class VideoInfo(NamedTuple):
    Supported: bool  # is this tivo-supported
    aCh: Optional[int] = None  # number of audio channels
    aCodec: Optional[str] = None  # audio codec
    aFreq: Optional[str] = None  # audio sample rate, number in string?
    aKbps: Optional[int] = None  # but always cast to int
    container: Optional[str] = None  # av container file format
    dar1: Optional[str] = None  # Desired? Aspect Ratio
    kbps: Optional[int] = None  # but always used as int
    mapAudio: Optional[List[Tuple[str, str]]] = None
    mapVideo: Optional[str] = None
    millisecs: Optional[float] = None  # duration? ffmpeg Override_millisecs
    par: Optional[str] = None  # string version of float? "1.232"?
    par1: Optional[str] = None  # string version e.g. "4:3"
    par2: Optional[float] = None  # float version of ratio
    rawmeta: Optional[Dict[str, str]] = None
    vCodec: Optional[str] = None
    vFps: Optional[str] = None  # string of float (maybe could be float)
    vHeight: Optional[int] = None  # video file height
    vWidth: Optional[int] = None  # video file height


def get_mpaa(rating: int) -> str:
    return HUMAN["mpaaRating"].get(rating, "NR")


def get_tv(rating: int) -> str:
    return HUMAN["tvRating"].get(rating, "NR")


def get_stars(rating: int) -> str:
    return HUMAN["starRating"].get(rating, "")


def get_color(value: int) -> str:
    return HUMAN["colorCode"].get(value, "COLOR")


def human_size(raw: Any) -> str:
    raw = float(raw)
    if raw > GB:
        tsize = "%.2f GB" % (raw / GB)
    elif raw > MB:
        tsize = "%.2f MB" % (raw / MB)
    elif raw > KB:
        tsize = "%.2f KB" % (raw / KB)
    else:
        tsize = "%d Bytes" % raw
    return tsize


def tag_data(element: minidom.Node, tag: str) -> str:
    for name in tag.split("/"):
        found = False
        for new_element in element.childNodes:
            if new_element.nodeName == name:
                found = True
                element = new_element
                break
        if not found:
            return ""
    if not element.firstChild:
        return ""
    return element.firstChild.data


def _vtag_data(element: minidom.Node, tag: str) -> List[str]:
    for name in tag.split("/"):
        new_element = element.getElementsByTagName(name)
        if not new_element:
            return []
        element = new_element[0]
    elements = element.getElementsByTagName("element")
    return [x.firstChild.data for x in elements if x.firstChild]


def _vtag_data_alternate(element: minidom.Node, tag: str) -> List[str]:
    elements = [element]
    for name in tag.split("/"):
        new_elements: List[str] = []
        for elmt in elements:
            new_elements += elmt.getElementsByTagName(name)
        elements = new_elements
    return [x.firstChild.data for x in elements if x.firstChild]


def _tag_value(element: minidom.Node, tag: str) -> Optional[int]:
    item = element.getElementsByTagName(tag)
    if item:
        value = item[0].attributes["value"].value
        return int(value[0])
    return None


@lru_cache(maxsize=64)
def from_moov(full_path: str) -> Dict[str, Any]:
    metadata = {}
    len_desc = 0

    try:
        mp4meta = mutagen.File(full_path)
        assert mp4meta
    except:
        return {}

    # The following 1-to-1 correspondence of atoms to pyTivo
    # variables is TV-biased
    keys = {"tvnn": "callsign", "tvsh": "seriesTitle"}
    isTVShow = False
    if "stik" in mp4meta:
        isTVShow = mp4meta["stik"] == mutagen.mp4.MediaKind.TV_SHOW
    else:
        isTVShow = "tvsh" in mp4meta
    for key, value in list(mp4meta.items()):
        if type(value) == list:
            value = value[0]
        if key in keys:
            metadata[keys[key]] = value
        elif key == "tven":
            # could be programId (EP, SH, or MV) or "SnEn"
            if value.startswith("SH"):
                metadata["isEpisode"] = "false"
            elif value.startswith("MV") or value.startswith("EP"):
                metadata["isEpisode"] = "true"
                metadata["programId"] = value
            elif key.startswith("S") and key.count("E") == 1:
                epstart = key.find("E")
                seasonstr = key[1:epstart]
                episodestr = key[epstart + 1 :]
                if seasonstr.isdigit() and episodestr.isdigit():
                    if len(episodestr) < 2:
                        episodestr = "0" + episodestr
                    metadata["episodeNumber"] = seasonstr + episodestr
        elif key == "tvsn":
            # put together tvsn and tves to make episodeNumber
            tvsn = str(value)
            tves = "00"
            if "tves" in mp4meta:
                tvesValue = mp4meta["tves"]
                if type(tvesValue) == list:
                    tvesValue = tvesValue[0]
                tves = str(tvesValue)
                if len(tves) < 2:
                    tves = "0" + tves
            metadata["episodeNumber"] = tvsn + tves
        # These keys begin with the copyright symbol \xA9
        elif key == "\xa9day":
            if isTVShow:
                if len(value) == 4:
                    value += "-01-01T16:00:00Z"
                metadata["originalAirDate"] = value
            else:
                if len(value) >= 4:
                    metadata["movieYear"] = value[:4]
            # metadata['time'] = value
        elif key in ["\xa9gen", "gnre"]:
            for k in ("vProgramGenre", "vSeriesGenre"):
                if k in metadata:
                    metadata[k].append(value)
                else:
                    metadata[k] = [value]
        elif key == "\xa9nam":
            if isTVShow:
                metadata["episodeTitle"] = value
            else:
                metadata["title"] = value

        # Description in desc, cmt, and/or ldes tags. Keep the longest.
        elif key in ["desc", "\xa9cmt", "ldes"] and len(value) > len_desc:
            metadata["description"] = value
            len_desc = len(value)

        # A common custom "reverse DNS format" tag
        elif key == "----:com.apple.iTunes:iTunEXTC" and (
            "us-tv" in value or "mpaa" in value
        ):
            rating = value.split("|")[1].upper()
            if rating in TV_RATINGS and "us-tv" in value:
                metadata["tvRating"] = TV_RATINGS[rating]
            elif rating in MPAA_RATINGS and "mpaa" in value:
                metadata["mpaaRating"] = MPAA_RATINGS[rating]

        # Actors, directors, producers, AND screenwriters may be in a long
        # embedded XML plist.
        elif key == "----:com.apple.iTunes:iTunMOVI" and "plistlib" in sys.modules:
            items = {
                "cast": "vActor",
                "directors": "vDirector",
                "producers": "vProducer",
                "screenwriters": "vWriter",
            }
            try:
                data: Dict[str, Any] = plistlib.loads(value)
            except:
                pass
            else:
                for item in items:
                    if item in data:
                        metadata[items[item]] = [x["name"] for x in data[item]]
        elif key == "----:com.pyTivo.pyTivo:tiVoINFO" and "plistlib" in sys.modules:
            try:
                data = plistlib.loads(value)
            except:
                pass
            else:
                for item in data:
                    metadata[item] = data[item]

    return metadata


def from_mscore(rawmeta: mutagen.FileType) -> Dict[str, Any]:
    metadata = {}
    keys = {
        "title": ["Title"],
        "description": ["Description", "WM/SubTitleDescription"],
        "episodeTitle": ["WM/SubTitle"],
        "callsign": ["WM/MediaStationCallSign"],
        "displayMajorNumber": ["WM/MediaOriginalChannel"],
        "originalAirDate": ["WM/MediaOriginalBroadcastDateTime"],
        "rating": ["WM/ParentalRating"],
        "credits": ["WM/MediaCredits"],
        "genre": ["WM/Genre"],
    }

    for tagname in keys:
        for tag in keys[tagname]:
            try:
                if tag in rawmeta:
                    value = rawmeta[tag][0]
                    if type(value) not in (str, str):
                        value = str(value)
                    if value:
                        metadata[tagname] = value
            except:
                pass

    if "episodeTitle" in metadata and "title" in metadata:
        metadata["seriesTitle"] = metadata["title"]
    if "genre" in metadata:
        value = metadata["genre"].split(",")
        metadata["vProgramGenre"] = value
        metadata["vSeriesGenre"] = value
        del metadata["genre"]
    if "credits" in metadata:
        value = [x.split("/") for x in metadata["credits"].split(";")]
        if len(value) > 3:
            metadata["vActor"] = [x for x in (value[0] + value[3]) if x]
            metadata["vDirector"] = [x for x in value[1] if x]
        del metadata["credits"]
    if "rating" in metadata:
        rating = metadata["rating"]
        if rating in TV_RATINGS:
            metadata["tvRating"] = TV_RATINGS[rating]
        del metadata["rating"]

    return metadata


@lru_cache(maxsize=64)
def from_dvrms(full_path: str) -> Dict[str, Any]:
    try:
        rawmeta = mutagen.File(full_path)
        assert rawmeta
    except:
        return {}

    metadata = from_mscore(rawmeta)
    return metadata


def from_eyetv(full_path: str) -> Dict[str, Any]:
    keys = {
        "TITLE": "title",
        "SUBTITLE": "episodeTitle",
        "DESCRIPTION": "description",
        "YEAR": "movieYear",
        "EPISODENUM": "episodeNumber",
    }
    metadata: Dict[str, Any] = {}
    path = os.path.dirname(full_path)
    eyetvp = [x for x in os.listdir(path) if x.endswith(".eyetvp")][0]
    eyetvp = os.path.join(path, eyetvp)
    try:
        eyetv = plistlib.readPlist(eyetvp)
    except:
        return metadata
    if "epg info" in eyetv:
        info = eyetv["epg info"]
        for key in keys:
            if info[key]:
                metadata[keys[key]] = info[key]
        if info["SUBTITLE"]:
            metadata["seriesTitle"] = info["TITLE"]
        if info["ACTORS"]:
            metadata["vActor"] = [x.strip() for x in info["ACTORS"].split(",")]
        if info["DIRECTOR"]:
            metadata["vDirector"] = [info["DIRECTOR"]]

        for ptag, etag, ratings in [
            ("tvRating", "TV_RATING", TV_RATINGS),
            ("mpaaRating", "MPAA_RATING", MPAA_RATINGS),
            ("starRating", "STAR_RATING", STAR_RATINGS),
        ]:
            x = info[etag].upper()
            if x and x in ratings:
                metadata[ptag] = ratings[x]

        # movieYear must be set for the mpaa/star ratings to work
        if (
            "mpaaRating" in metadata or "starRating" in metadata
        ) and "movieYear" not in metadata:
            metadata["movieYear"] = eyetv["info"]["start"].year
    return metadata


def from_text(full_path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    path, name = os.path.split(full_path)
    title, ext = os.path.splitext(name)

    search_paths = []
    ptmp = full_path
    while ptmp:
        parent = os.path.dirname(ptmp)
        if ptmp != parent:
            ptmp = parent
        else:
            break
        search_paths.append(os.path.join(ptmp, "default.txt"))

    search_paths.append(os.path.join(path, title) + ".properties")
    search_paths.reverse()

    search_paths += [
        full_path + ".txt",
        os.path.join(path, ".meta", "default.txt"),
        os.path.join(path, ".meta", name) + ".txt",
    ]

    for metafile in search_paths:
        if os.path.exists(metafile):
            sep = ":="[metafile.endswith(".properties")]
            with open(metafile, "r") as metafile_fh:
                for line in metafile_fh:
                    if line.startswith(BOM):
                        line = line[3:]
                    if line.strip().startswith("#") or not sep in line:
                        continue
                    key, value = [x.strip() for x in line.split(sep, 1)]
                    if not key or not value:
                        continue
                    if key.startswith("v"):
                        if key in metadata:
                            metadata[key].append(value)
                        else:
                            metadata[key] = [value]
                    else:
                        metadata[key] = value

    for rating, ratings in [
        ("tvRating", TV_RATINGS),
        ("mpaaRating", MPAA_RATINGS),
        ("starRating", STAR_RATINGS),
    ]:
        x = metadata.get(rating, "").upper()
        if x in ratings:
            metadata[rating] = ratings[x]
        else:
            try:
                x = int(x)
                metadata[rating] = x
            except:
                pass

    return metadata


def basic(full_path: str, mtime: Optional[float] = None) -> Dict[str, Any]:
    base_path, name = os.path.split(full_path)
    title, ext = os.path.splitext(name)
    if not mtime:
        mtime = os.path.getmtime(full_path)
    try:
        originalAirDate = datetime.utcfromtimestamp(mtime)
    except:
        originalAirDate = datetime.utcnow()

    metadata = {"title": title, "originalAirDate": originalAirDate.isoformat()}
    ext = ext.lower()
    if ext in [".mp4", ".m4v", ".mov"]:
        metadata.update(from_moov(full_path))
    elif ext in [".dvr-ms", ".asf", ".wmv"]:
        metadata.update(from_dvrms(full_path))
    elif "plistlib" in sys.modules and base_path.endswith(".eyetv"):
        metadata.update(from_eyetv(full_path))
    metadata.update(from_nfo(full_path))
    metadata.update(from_text(full_path))

    return metadata


def from_container(xmldoc: minidom.Document) -> Dict[str, Any]:
    metadata = {}

    keys = {
        "title": "Title",
        "episodeTitle": "EpisodeTitle",
        "description": "Description",
        "programId": "ProgramId",
        "seriesId": "SeriesId",
        "episodeNumber": "EpisodeNumber",
        "tvRating": "TvRating",
        "displayMajorNumber": "SourceChannel",
        "callsign": "SourceStation",
        "showingBits": "ShowingBits",
        "mpaaRating": "MpaaRating",
    }

    details = xmldoc.getElementsByTagName("Details")[0]

    for key in keys:
        data: Any = tag_data(details, keys[key])
        if data:
            if key == "description":
                data = data.replace(TRIBUNE_CR, "").replace(ROVI_CR, "")
                if data.endswith(" *"):
                    data = data[:-2]
            elif key == "tvRating":
                data = int(data)
            elif key == "displayMajorNumber":
                if "-" in data:
                    data, metadata["displayMinorNumber"] = data.split("-")
            metadata[key] = data

    return metadata


def from_details(xml: str) -> Dict[str, Any]:
    metadata = {}
    data: Any

    xmldoc = minidom.parseString(xml)
    showing = xmldoc.getElementsByTagName("showing")[0]
    program = showing.getElementsByTagName("program")[0]

    items = {
        "description": "program/description",
        "title": "program/title",
        "episodeTitle": "program/episodeTitle",
        "episodeNumber": "program/episodeNumber",
        "programId": "program/uniqueId",
        "seriesId": "program/series/uniqueId",
        "seriesTitle": "program/series/seriesTitle",
        "originalAirDate": "program/originalAirDate",
        "isEpisode": "program/isEpisode",
        "movieYear": "program/movieYear",
        "partCount": "partCount",
        "partIndex": "partIndex",
        "time": "time",
    }

    for item in items:
        data = tag_data(showing, items[item])
        if data:
            if item == "description":
                data = data.replace(TRIBUNE_CR, "").replace(ROVI_CR, "")
                if data.endswith(" *"):
                    data = data[:-2]
            metadata[item] = data

    vItems = [
        "vActor",
        "vChoreographer",
        "vDirector",
        "vExecProducer",
        "vProgramGenre",
        "vGuestStar",
        "vHost",
        "vProducer",
        "vWriter",
    ]

    for item in vItems:
        data = _vtag_data(program, item)
        if data:
            metadata[item] = data

    sb = showing.getElementsByTagName("showingBits")
    if sb:
        metadata["showingBits"] = sb[0].attributes["value"].value

    # for tag in ['starRating', 'mpaaRating', 'colorCode']:
    for tag in ["starRating", "mpaaRating"]:
        value = _tag_value(program, tag)
        if value:
            metadata[tag] = value

    rating = _tag_value(showing, "tvRating")
    if rating:
        metadata["tvRating"] = rating

    return metadata


def _nfo_vitems(source: List[str], metadata: Dict[str, Any]) -> Dict[str, Any]:

    vItems = {
        "vGenre": "genre",
        "vWriter": "credits",
        "vDirector": "director",
        "vActor": "actor/name",
    }

    for key in vItems:
        data = _vtag_data_alternate(source, vItems[key])
        if data:
            metadata.setdefault(key, [])
            for dat in data:
                if not dat in metadata[key]:
                    metadata[key].append(dat)

    if "vGenre" in metadata:
        metadata["vSeriesGenre"] = metadata["vProgramGenre"] = metadata["vGenre"]

    return metadata


def _parse_nfo(nfo_path: str, nfo_data: Optional[List[str]] = None) -> minidom.Document:
    # nfo files can contain XML or a URL to seed the XBMC metadata scrapers
    # It's also possible to have both (a URL after the XML metadata)
    # pyTivo only parses the XML metadata, but we'll try to stip the URL
    # from mixed XML/URL files.  Returns `None` when XML can't be parsed.
    if nfo_data is None:
        with open(nfo_path, "r") as nfo_fh:
            nfo_data = [line.strip() for line in nfo_fh]
    xmldoc = None
    try:
        xmldoc = minidom.parseString(os.linesep.join(nfo_data))
    except expat.ExpatError as err:
        if expat.ErrorString(err.code) == expat.errors.XML_ERROR_INVALID_TOKEN:
            # might be a URL outside the xml
            while len(nfo_data) > err.lineno:
                if len(nfo_data[-1]) == 0:
                    nfo_data.pop()
                else:
                    break
            if len(nfo_data) == err.lineno:
                # last non-blank line contains the error
                nfo_data.pop()
                return _parse_nfo(nfo_path, nfo_data)
    return xmldoc


@lru_cache(maxsize=64)
def _from_tvshow_nfo(tvshow_nfo_path: str) -> Dict[str, Any]:
    items = {
        "description": "plot",
        "title": "title",
        "seriesTitle": "showtitle",
        "starRating": "rating",
        "tvRating": "mpaa",
    }

    metadata: Dict[str, Any] = {}

    xmldoc = _parse_nfo(tvshow_nfo_path)
    if not xmldoc:
        return metadata

    tvshow = xmldoc.getElementsByTagName("tvshow")
    if tvshow:
        tvshow = tvshow[0]
    else:
        return metadata

    for item in items:
        data = tag_data(tvshow, items[item])
        if data:
            metadata[item] = data

    metadata = _nfo_vitems(tvshow, metadata)

    return metadata


def _from_episode_nfo(nfo_path: str, xmldoc: minidom.Document) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    items = {
        "description": "plot",
        "episodeTitle": "title",
        "seriesTitle": "showtitle",
        "originalAirDate": "aired",
        "starRating": "rating",
        "tvRating": "mpaa",
    }

    # find tvshow.nfo
    path = nfo_path
    while True:
        basepath = os.path.dirname(path)
        if path == basepath:
            break
        path = basepath
        tv_nfo = os.path.join(path, "tvshow.nfo")
        if os.path.exists(tv_nfo):
            metadata.update(_from_tvshow_nfo(tv_nfo))
            break

    episode = xmldoc.getElementsByTagName("episodedetails")
    if episode:
        episode = episode[0]
    else:
        return metadata

    metadata["isEpisode"] = "true"
    for item in items:
        data = tag_data(episode, items[item])
        if data:
            metadata[item] = data

    season = tag_data(episode, "displayseason")
    if not season or season == "-1":
        season = tag_data(episode, "season")
    if not season:
        season = "1"

    ep_num = tag_data(episode, "displayepisode")
    if not ep_num or ep_num == "-1":
        ep_num = tag_data(episode, "episode")
    if ep_num and ep_num != "-1":
        metadata["episodeNumber"] = "%d%02d" % (int(season), int(ep_num))

    if "originalAirDate" in metadata:
        metadata["originalAirDate"] += "T00:00:00Z"

    metadata = _nfo_vitems(episode, metadata)

    return metadata


def _from_movie_nfo(xmldoc: minidom.Document) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    movie = xmldoc.getElementsByTagName("movie")
    if movie:
        movie = movie[0]
    else:
        return metadata

    items = {
        "description": "plot",
        "title": "title",
        "movieYear": "year",
        "starRating": "rating",
        "mpaaRating": "mpaa",
    }

    metadata["isEpisode"] = "false"

    for item in items:
        data = tag_data(movie, items[item])
        if data:
            metadata[item] = data

    metadata["movieYear"] = "%04d" % int(metadata.get("movieYear", 0))

    metadata = _nfo_vitems(movie, metadata)
    return metadata


@lru_cache(maxsize=64)
def from_nfo(full_path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}

    nfo_path = "%s.nfo" % os.path.splitext(full_path)[0]
    if not os.path.exists(nfo_path):
        return metadata

    xmldoc = _parse_nfo(nfo_path)
    if not xmldoc:
        return metadata

    if xmldoc.getElementsByTagName("episodedetails"):
        # it's an episode
        metadata.update(_from_episode_nfo(nfo_path, xmldoc))
    elif xmldoc.getElementsByTagName("movie"):
        # it's a movie
        metadata.update(_from_movie_nfo(xmldoc))

    rating: Optional[int]

    # common nfo cleanup
    if "starRating" in metadata:
        # .NFO 0-10 -> TiVo 1-7
        rating = int(float(metadata["starRating"]) * 6 / 10 + 1.5)
        metadata["starRating"] = rating

    for key, mapping in [("mpaaRating", MPAA_RATINGS), ("tvRating", TV_RATINGS)]:
        if key in metadata:
            rating = mapping.get(metadata[key], None)
            if rating:
                metadata[key] = rating
            else:
                del metadata[key]

    return metadata


def _tdcat_bin(tdcat_path: str, full_path: str, tivo_mak: str) -> str:
    tcmd = [tdcat_path, "-m", tivo_mak, "-2", full_path]
    tdcat = subprocess.run(tcmd, stdout=subprocess.PIPE, universal_newlines=True)
    return tdcat.stdout


def _tdcat_py(full_path: str, tivo_mak: str) -> str:
    xml_data = {}

    tfile = open(full_path, "rb")
    header = tfile.read(16)
    offset, chunks = struct.unpack(">LH", header[10:])
    rawdata = tfile.read(offset - 16)
    tfile.close()

    count = 0
    for i in range(chunks):
        chunk_size, data_size, id, enc = struct.unpack(
            ">LLHH", rawdata[count : count + 12]
        )
        count += 12
        data = rawdata[count : count + data_size]
        xml_data[id] = {"enc": enc, "data": data, "start": count + 16}
        count += chunk_size - 12

    chunk = xml_data[2]
    details = chunk["data"]
    if chunk["enc"]:
        xml_key = xml_data[3]["data"]

        hexmak = hashlib.md5(b"tivo:TiVo DVR:" + tivo_mak.encode("utf-8")).hexdigest()
        key = hashlib.sha1(hexmak + xml_key).digest()[:16] + b"\0\0\0\0"

        turkey = hashlib.sha1(key[:17]).digest()
        turiv = hashlib.sha1(key).digest()

        details = Turing(turkey, turiv).crypt(details, chunk["start"])

    return details


@lru_cache(maxsize=64)
def from_tivo(full_path: str) -> Dict[str, str]:
    tdcat_path = get_bin("tdcat")
    tivo_mak = get_server("tivo_mak", "")
    try:
        assert tivo_mak
        if tdcat_path:
            details = _tdcat_bin(tdcat_path, full_path, tivo_mak)
        else:
            details = _tdcat_py(full_path, tivo_mak)
        metadata = from_details(details)
    except:
        metadata = {}

    return metadata


def force_utf8(text: str) -> str:
    return text.encode("utf-8").decode("utf-8")


def dump(output: TextIO, metadata: Dict[str, Any]) -> None:
    for key in metadata:
        value = metadata[key]
        if type(value) == list:
            for item in value:
                output.write("%s: %s\n" % (key, item.encode("utf-8")))
        else:
            if key in HUMAN and value in HUMAN[key]:
                output.write("%s: %s\n" % (key, HUMAN[key][value]))
            else:
                output.write("%s: %s\n" % (key, value.encode("utf-8")))


def video_info(inFile: str, cache: bool = True) -> VideoInfo:
    vInfo: Dict[str, Any] = {}
    mtime = os.path.getmtime(inFile)
    if cache:
        if inFile in INFO_CACHE and INFO_CACHE[inFile][0] == mtime:
            LOGGER.debug("CACHE HIT! %s" % inFile)
            return INFO_CACHE[inFile][1]

    vInfo["Supported"] = True

    ffmpeg_path = get_bin("ffmpeg")
    if ffmpeg_path is None:
        if os.path.splitext(inFile)[1].lower() not in [
            ".mpg",
            ".mpeg",
            ".vob",
            ".tivo",
            ".ts",
        ]:
            vInfo["Supported"] = False
        vInfo.update({"millisecs": 0, "vWidth": 704, "vHeight": 480, "rawmeta": {}})
        vid_info = VideoInfo(**vInfo)
        if cache:
            INFO_CACHE[inFile] = (mtime, vid_info)
        return vid_info

    cmd = [ffmpeg_path, "-i", inFile]
    # Windows and other OS buffer 4096 and ffmpeg can output more than that.
    err_tmp = tempfile.TemporaryFile()
    ffmpeg = subprocess.Popen(
        cmd, stderr=err_tmp, stdout=subprocess.PIPE, stdin=subprocess.PIPE
    )

    # wait configured # of seconds: if ffmpeg is not back give up
    limit = getFFmpegWait()
    if limit:
        for i in range(limit * 20):
            time.sleep(0.05)
            if not ffmpeg.poll() is None:
                break

        if ffmpeg.poll() is None:
            kill(ffmpeg)
            vInfo["Supported"] = False
            vid_info = VideoInfo(**vInfo)
            if cache:
                INFO_CACHE[inFile] = (mtime, vid_info)
            return vid_info
    else:
        ffmpeg.wait()

    err_tmp.seek(0)
    output = err_tmp.read().decode("utf-8")
    err_tmp.close()
    LOGGER.debug("ffmpeg output=%s" % output)

    attrs = {
        "container": r"Input #0, ([^,]+),",
        "vCodec": r"Video: ([^, ]+)",  # video codec
        "aKbps": r".*Audio: .+, (.+) (?:kb/s).*",  # audio bitrate
        "aCodec": r".*Audio: ([^, ]+)",  # audio codec
        "aFreq": r".*Audio: .+, (.+) (?:Hz).*",  # audio frequency
        "mapVideo": r"([0-9]+[.:]+[0-9]+).*: Video:.*",
    }  # video mapping

    for attr in attrs:
        rezre = re.compile(attrs[attr])
        x = rezre.search(output)
        if x:
            if attr in ["aKbps"]:
                vInfo[attr] = int(x.group(1))
            else:
                vInfo[attr] = x.group(1)
        else:
            if attr in ["container", "vCodec"]:
                vInfo[attr] = ""
                vInfo["Supported"] = False
            else:
                vInfo[attr] = None
            LOGGER.debug("failed at " + attr)

    rezre = re.compile(
        r".*Audio: .+, (?:(\d+)(?:(?:\.(\d).*)?(?: channels.*)?)|(stereo|mono)),.*"
    )
    x = rezre.search(output)
    if x:
        if x.group(3):
            if x.group(3) == "stereo":
                vInfo["aCh"] = 2
            elif x.group(3) == "mono":
                vInfo["aCh"] = 1
        elif x.group(2):
            vInfo["aCh"] = int(x.group(1)) + int(x.group(2))
        elif x.group(1):
            vInfo["aCh"] = int(x.group(1))
        else:
            vInfo["aCh"] = None
            LOGGER.debug("failed at aCh")
    else:
        vInfo["aCh"] = None
        LOGGER.debug("failed at aCh")

    rezre = re.compile(r".*Video: .+, (\d+)x(\d+)[, ].*")
    x = rezre.search(output)
    if x:
        vInfo["vWidth"] = int(x.group(1))
        vInfo["vHeight"] = int(x.group(2))
    else:
        vInfo["vWidth"] = None
        vInfo["vHeight"] = None
        vInfo["Supported"] = False
        LOGGER.debug("failed at vWidth/vHeight")

    rezre = re.compile(r".*Video: .+, (.+) (?:fps|tb\(r\)|tbr).*")
    x = rezre.search(output)
    if x:
        vInfo["vFps"] = x.group(1)
        if "." not in vInfo["vFps"]:
            vInfo["vFps"] += ".00"

        # Allow override only if it is mpeg2 and frame rate was doubled
        # to 59.94

        if vInfo["vCodec"] == "mpeg2video" and vInfo["vFps"] != "29.97":
            # First look for the build 7215 version
            rezre = re.compile(r".*film source: 29.97.*")
            x = rezre.search(output.lower())
            if x:
                LOGGER.debug("film source: 29.97 setting vFps to 29.97")
                vInfo["vFps"] = "29.97"
            else:
                # for build 8047:
                rezre = re.compile(
                    r".*frame rate differs from container " + r"frame rate: 29.97.*"
                )
                LOGGER.debug("Bug in VideoReDo")
                x = rezre.search(output.lower())
                if x:
                    vInfo["vFps"] = "29.97"
    else:
        vInfo["vFps"] = ""
        vInfo["Supported"] = False
        LOGGER.debug("failed at vFps")

    durre = re.compile(r".*Duration: ([0-9]+):([0-9]+):([0-9]+)\.([0-9]+),")
    d = durre.search(output)

    if d:
        vInfo["millisecs"] = (
            int(d.group(1)) * 3600 + int(d.group(2)) * 60 + int(d.group(3))
        ) * 1000 + int(d.group(4)) * (10 ** (3 - len(d.group(4))))
    else:
        vInfo["millisecs"] = 0

    # get bitrate of source for tivo compatibility test.
    rezre = re.compile(r".*bitrate: (.+) (?:kb/s).*")
    x = rezre.search(output)
    if x:
        vInfo["kbps"] = int(x.group(1))
    else:
        # Fallback method of getting video bitrate
        # Sample line:  Stream #0.0[0x1e0]: Video: mpeg2video, yuv420p,
        #               720x480 [PAR 32:27 DAR 16:9], 9800 kb/s, 59.94 tb(r)
        rezre = re.compile(
            r".*Stream #0\.0\[.*\]: Video: mpeg2video, "
            + r"\S+, \S+ \[.*\], (\d+) (?:kb/s).*"
        )
        x = rezre.search(output)
        if x:
            vInfo["kbps"] = int(x.group(1))
        else:
            vInfo["kbps"] = None
            LOGGER.debug("failed at kbps")

    # get par.
    rezre = re.compile(r".*Video: .+PAR ([0-9]+):([0-9]+) DAR [0-9:]+.*")
    x = rezre.search(output)
    if x and x.group(1) != "0" and x.group(2) != "0":
        vInfo["par1"] = x.group(1) + ":" + x.group(2)
        vInfo["par2"] = float(x.group(1)) / float(x.group(2))
    else:
        vInfo["par1"], vInfo["par2"] = None, None

    # get dar.
    rezre = re.compile(r".*Video: .+DAR ([0-9]+):([0-9]+).*")
    x = rezre.search(output)
    if x and x.group(1) != "0" and x.group(2) != "0":
        vInfo["dar1"] = x.group(1) + ":" + x.group(2)
    else:
        vInfo["dar1"] = None

    # get Audio Stream mapping.
    rezre = re.compile(r"([0-9]+[.:]+[0-9]+)(.*): Audio:(.*)")
    x = rezre.search(output)
    amap = []
    if x:
        for x in rezre.finditer(output):
            amap.append((x.group(1), x.group(2) + x.group(3)))
    else:
        amap.append(("", ""))
        LOGGER.debug("failed at mapAudio")
    vInfo["mapAudio"] = amap

    vInfo["par"] = None

    # get Metadata dump (newer ffmpeg).
    lines = output.split("\n")
    rawmeta = {}
    flag = False

    for line in lines:
        if line.startswith("  Metadata:"):
            flag = True
        else:
            if flag:
                if line.startswith("  Duration:"):
                    flag = False
                else:
                    try:
                        key, value = [x.strip() for x in line.split(":", 1)]
                        rawmeta[key] = [value]
                    except:
                        pass

    vInfo["rawmeta"] = rawmeta

    data = from_text(inFile)
    for key in data:
        if key.startswith("Override_"):
            vInfo["Supported"] = True
            if key.startswith("Override_mapAudio"):
                audiomap = dict(vInfo["mapAudio"])
                newmap = shlex.split(data[key])
                audiomap.update(list(zip(newmap[::2], newmap[1::2])))
                vInfo["mapAudio"] = sorted(
                    list(audiomap.items()), key=lambda k_v: (k_v[0], k_v[1])
                )
            elif key.startswith("Override_millisecs"):
                vInfo[key.replace("Override_", "")] = int(data[key])
            else:
                vInfo[key.replace("Override_", "")] = data[key]

    if cache:
        INFO_CACHE[inFile] = (mtime, vInfo)
    LOGGER.debug("; ".join(["%s=%s" % (k, v) for k, v in list(vInfo.items())]))
    vid_info = VideoInfo(**vInfo)
    if cache:
        INFO_CACHE[inFile] = (mtime, vid_info)
    return vid_info


def kill(popen: subprocess.Popen) -> None:
    LOGGER.debug("killing pid=%s" % str(popen.pid))
    if sys.platform == "win32":
        win32kill(popen.pid)
    else:
        import os, signal

        for i in range(3):
            LOGGER.debug("sending SIGTERM to pid: %s" % popen.pid)
            os.kill(popen.pid, signal.SIGTERM)
            time.sleep(0.5)
            if popen.poll() is not None:
                LOGGER.debug("process %s has exited" % popen.pid)
                break
        else:
            while popen.poll() is None:
                LOGGER.debug("sending SIGKILL to pid: %s" % popen.pid)
                os.kill(popen.pid, signal.SIGKILL)
                time.sleep(0.5)


def win32kill(pid: int) -> None:
    import ctypes

    # We ignore types for the next 3 lines so that the absence of windll
    #   on non-Windows platforms is not flagged as an error
    handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # type: ignore
    ctypes.windll.kernel32.TerminateProcess(handle, -1)  # type: ignore
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore
