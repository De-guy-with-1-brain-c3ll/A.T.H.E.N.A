# Install ATHENA

Choose one setup file. You do not need Python, Git or an editor on Windows.

| Download | Use it for |
| --- | --- |
| **ATHENA-Standalone-Windows-Setup.exe** | Run the assistant on a Windows 10/11 PC, using its microphone and speakers. |
| **ATHENA-Companion-Windows-Setup.exe** | Control a Linux device, share your PC microphone/speakers, receive files and open PC browser pages. |
| **ATHENA-Linux-Setup.run** | Install the assistant on a Linux SBC or Linux PC. |

## Windows: run Athena on your PC

1. Double-click the Standalone installer and follow Next → Install.
2. Open ATHENA Standalone. Enter your DeepSeek and DashScope API keys, select your timezone and save.
3. Click **Start listening** when you are ready. Setup never plays a test sound.
4. Open the dashboard for chat, settings, tasks, saved history and integrations. The local HTTPS certificate is generated on your PC; the browser may ask you to accept it once.

All assistant tools are included. Features requiring accounts need their credentials: Teams needs a Microsoft app ID and sign-in, Feishu needs its app credentials, and search providers need their keys. Edge speech needs internet but no key. This is a local installation, not a promise that cloud AI works offline.

Safe generated-code execution requires Docker Desktop with Linux containers. Click **Set up safe code execution**, finish Docker's setup and open Docker Desktop, then click the button again. Athena downloads its sandbox image automatically. Athena never falls back to running generated code directly on your PC.

For Windows VPN control, click **Prepare Windows VPN configuration**, select your exported Clash/Mihomo YAML, then import the generated `athena-vpn.yaml` into your Windows Mihomo client. Start that client's core; Athena can then toggle TUN and switch endpoints. The VPN client must have permission to manage Windows network routes.

Your keys, files and conversations are stored in your own Windows profile under `%LOCALAPPDATA%\ATHENA`. Upgrading the app preserves them. Uninstall removes the application; your personal data is kept.

## Pair a Windows PC with a Linux device

1. Install **ATHENA Companion** on the PC.
2. Click **Copy PC pairing code**. Keep the code private.
3. Run the Linux installer and paste that PC code when it asks. It configures file transfer and PC browser control automatically.
4. At the end, Linux setup shows a device code. Click **Pair Linux device** in Companion and paste it.
5. Connect and start Athena's voice service from Overview (or the dashboard). Then click **Start PC microphone + speaker** in Microphone. The PC bridge stays connected when you switch listening back to the Linux device.

Allow the receiver on **private networks** if Windows Firewall asks. If transfers are blocked, click **Allow file receiving on private networks** and approve Windows' administrator prompt. This allows only this receiver on port 8781 from the local subnet, on private networks. The devices need to be on the same local network. Copying a pairing code does not open your microphone or play sound. The receiver starts again when you open Companion.

## Linux SBC setup

Use a current OS with **systemd and Python 3.11+**:

- Debian 12+, Ubuntu 24.04+, Armbian, Raspberry Pi OS based on Debian 12+.
- Current Fedora or Arch with their normal package repositories.
- ARM64, ARMv7 or x86-64. Very old ARMv6 boards and non-systemd distributions are not supported by this installer.

Use at least 1 GB RAM; 2 GB or more is recommended for running several integrations together. Setup scales service memory limits to the board's RAM. Linux installation downloads architecture-specific Python dependencies; it is not a Windows binary running through emulation.

Download the Linux setup file, open a terminal in its Downloads folder and run:

```sh
bash ATHENA-Linux-Setup.run
```

It asks for administrator permission, installs dependencies, then guides you through keys, timezone, dashboard password and pairing. You do not edit configuration files. Voice is left stopped until you start it in the dashboard. A microphone and output device must be available for local voice, or pair a PC and use its devices.

If the board has no audio hardware, choose the paired PC microphone/speakers during setup. Start Athena's voice service; it will wait for the PC bridge. Then start the microphone in Companion.

Internet is required for package installation and cloud features. Some distributions require additional repositories for FFmpeg; setup stops with a visible error if dependencies cannot be installed. Hardware compatibility depends on the OS audio drivers.

Re-running setup keeps existing settings by default and builds a new runtime before switching versions. Conversations and files remain in `/opt/athena/data`. Services can be managed in the dashboard. Setup offers optional VPN installation and configuration import, then leaves the VPN stopped. You need your own VPN account and a kernel with TUN support. Setup also offers guided Teams sign-in.

## Build and verify releases

Maintainers use Python 3.11+, project dependencies, PyInstaller and Inno Setup:

```sh
python packaging/build.py --linux
python packaging/build.py --windows --iscc "C:/path/to/ISCC.exe"
```

Output is under `dist/installers`, with `SHA256SUMS.txt`. Builds select source and public examples explicitly; they do not include developer keys, conversations, logs, local models or machine addresses. Linux `--check` verifies the extracted package without changing the system. Windows executables support `--self-test` without microphone capture or speaker playback.
