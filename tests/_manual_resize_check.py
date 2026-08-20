"""Headless-Edge CDP check for the right value-panel drag-resize.

Not part of the pytest suite (needs a browser + running server); run directly:
    python tests/_manual_resize_check.py
"""
import asyncio, json, subprocess, sys, tempfile, time, urllib.request
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
APP = "http://127.0.0.1:9139"
CDP_PORT = 9222


class CDP:
    def __init__(self, ws):
        self.ws, self.i = ws, 0

    async def call(self, method, **params):
        self.i += 1
        await self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr,
                            returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    async def mouse(self, type_, x, y):
        await self.call("Input.dispatchMouseEvent", type=type_, x=x, y=y,
                        button="left", buttons=1, clickCount=1, pointerType="mouse")


async def main():
    profile = tempfile.mkdtemp()
    edge = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={profile}", "--window-size=1400,900",
         "--no-first-run", "--disable-gpu", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
            page = next((t for t in tabs if t["type"] == "page"), None)
            if page:
                ws_url = page["webSocketDebuggerUrl"]
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        print("FAIL: could not reach Edge CDP"); edge.kill(); return 1

    fails = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        c = CDP(ws)
        await c.call("Page.enable")
        await c.call("Runtime.enable")
        await c.call("Page.navigate", url=APP)
        await asyncio.sleep(4)

        errs = await c.js("(window.__errs||[]).length")
        w0 = await c.js("document.getElementById('valuePanel').offsetWidth")
        print(f"initial panel width = {w0}px")
        if w0 != 330:
            fails.append(f"expected default 330px, got {w0}")

        # handle geometry
        box = await c.js("(()=>{const r=document.getElementById('vresizer')"
                         ".getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()")
        print(f"handle rect = {box}")
        cx = box["x"] + box["w"] / 2
        cy = box["y"] + box["h"] / 2

        # cursor style on the handle
        cur = await c.js("getComputedStyle(document.getElementById('vresizer')).cursor")
        if cur != "col-resize":
            fails.append(f"handle cursor = {cur}, expected col-resize")

        # --- drag left by 200px -> panel should widen by ~200 ---
        await c.mouse("mousePressed", cx, cy)
        dragging_cls = await c.js("document.getElementById('vresizer').classList.contains('dragging')")
        body_cls = await c.js("document.body.classList.contains('vresizing')")
        if not dragging_cls:
            fails.append("handle did not get .dragging on pointerdown")
        if not body_cls:
            fails.append("body did not get .vresizing on pointerdown")
        for step in range(1, 11):
            await c.mouse("mouseMoved", cx - 20 * step, cy)
            await asyncio.sleep(0.02)
        await c.mouse("mouseReleased", cx - 200, cy)
        await asyncio.sleep(0.3)

        w1 = await c.js("document.getElementById('valuePanel').offsetWidth")
        print(f"after drag-left-200 width = {w1}px")
        if not (abs(w1 - (w0 + 200)) <= 12):
            fails.append(f"expected ~{w0+200}px after drag, got {w1}")

        if await c.js("document.getElementById('vresizer').classList.contains('dragging')"):
            fails.append(".dragging not cleared on pointerup")
        if await c.js("document.body.classList.contains('vresizing')"):
            fails.append(".vresizing not cleared on pointerup")

        stored = await c.js("localStorage.getItem('canprobe.valuePanelWidth')")
        print(f"localStorage = {stored}")
        if stored is None or abs(int(stored) - w1) > 2:
            fails.append(f"width not persisted correctly (stored={stored}, actual={w1})")

        # --- persistence across reload ---
        await c.call("Page.navigate", url=APP)
        await asyncio.sleep(4)
        w2 = await c.js("document.getElementById('valuePanel').offsetWidth")
        print(f"after reload width = {w2}px")
        if abs(w2 - w1) > 2:
            fails.append(f"width not restored after reload ({w2} vs {w1})")

        # --- max clamp: drag far past 60% of window ---
        box = await c.js("(()=>{const r=document.getElementById('vresizer')"
                         ".getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()")
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        await c.mouse("mousePressed", cx, cy)
        await c.mouse("mouseMoved", 5, cy)
        await c.mouse("mouseReleased", 5, cy)
        await asyncio.sleep(0.3)
        w3 = await c.js("document.getElementById('valuePanel').offsetWidth")
        cap = await c.js("Math.round(window.innerWidth*0.6)")
        print(f"after drag-to-edge width = {w3}px (60% cap = {cap})")
        if w3 > cap + 2:
            fails.append(f"max clamp failed: {w3} > {cap}")

        # --- min clamp: drag far right ---
        box = await c.js("(()=>{const r=document.getElementById('vresizer')"
                         ".getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()")
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        await c.mouse("mousePressed", cx, cy)
        await c.mouse("mouseMoved", await c.js("window.innerWidth") - 2, cy)
        await c.mouse("mouseReleased", await c.js("window.innerWidth") - 2, cy)
        await asyncio.sleep(0.3)
        w4 = await c.js("document.getElementById('valuePanel').offsetWidth")
        print(f"after drag-to-right width = {w4}px (min = 220)")
        if w4 < 218:
            fails.append(f"min clamp failed: {w4} < 220")

        # --- double-click resets to default ---
        box = await c.js("(()=>{const r=document.getElementById('vresizer')"
                         ".getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()")
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        for _ in range(2):
            await c.mouse("mousePressed", cx, cy)
            await c.mouse("mouseReleased", cx, cy)
        await c.call("Input.dispatchMouseEvent", type="mousePressed", x=cx, y=cy,
                     button="left", buttons=1, clickCount=2, pointerType="mouse")
        await c.call("Input.dispatchMouseEvent", type="mouseReleased", x=cx, y=cy,
                     button="left", buttons=1, clickCount=2, pointerType="mouse")
        await asyncio.sleep(0.3)
        w5 = await c.js("document.getElementById('valuePanel').offsetWidth")
        print(f"after dblclick width = {w5}px (default 330)")
        if w5 != 330:
            fails.append(f"dblclick reset failed: {w5} != 330")

        # --- chart still alive & sized, no JS errors ---
        chart_w = await c.js("document.querySelector('#chart canvas') ? "
                             "document.querySelector('#chart canvas').clientWidth : -1")
        print(f"chart canvas width = {chart_w}px")
        if chart_w <= 0:
            fails.append("chart canvas missing or zero-width after resizes")

    edge.kill()
    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


sys.exit(asyncio.run(main()))
