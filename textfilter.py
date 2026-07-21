"""正文後處理:跨章重複樣板自動偵測 + 使用者自訂過濾規則。

兩層都在「合併輸出」階段執行(不動每章快取),所以改規則後重跑同一本書
會直接用快取重新過濾,不需要重新下載。
"""
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_RULES_HEADER = """\
# 自訂過濾規則:每行一條,符合的整段會從輸出移除
# - 直接寫文字     = 段落「包含」該文字就移除,例如: 一秒記住本站
# - re: 開頭       = 正則表達式,例如: re:^第\\d+章$
# - # 開頭是註解,空行忽略
# 儲存後,下一次下載(或重跑同一本書)生效
"""


def rules_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "filter_rules.txt"
    return Path(__file__).parent / "filter_rules.txt"


def ensure_rules_file() -> Path:
    p = rules_path()
    if not p.exists():
        p.write_text(DEFAULT_RULES_HEADER, encoding="utf-8")
    return p


def load_rules():
    """回傳 [(kind, pattern)];kind 為 'str'(包含比對) 或 're'(正則)。"""
    p = rules_path()
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


def drop_repeated(contents, min_len=5, ratio=0.3, min_hits=3):
    """跨章重複段落 = 網站宣傳/廣告樣板,自動移除。

    同一段文字(≥min_len 字、含中英文字,純符號分隔線不算)出現在
    ≥max(min_hits, 章數*ratio) 個章節就視為樣板。
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

    threshold = max(min_hits, int(n * ratio))
    boiler = {p for p, c in counter.items() if c >= threshold}
    if not boiler:
        return contents, []

    cleaned = []
    for text in contents:
        kept = [p for p in text.split("\n\n") if p.strip() not in boiler]
        cleaned.append("\n\n".join(kept))
    return cleaned, sorted(boiler, key=lambda p: -counter[p])
