#ifndef Variant
  #define Variant "Standalone"
#endif
#define AppLabel "ATHENA " + Variant
#define AppExe "ATHENA " + Variant + ".exe"
[Setup]
AppId=ATHENA-{#Variant}
AppName={#AppLabel}
AppVersion=0.1.0.89
DefaultDirName={localappdata}\Programs\{#AppLabel}
DefaultGroupName={#AppLabel}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installers
OutputBaseFilename=ATHENA-{#Variant}-Windows-Setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
SetupLogging=yes
[Files]
Source: "..\dist\{#AppLabel}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\docs\installation.md"; DestDir: "{app}"; DestName: "Read me.txt"
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"
Source: "GPL-3.0.txt"; DestDir: "{app}"
[Icons]
Name: "{group}\{#AppLabel}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppLabel}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{group}\Uninstall {#AppLabel}"; Filename: "{uninstallexe}"
[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppLabel}"; Flags: postinstall nowait skipifsilent
[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--stop-all"; Flags: runhidden waituntilterminated
