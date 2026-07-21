"""小說下載器 CLI:輸入目錄頁網址,下載全書、過濾廣告、合併成單一 TXT。

用法:
    python novel_dl.py <目錄頁網址> [--out 輸出資料夾] [--delay 2.0] [--start N] [--end N]

範例:
    python novel_dl.py https://www.69shuba.com/book/67964.htm
    python novel_dl.py https://www.69shuba.com/book/67964.htm --start 100 --end 200
"""
import argparse
import sys
from pathlib import Path

from downloader_task import download_novel

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent  # E:\AI gravity project\


def main():
    parser = argparse.ArgumentParser(description="小說下載器(自動過濾廣告)")
    parser.add_argument("url", help="小說目錄頁或簡介頁網址")
    parser.add_argument("--out", help="輸出資料夾(預設: 上層目錄)")
    parser.add_argument("--delay", type=float, default=2.0, help="章節間延遲秒數(預設 2.0,被擋時自動加倍)")
    parser.add_argument("--start", type=int, help="起始章(1-based,含)")
    parser.add_argument("--end", type=int, help="結束章(含)")
    parser.add_argument("--limit", type=int, help="只下載前 N 章(測試用,等同 --end N)")
    parser.add_argument("--title", help="覆寫書名(輸出檔名也會用它)")
    args = parser.parse_args()

    end = args.end
    if args.limit and not end:
        end = (args.start or 1) + args.limit - 1

    def cb(stage, current, total, msg):
        if stage == "chapter":
            print(f"\r{msg}", end="", flush=True)
        else:
            print(("\n" if stage == "done" else "") + msg)

    download_novel(
        args.url,
        Path(args.out) if args.out else DEFAULT_OUT_DIR,
        title_override=args.title or "",
        delay=args.delay,
        callback=cb,
        start=args.start,
        end=end,
    )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        main()
    except KeyboardInterrupt:
        print("\n已中斷。重跑同一指令會從快取續傳,不會重抓已完成的章節。")
        sys.exit(1)
