"""Headless-Edge CDP check for row spacing + X/Y zoom buttons.

Not collected by pytest (needs a browser + running server); run directly:
    python tests/_manual_zoom_check.py
"""
import asyncio, json, subprocess, sys, tempfile, time, urllib.request
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
APP = "http://127.0.0.1:9140"
CDP_PORT = 9223


class CDP:
    def __init__(self, ws):
        self.ws, self.i = ws, 0

    async def call(self, method, **params):
        self.i += 1
        await self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=f"(()=>{{{expr}}})()",
                            returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    async def click(self, sel):
        box = await self.js(f"const e=document.querySelector('{sel}');"
                            "const r=e.getBoundingClientRect();"
                            "return {x:r.x+r.width/2,y:r.y+r.height/2,d:e.disabled};")
        if box["d"]:
            return False
        for t in ("mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", type=t, x=box["x"], y=box["y"],
                            button="left", buttons=1, clickCount=1, pointerType="mouse")
        await asyncio.sleep(0.35)
        return True


async def main():
    prof = tempfile.mkdtemp()
    edge = subprocess.Popen(
        [EDGE, "--headless=new", f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={prof}", "--window-size=1500,950",
         "--no-first-run", "--disable-gpu", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list"))
            p = next((t for t in tabs if t["type"] == "page"), None)
            if p:
                ws_url = p["webSocketDebuggerUrl"]; break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        print("FAIL: no CDP"); edge.kill(); return 1

    fails = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        c = CDP(ws)
        await c.call("Page.enable"); await c.call("Runtime.enable")
        await c.call("Page.navigate", url=APP)
        await asyncio.sleep(4)
        # 必须在导航之后装，否则会被新文档冲掉
        await c.js("window.__errs=[];"
                   "window.addEventListener('error',e=>window.__errs.push(''+e.message));"
                   "window.addEventListener('unhandledrejection',e=>window.__errs.push('rej:'+e.reason));"
                   "return 1")

        # pick 3 signals so we have real strips
        n = await c.js("""
          const names=state.signals.slice(0,3).map(s=>s.name);
          names.forEach(x=>state.selected.add(x));
          return renderCharts().then(()=>state.selected.size);
        """)
        await asyncio.sleep(1.2)
        print(f"selected signals = {n}")

        async def geom():
            return await c.js("""
              const o=state.chart.getOption();
              return {h:parseInt(document.getElementById('chart').style.height),
                      yz:state.yZoom,
                      gh:o.grid.map(g=>g.height), gt:o.grid.map(g=>g.top),
                      t0:state.t0, t1:state.t1};
            """)

        g0 = await geom()
        print(f"base: chart h={g0['h']}px strip heights={g0['gh']} tops={g0['gt']}")
        if g0["gh"][0] != 76:
            fails.append(f"base strip height {g0['gh'][0]} != 76")
        gap = g0["gt"][1] - g0["gt"][0] - g0["gh"][0]
        print(f"base gap = {gap}px")
        if gap != 12:
            fails.append(f"base gap {gap} != 12")

        # --- Y+ ---
        if not await c.click("#btnYIn"):
            fails.append("#btnYIn disabled at base")
        g1 = await geom()
        print(f"after Y+: yZoom={g1['yz']:.3f} strip h={g1['gh'][0]} chart h={g1['h']}")
        if not (g1["gh"][0] > g0["gh"][0] and g1["h"] > g0["h"]):
            fails.append("Y+ did not increase strip/chart height")

        # --- Y- back ---
        await c.click("#btnYOut")
        g2 = await geom()
        print(f"after Y-: yZoom={g2['yz']:.3f} strip h={g2['gh'][0]}")
        if abs(g2["yz"] - 1) > 1e-6:
            fails.append(f"Y- did not return to 1.0 (got {g2['yz']})")

        # --- Y max clamp ---
        for _ in range(12):
            if not await c.click("#btnYIn"):
                break
        g3 = await geom()
        print(f"Y max: yZoom={g3['yz']:.3f} (cap 4) btnYIn disabled={await c.js(chr(114)+'eturn document.getElementById(\"btnYIn\").disabled')}")
        if g3["yz"] > 4 + 1e-6:
            fails.append(f"yZoom exceeded max: {g3['yz']}")
        if not await c.js('return document.getElementById("btnYIn").disabled'):
            fails.append("btnYIn not disabled at Y max")

        await c.click("#btnZoomReset")
        g4 = await geom()
        print(f"after reset: yZoom={g4['yz']} span={g4['t1']-g4['t0']:.3f}")
        if abs(g4["yz"] - 1) > 1e-6:
            fails.append("reset did not restore yZoom=1")

        # --- X zoom, anchored on cursor ---
        full = await c.js("return state.status.end - state.status.start")
        await c.js("setCursor(state.status.start + (state.status.end-state.status.start)*0.5); return 1")
        await asyncio.sleep(0.4)
        s0 = await geom()
        await c.click("#btnXIn")
        s1 = await geom()
        span0, span1 = s0["t1"] - s0["t0"], s1["t1"] - s1["t0"]
        print(f"X+: span {span0:.3f} -> {span1:.3f}s (full {full:.3f})")
        if not (span1 < span0 * 0.95):
            fails.append(f"X+ did not narrow span ({span0}->{span1})")
        # cursor must stay inside the new window
        tcur = await c.js("return state.t")
        if not (s1["t0"] - 1e-6 <= tcur <= s1["t1"] + 1e-6):
            fails.append(f"cursor {tcur} fell outside window after X+ [{s1['t0']},{s1['t1']}]")

        # dataZoom actually applied to the chart, not just state
        dz = await c.js("const o=state.chart.getOption();"
                        "return {sv:o.dataZoom[0].startValue, ev:o.dataZoom[0].endValue};")
        print(f"chart dataZoom = {dz}")
        if dz["sv"] is None or abs(dz["sv"] - s1["t0"]) > max(0.05, span1 * 0.02):
            fails.append(f"chart dataZoom not synced with state ({dz} vs {s1['t0']})")

        await c.click("#btnXOut")
        s2 = await geom()
        print(f"X-: span -> {s2['t1']-s2['t0']:.3f}s")
        if not (s2["t1"] - s2["t0"] > span1 * 1.05):
            fails.append("X- did not widen span")

        # --- X out clamps at full range and disables ---
        for _ in range(15):
            if not await c.click("#btnXOut"):
                break
        s3 = await geom()
        print(f"X full: span={s3['t1']-s3['t0']:.3f} (full {full:.3f}) "
              f"btnXOut disabled={await c.js(chr(114)+'eturn document.getElementById(\"btnXOut\").disabled')}")
        if s3["t1"] - s3["t0"] > full + 1e-6:
            fails.append("span exceeded full range")

        # --- Y zoom must not refetch series (uses cache) ---
        before = await c.js("window.__fetches=0;const of=window.fetch;"
                            "window.fetch=function(){window.__fetches++;return of.apply(this,arguments)};"
                            "return 0;")
        await c.click("#btnYIn")
        nf = await c.js("return window.__fetches")
        print(f"network calls during Y zoom = {nf}")
        if nf != 0:
            fails.append(f"Y zoom triggered {nf} fetch(es); should reuse cache")

        errs = await c.js("return window.__errs")
        print(f"JS errors = {errs}")
        if errs:
            fails.append(f"JS errors: {errs}")

        cw = await c.js("const cv=document.querySelector('#chart canvas');return cv?cv.clientWidth:-1")
        if cw <= 0:
            fails.append("chart canvas gone after zooming")

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
