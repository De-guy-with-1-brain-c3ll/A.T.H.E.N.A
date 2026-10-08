"""Exercise the Pi/computer audio handoff without capture, synthesis or playback."""
import json
import sys

from athena.desktop import Client, probe
from athena.desktop_audio import open_socket


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.33.153"
    address, fingerprint = probe(host, 3)
    client = Client(address, fingerprint)
    client.login("")
    sock = open_socket(client)
    try:
        sock.send(json.dumps({"type": "capabilities", "speech": False}))
        ready = json.loads(sock.recv())
        if ready.get("type") != "ready":
            raise RuntimeError("The silent browser audio handshake failed.")
        status = client.request("/api/audio-route")
        if not status.get("computer_ready"):
            raise RuntimeError("The computer audio connection did not become ready.")
        computer = client.request("/api/audio-route", {"target": "computer"})
        if computer.get("target") != "computer":
            raise RuntimeError("The computer audio route did not activate.")
        pi = client.request("/api/audio-route", {"target": "pi"})
        if pi.get("target") != "pi":
            raise RuntimeError("The Pi audio route did not activate.")
        print("PASS: silent computer and Pi audio route handoff; restored to Pi.")
    finally:
        try:
            client.request("/api/audio-route", {"target": "pi"})
        finally:
            sock.close(timeout=.2)


if __name__ == "__main__":
    main()
