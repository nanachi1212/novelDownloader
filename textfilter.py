"""正文後處理:跨章重複樣板自動偵測 + 使用者自訂過濾規則。

兩層都在「合併輸出」階段執行(不動每章快取),所以改規則後重跑同一本書
會直接用快取重新過濾,不需要重新下載。
"""
import re
import sys
import math
from collections import Counter
from pathlib import Path

DEFAULT_RULES_HEADER = """\
# 自訂過濾規則:每行一條,符合的整段會從輸出移除
# - 直接寫文字     = 段落「包含」該文字就移除,例如: 一秒記住本站
# - re: 開頭       = 正則表達式,例如: re:^第\\d+章$
# - # 開頭是註解,空行忽略
# 儲存後,下一次下載(或重跑同一本書)生效
"""

AUTO_BOILERPLATE_RE = re.compile(
    r"(?:閱讀全文|阅读全文|關閉|关闭|廣告|广告|本站|本網站|本网站|手機用戶|手机用户|"
    r"記住本站|记住本站|章節報錯|章节报错|加入書籤|加入书签|推薦票|推荐票|"
    r"關燈|关灯|護眼|护眼|本章完|^推$|^[小中大]$)",
    re.I,
)


def rules_dir() -> Path:
    """使用者可編輯的規則檔目錄。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def bundled_rules_dir() -> Path:
    """PyInstaller 內建預設規則目錄；原始碼模式與可編輯目錄相同。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return rules_dir()


def rules_path(site_hint: str = None) -> Path:
    """per-site 規則優先,全局規則備援。
    site_hint: 網域名或網站識別符,如 'xbanxia.cc' 或 'xbanxia'
    """
    dirs = [rules_dir()]
    bundled = bundled_rules_dir()
    if bundled not in dirs:
        dirs.append(bundled)
    if site_hint:
        # 嘗試 filter_rules_xbanxia.txt 或 filter_rules_xbanxia.cc.txt
        site_clean = site_hint.replace(".", "_").replace("www_", "").lower()
        for directory in dirs:
            site_file = directory / f"filter_rules_{site_clean}.txt"
            if site_file.exists():
                return site_file
    # 回到全局規則
    for directory in dirs:
        global_file = directory / "filter_rules.txt"
        if global_file.exists():
            return global_file
    return rules_dir() / "filter_rules.txt"


def ensure_rules_file(site_hint: str = None) -> Path:
    """確保規則檔存在。若 site_hint 指定,則建立 per-site 檔案。"""
    if site_hint:
        site_clean = site_hint.replace(".", "_").replace("www_", "").lower()
        p = rules_dir() / f"filter_rules_{site_clean}.txt"
        if not p.exists():
            p.write_text(f"# {site_hint} 專用過濾規則\n\n" + DEFAULT_RULES_HEADER, encoding="utf-8")
        return p
    else:
        p = rules_dir() / "filter_rules.txt"
        if not p.exists():
            p.write_text(DEFAULT_RULES_HEADER, encoding="utf-8")
        return p


def load_rules(site_hint: str = None):
    """回傳 [(kind, pattern)];kind 為 'str'(包含比對) 或 're'(正則)。
    site_hint: 優先載入該網站的專用規則,找不到則用全局規則。
    """
    p = rules_path(site_hint)
    if not p.exists():
        return []
    rules = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            try:
                rules.append(("re", re.compile(line[3:])))
            except re.error:
                continue  # 寫壞的正則直接跳過,不讓下載掛掉
        else:
            rules.append(("str", line))
    return rules


def apply_rules(text: str, rules) -> str:
    """逐段套用自訂規則,命中的段落整段移除。"""
    if not rules:
        return text
    kept = []
    for para in text.split("\n\n"):
        s = para.strip()
        hit = any((kind == "str" and pat in s) or (kind == "re" and pat.search(s))
                  for kind, pat in rules)
        if not hit:
            kept.append(para)
    return "\n\n".join(kept)


def drop_repeated(contents, min_len=1, ratio=0.3, min_hits=3):
    """跨章重複段落 = 網站宣傳/廣告樣板,自動移除。

    高可信度 UI／廣告詞出現 ≥2 章就移除；一般文字只有長句在至少 80%
    章節完全相同時才視為樣板。這可避免把「片刻後。」或小說固定地名誤刪。
    回傳 (清理後 contents, 被移除的段落 list)。章數太少(<5)不啟用。
    """
    n = len(contents)
    if n < 5:
        return contents, []

    def countable(s):
        return len(s) >= min_len and re.search(r"[一-鿿\w]", s)

    counter = Counter()
    for text in contents:
        paras = {p.strip() for p in text.split("\n\n") if countable(p.strip())}
        for p in paras:
            counter[p] += 1

    boiler = set()
    for p, c in counter.items():
        plen = len(p)
        if AUTO_BOILERPLATE_RE.search(p) and c >= 2:
            boiler.add(p)
            continue
        if plen >= 9 and c >= max(4, math.ceil(n * 0.8)):
            boiler.add(p)

    if not boiler:
        return contents, []

    cleaned = []
    for text in contents:
        kept = [p for p in text.split("\n\n") if p.strip() not in boiler]
        cleaned.append("\n\n".join(kept))
    return cleaned, sorted(boiler, key=lambda p: -counter[p])
