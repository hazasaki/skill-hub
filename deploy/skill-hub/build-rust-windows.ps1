param(
    [string]$DockerContext = 'desktop-linux',
    [ValidateSet('refs', 'near', 'metadata')][string[]]$Library = @('refs', 'near', 'metadata'),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'rust/build')
)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path (Join-Path $PSScriptRoot 'rust')).Path.Replace('\', '/')
$output = (New-Item -ItemType Directory -Force $OutputDirectory).FullName.Replace('\', '/')
$build = @'
set -eu
apt-get update -qq
apt-get install -y -qq gcc-mingw-w64-x86-64 >/dev/null
rustup target add x86_64-pc-windows-gnu
for lib do
    rustc -O --target x86_64-pc-windows-gnu --crate-type cdylib "/src/$lib.rs" -o "/out/skill_hub_$lib.dll"
done
'@
$build = $build.Replace([string][char]13, '')
docker --context $DockerContext run --rm -v "${source}:/src:ro" -v "${output}:/out" 'rust:1.85-slim@sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36' sh -c $build sh @Library
if ($LASTEXITCODE -ne 0) { throw "Windows DLL build failed with exit code $LASTEXITCODE" }
