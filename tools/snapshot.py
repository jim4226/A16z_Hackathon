"""Render a robot GLB to PNG(s) headlessly, for checking geometry by eye.

  python tools/snapshot.py path/to/robot.glb out.png [--views "35,20;-60,15;180,10"] [--size 1200x900]

Serves the repo over a throwaway local HTTP server and screenshots
tools/glb_snapshot.html with Playwright (Chromium at /opt/pw-browsers or the
Playwright default). One PNG per view: out.png, out_2.png, ...
"""

import argparse
import asyncio
import http.server
import os
import shutil
import socketserver
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def shoot(url_base, glb_rel, views, size, out):
    from playwright.async_api import async_playwright

    exe = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=exe if os.path.exists(exe) else None,
                                    args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader"])
        w, h = map(int, size.split("x"))
        pg = await b.new_page(viewport={"width": w, "height": h})
        outs = []
        for i, v in enumerate(views):
            az, el = v.split(",")
            await pg.goto(f"{url_base}/tools/glb_snapshot.html?glb=/{glb_rel}&az={az}&el={el}")
            await pg.wait_for_function("document.title === 'ready' || document.title.startsWith('error')", timeout=60000)
            if (await pg.title()).startswith("error"):
                raise SystemExit(await pg.title())
            path = out if i == 0 else out.replace(".png", f"_{i + 1}.png")
            await pg.screenshot(path=path)
            outs.append(path)
        await b.close()
        return outs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("glb")
    ap.add_argument("out")
    ap.add_argument("--views", default="35,20", help="semicolon-separated az,el pairs in degrees")
    ap.add_argument("--size", default="1200x900")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(dir=ROOT, prefix=".snap_")
    try:
        shutil.copy(a.glb, os.path.join(tmp, "model.glb"))
        # serve /static from the studio static dir and /tools from tools/
        class H(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                path = path.split("?")[0]
                if path.startswith("/static/"):
                    return os.path.join(ROOT, "morph", "studio", path.lstrip("/"))
                return os.path.join(ROOT, path.lstrip("/"))

            def log_message(self, *x):
                pass
        H.extensions_map[".js"] = "text/javascript"
        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        rel = os.path.relpath(os.path.join(tmp, "model.glb"), ROOT)
        print("\n".join(asyncio.run(shoot(f"http://127.0.0.1:{port}", rel, a.views.split(";"), a.size, a.out))))
        srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
