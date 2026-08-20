"""Headless-Edge CDP check for the sidebar function trigger buttons.

Not collected by pytest (needs a browser + running server); run directly:
    python tests/_manual_funcbtn_check.py
"""
import asyncio, json, subprocess, sys, tempfile, time, urllib.request
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
APP = "http://127.0.0.1:9141"
CDP_PORT = 9224


class CDP:
    def __init__(self, ws):
        self.ws, self.i = ws, 0

    async def call(self, method, **p):
        self.i += 1
        await self.ws.send(json.dumps({"id": self.i, "method": method, "params": p}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=f"(async()=>{{{expr}}})()",
                            returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    async def click_nth(self, sel, n):
        box = await self.js(f"const e=document.querySelectorAll('{sel}')[{n}];"
                            "if(!e)return null;const r=e.getBoundingClientRect();"
                            "return {x:r.x+r.width/2,y:r.y+r.height/2,d:e.disabled};")
        if not box or box["d"]:
            return False
        for t in ("mousePressed", "mouseReleased"):
            await self.call("Input.dispatchMouseEvent", type=t, x=box["x"], y=box["y"],
                            button="left", buttons=1, clickCount=1, pointerType="mouse")
        await asyncio.sleep(0.6)
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
        await asyncio.sleep(4.5)
        await c.js("window.__errs=[];"
                   "window.addEventListener('error',e=>window.__errs.push(''+e.message));"
                   "window.addEventListener('unhandledrejection',e=>window.__errs.push('rej:'+e.reason));"
                   "return 1")

        n = await c.js("return document.querySelectorAll('.funcbtn').length")
        labels = await c.js("return [...document.querySelectorAll('.funcbtn')]"
                            ".map(b=>b.textContent.trim())")
        print(f"function buttons = {n} -> {labels}")
        if n != 2:
            fails.append(f"expected 2 function buttons from sample spec, got {n}")

        sel0 = await c.js("return [...state.selected]")
        print(f"selection before = {sel0}")

        # --- click first function (cruise: 4 signals) ---
        if not await c.click_nth(".funcbtn", 0):
            fails.append("first function button not clickable")
        sel1 = await c.js("return [...state.selected]")
        msg1 = await c.js("return document.getElementById('funcMsg').textContent")
        print(f"after click #1 = {sorted(sel1)}  msg={msg1!r}")
        expect1 = {"BrakePedal", "CruiseCancelBtn", "CruiseSetBtn", "VehSpd"}
        if set(sel1) != expect1:
            fails.append(f"cruise signals wrong: {sorted(sel1)} != {sorted(expect1)}")

        # chart actually got those strips
        grids = await c.js("return state.chart.getOption().grid.length")
        print(f"chart strips = {grids}")
        if grids != len(sel1):
            fails.append(f"chart has {grids} strips for {len(sel1)} signals")

        # tree checkboxes reflect the selection
        checked = await c.js("return [...document.querySelectorAll('.tree-sig input')]"
                             ".filter(i=>i.checked).length")
        print(f"checked boxes in signal tree = {checked}")
        if checked != len(sel1):
            fails.append(f"signal tree shows {checked} checked, expected {len(sel1)}")

        # --- click again: append semantics, nothing added, no duplication ---
        await c.click_nth(".funcbtn", 0)
        sel2 = await c.js("return [...state.selected]")
        msg2 = await c.js("return document.getElementById('funcMsg').textContent")
        print(f"after re-click #1 = {sorted(sel2)}  msg={msg2!r}")
        if set(sel2) != expect1:
            fails.append("re-click changed the selection")
        if "已在图中" not in (msg2 or ""):
            fails.append(f"expected '已在图中' feedback on re-click, got {msg2!r}")

        # --- click second function: must APPEND, not replace ---
        await c.click_nth(".funcbtn", 1)
        sel3 = await c.js("return [...state.selected]")
        msg3 = await c.js("return document.getElementById('funcMsg').textContent")
        print(f"after click #2 = {sorted(sel3)}  msg={msg3!r}")
        if not expect1.issubset(set(sel3)):
            fails.append(f"append semantics broken — first function's signals lost: {sorted(sel3)}")
        if "MotorTemp" not in sel3:
            fails.append("overtemp signal MotorTemp not added")
        if len(sel3) != 5:
            fails.append(f"expected 5 signals after appending, got {len(sel3)}")

        grids = await c.js("return state.chart.getOption().grid.length")
        print(f"chart strips after append = {grids}")
        if grids != 5:
            fails.append(f"chart strips {grids} != 5")

        # --- missing-signal path: spec referencing signals absent from the DBC ---
        bad = ("functions:\n"
               "  - id: ghost\n"
               "    name: \"不存在的功能\"\n"
               "    enter: {signal: NoSuchSignalXYZ, op: \">\", value: 1}\n")
        await c.js(f"""
          await fetch('/api/spec',{{method:'POST',headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{text:{json.dumps(bad)}}})}});
          await refreshFunctions();
          return 1;
        """)
        await asyncio.sleep(0.5)
        info = await c.js("const b=document.querySelector('.funcbtn');"
                          "return {n:document.querySelectorAll('.funcbtn').length,"
                          "txt:b?b.textContent.trim():null, dis:b?b.disabled:null,"
                          "title:b?b.title:null};")
        print(f"ghost-spec button = {info}")
        if info["n"] != 1 or not info["dis"]:
            fails.append(f"button for all-missing function should be disabled: {info}")
        if "NoSuchSignalXYZ" not in (info["title"] or ""):
            fails.append("tooltip should name the missing signal")

        # --- collapse / expand ---
        async def panel():
            return await c.js(
                "const p=document.getElementById('funcPanel'),"
                "b=document.getElementById('functionButtons'),"
                "t=document.getElementById('funcToggle');"
                "return {collapsed:p.classList.contains('collapsed'),"
                " visible:b.offsetHeight>0,"
                " aria:t.getAttribute('aria-expanded'),"
                " ls:localStorage.getItem('canprobe.funcCollapsed')};")

        p0 = await panel()
        print(f"panel initial = {p0}")
        if p0["collapsed"] or not p0["visible"]:
            fails.append(f"panel should start expanded: {p0}")

        await c.click_nth("#funcToggle", 0)
        p1 = await panel()
        print(f"after toggle   = {p1}")
        if not p1["collapsed"] or p1["visible"]:
            fails.append(f"toggle did not collapse the list: {p1}")
        if p1["aria"] != "false":
            fails.append(f"aria-expanded should be false when collapsed: {p1['aria']}")
        if p1["ls"] != "1":
            fails.append(f"collapsed state not persisted: {p1['ls']}")

        # 折叠后标题栏仍在，计数仍可见（不然不知道有几个功能）
        head = await c.js("const t=document.getElementById('funcToggle');"
                          "return {h:t.offsetHeight>0, txt:t.textContent.trim()};")
        print(f"header while collapsed = {head}")
        if not head["h"]:
            fails.append("header disappeared when collapsed")

        await c.call("Page.navigate", url=APP)
        await asyncio.sleep(4.5)
        p2 = await panel()
        print(f"after reload   = {p2}")
        if not p2["collapsed"] or p2["visible"]:
            fails.append(f"collapsed state not restored after reload: {p2}")

        await c.js("window.__errs=[];"
                   "window.addEventListener('error',e=>window.__errs.push(''+e.message));return 1")
        await c.click_nth("#funcToggle", 0)
        p3 = await panel()
        print(f"after re-expand= {p3}")
        if p3["collapsed"] or not p3["visible"]:
            fails.append(f"could not expand again: {p3}")

        errs = await c.js("return window.__errs")
        print(f"JS errors = {errs}")
        if errs:
            fails.append(f"JS errors: {errs}")

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
