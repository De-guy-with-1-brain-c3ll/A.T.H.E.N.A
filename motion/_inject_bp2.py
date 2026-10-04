import os, re, io

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
FILES = [f for f in sorted(os.listdir(SRC)) if re.match(r"^0[1-8]-.*\.html$", f)]

CSS = """.bp{position:absolute;inset:0;pointer-events:none;z-index:0}
.bp .b{stroke:rgba(240,166,60,.42);stroke-width:1.5;stroke-dasharray:400;stroke-dashoffset:400;
  animation:bdraw 1.5s var(--ease) var(--bd,.2s) forwards}
.bp .b.dim{stroke:rgba(240,166,60,.24)}
.bp .b.thin{stroke-width:1}
.bp .b.hot{stroke:rgba(240,166,60,.72);stroke-width:2.5;stroke-dasharray:600;stroke-dashoffset:600;
  animation:bdraw 1.9s var(--ease) var(--bd,.35s) forwards}
.bp .dot{fill:var(--accent);opacity:0;animation:dotin .5s var(--out) var(--dd,1.1s) forwards}
.bp .txt{
  fill:rgba(240,166,60,.55);font-size:15px;letter-spacing:.2em;opacity:0;
  animation:fadein .7s var(--out) var(--td,1.35s) forwards;
}
.bp .txt.dim{fill:rgba(240,166,60,.32)}
.bp .txt.k{fill:rgba(240,166,60,.85);font-weight:600;letter-spacing:.26em}
.bp .lx{stroke:rgba(240,166,60,.2);stroke-width:1;stroke-dasharray:4 8;
  animation:spin 34s linear infinite;transform-origin:960px 540px}
.bp .pulse{fill:var(--accent);opacity:.18}
.bp .pulse.r{animation:ping 4.6s var(--out) 2s infinite}
.bp .pulse.r2{animation:ping 4.6s var(--out) 4.1s infinite}
@keyframes fadein{to{opacity:1}}
@keyframes bdraw{to{stroke-dashoffset:0}}
@keyframes dotin{to{opacity:1}}
@keyframes ping{0%{opacity:.2;r:38}70%{opacity:0;r:150}100%{opacity:0;r:150}}
@keyframes spin{to{transform:rotate(360deg)}}
"""

for f in FILES:
    p = os.path.join(SRC, f)
    s = io.open(p, encoding="utf-8").read()
    if ".bp{" in s:
        print("skip", f)
        continue
    anchor = "@keyframes bdraw{to{stroke-dashoffset:0}}"
    if anchor not in s:
        print("NO ANCHOR", f)
        continue
    s = s.replace(anchor, CSS.rstrip(), 1)
    s = s.replace('<text class="bp-t txt"', '<text class="txt"', 1)
    io.open(p, "w", encoding="utf-8").write(s)
    print("patched", f)
