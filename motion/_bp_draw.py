import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
    pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    pg.goto("file:///" + os.path.join(SRC, "05-kinetic-title.html").replace("\\", "/"))

    def inkcount():
        return pg.evaluate("""() => {
            let partial = 0, done = 0, hidden = 0;
            document.querySelectorAll('.bpLayer .ink').forEach(el => {
                const cs = getComputedStyle(el);
                const l = parseFloat(cs.strokeDasharray) || 0;
                const o = parseFloat(cs.strokeDashoffset) || 0;
                if (o > 1) { partial++; } else { done++; }
            });
            return {partial, done};
        }""")

    pg.wait_for_timeout(300)
    snap_a = inkcount()
    pg.screenshot(path=os.path.join(OUT, "_draw_t0.png"))
    pg.wait_for_timeout(1500)
    snap_b = inkcount()
    pg.screenshot(path=os.path.join(OUT, "_draw_t1.png"))
    pg.wait_for_timeout(2500)
    snap_c = inkcount()
    pg.screenshot(path=os.path.join(OUT, "_draw_t2.png"))
    b.close()

lines = ["t=0.3s  %s" % snap_a, "t=1.8s  %s" % snap_b, "t=4.3s  %s" % snap_c]
open(os.path.join(OUT, "_drawlog.txt"), "w", encoding="utf-8").write("\n".join(lines))
print("\n".join(lines))
