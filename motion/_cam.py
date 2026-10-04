import io, os, re

SRC = os.path.dirname(os.path.abspath(__file__))
FILES = ["01-architecture-diagram.html", "02-flowchart.html", "03-stat-counters.html",
         "04-terminal.html", "05-kinetic-title.html", "06-snags.html",
         "07-tools.html", "08-latency-bars.html"]

HELPERS = """
const stageEl=document.getElementById('stage');
let baseK=1,camOn=false,camZ=1,camTX=0,camTY=0;
const focusMap={};
function stageRect(el){
  let x=0,y=0,n=el;
  while(n&&n!==stageEl){x+=n.offsetLeft;y+=n.offsetTop;n=n.offsetParent;}
  return {x,y,w:el.offsetWidth,h:el.offsetHeight};
}
groups.forEach(g=>{
  const s=+g.dataset.s;if(!s)return;
  const r=stageRect(g);
  if(!r.w||!r.h)return;
  const f=focusMap[s]||(focusMap[s]={x1:1e9,y1:1e9,x2:-1e9,y2:-1e9});
  f.x1=Math.min(f.x1,r.x);f.y1=Math.min(f.y1,r.y);
  f.x2=Math.max(f.x2,r.x+r.w);f.y2=Math.max(f.y2,r.y+r.h);
});
function focusFor(s){
  const f=focusMap[s];if(!f)return null;
  const P=80;
  const x1=Math.max(0,f.x1-P),y1=Math.max(0,f.y1-P);
  const x2=Math.min(1920,f.x2+P),y2=Math.min(1080,f.y2+P);
  const z=Math.min(1920/(x2-x1),1080/(y2-y1),1.75);
  if(z<1.06)return null;
  return {z,tx:-(baseK*z)*((x1+x2)/2-960),ty:-(baseK*z)*((y1+y2)/2-540)};
}
function applyT(){
  if(camOn){stageEl.style.transform='translate('+camTX+'px,'+camTY+'px) scale('+(baseK*camZ)+')';}
  else{stageEl.style.transform='scale('+baseK+')';}
}
function camTo(f){camZ=f.z;camTX=f.tx;camTY=f.ty;camOn=true;applyT();}
function camOff(){camOn=false;camZ=1;camTX=0;camTY=0;applyT();}
"""

NEXT_NEW = """function next(){
  if(camOn){camOff();return;}
  if(step<TOTAL){step++;sync();const f=focusFor(step);if(f)camTo(f);}
}"""

GO_NEW = """window.go=n=>{step=n;sync();camOff();};
window.cam=v=>{const f=focusFor(step||1);if(v&&f)camTo(f);else camOff();};"""

FIT_NEW = """function fitStage(){
  baseK=Math.min(innerWidth/1920,innerHeight/1080);
  stageEl.style.transition='none';
  applyT();
  requestAnimationFrame(()=>{stageEl.style.transition='transform 1.25s cubic-bezier(.2,.8,.2,1)';});
}"""

FIT_OLD = """function fitStage(){
  const s=document.getElementById('stage');
  const k=Math.min(innerWidth/1920,innerHeight/1080);
  s.style.transform='scale('+k+')';
}"""

NEXT_OLD = """function next(){
  if(step<TOTAL){step++;sync();}
}"""

RESETS = [
    ("function reset(){\n  step=0;sync();\n}", "function reset(){\n  step=0;sync();camOff();\n}"),
    ("function reset(){\n  step=0;resetCounters();sync();\n}", "function reset(){\n  step=0;resetCounters();sync();camOff();\n}"),
]

for name in FILES:
    p = os.path.join(SRC, name)
    s = io.open(p, encoding="utf-8").read()
    if "focusFor" in s:
        print("skip", name)
        continue

    assert NEXT_OLD in s, name + ": next not found"
    s = s.replace(NEXT_OLD, NEXT_NEW, 1)

    done = False
    for old, new in RESETS:
        if old in s:
            s = s.replace(old, new, 1)
            done = True
            break
    assert done, name + ": reset not found"

    assert FIT_OLD in s, name + ": fitStage not found"
    s = s.replace(FIT_OLD, FIT_NEW, 1)

    assert "window.go=n=>{step=n;sync();};" in s, name + ": go not found"
    s = s.replace("window.go=n=>{step=n;sync();};", GO_NEW, 1)

    anchor = "function sync(){"
    assert anchor in s, name + ": sync anchor not found"
    s = s.replace(anchor, HELPERS + anchor, 1)

    if "overflow:hidden" not in s:
        s = s.replace("</style>", "html,body{overflow:hidden}\n</style>", 1)

    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("cam", name)
