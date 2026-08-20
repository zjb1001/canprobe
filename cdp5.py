import asyncio, json, base64, urllib.request, websockets
OUT="C:/Users/uie56729/AppData/Local/Temp/edgeshot/"
async def main():
    tabs=json.load(urllib.request.urlopen("http://127.0.0.1:9337/json"))
    pg=[t for t in tabs if "127.0.0.1:8881" in t.get("url","")][0]
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
        print("功能按钮:", await js("[...document.querySelectorAll('.funcbtn')].map(b=>b.textContent)"))
        # 点 HDC 全链路按钮 —— 应把整组 8 个信号按顺序加进 Graphics
        print("点 HDC:", await js("""(async()=>{
          const b=[...document.querySelectorAll('.funcbtn')].find(x=>x.textContent.includes('HDC'));
          b.click(); await new Promise(r=>setTimeout(r,2500));
          return {加入信号: [...state.selected], 提示: document.getElementById('funcMsg').textContent};})()"""))
        await asyncio.sleep(2)
        # 事件栏筛到 hdc，跳到 31.911 那条
        print("跳事件:", await js("""(async()=>{
          state.evFuncs.add('hdc'); await refreshEvents(); await new Promise(r=>setTimeout(r,600));
          const rows=[...document.querySelectorAll('.evrow')];
          const r=rows.find(x=>x.querySelector('.ev-t').textContent.startsWith('31.9'));
          r.click();
          return {行数:rows.length, 光标:document.getElementById('timeLabel').textContent,
                  摘要:r.querySelector('.ev-why').textContent.slice(0,60)};})()"""))
        await asyncio.sleep(1.5)
        r=await send("Page.captureScreenshot", format="png")
        open(OUT+"ui8.png","wb").write(base64.b64decode(r["result"]["data"])); print("saved")
asyncio.run(main())
