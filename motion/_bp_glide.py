import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
    pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    pg.goto("file:///" + os.path.join(SRC, "05-kinetic-title.html").replace("\\", "/"))
    pg.wait_for_timeout(2000)

    def dots():
        return pg.evaluate("""() => {
            const p = [];
            document.querySelectorAll('.gdot').forEach(c => {
                const b = c.getBoundingClientRect();
                p.push(Math.round(b.x) + ',' + Math.round(b.y));
            });
            return p.join(' | ');
        }""")

    a = dots()
    pg.wait_for_timeout(1200)
    c = dots()
    b.close()

open(os.path.join(OUT, "_glider.txt"), "w", encoding="utf-8").write("t=2.0s  " + a + "\nt=3.2s  " + c)
print(a)
print(c)
