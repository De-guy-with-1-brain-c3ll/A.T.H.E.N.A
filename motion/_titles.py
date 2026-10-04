import io, os, re

SRC = os.path.dirname(os.path.abspath(__file__))
TITLED = ["01-architecture-diagram.html", "02-flowchart.html", "03-stat-counters.html",
          "06-snags.html", "07-tools.html", "08-latency-bars.html"]
ALL = TITLED + ["04-terminal.html", "05-kinetic-title.html"]

TITLE_RE = re.compile(r'[ \t]*<div class="zone title rv slide" data-s="1">.*?</div>\n')
DS_RE = re.compile(r'data-s="(\d+)"')
TOTAL_RE = re.compile(r'const TOTAL=(\d+);')

CAM_OLD = "transform 1.25s cubic-bezier(.2,.8,.2,1)"
CAM_NEW = "transform 1.8s cubic-bezier(.19,1,.22,1)"

for name in ALL:
    p = os.path.join(SRC, name)
    s = io.open(p, encoding="utf-8").read()

    if name in TITLED:
        s, n = TITLE_RE.subn("", s, count=1)
        assert n == 1, name + ": title block not found"

        def bump(m):
            v = int(m.group(1))
            return 'data-s="%d"' % (v - 1 if v >= 2 else v)
        s = DS_RE.sub(bump, s)

        m = TOTAL_RE.search(s)
        old = int(m.group(1))
        s = TOTAL_RE.sub("const TOTAL=%d;" % (old - 1), s, count=1)
        print("%s  TOTAL %d -> %d" % (name, old, old - 1))
    else:
        print("%s  (no title removal)" % name)

    if CAM_OLD in s:
        s = s.replace(CAM_OLD, CAM_NEW, 1)

    io.open(p, "w", encoding="utf-8", newline="\n").write(s)

p = os.path.join(SRC, "05-kinetic-title.html")
s = io.open(p, encoding="utf-8").read()
if "by Unsupervised Learning" not in s:
    anchor = '<span class="cap">build</span>\n  </div>'
    assert anchor in s, "05: idx anchor missing"
    byline = '\n  <div class="rv rise" data-s="5" style="position:absolute;left:96px;bottom:92px;font-size:21px;letter-spacing:.32em;text-transform:uppercase;color:var(--dim)">( by Unsupervised Learning )</div>'
    s = s.replace(anchor, anchor + byline, 1)
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("05  byline added")
