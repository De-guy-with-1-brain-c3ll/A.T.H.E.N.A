"""Silent dashboard probe for a Pi coding artifact and PC inbox transfer."""
import json
import sys
import time

from athena.desktop import Client, probe


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.33.153"
    _, fingerprint = probe(host, timeout=3)
    client = Client(host, fingerprint)
    client.login("")
    nonce = str(int(time.time()))
    prompt = sys.argv[2] if len(sys.argv) > 2 else (
        "Quiet text task. Use the coding workspace tool to create a project named "
        f"transfer_probe_{nonce} and write probe.txt containing exactly "
        f"ATHENA transfer probe {nonce}. Then use upload_to_pc to send that saved "
        "probe.txt to the configured PC inbox. Report the actual transfer receipt. "
        "Do not use speech, audio, or speakers."
    )
    job = client.request("/api/chat", {"text": prompt})
    identity = job["id"]
    print(json.dumps({"job_id": identity, "nonce": nonce}))
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = client.request(f"/api/chat/{identity}")
        if result.get("status") == "complete":
            print(json.dumps(result, ensure_ascii=False))
            monitor = client.request("/api/monitor")
            print(json.dumps({"operations": monitor.get("operations", [])[:5],
                              "transfer": monitor.get("transfer", {})}, ensure_ascii=False))
            return 0
        time.sleep(3)
    print("Pi chat task timed out.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
