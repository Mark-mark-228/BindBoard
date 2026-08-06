; BindBoard — Inno Setup script
; Собирает BindBoard-Setup.exe (не требует прав администратора)

#define AppName "BindBoard"
#define AppVersion "1.0"
#define AppExe "BindBoard.exe"

[Setup]
AppId={{B1ND-B04RD-2024-MARK-AABB}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=BindBoard
AppPublisherURL=https://github.com
UninstallDisplayName=BindBoard
UninstallDisplayIcon={app}\{#AppExe}

; Устанавливается в %LocalAppData%\BindBoard — без прав админа
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

OutputDir=dist
OutputBaseFilename=BindBoard-Setup
SetupIconFile=bindboard.ico
WizardStyle=modern
WizardResizable=no

Compression=lzma2/ultra64
SolidCompression=yes
DirExistsWarning=no
DisableProgramGroupPage=yes

; Минимальная версия Windows 10
MinVersion=10.0.17763

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
russian.CreateDesktopIcon=Создать ярлык на рабочем столе
russian.RunAfterInstall=Запустить BindBoard после установки
english.CreateDesktopIcon=Create desktop shortcut
english.RunAfterInstall=Launch BindBoard after install

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\BindBoard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";          Filename: "{app}\{#AppExe}"
Name: "{group}\Удалить BindBoard";   Filename: "{uninstallexe}"; Languages: russian
Name: "{group}\Uninstall BindBoard"; Filename: "{uninstallexe}"; Languages: english
Name: "{userdesktop}\{#AppName}";    Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:RunAfterInstall}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Удаляем конфиг пользователя только если он сам захочет (не трогаем автоматически)
; Type: filesandordirs; Name: "{localappdata}\BindBoard\BindBoard"
