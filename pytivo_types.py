from typing import Dict, List

class Bdict(dict):
    def getboolean(self, x: str) -> bool:
        return self.get(x, "False").lower() in ("1", "yes", "true", "on")


Query = Dict[str, List[str]]
Settings = Bdict
