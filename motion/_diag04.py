import os
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
OUT = os.path.join(SRC, "_verify")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--force-color-profile=srgb"])
    pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    msgs = []
    pg.on("pageerror", lambda e: msgs.append("PAGEERROR: " + str(e)))
    pg.goto("file:///" + os.path.join(SRC, "04-terminal.html").replace("\\", "/"))
    pg.wait_for_timeout(400)
    pg.evaluate("go(9)")
    pg.wait_for_timeout(3000)
    info = pg.evaluate("""() => {
        const term = document.querySelector('.term');
        const line = document.querySelector('.line[data-s="2"]');
        const cs = getComputedStyle(term);
        return {
            step: window.step !== undefined ? 'yes' : 'no',
            onCount: document.querySelectorAll('[data-s].on').length,
            termOpacity: cs.opacity,
            termAnim: cs.animationName + ' / ' + cs.animationPlayState,
            termRect: JSON.stringify(term.getBoundingClientRect()),
            lineOpacity: getComputedStyle(line).opacity,
            typedWidth: getComputedStyle(document.querySelector('.typed')).width,
            stageW: document.getElementById('stage').getBoundingClientRect().width
        };
    }""")
    pg.screenshot(path=os.path.join(OUT, "_diag04.png"))
    b.close()

lines = [str(info)]
lines += msgs
open(os.path.join(OUT, "_diag04.txt"), "w", encoding="utf-8").write("\n".join(lines))
print("done")
