# Installer release validation — 2026-10-08

Release: `0.1.0-pi.89`. Includes the repairs documented in
[October 7 repair validation](october-7-repair-validation.md).

## Packages

- Windows Standalone: per-user installer, bundled Python, FFmpeg, Tk setup,
  dashboard, file receiver, voice workers and all registered tools.
- Windows Companion: per-user installer, authenticated receiver/browser bridge,
  microphone/speaker bridge and certificate-pinned device pairing.
- Linux: checksum-verified self-extracting installer with guided setup,
  architecture-native dependencies, systemd services, scaled memory limits,
  optional VPN and Teams login. Supported CPU families: ARM64, ARMv7, x86-64.

Windows generated-code execution needs Docker Desktop; setup downloads the
container image after its engine is running. Windows VPN control integrates an
existing Mihomo/Clash client. Cloud/account features still need credentials.

## Checks

- Regression suite: **1,001 tests, nine skipped**, no failures.
- Pairing validation: wrong role, malformed codes, public/loopback addresses,
  invalid fingerprints, missing keys; local certificate persistence.
- Windows control/audio IPC: authenticated loopback sockets; unauthorized
  clients rejected before audio attachment or control actions.
- Both real Windows setup programs: silent install into isolated directories,
  packaged GUI/assets/tool-discovery/FFmpeg self-test, HTTPS dashboard health and
  password login, five byte-exact SHA-256 transfers each (0, 1, 65,535, 65,536,
  1,048,576 bytes), uninstall and retained profile data.
- Linux `.run` extraction/checksum/check mode executed under real Ubuntu WSL;
  package contents and shell line endings verified.

No microphone recording or speaker playback was used for installer QA. These
checks do not prove every audio driver, VPN provider, SBC or Linux image works.
Fresh physical installations on ARMv7 and all listed distributions have not
been tested. Previous Pi live repair tests are recorded separately.

## Reproduce

Build using `packaging/build.py`. Run `tools/check_installers.py` for silent
Windows install/uninstall and transfer checks. Run the Linux installer with
`--check` for a non-mutating extraction/integrity check. GitHub Actions can build
both Windows packages and the Linux package on a tag or manual dispatch.
