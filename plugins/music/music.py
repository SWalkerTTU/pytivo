from functools import partial
import os
import random
import re
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Dict, Any, List, Optional, Union, Callable, Tuple
import unicodedata
import urllib.request, urllib.parse, urllib.error
from xml.sax.saxutils import escape

import mutagen  # type: ignore
from mutagen.easyid3 import EasyID3  # type: ignore
from mutagen.mp3 import MP3  # type: ignore
from Cheetah.Template import Template  # type: ignore

from lrucache import LRUCache
import config
from plugin import Plugin, quote, unquote, FileData, SortList, FileDataLike
from plugins.video.transcode import kill

if TYPE_CHECKING:
    from httpserver import TivoHTTPHandler

SCRIPTDIR = os.path.dirname(__file__)

CLASS_NAME = "Music"

PLAYLISTS = (".m3u", ".m3u8", ".ram", ".pls", ".b4s", ".wpl", ".asx", ".wax", ".wvx")

TRANSCODE = (
    ".mp4",
    ".m4a",
    ".flc",
    ".ogg",
    ".wma",
    ".aac",
    ".wav",
    ".aif",
    ".aiff",
    ".au",
    ".flac",
)

TAGNAMES = {
    "artist": ["\xa9ART", "Author"],
    "title": ["\xa9nam", "Title"],
    "album": ["\xa9alb", "WM/AlbumTitle"],
    "date": ["\xa9day", "WM/Year"],
    "genre": ["\xa9gen", "WM/Genre"],
}

BLOCKSIZE = 64 * 1024

# Search strings for different playlist types
asxfile = re.compile('ref +href *= *"([^"]*)"', re.IGNORECASE).search
wplfile = re.compile('media +src *= *"([^"]*)"', re.IGNORECASE).search
b4sfile = re.compile('Playstring="file:([^"]*)"').search
plsfile = re.compile("[Ff]ile(\d+)=(.+)").match
plstitle = re.compile("[Tt]itle(\d+)=(.+)").match
plslength = re.compile("[Ll]ength(\d+)=(\d+)").match

# Duration -- parse from ffmpeg output
durre = re.compile(r".*Duration: ([0-9]+):([0-9]+):([0-9]+)\.([0-9]+),").search

# Preload the templates
tfname = os.path.join(SCRIPTDIR, "templates", "container.tmpl")
tpname = os.path.join(SCRIPTDIR, "templates", "m3u.tmpl")
iname = os.path.join(SCRIPTDIR, "templates", "item.tmpl")
with open(tfname, "rb") as tfname_fh:
    FOLDER_TEMPLATE = tfname_fh.read()
with open(tpname, "rb") as tpname_fh:
    PLAYLIST_TEMPLATE = tpname_fh.read()
with open(iname, "rb") as iname_fh:
    ITEM_TEMPLATE = iname_fh.read()

# TODO: No more subprocess.Popen._make_inheritable, need to verify on Windows
## XXX BIG HACK
## subprocess is broken for me on windows so super hack
# def patchSubprocess() -> None:
#    o = subprocess.Popen._make_inheritable
#
#    def _make_inheritable(self, handle):
#        if not handle:
#            return subprocess.GetCurrentProcess()
#        return o(self, handle)
#
#    subprocess.Popen._make_inheritable = _make_inheritable
#
#
# mswindows = sys.platform == "win32"
# if mswindows:
#    patchSubprocess()


def get_tag(tagname, d):
    for tag in [tagname] + TAGNAMES[tagname]:
        try:
            if tag in d:
                value = d[tag][0]
                if type(value) not in [str, str]:
                    value = str(value)
                return value
        except:
            pass
    return ""


def build_recursive_list(
    path: str,
    recurse: bool = True,
    filterFunction: Optional[Callable] = None,
    file_type: Optional[List[str]] = None,
) -> List[FileDataMusic]:
    files = []
    try:
        for f in os.listdir(path):
            if f.startswith("."):
                continue
            f = os.path.join(path, f)
            isdir = os.path.isdir(f)
            if recurse and isdir:
                files.extend(
                    build_recursive_list(f, recurse, filterFunction, file_type)
                )
            else:
                if filterFunction is None or filterFunction(f, file_type):
                    files.append(FileDataMusic(f, isdir))
    except:
        pass
    return files


class FileDataMusic(FileData):
    def __init__(self, name: str, isdir: bool) -> None:
        super().__init__(name, isdir)
        self.isplay = os.path.splitext(name)[1].lower() in PLAYLISTS
        self.title = ""
        self.duration = 0


class Music(Plugin):
    CONTENT_TYPE = "x-container/tivo-music"
    AUDIO = "audio"
    DIRECTORY = "dir"
    PLAYLIST = "play"

    media_data_cache = LRUCache(300)
    recurse_cache = LRUCache(5)
    dir_cache = LRUCache(10)

    def send_file(
        self, handler: "TivoHTTPHandler", path: str, query: Dict[str, Any]
    ) -> None:
        seek = int(query.get("Seek", [0])[0])
        duration = int(query.get("Duration", [0])[0])
        always = handler.container.getboolean("force_ffmpeg") and config.get_bin(
            "ffmpeg"
        )

        ext = os.path.splitext(path)[1].lower()
        needs_transcode = ext in TRANSCODE or seek or duration or always

        if not needs_transcode:
            fsize = os.path.getsize(path)
            handler.send_response(200)
            handler.send_header("Content-Length", str(fsize))
        else:
            if config.get_bin("ffmpeg") is None:
                handler.server.logger.error("ffmpeg is not found.  Aborting transcode.")
                return
            handler.send_response(206)
            handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Content-Type", "audio/mpeg")
        handler.end_headers()

        if needs_transcode:
            cmd: List[str]
            cmd = [config.get_bin("ffmpeg"), "-i", path, "-vn"]  # type: ignore
            if ext in [".mp3", ".mp2"]:
                cmd += ["-acodec", "copy"]
            else:
                cmd += ["-ab", "320k", "-ar", "44100"]
            cmd += ["-f", "mp3", "-"]
            if seek:
                cmd[-1:] = ["-ss", "%.3f" % (seek / 1000.0), "-"]
            if duration:
                cmd[-1:] = ["-t", "%.3f" % (duration / 1000.0), "-"]

            ffmpeg = subprocess.Popen(cmd, bufsize=BLOCKSIZE, stdout=subprocess.PIPE)
            while True:
                try:
                    block = ffmpeg.stdout.read(BLOCKSIZE)
                    handler.wfile.write(b"%x\r\n" % len(block))
                    handler.wfile.write(block)
                    handler.wfile.write(b"\r\n")
                except Exception as msg:
                    handler.server.logger.info(msg)
                    kill(ffmpeg)
                    break

                if not block:
                    break
        else:
            f = open(path, "rb")
            try:
                shutil.copyfileobj(f, handler.wfile)
            except:
                pass
            f.close()

        try:
            handler.wfile.flush()
        except Exception as msg:
            handler.server.logger.info(msg)

    def AudioFileFilter(
        self, f: str, filter_type: Optional[str] = None
    ) -> Union[bool, str]:
        ext = os.path.splitext(f)[1].lower()

        file_type: Union[bool, str]

        if ext in (".mp3", ".mp2") or (ext in TRANSCODE and config.get_bin("ffmpeg")):
            return self.AUDIO
        else:
            file_type = False

            if filter_type is None or filter_type.split("/")[0] != self.AUDIO:
                if ext in PLAYLISTS:
                    file_type = self.PLAYLIST
                elif os.path.isdir(f):
                    file_type = self.DIRECTORY

            return file_type

    def media_data(self, f: FileDataMusic, local_base_path: str) -> Dict[str, Any]:
        if f.name in self.media_data_cache:
            return self.media_data_cache[f.name]

        item: Dict[str, Any] = {}
        item["path"] = f.name
        item["part_path"] = f.name.replace(local_base_path, "", 1)
        item["name"] = os.path.basename(f.name)
        item["is_dir"] = f.isdir
        item["is_playlist"] = f.isplay
        item["params"] = "No"

        if f.title:
            item["Title"] = f.title

        if f.duration > 0:
            item["Duration"] = f.duration

        if f.isdir or f.isplay or "://" in f.name:
            self.media_data_cache[f.name] = item
            return item

        # If the format is: (track #) Song name...
        # artist, album, track = f.name.split(os.path.sep)[-3:]
        # track = os.path.splitext(track)[0]
        # if track[0].isdigit:
        #    track = ' '.join(track.split(' ')[1:])

        # item['SongTitle'] = track
        # item['AlbumTitle'] = album
        # item['ArtistName'] = artist

        ext = os.path.splitext(f.name)[1].lower()

        try:
            # If the file is an mp3, let's load the EasyID3 interface
            if ext == ".mp3":
                audioFile = MP3(f.name, ID3=EasyID3)
            else:
                # Otherwise, let mutagen figure it out
                audioFile = mutagen.File(f.name)

            if audioFile:
                # Pull the length from the FileType, if present
                if audioFile.info.length > 0:
                    item["Duration"] = int(audioFile.info.length * 1000)

                # Grab our other tags, if present
                artist = get_tag("artist", audioFile)
                title = get_tag("title", audioFile)
                if artist == "Various Artists" and "/" in title:
                    artist, title = [x.strip() for x in title.split("/")]
                item["ArtistName"] = artist
                item["SongTitle"] = title
                item["AlbumTitle"] = get_tag("album", audioFile)
                item["AlbumYear"] = get_tag("date", audioFile)[:4]
                item["MusicGenre"] = get_tag("genre", audioFile)
        except Exception as msg:
            print(msg)

        ffmpeg_path = config.get_bin("ffmpeg")
        if "Duration" not in item and ffmpeg_path:
            cmd = [ffmpeg_path, "-i", f.name]
            ffmpeg = subprocess.Popen(
                cmd,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
            )

            # wait 10 sec if ffmpeg is not back give up
            for i in range(200):
                time.sleep(0.05)
                if not ffmpeg.poll() == None:
                    break

            if ffmpeg.poll() != None:
                output = ffmpeg.stderr.read()
                d = durre(output.decode("utf-8"))
                if d:
                    millisecs = (
                        int(d.group(1)) * 3600 + int(d.group(2)) * 60 + int(d.group(3))
                    ) * 1000 + int(d.group(4)) * (10 ** (3 - len(d.group(4))))
                else:
                    millisecs = 0
                item["Duration"] = millisecs

        if "Duration" in item and ffmpeg_path:
            item["params"] = "Yes"

        self.media_data_cache[f.name] = item
        return item

    # this is a TivoConnect Command, so must be named this exactly
    def QueryContainer(self, handler: "TivoHTTPHandler", query: Dict[str, Any]) -> None:
        subcname = query["Container"][0]
        local_base_path = self.get_local_base_path(handler, query)

        if not self.get_local_path(handler, query):
            handler.send_error(404)
            return

        if os.path.splitext(subcname)[1].lower() in PLAYLISTS:
            t = Template(PLAYLIST_TEMPLATE)
            t.files, t.total, t.start = self.get_playlist(handler, query)
        else:
            t = Template(FOLDER_TEMPLATE)
            t.files, t.total, t.start = self.get_files(
                handler, query, self.AudioFileFilter
            )
        t.files = list(
            map(partial(self.media_data, local_base_path=local_base_path), t.files)
        )
        t.container = handler.cname
        t.name = subcname
        t.quote = quote
        t.escape = escape

        handler.send_xml(str(t))

    # this is a TivoConnect Command, so must be named this exactly
    def QueryItem(self, handler: "TivoHTTPHandler", query: Dict[str, Any]) -> None:
        uq = urllib.parse.unquote_plus
        splitpath = [x for x in uq(query["Url"][0]).split("/") if x]
        path = os.path.join(handler.container["path"], *splitpath[1:])

        if path in self.media_data_cache:
            t = Template(ITEM_TEMPLATE)
            t.file = self.media_data_cache[path]
            t.escape = escape
            handler.send_xml(str(t))
        else:
            handler.send_error(404)

    def parse_playlist(self, list_name: str, recurse: bool) -> List[FileDataMusic]:

        ext = os.path.splitext(list_name)[1].lower()

        try:
            url = list_name.index("http://")
            list_name = list_name[url:]
            list_file = urllib.request.urlopen(list_name)
        except:
            list_file = open(list_name)
            local_path = os.path.sep.join(list_name.split(os.path.sep)[:-1])

        if ext in (".m3u", ".pls"):
            charset = "cp1252"
        else:
            charset = "utf-8"

        if ext in (".wpl", ".asx", ".wax", ".wvx", ".b4s"):
            playlist = []
            for line in list_file:
                line = str(line, charset).encode("utf-8")
                if ext == ".wpl":
                    s = wplfile(line)
                elif ext == ".b4s":
                    s = b4sfile(line)
                else:
                    s = asxfile(line)
                if s:
                    playlist.append(FileDataMusic(s.group(1), False))

        elif ext == ".pls":
            names, titles, lengths = {}, {}, {}
            for line in list_file:
                line = str(line, charset).encode("utf-8")
                s = plsfile(line)
                if s:
                    names[s.group(1)] = s.group(2)
                else:
                    s = plstitle(line)
                    if s:
                        titles[s.group(1)] = s.group(2)
                    else:
                        s = plslength(line)
                        if s:
                            lengths[s.group(1)] = int(s.group(2))
            playlist = []
            for key in names:
                f = FileDataMusic(names[key], False)
                if key in titles:
                    f.title = titles[key]
                if key in lengths:
                    f.duration = lengths[key]
                playlist.append(f)

        else:  # ext == '.m3u' or '.m3u8' or '.ram'
            duration, title = 0, ""
            playlist = []
            for line in list_file:
                line = str(line.strip(), charset).encode("utf-8")
                if line:
                    if line.startswith("#EXTINF:"):
                        try:
                            duration, title = line[8:].split(",", 1)
                            duration = int(duration)
                        except ValueError:
                            duration = 0

                    elif not line.startswith("#"):
                        f = FileDataMusic(line, False)
                        f.title = title.strip()
                        f.duration = duration
                        playlist.append(f)
                        duration, title = 0, ""

        list_file.close()

        # Expand relative paths
        for i in range(len(playlist)):
            if not "://" in playlist[i].name:
                name = playlist[i].name
                if not os.path.isabs(name):
                    name = os.path.join(local_path, name)
                playlist[i].name = os.path.normpath(name)

        if recurse:
            newlist = []
            for fdata in playlist:
                if fdata.isplay:
                    newlist.extend(self.parse_playlist(fdata.name, recurse))
                else:
                    newlist.append(fdata)

            playlist = newlist

        return playlist

    # Returns List[Any] but really we want here List[FileDataMusic] and in
    #   parent List[FileData]
    def get_files(
        self,
        handler: "TivoHTTPHandler",
        query: Dict[str, Any],
        filterFunction: Optional[Callable] = None,
        force_alpha: bool = False,  # unused in this plugin
        allow_recurse: bool = False,  # unused in this plugin
    ) -> Tuple[List[Any], int, int]:
        path = self.get_local_path(handler, query)

        file_type = query.get("Filter", [""])[0]

        recurse = query.get("Recurse", ["No"])[0] == "Yes"

        filelist = SortList[FileDataMusic]([])
        rc = self.recurse_cache
        dc = self.dir_cache
        if recurse:
            if path in rc:
                filelist = rc[path]
        else:
            updated = os.path.getmtime(path)
            if path in dc and dc.mtime(path) >= updated:
                filelist = dc[path]
            for p in rc:
                if path.startswith(p) and rc.mtime(p) < updated:
                    del rc[p]

        if not filelist:
            filelist = SortList[FileDataMusic](build_recursive_list(path, recurse))

            if recurse:
                rc[path] = filelist
            else:
                dc[path] = filelist

        # Sort it
        seed = ""
        start = ""
        sortby = query.get("SortOrder", ["Normal"])[0]
        if "Random" in sortby:
            if "RandomSeed" in query:
                seed = query["RandomSeed"][0]
                sortby += seed
            if "RandomStart" in query:
                start = query["RandomStart"][0]
                sortby += start

        if filelist.unsorted or filelist.sortby != sortby:
            if "Random" in sortby:
                self.random_lock.acquire()
                if seed:
                    random.seed(seed)
                random.shuffle(filelist.files)
                self.random_lock.release()
                if start:
                    local_base_path = self.get_local_base_path(handler, query)
                    start = unquote(start)
                    start = start.replace(
                        os.path.sep + handler.cname, local_base_path, 1
                    )
                    filenames = [x.name for x in filelist.files]
                    try:
                        index = filenames.index(start)
                        i = filelist.files.pop(index)
                        filelist.files.insert(0, i)
                    except ValueError:
                        handler.server.logger.warning("Start not found: " + start)
            else:
                # secondary by ascending name
                filelist.files.sort(key=lambda x: x.name)
                # primary by descending isdir
                filelist.files.sort(key=lambda x: x.isdir, reverse=True)

            filelist.sortby = sortby
            filelist.unsorted = False

        files = filelist.files[:]

        # Trim the list
        files, total, start_item = self.item_count(
            handler, query, handler.cname, files, filelist.last_start
        )
        filelist.last_start = start_item
        return files, total, start_item

    def get_playlist(self, handler, query):
        subcname = query["Container"][0]

        try:
            url = subcname.index("http://")
            list_name = subcname[url:]
        except:
            list_name = self.get_local_path(handler, query)

        recurse = query.get("Recurse", ["No"])[0] == "Yes"
        playlist = self.parse_playlist(list_name, recurse)

        # Shuffle?
        if "Random" in query.get("SortOrder", ["Normal"])[0]:
            seed = query.get("RandomSeed", [""])[0]
            start = query.get("RandomStart", [""])[0]

            self.random_lock.acquire()
            if seed:
                random.seed(seed)
            random.shuffle(playlist)
            self.random_lock.release()
            if start:
                local_base_path = self.get_local_base_path(handler, query)
                start = unquote(start)
                start = start.replace(os.path.sep + handler.cname, local_base_path, 1)
                filenames = [x.name for x in playlist]
                try:
                    index = filenames.index(start)
                    i = playlist.pop(index)
                    playlist.insert(0, i)
                except ValueError:
                    handler.server.logger.warning("Start not found: " + start)

        # Trim the list
        return self.item_count(handler, query, handler.cname, playlist)
