import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")
log = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("file:///" + os.path.join(SRC, "08-latency-bars.html").replace("\\", "/"))
    pg.wait_for_timeout(500)

    def state():
        return pg.evaluate("({t:document.getElementById('stage').style.transform,s:step})")

    pg.mouse.click(960, 540, button="left")
    pg.wait_for_timeout(1500)
    log.append("L1: " + str(state()))
    pg.mouse.click(960, 540, button="left")
    pg.wait_for_timeout(1500)
    log.append("L2: " + str(state()))
    pg.mouse.click(960, 540, button="left")
    pg.wait_for_timeout(1500)
    log.append("L3: " + str(state()))
    pg.mouse.click(960, 540, button="right")
    pg.wait_for_timeout(1500)
    log.append("R-click zoom out: " + str(state()))
    pg.mouse.click(960, 540, button="left")
    pg.wait_for_timeout(1500)
    log.append("L after zoom out: " + str(state()))
    pg.keyboard.press("r")
    pg.wait_for_timeout(1400)
    log.append("R-key reset: " + str(state()))
    log.append("errors: " + (" | ".join(errs) if errs else "none"))
    b.close()

open(os.path.join(OUT, "_camtest2.txt"), "w").write("\n".join(log))
