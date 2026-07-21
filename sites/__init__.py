"""站點註冊表:依網域選 adapter。新增網站時 import 並加入 ADAPTERS。"""
from urllib.parse import urlparse

from .shuba69 import Shuba69
from .sunzhinan import SunzhinanAdapter
from .xbanxia import XbanxiaAdapter
from .czbooks import CzbooksAdapter

ADAPTERS = [Shuba69, SunzhinanAdapter, XbanxiaAdapter, CzbooksAdapter]


def get_adapter(url: str):
    host = urlparse(url).netloc.lower()
    for cls in ADAPTERS:
        if host in cls.domains:
            return cls()
    supported = ", ".join(sorted({d for cls in ADAPTERS for d in cls.domains}))
    raise ValueError(f"不支援的網站: {host}(目前支援: {supported})")
