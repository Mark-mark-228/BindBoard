# build.ps1 — собирает BindBoard-Setup.exe
# Запускать из папки C:\BindBoard\
Set-Location $PSScriptRoot

$ErrorActionPreference = 'Stop'

# ── 1. Зависимости Python ────────────────────────────────────────
Write-Host ""
Write-Host "=== [1/3] Устанавливаем зависимости ===" -ForegroundColor Cyan
pip install pywebview pyinstaller --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "pip завершился с ошибкой" -ForegroundColor Red; exit 1 }

# ── 2. Собираем EXE через PyInstaller ───────────────────────────
Write-Host ""
Write-Host "=== [2/3] Собираем BindBoard.exe ===" -ForegroundColor Cyan
pyinstaller `
    --name "BindBoard" `
    --windowed `
    --onedir `
    --icon "bindboard.ico" `
    --add-data "app.html;." `
    --add-data "fonts;fonts" `
    --add-data "assets;assets" `
    --hidden-import "webview.platforms.winforms" `
    --hidden-import "clr" `
    --collect-all "webview" `
    --noconfirm `
    app.py

if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller завершился с ошибкой" -ForegroundColor Red; exit 1 }

# ── 3. Ищем Inno Setup Compiler ─────────────────────────────────
Write-Host ""
Write-Host "=== [3/3] Собираем установщик ===" -ForegroundColor Cyan

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
