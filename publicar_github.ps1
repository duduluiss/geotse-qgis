# Script de inicialização e envio automático para o GitHub (duduluiss/geotse-qgis)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "[AVISO] O Git não está instalado no PATH. Você pode instalar via 'winget install Git.Git' ou subir os arquivos pelo GitHub Desktop / navegador." -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path ".git")) {
    git init
}

git config user.name "Luís Eduardo Araújo de Souza"
git config user.email "luisaraujodesouza.geo@gmail.com"
git branch -M main

$remotes = git remote
if ($remotes -notcontains "origin") {
    git remote add origin "https://github.com/duduluiss/geotse-qgis.git"
} else {
    git remote set-url origin "https://github.com/duduluiss/geotse-qgis.git"
}

git add .
git commit -m "Release v1.0.0 - GeoTSE - Espacializador Eleitoral (QGIS 3 & QGIS 4)"
git tag -f v1.0.0

Write-Host "Enviando código e tag v1.0.0 para https://github.com/duduluiss/geotse-qgis ..." -ForegroundColor Cyan
git push -u origin main
git push origin v1.0.0 --force
Write-Host "[OK] Repositório e Release v1.0.0 publicados com sucesso!" -ForegroundColor Green
