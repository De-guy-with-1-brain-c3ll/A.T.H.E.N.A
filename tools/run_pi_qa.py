"""Stage and run bounded voice QA on the Pi without changing running services."""
import argparse
import getpass
import os
from pathlib import Path
import shlex
import time
import paramiko

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=(ROOT/"orange_pi/pi-address.txt").read_text().strip())
    parser.add_argument("--prompts", action="store_true")
    parser.add_argument("--tests", action="store_true", help="Run voice/tool regressions with Pi libraries")
    parser.add_argument("--interactions", action="store_true", help="Evaluate 27 model cases; bounded paid requests")
    parser.add_argument("--ids", help="Comma-separated interaction case IDs to retest")
    parser.add_argument("--deploy", action="store_true", help="Install the signed published release")
    parser.add_argument("--pc-inbox", action="store_true", help="Send a harmless synthetic Pi-to-PC receipt test")
    parser.add_argument("--workflow", action="store_true", help="Validate actual background tool wiring without model calls")
    parser.add_argument("--race-lookup", action="store_true", help="One silent live race correction lookup, capped at four model requests")
    parser.add_argument("--information-lookup", action="store_true", help="Three silent live public information checks, capped at four model calls each")
    parser.add_argument("--model-tool-flow", action="store_true", help="Silent live clock/weather/contextual status routing checks")
    parser.add_argument("--web-grounding", action="store_true", help="Silent focused reader and bounded real-model search retry checks")
    parser.add_argument("--pc-browser", action="store_true", help="Verify the PC browser with one screenshot")
    parser.add_argument("--release-qa", help="Run pi_silent_release_qa.py: transfers, teams, or interruptions")
    parser.add_argument("--vision", action="store_true", help="Include one bounded paid Qwen vision call with --pc-browser")
    parser.add_argument("--command", help="Run an explicit diagnostic command instead of the QA suite")
    args = parser.parse_args()
    client = paramiko.SSHClient()
    from paramiko.hostkeys import HostKeyEntry, InvalidHostKey
    for line in (Path.home()/".ssh/known_hosts").read_text().splitlines():
        if not line.strip() or line.startswith("#"): continue
        try:
            entry = HostKeyEntry.from_line(line)
        except (InvalidHostKey, ValueError):
            continue
        if entry and entry.key:
            for hostname in entry.hostnames:
                client.get_host_keys().add(hostname, entry.key.get_name(), entry.key)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(args.host, username="root", password=os.environ.get("ATHENA_PI_PASSWORD")
                   or getpass.getpass("Pi password: "), timeout=30, banner_timeout=30,
                   auth_timeout=20, look_for_keys=False, allow_agent=False)
    try:
        if args.deploy:
            stage = f"/tmp/athena-install-{int(time.time())}"
            sftp = client.open_sftp()
            sftp.mkdir(stage)
            import json
            feed = ROOT/'orange_pi/update_feed'
            manifest = json.loads((feed/'manifest.json').read_text())
            for source in [feed/'manifest.json', feed/manifest['archive'],
                           ROOT/'orange_pi/pi/update_client.py', ROOT/'tools/install_staged_release.py']:
                sftp.put(str(source), stage+'/'+source.name)
            command = f"/usr/bin/python3 -u {stage}/install_staged_release.py {stage}"
        elif args.release_qa:
            # The release archive ships src/athena only, so this tool has to be
            # staged explicitly rather than read from the installed release.
            stage = f"/tmp/athena-release-qa-{int(time.time())}"
            sftp = client.open_sftp()
            sftp.mkdir(stage)
            sftp.put(str(ROOT/"tools/pi_silent_release_qa.py"), stage+"/pi_silent_release_qa.py")
            command = (f"ATHENA_PROJECT_ROOT={shlex.quote(stage)} "
                       f"/opt/athena/current/.venv/bin/python {stage}/pi_silent_release_qa.py "
                       f"{shlex.quote(args.release_qa)}")
        elif args.pc_inbox or args.workflow or args.pc_browser or args.race_lookup or args.information_lookup or args.model_tool_flow or args.web_grounding:
            stage = f"/tmp/athena-inbox-qa-{int(time.time())}"
            sftp = client.open_sftp()
            sftp.mkdir(stage)
            script = 'check_web_grounding.py' if args.web_grounding else 'check_model_tool_flow.py' if args.model_tool_flow else 'check_information_lookup.py' if args.information_lookup else 'check_race_lookup.py' if args.race_lookup else 'check_pc_browser.py' if args.pc_browser else 'check_workflow.py' if args.workflow else 'check_pc_inbox.py'
            sftp.put(str(ROOT/'tools'/script), stage+'/'+script)
            command = f"/opt/athena/current/.venv/bin/python {stage}/{script}" + (' --vision' if args.pc_browser and args.vision else '')
        elif args.command:
            command = args.command
        else:
            stage = f"/tmp/athena-qa-{int(time.time())}"
            sftp = client.open_sftp()
            sftp.mkdir(stage)
            sftp.mkdir(stage+"/src")
            def upload_tree(source, destination):
                sftp.mkdir(destination)
                for path in source.iterdir():
                    if path.name == "__pycache__": continue
                    if path.is_dir(): upload_tree(path, destination+"/"+path.name)
                    elif path.suffix not in {".pyc", ".pyo"}: sftp.put(str(path), destination+"/"+path.name)
            upload_tree(ROOT/"src/athena", stage+"/src/athena")
            if args.tests:
                upload_tree(ROOT/"tests", stage+"/tests")
                sftp.mkdir(stage+'/orange_pi')
                sftp.mkdir(stage+'/orange_pi/config')
                for name in ['.env.example', 'orange_pi/README.md', 'orange_pi/config/athena.env.example']:
                    sftp.put(str(ROOT/name), stage+'/'+name)
            sftp.put(str(ROOT/"tools/voice_qa.py"), stage+"/voice_qa.py")
            if args.interactions:
                sftp.put(str(ROOT/'tools/interaction_qa.py'),stage+'/interaction_qa.py')
            keyword = ROOT/"outputs/models/keyword"
            if keyword.is_dir():
                target = "/opt/athena/models/keyword"
                try: sftp.mkdir(target)
                except OSError: pass
                for path in keyword.iterdir():
                    if path.is_file(): sftp.put(str(path), target+"/"+path.name)
            command = (f"ATHENA_PROJECT_ROOT={shlex.quote(stage)} "
                f"ATHENA_KEYWORD_MODEL_DIR=/opt/athena/models/keyword "
                f"/opt/athena/current/.venv/bin/python {stage}/voice_qa.py "
                f"{'--prompts' if args.prompts else ''} --output {stage}/report.json")
            if args.tests:
                # The stage root is on the path as well as src and tests: several
                # modules do `from tests.test_search_refinement import ...`, which
                # only resolves when the parent of the tests package is importable.
                command = (f"PYTHONPATH={stage}/src:{stage}/tests:{stage} "
                    "/opt/athena/current/.venv/bin/python -m unittest -q -b "
                    "test_latency_optimization test_edge_tts test_teams test_remote_audio "
                    "test_background_agents test_voice_controls test_web_tools test_music_audio_focus test_workflows test_confirmation_flow test_pc_browser "
                    "test_netease test_youtube test_music_playlists test_speech_truncation test_interaction_style test_vpn test_search_refinement test_audio_routing test_tool_status test_agent_supervisor test_desktop_control "
                    "test_interruptions")
            if args.interactions:
                command=(f"ATHENA_PROJECT_ROOT={shlex.quote(stage)} "
                    f"/opt/athena/current/.venv/bin/python {stage}/interaction_qa.py "
                    f"{'--ids '+shlex.quote(args.ids) if args.ids else ''} --output {stage}/report.json")
        print("Running Pi diagnostics.", flush=True)
        _, stdout, stderr = client.exec_command(command, timeout=1200)
        for line in stdout: print(line.rstrip(), flush=True)
        error = stderr.read().decode(errors="replace")
        if error: print(error, flush=True)
        status = stdout.channel.recv_exit_status()
        if not args.command and not args.tests and not args.deploy and not args.release_qa and not args.pc_inbox and not args.workflow and not args.pc_browser and not args.race_lookup and not args.information_lookup and not args.model_tool_flow and not args.web_grounding:
            destination = ROOT/("outputs/interactions/pi-interactions.json" if args.interactions
                                else "outputs/optimization/pi-voice-qa.json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(stage+"/report.json", str(destination))
            print(f"Report: {destination}")
            sftp.close()
        raise SystemExit(status)
    finally:
        client.close()


if __name__ == "__main__": main()
