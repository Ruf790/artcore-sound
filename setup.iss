; ArtCore Sound installer — Inno Setup 6.3+
; Build: run build.bat (FFmpeg + PyInstaller first, then this script).
; Expects the PyInstaller output (with ffmpeg\ inside) in dist\ArtCoreSound\.

#define AppName      "ArtCore Sound"
#define AppVersion   "1.0.0"
#define AppPublisher "ArtCore"
#define AppURL       "https://github.com/Ruf790/artcore-sound"
#define AppExe       "ArtCoreSound.exe"

[Setup]
; Never change AppId: Windows uses it to find the installed copy for updates/uninstall
AppId={{A89CF4C3-5FAE-4C45-8E60-B5DE0CABB709}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=installer
OutputBaseFilename=ArtCoreSound-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
AppCopyright=Copyright (C) 2026 Ruf790 — GNU GPL v3
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
AppMutex=ArtCoreSoundMutex
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto
UsedUserAreasWarning=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ru.GroupIntegration=Интеграция с Windows:
en.GroupIntegration=Windows integration:
ru.OpenWith=«Открыть с помощью → ArtCore Sound» для звуковых файлов и видео
en.OpenWith="Open with → ArtCore Sound" for sound and video files

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "openwith";    Description: "{cm:OpenWith}";          GroupDescription: "{cm:GroupIntegration}"

[Files]
Source: "dist\ArtCoreSound\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "THIRD-PARTY.md"; DestDir: "{app}"; DestName: "THIRD-PARTY.txt"; Flags: ignoreversion

[InstallDelete]
; an older bundled FFmpeg goes away completely (its DLL names change between versions)
Type: filesandordirs; Name: "{app}\ffmpeg"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "ArtCore.Sound"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; HKA = HKLM for "all users", HKCU for "just me". «Open with» only — no file type is taken over.
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: openwith
; every format the editor opens (sound/__init__.py: AUDIO_EXT + VIDEO_EXT; a test keeps the two lists equal)
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".wav"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".flac"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mp3"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".m4a"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".aac"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".ac3"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".eac3"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".ec3"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".dts"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".dtshd"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".thd"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mka"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".ogg"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".oga"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".opus"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".wma"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".ape"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".wv"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".aiff"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".aif"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".aifc"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".alac"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mp2"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".tta"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".caf"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".w64"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".au"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".snd"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".amr"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mpc"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".spx"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mkv"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mp4"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".m4v"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mov"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".avi"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".wmv"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".asf"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".flv"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".webm"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".ts"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".m2ts"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mts"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mpg"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mpeg"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".vob"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".mxf"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".3gp"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".ogv"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".rm"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".rmvb"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".divx"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".f4v"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExe}\SupportedTypes"; ValueType: string; ValueName: ".evo"; ValueData: ""; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.wav\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.flac\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mp3\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.m4a\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.aac\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.ac3\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.eac3\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.ec3\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.dts\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.dtshd\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.thd\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mka\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.ogg\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.oga\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.opus\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.wma\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.ape\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.wv\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.aiff\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.aif\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.aifc\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.alac\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mp2\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.tta\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.caf\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.w64\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.au\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.snd\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.amr\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mpc\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.spx\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mkv\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mp4\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.m4v\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mov\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.avi\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.wmv\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.asf\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.flv\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.webm\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.ts\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.m2ts\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mts\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mpg\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mpeg\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.vob\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.mxf\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.3gp\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.ogv\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.rm\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.rmvb\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.divx\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.f4v\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith
Root: HKA; Subkey: "Software\Classes\.evo\OpenWithList\{#AppExe}"; ValueType: none; Flags: uninsdeletekey; Tasks: openwith

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; silent update from the program itself: start it again afterwards
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallDelete]
; Settings in %APPDATA%\ArtCoreSound are kept on purpose. The working files in %TEMP%\ArtCoreSound are removed
; by the program itself (each run its own folder; a folder left by a crash goes at the next start).
Type: filesandordirs; Name: "{app}"
