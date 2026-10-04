import subprocess, sys, os
log = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion\_browser.log"

r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                   capture_output=True, text=True)
text = "INSTALL RC=%s\n--OUT--\n%s\n--ERR--\n%s\n" % (r.returncode, r.stdout, r.stderr)

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        text += "\nLAUNCH OK, version=%s\n" % b.version
        b.close()
except Exception as e:
    text += "\nLAUNCH FAIL: %r\n" % (e,)

open(log, "w").write(text)
print("written")
