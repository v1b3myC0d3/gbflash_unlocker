$ErrorActionPreference = "Stop"
if (-not $env:PYINSTALLER_CONFIG_DIR) {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location) ".pyinstaller"
}
python -m PyInstaller --clean --noconfirm gbflash_unlock.spec
Write-Host "Windows executable directory: dist\\gbflash-unlock"
