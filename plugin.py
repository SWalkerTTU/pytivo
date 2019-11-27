import os
import random
import shutil
import sys
import threading
import time
from typing import (
    List,
    Any,
    Tuple,
    Dict,
    TYPE_CHECKING,
    Optional,
    Callable,
    Union,
    Type,
    TypeVar,
    Generic,
)
import urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler

from Cheetah.Filters import Filter  # type: ignore

from lrucache import LRUCache
from pytivo_types import Query, FileData, FileDataLike

if TYPE_CHECKING:
    from httpserver import TivoHTTPHandler


def no_anchor(handler: "TivoHTTPHandler", anchor: str) -> None:
    handler.server.logger.warning("Anchor not found: " + anchor)


# TODO 20191125 Maybe omit file_type if no filter functions use it?
def build_recursive_list(
    path: str,
    recurse: bool = True,
    filterFunction: Optional[Callable] = None,
    file_type: Optional[str] = None,
) -> List[FileData]:
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
                    files.append(FileData(f, isdir))
    except:
        print(f"build_recursive_list() Exception", file=sys.stderr)
        pass
    return files


def quote(in_str: str) -> str:
    if os.path.sep == "/":
        return urllib.parse.quote(in_str)
    else:
        return urllib.parse.quote(in_str.replace(os.path.sep, "/"))


def unquote(in_str: str) -> str:
    if os.path.sep == "/":
        return urllib.parse.unquote_plus(in_str)
    else:
        return os.path.normpath(urllib.parse.unquote_plus(in_str))


class Error:
    CONTENT_TYPE = "text/html"


class SortList(Generic[FileDataLike]):
    def __init__(self, files: List[FileDataLike]) -> None:
        self.files: List[FileDataLike] = files
        self.unsorted: bool = True
        self.sortby: Optional[str] = None
        self.last_start: int = 0


def GetPlugin(name: str) -> Union["Plugin", Error]:
    try:
        module_name = ".".join(["plugins", name, name])
        module = __import__(module_name, globals(), locals(), name)
        # mypy can't find CLASS_NAME for the following
        plugin = getattr(module, module.CLASS_NAME)()  # type: ignore
        return plugin
    except ImportError:
        # TODO 20191124: log this error instead of printing
        print(
            "Error no", name, "plugin exists. Check the type " "setting for your share."
        )
        # TODO 20191124: Actually raise a real error instead of returning
        #   this dumb error class
        return Error()


# TODO 20191125: We could define a Generic for the class that would take the
#   value of FileData, or FileDataMusic, etc. depending on the plugin, and
#   give proper types for the member methods?
class Plugin:
    random_lock = threading.Lock()

    CONTENT_TYPE = ""

    # TODO: use @functools.lru_cache instead
    recurse_cache = LRUCache(5)
    dir_cache = LRUCache(10)

    # TODO 20191124: What is going on here with __it__
    # TODO 20191124: add types to this
    def __new__(cls, *args, **kwds):
        it = cls.__dict__.get("__it__")
        if it is not None:
            return it
        cls.__it__ = it = object.__new__(cls)
        it.init(*args, **kwds)
        return it

    def init(self) -> None:
        pass

    def send_file(self, handler: "TivoHTTPHandler", path: str, query: Query) -> None:
        handler.send_content_file(path)

    def get_local_base_path(self, handler: "TivoHTTPHandler", query: Query) -> str:
        return os.path.normpath(handler.container["path"])

    def get_local_path(self, handler: "TivoHTTPHandler", query: Query) -> str:

        subcname = query["Container"][0]

        path = self.get_local_base_path(handler, query)
        for folder in subcname.split("/")[1:]:
            if folder == "..":
                # TODO 20191124: check that "" as error is ok
                return ""
            path = os.path.join(path, folder)
        return path

    def item_count(
        self,
        handler: "TivoHTTPHandler",
        query: Query,
        cname: str,
        files: List[FileDataLike],
        last_start: int = 0,
    ) -> Tuple[List[FileDataLike], int, int]:
        """Return only the desired portion of the list, as specified by
           ItemCount, AnchorItem and AnchorOffset. 'files' is
           a list of objects with a 'name' attribute.
        """

        totalFiles = len(files)
        index = 0

        if totalFiles and "ItemCount" in query:
            count = int(query["ItemCount"][0])

            if "AnchorItem" in query:
                bs = "/TiVoConnect?Command=QueryContainer&Container="
                local_base_path = self.get_local_base_path(handler, query)

                anchor = query["AnchorItem"][0]
                if anchor.startswith(bs):
                    anchor = anchor.replace(bs, "/", 1)
                anchor = unquote(anchor)
                anchor = anchor.replace(os.path.sep + cname, local_base_path, 1)
                if not "://" in anchor:
                    anchor = os.path.normpath(anchor)

                # if type(files[0]) == str:
                #    filenames = files
                # else:
                #    filenames = [x.name for x in files]
                filenames = [x.name for x in files]
                try:
                    index = filenames.index(anchor, last_start)
                except ValueError:
                    if last_start:
                        try:
                            index = filenames.index(anchor, 0, last_start)
                        except ValueError:
                            no_anchor(handler, anchor)
                    else:
                        no_anchor(handler, anchor)  # just use index = 0

                if count > 0:
                    index += 1

                if "AnchorOffset" in query:
                    index += int(query["AnchorOffset"][0])

            if count < 0:
                index = (index + count) % len(files)
                count = -count
            files = files[index : index + count]

        return files, totalFiles, index

    # Returns List[Any] but really we want here List[FileData] and in
    #   children parent List[FileData*]
    def get_files(
        self,
        handler: "TivoHTTPHandler",
        query: Query,
        filterFunction: Optional[Callable] = None,
        force_alpha: bool = False,
        allow_recurse: bool = True,
    ) -> Tuple[List[Any], int, int]:
        subcname = query["Container"][0]
        path = self.get_local_path(handler, query)

        file_type = query.get("Filter", [""])[0]

        recurse = allow_recurse and query.get("Recurse", ["No"])[0] == "Yes"

        filelist = SortList[FileData]([])
        # TODO: use @functools.lru_cache instead (but mtime not supplied?)
        rc = self.recurse_cache
        # TODO: use @functools.lru_cache instead (but mtime not supplied?)
        dc = self.dir_cache
        if recurse:
            if path in rc and rc.mtime(path) + 300 >= time.time():
                filelist = rc[path]
        else:
            updated = os.path.getmtime(path)
            if path in dc and dc.mtime(path) >= updated:
                filelist = dc[path]
            for p in rc:
                if path.startswith(p) and rc.mtime(p) < updated:
                    del rc[p]

        if not filelist.files:
            filelist = SortList[FileData](
                build_recursive_list(path, recurse, filterFunction, file_type)
            )

            if recurse:
                rc[path] = filelist
            else:
                # TODO 20191125: error here from lrucache line 159
                # TypeError: '<' not supported between instances of '__Node' and '__Node'
                dc[path] = filelist

        sortby = query.get("SortOrder", ["Normal"])[0]
        if filelist.unsorted or filelist.sortby != sortby:
            if force_alpha:
                # secondary by ascending name
                filelist.files.sort(key=lambda x: x.name)
                # primary by descending isdir
                filelist.files.sort(key=lambda x: x.isdir, reverse=True)
            elif sortby == "!CaptureDate":
                # most recent date at top
                filelist.files.sort(key=lambda x: x.mdate, reverse=True)
            else:
                filelist.files.sort(key=lambda x: x.name)

            filelist.sortby = sortby
            filelist.unsorted = False

        files = filelist.files[:]

        # Trim the list
        files, total, start = self.item_count(
            handler, query, handler.cname, files, filelist.last_start
        )
        if len(files) > 1:
            filelist.last_start = start
        return files, total, start
