"""站點註冊表:依網域選 adapter。新增網站時 import 並加入 ADAPTERS。"""
from urllib.parse import urlparse

from adapter_tools import load_user_adapters
from .shuba69 import Shuba69
from .sunzhinan import SunzhinanAdapter
from .xbanxia import XbanxiaAdapter
from .czbooks import CzbooksAdapter
from .shuku52 import Shuku52Adapter
from .novel543 import Novel543Adapter
from .generic import GenericAdapter
from .book8 import Book8Adapter
from .base import SiteAdapter

BUILTIN_ADAPTERS = [
    Shuba69,
    SunzhinanAdapter,
    XbanxiaAdapter,
    CzbooksAdapter,
    Shuku52Adapter,
    Novel543Adapter,
    Book8Adapter,
]
ADAPTERS = list(BUILTIN_ADAPTERS)

USER_ADAPTERS, USER_ADAPTER_ERRORS = load_user_adapters(SiteAdapter)
ADAPTERS.extend(USER_ADAPTERS)


def reload_adapters():
    """重新掃描 user_adapters，讓 GUI 不必重啟即可套用修改。"""
    global USER_ADAPTERS, USER_ADAPTER_ERRORS
    USER_ADAPTERS, USER_ADAPTER_ERRORS = load_user_adapters(SiteAdapter)
    ADAPTERS[:] = list(BUILTIN_ADAPTERS) + list(USER_ADAPTERS)
    return ADAPTERS


def get_adapter(url: str):
    host = urlparse(url).netloc.lower()
    for cls in ADAPTERS:
        if host in cls.domains:
            return cls()
    # 未註冊網站:用通用啟發式解析(下載前主流程會做預檢)
    return GenericAdapter()
