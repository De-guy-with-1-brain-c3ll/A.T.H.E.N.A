import os, subprocess, sys, time

BASE = r"C:\Users\Benjamin\Desktop\VSCODE projects\ATHENA SOURCE\motion"
FF = r"C:\Users\Benjamin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg.Shared_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build-shared\bin\ffmpeg.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
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
VF = ("ddagrab=output_idx=0:framerate=60,"
      "hwdownload,format=bgra,crop=2880:1620:0:90,"
      "scale=1920:1080:flags=lanczos")


def start_chrome(url):
    proc = subprocess.Popen([
        CHROME,
        "--remote-debugging-port=9223",
        "--user-data-dir=" + os.path.join(TMP, "profile"),
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
        "--start-fullscreen", "--window-size=2880,1800",
        url,
    ])
    time.sleep(4)
    return proc


def ffmpeg_record(path, dur):
    return subprocess.Popen([
        FF, "-y", "-hide_banner", "-loglevel", "error",
        "-filter_complex", VF,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-t", "%.2f" % dur, path,
    ])


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    from playwright.sync_api import sync_playwright

    first = "file:///" + os.path.join(BASE, DECKS[0][0]).replace("\\", "/")
    chrome = start_chrome(first)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9223")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.bring_to_front()

        if mode == "test":
            ff = ffmpeg_record(os.path.join(TMP, "test.mp4"), 3.0)
            time.sleep(3.4)
            ff.wait()
            subprocess.run([FF, "-y", "-loglevel", "error", "-ss", "1.5",
                            "-i", os.path.join(TMP, "test.mp4"),
                            "-frames:v", "1", os.path.join(TMP, "test_frame.png")],
                           check=True)
            print("test done")
            browser.close()
            chrome.kill()
            return

        log = []
        for fname, out, gap in DECKS:
            url = "file:///" + os.path.join(BASE, fname).replace("\\", "/")
            total = 0
            with open(os.path.join(BASE, fname), encoding="utf-8") as fh:
                for line in fh:
                    if "const TOTAL=" in line:
                        total = int(line.split("const TOTAL=")[1].split(";")[0])
            dur = 0.5 + LEAD + total * gap + TAIL + 1.5
            mp4 = os.path.join(OUTDIR, out + ".mp4")

            ff = ffmpeg_record(mp4, dur)
            time.sleep(0.5)
            page.goto(url)
            page.wait_for_timeout(int(LEAD * 1000))
            cx, cy = page.evaluate("[innerWidth/2, innerHeight/2]")
            for _ in range(total):
                page.mouse.click(cx, cy)
                page.wait_for_timeout(int(gap * 1000))
            page.wait_for_timeout(int((TAIL + 1.6) * 1000))
            ff.wait()
            sz = os.path.getsize(mp4) // 1024
            log.append("%s  %d steps  %.0fs  %d KB" % (out, total, dur, sz))

        browser.close()
    chrome.kill()
    print("\n".join(log))


main()
