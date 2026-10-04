import os, subprocess, sys, time, base64

BASE = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
SHELL = r"C:\Users\Benjamin\AppData\Local\ms-playwright\chromium_headless_shell-1243\chrome-headless-shell-win64\chrome-headless-shell.exe"
FF = r"C:\Users\Benjamin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build-shared\bin\ffmpeg.exe"
OUTDIR = r"C:\Users\Benjamin\Desktop"
TMP = os.path.join(BASE, "_rec_tmp")
os.makedirs(TMP, exist_ok=True)

DECKS = [
    ("01-architecture-diagram.html", "athena_01_architecture", 3.6),
    ("02-flowchart.html", "athena_02_state_machine", 3.6),
    ("03-stat-counters.html", "athena_03_counters", 4.6),
    ("04-terminal.html", "athena_04_benchmark", 4.2),
    ("05-kinetic-title.html", "athena_05_title", 4.2),
    ("06-snags.html", "athena_06_snags", 5.6),
    ("07-tools.html", "athena_07_tools", 6.6),
    ("08-latency-bars.html", "athena_08_latency", 4.8),
]

LEAD = 6.0
TAIL = 6.0
FPS = 60
DT = 1000.0 / FPS

INIT = r"""
window.__VT = 0;
window.__origin = null;
const __pnow = performance.now.bind(performance);
performance.now = function(){ return window.__VT; };
const __raf = window.requestAnimationFrame.bind(window);
window.requestAnimationFrame = function(cb){ return __raf(function(){ return cb(window.__VT); }); };
window.__vtInit = function(){ if (window.__origin === null) window.__origin = __pnow(); };
window.__vtSeek = function(){
  if (window.__origin === null) return;
  document.body.offsetHeight;
  const anims = document.getAnimations();
  for (let i = 0; i < anims.length; i++) {
    const a = anims[i];
    if (a.__vt0 === undefined) {
      a.__vt0 = window.__VT;
      try { a.pause(); } catch (e) {}
    }
    try { a.currentTime = Math.max(0, window.__VT - a.__vt0); } catch (e) {}
  }
  const svgs = document.querySelectorAll('svg');
  for (let i = 0; i < svgs.length; i++) {
    const s = svgs[i];
    if (s.__smp === undefined) {
      try { s.pauseAnimations(); } catch (e) {}
      s.__smp = true;
    }
    try { s.setCurrentTime(Math.max(0, window.__VT / 1000)); } catch (e) {}
  }
};
"""


def deck_total(fname):
    with open(os.path.join(BASE, fname), encoding="utf-8") as fh:
        for line in fh:
            if "const TOTAL=" in line:
                return int(line.split("const TOTAL=")[1].split(";")[0])
    raise RuntimeError(fname + ": TOTAL not found")


def capture_deck(page, client, fname, gap, jpgdir, out_mp4, log, lead=None, tail=None):
    lead = LEAD if lead is None else lead
    tail = TAIL if tail is None else tail
    total = deck_total(fname)
    url = "file:///" + os.path.join(BASE, fname).replace("\\", "/")

    page.goto(url, wait_until="load")
    page.wait_for_timeout(400)
    page.evaluate("__vtInit()")
    click_times = [(lead + i * gap) * 1000.0 for i in range(total)]
    nframes = int(round((lead + total * gap + tail) * FPS))
    ci = 0
    prev = None

    t0 = time.time()
    ff = subprocess.Popen(
        [FF, "-y", "-loglevel", "error",
         "-f", "mjpeg", "-framerate", str(FPS), "-i", "pipe:0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_mp4],
        stdin=subprocess.PIPE)
    for i in range(nframes):
        T = i * DT
        due = 0
        while ci < len(click_times) and click_times[ci] <= T + 1e-6:
            due += 1
            ci += 1
        page.evaluate(
            "window.__VT=%f; for(let k=0;k<%d;k++)window.dispatchEvent(new MouseEvent('mousedown',{button:0})); window.__vtSeek();" % (T, due)
        )
        page.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        r = client.send("Page.captureScreenshot", {"format": "jpeg", "quality": 88})
        ff.stdin.write(base64.b64decode(r["data"]))

    ff.stdin.close()
    ff.wait()
    if ff.returncode != 0:
        raise RuntimeError("ffmpeg failed rc=%d for %s" % (ff.returncode, out_mp4))
    dur = time.time() - t0
    line = "%s  steps=%d frames=%d  capture=%.0fs  size=%dKB" % (
        os.path.basename(out_mp4), total, nframes, dur, os.path.getsize(out_mp4) // 1024)
    log.append(line)
    print(line, flush=True)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=SHELL, args=[
            "--run-all-compositor-stages-before-draw",
        ])
        page = browser.new_page(viewport={"width": 1920, "height": 1080},
                                device_scale_factor=1)
        page.add_init_script(INIT)
        client = page.context.new_cdp_session(page)

        if mode == "test":
            capture_deck(page, client, "05-kinetic-title.html", 1.2,
                         os.path.join(TMP, "frames_test"),
                         os.path.join(TMP, "test.mp4"), [], lead=1.5, tail=1.0)
            browser.close()
            print("test done", flush=True)
            return

        log = []
        for fname, out, gap in DECKS:
            outp = os.path.join(OUTDIR, out + ".mp4")
            if os.path.exists(outp):
                line = "%s  SKIP (exists)" % out
                log.append(line)
                print(line, flush=True)
                continue
            capture_deck(page, client, fname, gap,
                         os.path.join(TMP, "frames_" + out),
                         outp, log)
            open(os.path.join(TMP, "_progress.txt"), "w", encoding="utf-8").write("\n".join(log))
        browser.close()
        print("ALL DONE", flush=True)


main()
