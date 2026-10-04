import os, sys
sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.abspath(__file__))
from playwright.sync_api import sync_playwright
SHELL = r"C:\Users\Benjamin\AppData\Local\ms-playwright\chromium_headless_shell-1243\chrome-headless-shell-win64\chrome-headless-shell.exe"
url = "file:///" + os.path.join(BASE, "03-stat-counters.html").replace("\\", "/")
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, executable_path=SHELL, args=["--run-all-compositor-stages-before-draw"])
    pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    pg.goto(url, wait_until="load")
    pg.wait_for_timeout(400)
    pg.evaluate("window.go(5)")
    pg.wait_for_timeout(4200)
    pg.screenshot(path=os.path.join(BASE, "_verify", "green03_full.png"))
    pg.evaluate("window.cam(1)")
    pg.wait_for_timeout(2200)
    pg.screenshot(path=os.path.join(BASE, "_verify", "green03_zoom.png"))
    b.close()
print("shots done")
