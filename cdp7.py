import asyncio, json, base64, urllib.request, websockets
OUT="C:/Users/uie56729/AppData/Local/Temp/edgeshot/"
async def main():
    tabs=json.load(urllib.request.urlopen("http://127.0.0.1:9338/json"))
    pg=[t for t in tabs if "127.0.0.1:8882" in t.get("url","")][0]
    async with websockets.connect(pg["webSocketDebuggerUrl"], max_size=60_000_000) as ws:
        i=[0]
        async def send(m,**p):
            i[0]+=1; await ws.send(json.dumps({"id":i[0],"method":m,"params":p}))
            while True:
                r=json.loads(await ws.recv())
                if r.get("id")==i[0]: return r
        async def js(e):
            r=await send("Runtime.evaluate",expression=e,awaitPromise=True,returnByValue=True)
            res=r.get("result",{})
            if res.get("exceptionDetails"): return {"__err__":res["exceptionDetails"]["exception"]["description"]}
            return res.get("result",{}).get("value")
        await send("Runtime.enable")
        clickfn = """(async(kw,mod)=>{
          const b=[...document.querySelectorAll('.funcbtn')].find(x=>x.textContent.includes(kw));
          b.dispatchEvent(new MouseEvent('click',{ctrlKey:mod,bubbles:true}));
          const msg=document.getElementById('funcMsg').textContent;
          await new Promise(r=>setTimeout(r,2200));
          return {提示:msg, 曲线数:state.selected.size, 曲线:[...state.selected],
                  高亮按钮:[...document.querySelectorAll('.funcbtn.on')].map(x=>x.querySelector('.fb-name').textContent),
                  事件chip:[...document.querySelectorAll('.evchip.on')].map(x=>x.textContent.trim()),
                  事件行数:document.querySelectorAll('.evrow').length};})"""
        print("① 点 HDC:      ", await js(f"({clickfn})('陡坡缓降',false)"))
        print("② 再点 ESC/TCS:", await js(f"({clickfn})('ESC-OFF',false)"))
        print("③ Ctrl+点 HDC: ", await js(f"({clickfn})('陡坡缓降',true)"))
        print("④ 手动勾一个信号后:", await js("""(async()=>{
          state.selected.add('WCBS_VehicleSpeed');state.activeFunction=null;
          renderFunctionButtons();await renderCharts();await new Promise(r=>setTimeout(r,800));
          return {高亮按钮:document.querySelectorAll('.funcbtn.on').length};})()"""))
        print("⑤ 回到 AVH:    ", await js(f"({clickfn})('AVH',false)"))
        await asyncio.sleep(2)
        r=await send("Page.captureScreenshot", format="png")
        open(OUT+"ui10.png","wb").write(base64.b64decode(r["result"]["data"])); print("saved")
asyncio.run(main())
