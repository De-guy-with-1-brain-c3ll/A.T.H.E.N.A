import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")

SHOTS = {
    "02-flowchart.html":     [7.3],
    "05-kinetic-title.html": [3.2],
}

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
    for fname, times in SHOTS.items():
        pg = b.new_page(viewport={"width": 960, "height": 540})
        pg.goto("file:///" + os.path.join(SRC, fname).replace("\\", "/"))
        for t in times:
            pg.wait_for_timeout(int(t * 1000))
            tag = ("%0.1f" % t).replace(".", "p")
            pg.screenshot(path=os.path.join(OUT, "fix_" + fname[:2] + "_t" + tag + ".png"))
        pg.close()
    b.close()
print("ok")
