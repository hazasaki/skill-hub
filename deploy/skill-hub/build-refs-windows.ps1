param([string]$DockerContext = 'desktop-linux')
& (Join-Path $PSScriptRoot 'build-rust-windows.ps1') -DockerContext $DockerContext -Library 'refs' -OutputDirectory (Join-Path $PSScriptRoot 'rust')
