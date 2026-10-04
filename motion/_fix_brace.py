import os, re, io

SRC = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
FILES = [f for f in sorted(os.listdir(SRC)) if re.match(r"^0[1-8]-.*\.html$", f)]

pat = re.compile(r"\.warmth\{[^}]*\}\s*\}", re.S)

for f in FILES:
    p = os.path.join(SRC, f)
    s = io.open(p, encoding="utf-8").read()
    n, cnt = pat.subn(
        ".warmth{\n  position:absolute;inset:0;\n"
        "  background:radial-gradient(ellipse 62% 50% at 50% 44%,rgba(240,166,60,.055),transparent 70%);\n"
        "  pointer-events:none;\n}", s)
    if cnt:
        io.open(p, "w", encoding="utf-8").write(n)
    print(f, "fixed" if cnt else "no-op")
