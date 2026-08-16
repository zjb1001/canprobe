#!/usr/bin/env python3
"""启动 CanProbe —— CAN 总线回放与信号分析工具。

用法:
    python3 run.py                 # 默认 127.0.0.1:8000，并尝试打开浏览器
    python3 run.py --port 9000     # 指定端口
    python3 run.py --no-browser    # 不自动打开浏览器
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="CanProbe — CAN 总线回放与信号分析")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"→ 打开浏览器访问 {url}  (Ctrl+C 退出)")
    uvicorn.run("canprobe.main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
