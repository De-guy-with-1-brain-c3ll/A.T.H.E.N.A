import subprocess, sys
r = subprocess.run([sys.executable, "-m", "pip", "install", "playwright"],
                   capture_output=True, text=True)
open(r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion\_pip.log", "w").write(
    "RC=%s\n--STDOUT--\n%s\n--STDERR--\n%s" % (r.returncode, r.stdout, r.stderr))
print("done", r.returncode)
