import os
from typing import Dict, List, TypeVar


class Bdict(dict):
    def getboolean(self, x: str) -> bool:
        return self.get(x, "False").lower() in ("1", "yes", "true", "on")


class FileData:
    def __init__(self, name: str, isdir: bool) -> None:
        self.name: str = name
        self.isdir: bool = isdir
        st = os.stat(name)
        self.mdate: float = st.st_mtime
        self.cdate: float = st.st_ctime
        self.size: int = st.st_size


FileDataLike = TypeVar("FileDataLike", bound=FileData)
Query = Dict[str, List[str]]
Settings = Bdict
