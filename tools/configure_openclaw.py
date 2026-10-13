"""Configure the private OpenClaw installation using ATHENA's DeepSeek key."""
import subprocess
import sys
import os

from athena.openclaw import command, environment, installation, redact


def main():
    env = environment()
    if not env.get("CUSTOM_API_KEY"):
        print("Configure DEEPSEEK_API_KEY in ATHENA's .env first.", file=sys.stderr)
        return 1
    args = command() + [
        "onboard", "--non-interactive", "--accept-risk", "--mode", "local",
        "--auth-choice", "custom-api-key", "--secret-input-mode", "ref",
        "--custom-base-url", "https://api.deepseek.com",
        "--custom-model-id", os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "--custom-provider-id", "athena-deepseek", "--custom-text-input",
        "--workspace", str(installation() / "workspace"),
        "--gateway-bind", "loopback", "--gateway-port", "18789",
        "--skip-channels", "--skip-skills", "--skip-search", "--skip-hooks",
        "--skip-ui", "--skip-health", "--no-install-daemon",
        "--suppress-gateway-token-output", "--json",
    ]
    result = subprocess.run(args, cwd=str(installation()), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    print(redact(result.stdout, env))
    if result.returncode:
        print(redact(result.stderr, env), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
