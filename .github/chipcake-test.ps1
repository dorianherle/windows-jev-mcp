# ChipCake studio, driven only by windows-jev on the hosted Windows desktop.
# 1. Shell: tagline, blank c-Si stack, working area 20 um, substrate 525 um.
# 2. Sample device: Herle 2023 flow, steps 00 through 08.
# 3. Open steps 01, 03, 05, 08 and check the step that appears.
# 4. Material data, Model details, step lists, 3D controls.
# 5. Dark mode and back. Export menu. Tutorial must have a control name.
# 6. New flow on SOI, then add a step and a layer.
$ErrorActionPreference = "Continue"
$PSNativeCommandUseErrorActionPreference = $false
$env:PYTHONIOENCODING = "utf-8"

$jev = Join-Path (Get-Location) ".venv\Scripts\windows-jev.exe"
$failed = New-Object System.Collections.Generic.List[string]

function Write-Result([string]$Name, [bool]$Ok, [string]$Detail) {
    if ($Ok) { Write-Host "PASS  $Name" }
    else {
        Write-Host "FAIL  $Name -- $Detail"
        $script:failed.Add($Name)
    }
}

function Invoke-Jev([string[]]$Command) {
    Write-Host ">> windows-jev $($Command -join ' | ')"
    $text = & $jev @Command 2>&1 | Out-String
    Write-Host $text
    return $text
}

function Get-Screen {
    return Invoke-Jev @("describe", "chipcake")
}

function Click-Control([string]$Name) {
    $text = Invoke-Jev @("run", "chipcake", $Name)
    $done = $text -match '"status": "done"'
    Write-Result "click $Name" $done $(if ($done) { "" } else { "status was not done" })
    return $text
}

& $jev check
if ($LASTEXITCODE -ne 0) { throw "windows-jev check failed" }

$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
Start-Process $edge -ArgumentList @("--guest", "--no-first-run", "--start-maximized", "https://chipcake.com")

$shell = ""
foreach ($i in 1..20) {
    Start-Sleep -Seconds 3
    Write-Host "---- windows ----"
    Invoke-Jev @("windows") | Out-Null
    $shell = Get-Screen
    if ($shell -match "Sample device" -and $shell -match "Process Flows") { break }
}
Write-Result "app shell" ($shell -match "Sample device" -and $shell -match "Process Flows. Piece of cake.") "shell never showed the studio"
Write-Result "blank stack is c-Si" ($shell -match "c-Si" -and $shell -match "Initial stack") "expected the starting substrate"
Write-Result "working area 20 um" ($shell -match "contains '20'") "width or depth was not 20"
Write-Result "substrate 525 um" ($shell -match "contains '525'") "substrate thickness was not 525"
Write-Result "Tutorial has a control name" ($shell -match '"Tutorial"') "Tutorial is not in the button list"
if ($script:failed.Contains("app shell")) {
    Write-Host "---- summary: $($failed.Count) failed ----"
    exit 1
}

Click-Control "Fit to stack" | Out-Null

$sample = Click-Control "Sample device"
Write-Result "sample device changed the screen" ($sample -match '"screen_changed": true') "click did not change the screen"
$flow = Get-Screen
if ($flow -match "Dismiss notification") { Click-Control "Dismiss notification" | Out-Null; $flow = Get-Screen }
Write-Result "Herle 2023 flow" ($flow -match "Herle 2023") "sample title missing"
foreach ($label in @(
    "Go to step 00: Initial stack",
    "Go to step 01: Sputter (PVD)",
    "Go to step 02: Spin coat",
    "Go to step 03: E-beam litho",
    "Go to step 04: Develop",
    "Go to step 05: RIE",
    "Go to step 06: RIE",
    "Go to step 07: Strip / lift-off",
    "Go to step 08: Vapor release"
)) {
    Write-Result "step listed: $label" ($flow -match [regex]::Escape($label)) "button missing"
}

function Open-Step([string]$Control, [string]$Expect) {
    $click = Invoke-Jev @("run", "chipcake", $Control)
    Write-Result "click $Control" ($click -match '"status": "done"') "status was not done"
    Write-Result "screen changed for $Control" ($click -match '"screen_changed": true') "selection did not change the screen"
    $screen = Get-Screen
    Write-Result "detail for $Control" ($screen -match [regex]::Escape($Expect)) "screen lacked $Expect"
    return $screen
}

Open-Step "Go to step 01: Sputter (PVD)" "contains 'Sputter (PVD)'" | Out-Null
Open-Step "Go to step 03: E-beam litho" "contains 'E-beam litho'" | Out-Null
Open-Step "Go to step 05: RIE" "Reactive Ion" | Out-Null
$vapor = Open-Step "Go to step 08: Vapor release" "contains 'Vapor release'"
Write-Result "vapor etch depth 0.06" ($vapor -match "contains '0.06'") "etch depth field was not 0.06"
Write-Result "vapor reference SiO2" ($vapor -match "contains 'SiO2'") "reference material was not SiO2"
Open-Step "Go to step 00: Initial stack" "c-Si" | Out-Null

foreach ($extra in @("Material data", "Model details", "All process steps", "Your steps", "Fit to view", "Zoom in", "Zoom out", "Full substrate", "Dimensions", "3D device overview")) {
    Click-Control $extra | Out-Null
}

$dark = Click-Control "Switch to dark mode"
$darkScreen = Get-Screen
Write-Result "dark mode" ($darkScreen -match "light") "theme control did not offer light mode"
$lightName = "Switch to light mode"
if ($darkScreen -match '"Switch to light[^"]*"') { $lightName = $Matches[0].Trim('"') }
Click-Control $lightName | Out-Null
$lightScreen = Get-Screen
Write-Result "back to light mode" ($lightScreen -match "Switch to dark mode") "dark-mode control did not return"

$export = "Export $([char]0x2304)"
Click-Control $export | Out-Null
$menu = Get-Screen
Write-Result "export menu" ($menu -match "PNG|PDF|PPTX|Shapes") "export actions did not appear"
if ($menu -match '"Overview"') { Click-Control "Overview" | Out-Null }

Click-Control "New" | Out-Null
$menu = Get-Screen
Write-Result "new-flow substrates" ($menu -match '"SOI"' -and $menu -match '"Si"') "Si or SOI was not offered"
Click-Control "SOI" | Out-Null
Start-Sleep -Seconds 2
$fresh = Get-Screen
Write-Result "sample flow replaced" ($fresh -notmatch "Herle 2023") "Herle 2023 was still on screen"
Write-Result "new stack present" ($fresh -match "Initial stack" -or $fresh -match "SOI") "no stack after choosing SOI"
function Count-Hits([string]$Text, [string]$Needle) {
    return ([regex]::Matches($Text, [regex]::Escape($Needle))).Count
}
$stepsBefore = Count-Hits $fresh "Go to step"
$removesBefore = Count-Hits $fresh "remove step"
Click-Control "+ add step" | Out-Null
Start-Sleep -Seconds 1
$added = Get-Screen
$grew = (Count-Hits $added "Go to step") -gt $stepsBefore -or (Count-Hits $added "remove step") -gt $removesBefore
Write-Result "add step" $grew "step list did not grow"
if ($added -match "\+ add layer on top") { Click-Control "+ add layer on top" | Out-Null }

Write-Host "---- summary: $($failed.Count) failed ----"
$failed | ForEach-Object { Write-Host "FAIL  $_" }
if ($failed.Count -gt 0) { exit 1 }
