import os, json
from playwright.sync_api import sync_playwright

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
f = os.path.join(SRC, "01-architecture-diagram.html").replace("\\", "/")

JS = """
() => {
  const r = {};
  const st = document.getElementById('stage');
  const cs = getComputedStyle(st);
  r.stage = {position: cs.position, width: cs.width, transform: cs.transform,
             display: cs.display};
  const t = document.querySelector('.title');
  const tc = getComputedStyle(t);
  r.title = {position: tc.position, left: tc.left, top: tc.top,
             rect: t.getBoundingClientRect()};
  const c = document.querySelector('.corner.tl');
  r.cornerTl = c.getBoundingClientRect();
  const p = document.querySelector('.panel');
  r.panel = p.getBoundingClientRect();
  const d = document.querySelector('.diagram');
  const dc = getComputedStyle(d);
  r.diagram = {position: dc.position, left: dc.left, rect: d.getBoundingClientRect()};
  r.body = {display: getComputedStyle(document.body).display,
            w: document.body.getBoundingClientRect()};
  return r;
}
"""

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 960, "height": 540})
    pg.goto("file:///" + f)
    pg.wait_for_timeout(1500)
    data = pg.evaluate(JS)
    b.close()

out = json.dumps(data, indent=1, default=str)
open(os.path.join(SRC, "_diag.txt"), "w").write(out)
print("ok")
