import re, glob, os

root_dir = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
for f in sorted(glob.glob(os.path.join(root_dir, "*.html"))):
    s = open(f, encoding="utf-8").read()
    print("=" * 70)
    print(os.path.basename(f), len(s), "chars")
    blocks = re.findall(r"<style>(.*?)</style>", s, re.S)
    body = "".join(blocks)
    m = re.search(r":root\s*\{(.*?)\}", body, re.S)
    root = m.group(1) if m else ""
    rest = body.replace(root, "")
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", rest)
    print("   hardcoded hex outside :root:", sorted(set(hexes)) if hexes else "NONE")
    print("   rgba() uses:", len(re.findall(r"rgba\(", rest)))
    print("   style blocks:", len(blocks), "| script blocks:", len(re.findall(r"<script", s)))
    print("   div balance:", s.count("<div") - s.count("</div>"))
    print("   comments in code:", len(re.findall(r"/\*", s)))
    print("   animation names:", sorted(set(re.findall(r"@keyframes\s+([\w-]+)", s))))
    print("   inline anim-delay vars:", sorted(set(re.findall(r"--d:([^;\"]+)", s)))[:14])
