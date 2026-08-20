"""手工验收：用 Playwright 真开一次界面，把优化后仍要照常工作的交互跑一遍。

不是单元测试（要一个跑着的服务 + 已装好的 DBC/日志/规格），放在 tests/ 下和
其他 _manual_* 脚本作伴。用法::

    python run.py --port 8777 --no-browser &
    curl -F file=@samples/xxx.dbc  http://127.0.0.1:8777/api/upload/dbc
    curl -F file=@samples/xxx.blf  http://127.0.0.1:8777/api/upload/log
    curl -F file=@samples/xxx.yaml http://127.0.0.1:8777/api/upload/spec
    python tests/_manual_ui_check.py http://127.0.0.1:8777 /tmp/shots
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
SHOTS = sys.argv[2] if len(sys.argv) > 2 else "."

errors: list[str] = []
ok: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (ok if cond else errors).append(f"{name}{(' — ' + detail) if detail else ''}")
    print(("  OK   " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1680, "height": 1000})
    console: list[str] = []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

    print("== 打开页面 ==")
    # 不等 networkidle：日志一装好分析就是惰性跑的，几百万帧下 /api/events 会挂着
    # 好几分钟，网络永远闲不下来 —— 这正是要验证的"加载不再被分析拖住"
    page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("#signalTree .tree-sig", timeout=120_000)
    page.wait_for_timeout(1500)
    page.screenshot(path=f"{SHOTS}/01-loaded.png")

    status = page.inner_text("#status")
    check("顶栏显示 DBC / 日志 / 规格", "4987907" in status.replace(",", "") or "帧" in status, status[:110])
    check("信号树已渲染", page.locator("#signalTree .tree-sig").count() > 100,
          f"{page.locator('#signalTree .tree-sig').count()} 行")
    check("功能按钮已渲染", page.locator("#functionButtons .funcbtn").count() > 0,
          f"{page.locator('#functionButtons .funcbtn').count()} 个")

    print("== 搜索过滤（事件委托 + 防抖） ==")
    page.fill("#signalSearch", "WCBS")
    page.wait_for_timeout(400)
    n_filtered = page.locator("#signalTree .tree-sig").count()
    n_all = page.evaluate("state.signals.length")
    # 报文名命中时该报文下所有信号都留下（与优化前的过滤谓词一致），所以只断言"变少了"
    check("搜索后信号树收窄", 0 < n_filtered < n_all, f"{n_filtered}/{n_all} 行")
    page.fill("#signalSearch", "")
    page.wait_for_timeout(400)

    print("== 勾选信号（委托的 change 事件） ==")
    row = page.locator("#signalTree .tree-sig").nth(3)
    sig_name = row.get_attribute("data-sig")
    row.locator("input").check()
    page.wait_for_timeout(2500)
    check("勾选后出现曲线", page.locator("#chart canvas").count() > 0, f"信号 {sig_name}")
    check("勾选后信号值面板有行", page.locator("#valueList .vrow").count() > 0)

    print("== 功能按钮：一次加载整条链路 ==")
    btn = page.locator("#functionButtons .funcbtn:not([disabled])").first
    fname = btn.inner_text().split("\n")[0]
    t0 = time.time()
    btn.click()
    page.wait_for_timeout(4000)
    n_sel = page.locator("#signalTree .tree-sig input:checked").count()
    check("功能按钮勾上了该功能的信号", n_sel > 1, f"{fname} → {n_sel} 条，{time.time()-t0:.1f}s")
    page.screenshot(path=f"{SHOTS}/02-function.png")

    print("== 光标叠加层 ==")
    box = page.locator("#chart").bounding_box()
    # 拖 seek 滑块：最确定的一条移动光标的路径
    page.evaluate("()=>{const s=$('seek'); s.value=400; s.dispatchEvent(new Event('input'));}")
    page.wait_for_timeout(800)
    cur = page.locator("#cursorLine")
    xform = cur.evaluate("el => el.style.transform")
    check("光标线可见且已定位", cur.is_visible() and "translateX" in (xform or ""),
          f"{xform} @ {page.inner_text('#timeLabel')}")
    check("拖动 seek 后时间标签跟着走", page.inner_text("#timeLabel") != "0.000 s",
          page.inner_text("#timeLabel"))

    # 点曲线把光标打到那一刻：ECharts series click → setCursor。沿纵向扫一遍
    # 保证一定压到线上（曲线在条带里的高度事先不知道）
    page.evaluate("window.__hits=[]; state.chart.on('click', q=>window.__hits.push(q.componentType))")
    before = page.evaluate("state.t")
    for dy in range(10, 86, 3):
        page.mouse.click(box["x"] + 700, box["y"] + dy)
    page.wait_for_timeout(700)
    check("点曲线能命中 series", page.evaluate("window.__hits").count("series") > 0,
          f"{page.evaluate('window.__hits')}")
    check("点曲线后光标移动了", page.evaluate("state.t") != before,
          f"{before:.3f} → {page.evaluate('state.t'):.3f}")
    check("光标叠加层跟着重定位", "translateX" in (cur.evaluate("el => el.style.transform") or ""))

    print("== X / Y 缩放 ==")
    page.click("#btnXIn"); page.wait_for_timeout(500)
    page.click("#btnXIn"); page.wait_for_timeout(700)
    zoomed = page.evaluate("[state.t0, state.t1]")
    check("X 放大后窗口变窄", zoomed[1] - zoomed[0] < page.evaluate("state.status.end - state.status.start"),
          f"{zoomed[1]-zoomed[0]:.1f}s")
    page.click("#btnYIn"); page.wait_for_timeout(400)
    page.screenshot(path=f"{SHOTS}/03-zoom.png")
    page.click("#btnXOut"); page.wait_for_timeout(500)
    page.click("#btnZoomReset"); page.wait_for_timeout(900)
    full = page.evaluate("[state.t0, state.t1, state.status.start, state.status.end]")
    check("复位回到全量范围", abs(full[1] - full[0] - (full[3] - full[2])) < 1e-6,
          f"{full[1]-full[0]:.1f}s vs {full[3]-full[2]:.1f}s")

    print("== 关键事件面板 ==")
    page.wait_for_selector("#evList .evrow, #evList .ev-empty", timeout=900_000)
    n_ev = page.locator("#evList .evrow").count()
    check("事件面板出结果", n_ev >= 0, f"{n_ev} 行 / {page.inner_text('#evCount')}")
    if n_ev:
        page.locator("#evList .evrow").nth(min(2, n_ev - 1)).click()   # 委托的 click
        page.wait_for_timeout(800)
        check("点事件后有行被高亮", page.locator("#evList .evrow.active").count() == 1)
        check("ribbon 有刻度", page.locator("#evRibbonTrack .ev-tick").count() > 0,
              f"{page.locator('#evRibbonTrack .ev-tick').count()} 个")

    print("== 快速求值开关 ==")
    page.click("#evFast")
    page.wait_for_selector("#evList .evrow, #evList .ev-empty", timeout=600_000)
    page.wait_for_timeout(1200)
    check("快速求值可切换", "on" in (page.locator("#evFast").get_attribute("class") or ""),
          page.inner_text("#evCount"))
    page.screenshot(path=f"{SHOTS}/04-events.png")
    page.click("#evFast")
    page.wait_for_timeout(1500)

    print("== 回放 ==")
    page.click("#btnPlay")
    page.wait_for_timeout(2500)
    moving = page.evaluate("state.t")
    page.click("#btnPlay")
    page.wait_for_timeout(400)
    check("播放推进了光标", moving > page.evaluate("state.status.start"), f"t={moving:.3f}")
    page.screenshot(path=f"{SHOTS}/05-play.png")

    print("== 通信诊断 ==")
    page.click("#btnDiag")
    page.wait_for_selector("#diagBody .diag-section, #diagBody .diag-error", timeout=600_000)
    check("诊断弹窗出内容", page.locator("#diagBody .diag-section").count() > 0)
    page.screenshot(path=f"{SHOTS}/06-diag.png")
    page.click("#diagClose")

    print("\n== 控制台 ==")
    for c in console[:25]:
        print("  " + c)
    check("无 JS 报错", not [c for c in console if c.startswith(("error", "pageerror"))],
          f"{len(console)} 条 console 消息")

    browser.close()

print(f"\n{len(ok)} 项通过, {len(errors)} 项失败")
for e in errors:
    print("  FAILED: " + e)
sys.exit(1 if errors else 0)
