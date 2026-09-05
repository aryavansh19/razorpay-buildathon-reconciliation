<#
.SYNOPSIS
    Windows convenience wrapper. Equivalent to the Makefile targets.

.DESCRIPTION
    Prefers the local .venv interpreter when one exists, otherwise falls back to
    whatever `python` resolves to. There are no dependencies to install.

.EXAMPLE
    .\run.ps1 demo
    .\run.ps1 sweep -Runs 200
    .\run.ps1 policy
    .\run.ps1 backends
    .\run.ps1 verify
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('demo', 'walkthrough', 'record', 'serve', 'publish', 'sweep', 'policy', 'backends', 'qa', 'ask', 'verify', 'clean', 'help')]
    [string]$Task = 'help',

    [int]$Runs = 200
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

switch ($Task) {
    'demo' { & $python -m recon.cli }
    'walkthrough' { & $python -m recon.demo }
    'record' { & (Join-Path $root 'tools\record_walkthrough.ps1') -Pace 1.8 }
    'serve' { & $python -m recon.serve }
    'publish' {
        & $python -m recon.cli --publish --quiet
        if ($LASTEXITCODE -ne 0) { throw "publish failed with exit $LASTEXITCODE" }
        Write-Host 'Wrote docs/index.html' -ForegroundColor Green
    }
    'sweep' { & $python -m recon.evals sweep --runs $Runs }
    'policy' { & $python -m recon.evals policy --runs 40 }
    'backends' { & $python -m recon.evals backends }
    'qa' { & $python -m recon.evals qa }
    'ask' { & $python -m recon.ask }
    'verify' {
        & $python -m recon.cli --strict --quiet
        if ($LASTEXITCODE -ne 0) { throw "recon.cli --strict failed with exit $LASTEXITCODE" }
        & $python -m recon.evals sweep --runs 40
        if ($LASTEXITCODE -ne 0) { throw "seed sweep failed with exit $LASTEXITCODE" }
        & $python -m recon.evals qa
        if ($LASTEXITCODE -ne 0) { throw "Q&A golden set failed with exit $LASTEXITCODE" }
        Write-Host 'verify: all checks passed' -ForegroundColor Green
    }
    'clean' {
        foreach ($dir in @('data', 'reports')) {
            $path = Join-Path $root $dir
            if (Test-Path $path) { Remove-Item -Recurse -Force $path }
        }
        Write-Host 'Removed data/ and reports/'
    }
    default {
        Write-Host 'Tasks:'
        Write-Host '  demo        Generate a batch, reconcile it, write the report'
        Write-Host '  walkthrough Paced 7-beat terminal demo, for screen recording'
        Write-Host '  record      Screen-record the walkthrough to media\walkthrough.mp4 (silent)'
        Write-Host '  serve       Local dashboard with the question box enabled'
        Write-Host '  publish     Write docs/index.html for GitHub Pages'
        Write-Host '  sweep       Reconcile many independently generated batches'
        Write-Host '  policy      Measure the inactive-order matching policy both ways'
        Write-Host '  backends    Compare the offline baseline against the hosted model'
        Write-Host '  qa          Grade the Q&A agent against the golden question set'
        Write-Host '  ask         Interactive Q&A over the reconciled run'
        Write-Host '  verify      Strict demo, seed sweep and Q&A golden set'
        Write-Host '  clean       Remove generated data and reports'
    }
}

exit $LASTEXITCODE
