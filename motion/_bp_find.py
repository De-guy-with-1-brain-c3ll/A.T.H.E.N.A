import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
    pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    pg.goto("file:///" + os.path.join(SRC, "08-latency-bars.html").replace("\\", "/"))
    pg.wait_for_timeout(2600)
    info = pg.evaluate("""() => {
        const out = [];
        document.querySelectorAll('.bpLayer path, .bpLayer rect, .bpLayer g').forEach(el => {
            const cs = getComputedStyle(el);
            if (cs.opacity === '0') return;
            const r = el.getBBox();
            if (r.width < 8 && r.height < 8) return;
            out.push([el.tagName, el.getAttribute('class'), Math.round(r.x) + ',' + Math.round(r.y),
                      Math.round(r.width) + 'x' + Math.round(r.height), cs.stroke, cs['stroke-width'], cs.opacity].join(' | '));
        });
        return out.slice(0, 40);
    }""")
    z = pg.evaluate("""() => {
        const s = document.querySelector('.bpLayer');
        return getComputedStyle(s).zIndex + ' / opacity ' + getComputedStyle(s).opacity;
    }""")
    b.close()

open(os.path.join(OUT, "_bpdiag.txt"), "w", encoding="utf-8").write(z + "\n" + "\n".join(info))
print("n=", len(info))
