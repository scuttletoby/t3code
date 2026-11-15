# fork-kit/desk/windows/start-desk.ps1: a Windows PC as a fork fleet desk pool, through one WSL2 distro
# (fork-kit/desk/README.md, "Windows"). Run it in a normal Windows PowerShell window (not as administrator):
#   powershell -ExecutionPolicy Bypass -File start-desk.ps1 -Repo OWNER/REPO -Sha RELEASE_SHA -MachineFile machine.json
#     [-FleetConfig fork-fleet.json] [-Distro fork-desk] [-MemoryGB N] [-SwapGB 4] [-Uninstall]
# It prints this PC's facts and asks before it changes anything. Safe to run again: every step checks what is there.
# Steps: the distro (Ubuntu 24.04); .wslconfig's [wsl2] memory cap and NAT (a dated copy of the old file is kept;
# other keys stay); the distro's own settings (the WSL walls: no interop, no Windows PATH or drives, its own
# resolv.conf, systemd); one `wsl --shutdown`; the machine file; the desk kit (bootstrap.sh, in the distro); a logon
# task that keeps the distro running; then `desk-install check` and this side's checks.
param(
  [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repo,   # both required, except with -Uninstall
  [ValidatePattern('^[0-9a-f]{40}$')][string]$Sha,
  [string]$MachineFile, [string]$FleetConfig,
  [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')][string]$Distro = 'fork-desk',
  [ValidateRange(0, 4096)][int]$MemoryGB = 0, [ValidateRange(0, 4096)][int]$SwapGB = 4,
  [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$env:WSL_UTF8 = '1'                      # wsl.exe's own lines in UTF-8, not UTF-16
$OutputEncoding = [Text.UTF8Encoding]::new($false)   # what is piped into wsl.exe: UTF-8, not ASCII
$Task = "fork-desk-$Distro"
$WslConfig = Join-Path $env:USERPROFILE '.wslconfig'

function Head([string]$t) { Write-Host "==== $t" -ForegroundColor Cyan }
function Run([string]$what, [scriptblock]$cmd) {
  & $cmd
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}
# PowerShell ends what it pipes into a program with CR LF: the script ends in a comment line, so bash never sees a
# stray CR, and LF only before it.
function Script([string]$s) { ($s -replace "`r", '') + "`n# end" }
function InDistro([string]$what, [string]$script) {   # bash as root in the distro, the script on stdin; its output
  Script $script | wsl.exe -d $Distro -u root --cd / -- bash -s
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}
function Into([string]$file, [string]$dest) {          # a Windows file into the distro, root-owned, mode 644
  if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "no such file: $file" }
  $text = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $file).ProviderPath) -replace "`r", ''   # UNC paths too
  $dir = $dest.Substring(0, $dest.LastIndexOf('/'))   # no quotes in the command: Windows PowerShell mangles them
  $text | wsl.exe -d $Distro -u root --cd / -- sh -c "umask 022; mkdir -p $dir && cat > $dest.new && mv $dest.new $dest"
  if ($LASTEXITCODE -ne 0) { throw "copying $file into the distro failed (exit $LASTEXITCODE)" }
}

# The distro's own settings, the ones only a restart of WSL applies. ROOT is for the tests only.
$Prepare = @'
set -eu
ROOT=${ROOT:-}
want='[boot]
systemd=true
[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
[network]
generateResolvConf=false'
mkdir -p "$ROOT/etc/binfmt.d"
if [ "$(cat "$ROOT/etc/wsl.conf" 2>/dev/null)" != "$want" ]; then
  [ ! -e "$ROOT/etc/wsl.conf" ] || cp -p "$ROOT/etc/wsl.conf" "$ROOT/etc/wsl.conf.bak-$(date +%Y%m%d-%H%M%S)"
  printf '%s\n' "$want" > "$ROOT/etc/wsl.conf.new" && mv "$ROOT/etc/wsl.conf.new" "$ROOT/etc/wsl.conf"
  echo 'changed: /etc/wsl.conf'
fi
# WSL's resolv.conf is a link into /mnt/wsl; a file of the distro's own instead (dnsTunneling answers at this address)
if [ -L "$ROOT/etc/resolv.conf" ] || [ ! -f "$ROOT/etc/resolv.conf" ]; then
  rm -f "$ROOT/etc/resolv.conf"
  printf 'nameserver 10.255.255.254\n' > "$ROOT/etc/resolv.conf"
  echo 'changed: /etc/resolv.conf'
fi
[ "$(readlink "$ROOT/etc/binfmt.d/WSLInterop.conf" 2>/dev/null)" = /dev/null ] ||
  { ln -sfn /dev/null "$ROOT/etc/binfmt.d/WSLInterop.conf"; echo 'changed: /etc/binfmt.d/WSLInterop.conf'; }
mkdir -p "$ROOT/etc/fork-kit"   # this distro is the desk's now: a later run asks no takeover question
[ -e "$ROOT/etc/fork-kit/desk-distro" ] || echo 'made or taken over by start-desk.ps1' > "$ROOT/etc/fork-kit/desk-distro"
'@
# What the running WSL has now, whatever this run changed: a run stopped before its `wsl --shutdown` (or settings
# changed by hand) still restarts. One line per reason; nothing when the live state is what the walls need.
$Live = @'
set -eu
ROOT=${ROOT:-}
[ "$(cat "$ROOT/proc/1/comm" 2>/dev/null)" = systemd ] || echo 'restart: PID 1 is not systemd'
for h in "$ROOT"/proc/sys/fs/binfmt_misc/WSLInterop*; do
  [ ! -e "$h" ] || { echo "restart: WSL interop is live (${h##*/})"; break; }
done
[ ! "$ROOT/etc/wsl.conf" -nt "$ROOT/proc/1" ] || echo 'restart: /etc/wsl.conf is newer than the running distro'
mem=$(sed -n 's/^MemTotal: *\([0-9]*\) kB$/\1/p' "$ROOT/proc/meminfo" 2>/dev/null || true)
[ -z "${WANT_KB:-}" ] || [ -z "$mem" ] || [ "$mem" -le "$WANT_KB" ] || echo "restart: WSL has $mem kB, above the cap"
mode=$(wslinfo --networking-mode 2>/dev/null || true)
[ -z "$mode" ] || [ "$mode" = nat ] || echo "restart: the networking mode is $mode, not nat"
'@
# .wslconfig, read and written as UTF-8 without a BOM: Windows PowerShell 5.1's Get-Content reads a file without a
# BOM as ANSI and Set-Content -Encoding ascii turns every other letter into '?' (a kernel= path with an accent in it
# then stops every distro). The tests run this part alone (between the two marker lines) under pwsh.
# --- wslconfig ---
function Read-WslConfig([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return @() }
  @([IO.File]::ReadAllLines($path, [Text.UTF8Encoding]::new($false)))   # a BOM (UTF-8 or UTF-16) is still read
}
function Wsl2Keys([string[]]$lines) {   # the keys of the [wsl2] section only
  $keys = @{}; $sec = ''
  foreach ($l in $lines) {
    if ($l -match '^\s*\[(.+)\]\s*$') { $sec = $Matches[1].Trim().ToLower(); continue }
    if ($sec -eq 'wsl2' -and $l -match '^\s*([^=#;\s]+)\s*=\s*(.*?)\s*$') { $keys[$Matches[1]] = $Matches[2] }
  }
  $keys
}
function Merge-WslConfig([string]$path, $want) {   # True when it changed the file (a dated copy of the old one kept)
  $lines = Read-WslConfig $path
  $out = [Collections.Generic.List[string]]::new(); $sec = ''; $done = @{}
  function AddMissing { foreach ($k in $want.Keys) { if (-not $done[$k]) { $out.Add("$k=$($want[$k])"); $done[$k] = 1 } } }
  foreach ($l in $lines) {
    if ($l -match '^\s*\[(.+)\]\s*$') { if ($sec -eq 'wsl2') { AddMissing }; $sec = $Matches[1].Trim().ToLower(); $out.Add($l); continue }
    if ($sec -eq 'wsl2' -and $l -match '^\s*([^=#;\s]+)\s*=' -and $want.Contains($Matches[1])) {
      $k = $Matches[1]; if (-not $done[$k]) { $out.Add("$k=$($want[$k])"); $done[$k] = 1 }; continue }
    $out.Add($l)
  }
  if ($sec -eq 'wsl2') { AddMissing }
  if ($done.Count -lt $want.Count) { $out.Add('[wsl2]'); AddMissing }
  if (($out -join "`n") -eq ($lines -join "`n")) { return $false }
  if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path "$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" }
  [IO.File]::WriteAllLines("$path.new", [string[]]$out, [Text.UTF8Encoding]::new($false))
  Move-Item -LiteralPath "$path.new" $path -Force
  $true
}
# --- end wslconfig ---
$Packages = 'set -eu; export DEBIAN_FRONTEND=noninteractive; need=; for p in python3 nftables iproute2 curl ca-certificates openssh-client tar; do dpkg -s $p >/dev/null 2>&1 || need="$need $p"; done; [ -z "$need" ] || { apt-get update -q && apt-get install -y -q $need; }'

try {
  Head 'This PC'
  $os = Get-CimInstance Win32_OperatingSystem; $cs = Get-CimInstance Win32_ComputerSystem
  $ram = [math]::Floor($cs.TotalPhysicalMemory / 1GB)
  if ($MemoryGB -eq 0) { $MemoryGB = [math]::Max(4, [math]::Min([math]::Floor($ram * 3 / 4), $ram - 6)) }
  if (-not $Uninstall -and $MemoryGB -gt $ram - 2) { throw "-MemoryGB $MemoryGB leaves Windows less than 2 GB of this PC's $ram GB" }
  "Windows $($os.Version) build $($os.BuildNumber); $($cs.NumberOfLogicalProcessors) threads; RAM $ram GB"
  'WSL:'; wsl.exe --version
  $have = @(wsl.exe -l -q | ForEach-Object { $_.Trim([char]0, ' ') }) -contains $Distro
  "distro ${Distro}: $(if ($have) { 'there' } else { 'not yet' })"
  if (Test-Path $WslConfig) { ".wslconfig now:"; Read-WslConfig $WslConfig | ForEach-Object { "  $_" } }
  # A distro of that name that this script did not make: its /etc/wsl.conf is replaced below
  $ours = $false
  if ($have) { wsl.exe -d $Distro -u root --cd / -- test -e /etc/fork-kit/desk-distro; $ours = $LASTEXITCODE -eq 0 }

  if ($Uninstall) {
    if ((Read-Host "Remove the desk kit from $Distro and the logon task $Task? Type yes") -ne 'yes') { 'Stopped: nothing changed.'; return }
    if ($have) { InDistro 'desk-install uninstall' '[ ! -x /opt/fork-fleet/desk-install ] || /opt/fork-fleet/desk-install uninstall' }
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    "Done. The distro and .wslconfig are kept; to remove the distro too: wsl --unregister $Distro"
    return
  }
  if (-not $MachineFile) { throw '-MachineFile is required: the machine file with the desk section (README)' }
  if (-not $Repo -or -not $Sha) { throw '-Repo and -Sha are required (the fork and its release commit)' }

  Write-Host ''
  Write-Host "Next: the distro $Distro; .wslconfig [wsl2] memory=${MemoryGB}GB swap=${SwapGB}GB networkingMode=nat" -ForegroundColor Yellow
  Write-Host 'dnsTunneling=true guiApplications=false (for ALL your WSL distros; a dated copy is kept); then ONE' -ForegroundColor Yellow
  Write-Host 'wsl --shutdown (every WSL distro and Docker Desktop stop), the desk kit, and a logon task.' -ForegroundColor Yellow
  if ($have -and -not $ours) {
    Write-Host "The distro $Distro is already there and this script did not make it. Its /etc/wsl.conf is REPLACED" -ForegroundColor Yellow
    Write-Host '(its [user] default= and every other line go; no /mnt/c, no Windows programs or PATH), and it runs fleet' -ForegroundColor Yellow
    Write-Host 'jobs from now on. Pick another -Distro to keep it as it is.' -ForegroundColor Yellow
    if ((Read-Host "Type the distro's name ($Distro) to take it over") -cne $Distro) { Write-Host 'Stopped: nothing changed.'; return }
  }
  elseif ((Read-Host 'Type yes to go on') -ne 'yes') { Write-Host 'Stopped: nothing changed.'; return }

  Head 'the distro'
  if (-not $have) {
    wsl.exe --install -d Ubuntu-24.04 --name $Distro --no-launch
    if ($LASTEXITCODE -ne 0) { throw "wsl --install failed (exit $LASTEXITCODE): run wsl --update (or restart Windows after WSL's own install), then this again" }
  }
  Run 'the first start of the distro' { wsl.exe -d $Distro -u root --cd / -- true }

  Head '.wslconfig'
  $want = [ordered]@{ memory = "${MemoryGB}GB"; swap = "${SwapGB}GB"; networkingMode = 'nat'; dnsTunneling = 'true'; guiApplications = 'false' }
  $restart = Merge-WslConfig $WslConfig $want
  if ($restart) { 'changed: .wslconfig' }

  Head "the distro's own settings"
  $changed = InDistro "the distro's settings" $Prepare
  $changed
  # The live state decides, not only what this run changed: a run stopped before its shutdown restarts next time
  $live = @(InDistro 'the running settings' ("WANT_KB=$([long]$MemoryGB * 1048576)`n" + $Live))
  $up = [double]((wsl.exe -d $Distro -u root --cd / -- cat /proc/uptime) -split ' ')[0]
  if ((Test-Path $WslConfig) -and ((Get-Date) - (Get-Item -LiteralPath $WslConfig).LastWriteTime).TotalSeconds -lt $up) {
    $live += 'restart: .wslconfig is newer than the running WSL'
  }
  $live
  if ($restart -or $changed -or $live) {
    Head 'wsl --shutdown (the new settings apply at the next start)'
    Run 'wsl --shutdown' { wsl.exe --shutdown }
  }
  InDistro 'the packages the fleet needs' $Packages

  Head 'the machine file and the desk kit'
  Into $MachineFile '/etc/fork-kit/machine.json'
  $extra = ''
  if ($FleetConfig) { Into $FleetConfig '/etc/fork-kit/desk-fleet.json'; $extra = ' --fleet-config /etc/fork-kit/desk-fleet.json' }
  InDistro 'the desk kit (its FAIL line above says why)' @"
set -eu
curl -fsSL --proto =https https://raw.githubusercontent.com/$Repo/$Sha/fork-kit/desk/bootstrap.sh -o /root/desk-bootstrap.sh
sh /root/desk-bootstrap.sh --repo $Repo --sha $Sha$extra
"@

  Head "the logon task $Task (keeps the distro running while you are logged in)"
  $a = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument "--headless wsl.exe -d $Distro -u root --cd / -- sleep infinity"
  $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  Register-ScheduledTask -TaskName $Task -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $me) `
    -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force | Out-Null
  if ((Get-ScheduledTask $Task).State -ne 'Running') { Start-ScheduledTask $Task }

  Head 'check'
  $bad = 0
  wsl.exe -d $Distro -u root --cd / -- /opt/fork-fleet/desk-install check
  if ($LASTEXITCODE -ne 0) { $bad++ }
  $cfg = Wsl2Keys (Read-WslConfig $WslConfig)
  foreach ($k in $want.Keys) {
    if ($cfg[$k] -eq $want[$k]) { "PASS  wslconfig  [wsl2] $k=$($want[$k])" } else { "FAIL  wslconfig  [wsl2] no $k=$($want[$k])"; $bad++ }
  }
  $t = Get-ScheduledTask $Task -ErrorAction SilentlyContinue
  if ($t -and $t.State -eq 'Running') { "PASS  logon-task  $Task running" } else { "FAIL  logon-task  $Task is $(if ($t) { $t.State } else { 'missing' })"; $bad++ }
  if ($bad -eq 0) { Write-Host 'ALL PASS: this PC takes fleet jobs while you are logged in.' -ForegroundColor Green }
  else { Write-Host 'Some FAIL lines above (a template FAIL clears by itself in a few minutes; run this again then).' -ForegroundColor Red; exit 1 }
}
catch {
  Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host 'Nothing printed here is a secret. Fix it and run this again: every step checks what is there.' -ForegroundColor Red
  exit 1
}
