# build.ps1 — собирает BindBoard: EXE (по BindBoard.spec) → самопроверка → установщик/ZIP
# Запускать из папки проекта (C:\BindBoard\)
Set-Location $PSScriptRoot

$ErrorActionPreference = 'Stop'

# ── 1. Зависимости Python ────────────────────────────────────────
Write-Host ""
Write-Host "=== [1/4] Устанавливаем зависимости ===" -ForegroundColor Cyan
pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip завершился с ошибкой" -ForegroundColor Red; exit 1 }

# ── 2. Собираем EXE через PyInstaller (настройки — в BindBoard.spec) ──
Write-Host ""
Write-Host "=== [2/4] Собираем BindBoard.exe ===" -ForegroundColor Cyan
pyinstaller BindBoard.spec --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller завершился с ошибкой" -ForegroundColor Red; exit 1 }

# ── 3. Самопроверка собранного EXE (во временной папке, реальный конфиг не трогаем) ──
Write-Host ""
Write-Host "=== [3/4] Проверяем сборку (--selftest) ===" -ForegroundColor Cyan
$testHome = Join-Path $env:TEMP ("bindboard-selftest-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testHome | Out-Null
$env:BINDBOARD_HOME = $testHome
$proc = Start-Process -FilePath "dist\BindBoard\BindBoard.exe" -ArgumentList '--selftest' -PassThru -Wait
Remove-Item Env:\BINDBOARD_HOME
if ($proc.ExitCode -ne 0) {
    Write-Host "  Самопроверка не прошла (код $($proc.ExitCode)):" -ForegroundColor Red
    Get-Content (Join-Path $testHome 'selftest.json') -ErrorAction SilentlyContinue
    exit 1
}
Remove-Item -Recurse -Force $testHome
Write-Host "  OK" -ForegroundColor Green

# ── 4. Ищем Inno Setup Compiler ─────────────────────────────────
Write-Host ""
Write-Host "=== [4/4] Собираем установщик ===" -ForegroundColor Cyan

$isccPaths = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 5\ISCC.exe"
)
$iscc = $isccPaths | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    # Пробуем winget
    Write-Host "  Inno Setup не найден, устанавливаем через winget..." -ForegroundColor Yellow
    winget install --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements 2>$null
    Start-Sleep -Seconds 3
    $iscc = $isccPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if (-not $iscc) {
    Write-Host ""
    Write-Host "  Inno Setup не найден." -ForegroundColor Yellow
    Write-Host "  Скачай с https://jrsoftware.org/isdl.php и запусти build.ps1 снова." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Пока что ZIP:" -ForegroundColor Cyan
    $ver = Get-Date -Format "yyyy-MM-dd"
    Compress-Archive -Path "dist\BindBoard\*" -DestinationPath "dist\BindBoard-$ver.zip" -Force
    Write-Host "  dist\BindBoard-$ver.zip" -ForegroundColor Yellow
    exit 0
}

Write-Host "  Используем: $iscc" -ForegroundColor DarkGray
& $iscc "setup.iss"

if ($LASTEXITCODE -ne 0) { Write-Host "Inno Setup завершился с ошибкой" -ForegroundColor Red; exit 1 }

# ── Итог ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "  Готово! Файл для раздачи:" -ForegroundColor Green
Write-Host ""
Write-Host "  dist\BindBoard-Setup.exe" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Инструкция для друга:" -ForegroundColor Cyan
Write-Host "    1. Запустить BindBoard-Setup.exe"
Write-Host "    2. Нажать Далее / Далее / Установить"
Write-Host "    3. Готово — ярлык на рабочем столе"
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
