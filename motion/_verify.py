import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")
os.makedirs(OUT, exist_ok=True)

DECKS = {
    "01-architecture-diagram.html": 11,
    "02-flowchart.html": 10,
    "03-stat-counters.html": 5,
    "04-terminal.html": 9,
    "05-kinetic-title.html": 5,
    "06-snags.html": 4,
    "07-tools.html": 4,
    "08-latency-bars.html": 5,
}

report = []

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
    for fname, total in DECKS.items():
        pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        msgs = []

        def on_console(m):
            if m.type == "error":
                msgs.append("console: " + m.text)

        pg.on("console", on_console)
        pg.on("pageerror", lambda e: msgs.append("PAGEERROR: " + str(e)))
        pg.goto("file:///" + os.path.join(SRC, fname).replace("\\", "/"))
        pg.wait_for_timeout(400)
        base = fname[:2]
        for n in range(1, total + 1):
            pg.evaluate("go(%d)" % n)
            pg.wait_for_timeout(320)
        pg.wait_for_timeout(4400)
        pg.screenshot(path=os.path.join(OUT, base + "_final.png"))
        pg.evaluate("cam(1)")
        pg.wait_for_timeout(1600)
        pg.screenshot(path=os.path.join(OUT, base + "_zoom.png"))
        pg.evaluate("cam(0)")
        pg.wait_for_timeout(1500)
        pg.evaluate("go(0)")
        pg.wait_for_timeout(300)
        mid = max(1, total // 2)
        pg.evaluate("go(%d)" % mid)
        pg.wait_for_timeout(2700)
        pg.screenshot(path=os.path.join(OUT, base + "_mid%02d.png" % mid))
        report.append("%s total=%d %s" % (fname, total, ("ERR: " + " | ".join(msgs)) if msgs else "ok"))
        pg.close()
    b.close()

open(os.path.join(OUT, "_console.txt"), "w", encoding="utf-8").write("\n".join(report))
print("\n".join(report))
