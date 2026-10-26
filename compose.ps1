param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArguments
)
$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$envFile = Join-Path $projectDirectory 'backend/.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw 'Missing backend/.env. Copy backend/.env.example and configure the model service.'
}
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExecutable = $dockerCommand.Source
} else {
    $dockerExecutable = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
    if (-not (Test-Path -LiteralPath $dockerExecutable)) {
        throw 'Docker executable was not found. Start Docker Desktop and check its installation.'
    }
}
if (-not $ComposeArguments) { $ComposeArguments = @('ps') }
& $dockerExecutable compose --project-name ordo --project-directory $projectDirectory --env-file $envFile -f (Join-Path $projectDirectory 'docker-compose.yml') @ComposeArguments
exit $LASTEXITCODE
