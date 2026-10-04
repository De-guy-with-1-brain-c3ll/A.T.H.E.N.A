import os, base64, time

SHELL = r"C:\Users\Benjamin\AppData\Local\ms-playwright\chromium_headless_shell-1243\chrome-headless-shell-win64\chrome-headless-shell.exe"
TMP = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion\_rec_tmp"

CASES = {
    "bare": [],
    "racsbd": ["--run-all-compositor-stages-before-draw"],
    "noflags_threaded": ["--disable-threaded-compositing"],
    "anim": ["--disable-threaded-animation", "--disable-threaded-compositing"],
}

from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    for name, flags in CASES.items():
        try:
            b = p.chromium.launch(headless=True, executable_path=SHELL, args=flags)
            pg = b.new_page(viewport={"width": 960, "height": 540})
            pg.goto("file:///" + r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion\05-kinetic-title.html".replace("\\", "/"))
            pg.wait_for_timeout(300)
            cl = pg.context.new_cdp_session(pg)
            r = cl.send("Page.captureScreenshot", {"format": "jpeg", "quality": 70})
            print(name, "OK", len(r["data"]), flush=True)
            b.close()
        except Exception as e:
            print(name, "FAIL", str(e).splitlines()[-1][:120], flush=True)
            try:
                b.close()
            except Exception:
                pass
