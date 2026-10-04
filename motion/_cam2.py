import io, os

SRC = os.path.dirname(os.path.abspath(__file__))
FILES = ["01-architecture-diagram.html", "02-flowchart.html", "03-stat-counters.html",
         "04-terminal.html", "05-kinetic-title.html", "06-snags.html",
         "07-tools.html", "08-latency-bars.html"]

NEXT_OLD = """function next(){
  if(camOn){camOff();return;}
  if(step<TOTAL){step++;sync();const f=focusFor(step);if(f)camTo(f);}
}"""
NEXT_NEW = """function next(){
  if(step<TOTAL){step++;sync();const f=focusFor(step);if(f)camTo(f);}
}"""

MOUSE_OLD = "addEventListener('mousedown',next);"
MOUSE_NEW = """addEventListener('mousedown',e=>{if(e.button===2){camOff();}else{next();}});
addEventListener('contextmenu',e=>e.preventDefault());"""

for name in FILES:
    p = os.path.join(SRC, name)
    s = io.open(p, encoding="utf-8").read()
    if "contextmenu" in s:
        print("skip", name)
        continue
    assert NEXT_OLD in s, name + ": next not found"
    assert MOUSE_OLD in s, name + ": mousedown not found"
    s = s.replace(NEXT_OLD, NEXT_NEW, 1)
    s = s.replace(MOUSE_OLD, MOUSE_NEW, 1)
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("cam2", name)
