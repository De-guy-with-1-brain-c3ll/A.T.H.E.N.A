import os, re, io

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
FILES = [f for f in sorted(os.listdir(SRC)) if re.match(r"^0[1-8]-.*\.html$", f)]

NEW_GRID = """.grid{
  position:absolute;inset:0;
  background-image:
    linear-gradient(to right,rgba(240,166,60,.075) 1px,transparent 1px),
    linear-gradient(to bottom,rgba(240,166,60,.075) 1px,transparent 1px),
    linear-gradient(to right,rgba(240,166,60,.032) 1px,transparent 1px),
    linear-gradient(to bottom,rgba(240,166,60,.032) 1px,transparent 1px);
  background-size:240px 240px,240px 240px,48px 48px,48px 48px;
  pointer-events:none;
}
.warmth{
  position:absolute;inset:0;
  background:radial-gradient(ellipse 62% 50% at 50% 44%,rgba(240,166,60,.055),transparent 70%);
  pointer-events:none;
}
"""

BLUEPRINT = """
.bp{position:absolute;inset:0;pointer-events:none;z-index:0}
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
@keyframes bdraw{to{stroke-dashoffset:0}}
@keyframes dotin{to{opacity:1}}
@keyframes ping{0%{opacity:.2;r:38}70%{opacity:0;r:150}100%{opacity:0;r:150}}
@keyframes spin{to{transform:rotate(360deg)}}
"""

SVG = """
  <svg class="bp" viewBox="0 0 1920 1080" preserveAspectRatio="none">
    <path class="b" pathLength="400" d="M44,175 H278" style="--bd:.15s"/>
    <path class="b dim thin" pathLength="400" d="M44,214 H188" style="--bd:.55s"/>
    <circle class="dot" cx="282" cy="175" r="3" style="--dd:.9s"/>
    <path class="b" pathLength="400" d="M1642,110 V150 M1642,110 H1876" style="--bd:1.1s"/>
    <circle class="dot" cx="1642" cy="152" r="3" style="--dd:1.6s"/>
    <path class="b dim" pathLength="400" d="M1876,150 V968" style="--bd:1.5s"/>
    <path class="b dim thin" pathLength="400" d="M1848,968 H1876" style="--bd:2.2s"/>
    <path class="b thin dim" pathLength="400" d="M44,1010 H300" style="--bd:1.7s"/>
    <circle class="dot" cx="303" cy="1010" r="2.5" style="--dd:2.1s"/>

    <path class="b dim thin" pathLength="400" d="M596,300 H900 M596,300 V360" style="--bd:1.3s"/>
    <path class="b dim thin" pathLength="400" d="M1320,262 H1650 M1650,262 V320" style="--bd:1.45s"/>

    <path class="b thin" pathLength="400" d="M1566,152 V316 M1544,152 H1588 M1544,316 H1588" style="--bd:.45s"/>
    <text class="bp-t txt" x="1580" y="248" text-anchor="middle" style="--td:1.2s">556</text>

    <path class="b thin" pathLength="400" d="M1546,516 H1620 M1546,655 H1620 M1620,516 V655" style="--bd:.8s"/>
    <text class="txt dim" x="1632" y="592" dominant-baseline="central" style="--td:1.3s">H 139</text>

    <ellipse class="lx" cx="960" cy="540" rx="372" ry="372"/>
    <ellipse class="lx" cx="960" cy="540" rx="452" ry="452"/>

    <circle class="b thin dim" cx="96" cy="540" r="30" pathLength="400" style="--bd:.6s"/>

    <path class="b hot" pathLength="600" d="M1888,1018 H1790 V930" style="--bd:.25s"/>
    <text class="txt k" x="1786" y="1058" text-anchor="end" style="--td:.95s">ATHENA</text>

    <circle class="pulse r" cx="1252" cy="140" r="38"/>
    <circle class="pulse r2" cx="1252" cy="140" r="38"/>
  </svg>
"""

for f in FILES:
    p = os.path.join(SRC, f)
    s = io.open(p, encoding="utf-8").read()
    if ".bp{" in s:
        print("skip", f)
        continue

    s = s.replace(".warmth{\n  position:absolute;inset:0;\n  background:radial-gradient(ellipse 62% 50% at 50% 44%,rgba(240,166,60,.04),transparent 70%);\n  pointer-events:none;\n}\n}", "{WARM}", 1)

    start = s.index(".grid{")
    warm = s.index(".warmth{", start)
    end = s.index("pointer-events:none;", warm) + len("pointer-events:none;")
    s = s[:start] + NEW_GRID + s[end:]

    s = s.replace("<div class=\"corner bl\"></div><div class=\"corner br\"></div>",
                  "<div class=\"corner bl\"></div><div class=\"corner br\"></div>" + SVG, 1)

    s = s.replace("@keyframes popin", BLUEPRINT + "@keyframes popin", 1)
    s = s.replace("z-index:0}", "z-index:0}", 1)
    s = s.replace("</head>", "<style>.zone{z-index:2}</style>\n</head>", 1)

    io.open(p, "w", encoding="utf-8").write(s)
    print("patched", f)
