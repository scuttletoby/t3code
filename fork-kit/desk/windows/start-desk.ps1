# fork-kit/desk/windows/start-desk.ps1: a Windows PC as a fork fleet desk pool, through one WSL2 distro
# (fork-kit/desk/README.md, "Windows"). Run it in a normal Windows PowerShell window (not as administrator):
#   powershell -ExecutionPolicy Bypass -File start-desk.ps1 -Repo OWNER/REPO -Sha RELEASE_SHA -MachineFile machine.json
#     [-FleetConfig fork-fleet.json] [-Distro fork-desk] [-MemoryGB N] [-SwapGB 4] [-Uninstall]
# -MemoryGB is the [wsl2] memory cap; without it, a higher one already in .wslconfig is kept, up to the RAM less 2 GB.
# It prints this PC's facts and asks before it changes anything. Safe to run again: every step checks what is there.
# Steps: the distro (Ubuntu 24.04); .wslconfig's [wsl2] memory cap and NAT (a dated copy of the old file is kept;
# other keys stay); the distro's own settings (the WSL walls: no interop, no Windows PATH or drives, its own
# resolv.conf, systemd); one `wsl --shutdown`; the machine file; the desk kit (bootstrap.sh, in the distro); a logon
# task that keeps the distro running; then `desk-install check` and this side's checks.
param(
  [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')][string]$Repo,   # both required, except with -Uninstall
  [ValidateScript({ $_ -cmatch '^[0-9a-f]{40}$' })][string]$Sha,   # -cmatch: ValidatePattern ignores case
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
# Marks on the Windows side, for a run that stops: `DISTRO.made` (this script is installing the distro: a later run
# asks no takeover question), `wslconfig.restart` (.wslconfig or the distro's wsl.conf changed and its `wsl --shutdown`
# is still owed: VM uptime and clock skip sleep). .wslconfig is shared by every distro, so its mark is too: a run for another -Distro still owes it.
$Marks = Join-Path $env:LOCALAPPDATA 'fork-desk'
$Made = Join-Path $Marks "$Distro.made"; $Owed = Join-Path $Marks 'wslconfig.restart'
function Mark([string]$path) { New-Item -ItemType Directory -Force -Path $Marks | Out-Null; Set-Content -LiteralPath $path 'start-desk.ps1' }

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
if [ -n "${CHECK_ONLY:-}" ]; then   # only say whether wsl.conf will change: the owed mark is written before it is
  [ "$(cat "$ROOT/etc/wsl.conf" 2>/dev/null)" = "$want" ] || echo 'differs: /etc/wsl.conf'
  exit 0
fi
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
# when PID 1 started (boot time + its start in clock ticks; the mtime of /proc/1 is only when that entry was made)
tick=$(getconf CLK_TCK 2>/dev/null || echo 100)
since=$(sed -n 's/.*\x29 //p' "$ROOT/proc/1/stat" 2>/dev/null | cut -d' ' -f20)   # after the name: field 22
boot=$(sed -n 's/^btime \([0-9]*\)$/\1/p' "$ROOT/proc/stat" 2>/dev/null || true)
conf=$(stat -c %Y "$ROOT/etc/wsl.conf" 2>/dev/null || echo 0)
case "$since" in (''|*[!0-9]*) since= ;; esac
case "$boot" in (''|*[!0-9]*) since= ;; esac
if [ -z "$since" ]; then echo 'restart: the start time of PID 1 cannot be read'
elif [ "$conf" -ge $((boot + since / tick)) ]; then echo 'restart: /etc/wsl.conf is newer than the running distro'; fi
mem=$(sed -n 's/^MemTotal: *\([0-9]*\) kB$/\1/p' "$ROOT/proc/meminfo" 2>/dev/null || true)
[ -z "${WANT_KB:-}" ] || [ -z "$mem" ] || [ "$mem" -le "$WANT_KB" ] || echo "restart: WSL has $mem kB, above the cap"
mode=$(wslinfo --networking-mode 2>/dev/null || true)
[ -z "$mode" ] || [ "$mode" = nat ] || echo "restart: the networking mode is $mode, not nat"
# automount=false: no Windows drive mounted under /mnt
if awk '$2 ~ /^\/mnt\/[a-z]$/ { found = 1 } END { exit !found }' "$ROOT/proc/mounts" 2>/dev/null; then
  echo 'restart: Windows drives are mounted under /mnt'
fi
'@
# .wslconfig, read and written as UTF-8 without a BOM: Windows PowerShell 5.1's Get-Content reads a file without a
# BOM as ANSI and Set-Content with the ASCII encoding turns every other letter into '?' (a kernel= path with an accent in it
# then stops every distro). The tests run this part alone (between the two marker lines) under pwsh.
# --- wslconfig ---
function Read-WslConfig([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return @() }
  @([IO.File]::ReadAllLines($path, [Text.UTF8Encoding]::new($false)))   # a BOM (UTF-8 or UTF-16) is still read
}
# networkingMode and dnsTunneling were [experimental] keys before WSL 2.0; an old line there can still win over
# [wsl2], so Merge-WslConfig removes them there (the [wsl2] ones are what the desk needs) and the check FAILs on them.
$Experimental = @('networkingMode', 'dnsTunneling')
# The section after line $l, as WSL's ParseSection reads a header: space or tab, an open bracket, a letter then letters
# or digits, a close bracket, space or tab, then the end or a # comment; any case. A bracket line it rejects keeps the
# length of $sec of the letters read (none: $sec as it was; fewer pad with NUL and match no section). $null: no bracket.
function NextSection([string]$l, [string]$sec) {
  if ($l -cnotmatch '^[ \t]*\[(([A-Za-z][A-Za-z0-9]*)(\][ \t]*(#.*)?$)?)?') { return $null }
  if ($Matches[3]) { return $Matches[2].ToLower() }
  if ($Matches[2]) { return $Matches[2].ToLower().PadRight($sec.Length, [char]0).Substring(0, $sec.Length) }
  $sec
}
# A key=value line, trimmed of space and tab only, as WSL's config parser does: a non-breaking space or a form feed
# stays in the value (WSL then rejects a memory= value, so it is rewritten), where .NET's \s would drop it.
function SectionKeys([string[]]$lines, [string]$section) {   # the keys of one section only
  $keys = @{}; $sec = ''
  foreach ($l in $lines) {
    $next = NextSection $l $sec
    if ($null -ne $next) { $sec = $next; continue }
    if ($sec -eq $section -and $l -match '^[ \t]*([^=#; \t]+)[ \t]*=[ \t]*(.*?)[ \t]*$') { $keys[$Matches[1]] = $Matches[2] }
  }
  $keys
}
function Wsl2Keys([string[]]$lines) { SectionKeys $lines 'wsl2' }
# The merged lines and one note per change (old -> new). The question shows these notes and the file is written only
# when there is one, so "no change" always means the file is left alone. A [wsl2] line whose value already matches
# (trimmed, any case: as the final check compares) is kept as it is; a repeated key's later lines are removed.
function MergeLines([string[]]$lines, $want) {
  $out = [Collections.Generic.List[string]]::new(); $notes = [Collections.Generic.List[string]]::new(); $sec = ''; $done = @{}
  function AddMissing { foreach ($k in $want.Keys) { if (-not $done[$k]) { $out.Add("$k=$($want[$k])"); $done[$k] = 1; $notes.Add("  [wsl2] ${k}: (not set) -> $($want[$k])") } } }
  foreach ($l in $lines) {
    $next = NextSection $l $sec
    if ($null -ne $next) { if ($sec -eq 'wsl2' -and $next -ne 'wsl2') { AddMissing }; $sec = $next; $out.Add($l); continue }
    if ($sec -eq 'wsl2' -and $l -match '^[ \t]*([^=#; \t]+)[ \t]*=[ \t]*(.*?)[ \t]*$' -and $want.Contains($Matches[1])) {
      $k = $Matches[1]; $v = $Matches[2]
      if ($done[$k]) { $notes.Add("  [wsl2] $k=${v}: a repeated line, removed"); continue }
      $done[$k] = 1
      if ($v -eq $want[$k]) { $out.Add($l) } else { $out.Add("$k=$($want[$k])"); $notes.Add("  [wsl2] ${k}: $v -> $($want[$k])") }
      continue }
    if ($sec -eq 'experimental' -and $l -match '^[ \t]*([^=#; \t]+)[ \t]*=[ \t]*(.*?)[ \t]*$' -and $Matches[1] -in $Experimental) {
      $notes.Add("  [experimental] $($Matches[1])=$($Matches[2]): removed (the [wsl2] one is used)"); continue }
    $out.Add($l)
  }
  if ($sec -eq 'wsl2') { AddMissing }
  if ($done.Count -lt $want.Count) { $out.Add('[wsl2]'); AddMissing }
  [pscustomobject]@{ Out = $out; Notes = $notes }
}
function Changes([string[]]$lines, $want) { (MergeLines $lines $want).Notes }   # what Merge-WslConfig changes
function Merge-WslConfig([string]$path, $want, [scriptblock]$before = {}) {   # True when it changed the file (a dated copy of the old one kept; $before runs first)
  $merged = MergeLines (Read-WslConfig $path) $want
  if ($merged.Notes.Count -eq 0) { return $false }   # the same test as the question's
  & $before
  if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path "$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" }
  [IO.File]::WriteAllLines("$path.new", [string[]]$merged.Out, [Text.UTF8Encoding]::new($false))
  Move-Item -LiteralPath "$path.new" $path -Force
  $true
}
# A [wsl2] memory= value in kB (8GB, 8192MB, 8G; no unit: bytes), or -1 when WSL cannot read it. WSL's parser
# (ParseMemorySize) takes a number and B, K, KB, M, MB, G, GB, T or TB exactly: no space before the unit, upper case
# (`26 GB` or `26gb` makes it warn and run on half the RAM).
function CapKb([string]$v) {
  if ($v -cnotmatch '^([0-9]{1,13})([KMGT]?)B?$') { return [long]-1 }
  [long][math]::Min(1e15, [math]::Floor([double]$Matches[1] * [math]::Pow(1024, ' KMGT'.IndexOf($Matches[2])) / 1024))
}
# The memory cap to want: $gb GB, or the one already in .wslconfig, kept as it is written (neither lowered nor a FAIL,
# and the live check holds WSL to it) while WSL reads it and it is $gb to $most GB.
function WantMemory([string]$path, [int]$gb, [int]$most) {
  $have = (Wsl2Keys (Read-WslConfig $path))['memory']
  $kb = if ($have) { CapKb $have } else { -1 }
  if ($kb -ge [long]$gb * 1048576 -and $kb -le [long]$most * 1048576) { return $have }
  "${gb}GB"
}
function WslVersion([string[]]$lines, [string]$name) {   # the WSL version of one distro in `wsl -l -v`; 0: not there
  foreach ($l in $lines) {
    $f = @(($l.Trim([char]0, ' ', '*', "`t") -replace [char]0, '') -split '\s+')
    if ($f.Count -ge 3 -and $f[0] -eq $name -and $f[-1] -match '^[0-9]+$') { return [int]$f[-1] }
  }
  0
}
# --- end wslconfig ---
$Packages = 'set -eu; export DEBIAN_FRONTEND=noninteractive; need=; for p in python3 nftables iproute2 curl ca-certificates openssh-client tar; do dpkg -s $p >/dev/null 2>&1 || need="$need $p"; done; [ -z "$need" ] || { apt-get update -q && apt-get install -y -q $need; }'

try {
  Head 'This PC'
  $os = Get-CimInstance Win32_OperatingSystem; $cs = Get-CimInstance Win32_ComputerSystem
  $ram = [math]::Floor($cs.TotalPhysicalMemory / 1GB)
  $asked = $MemoryGB -ne 0
  if (-not $asked) { $MemoryGB = [math]::Max(4, [math]::Min([math]::Floor($ram * 3 / 4), $ram - 6)) }
  if (-not $Uninstall -and $MemoryGB -gt $ram - 2) {
    if ($asked) { throw "-MemoryGB $MemoryGB leaves Windows less than 2 GB of this PC's $ram GB" }
    throw "this PC has $ram GB of RAM: a desk needs at least 4 GB for WSL and 2 GB for Windows"
  }
  "Windows $($os.Version) build $($os.BuildNumber); $($cs.NumberOfLogicalProcessors) threads; RAM $ram GB"
  'WSL:'; wsl.exe --version
  $version = WslVersion @(wsl.exe -l -v) $Distro   # read without starting any distro
  $have = $version -ne 0
  "distro ${Distro}: $(if ($have) { "there (WSL $version)" } else { 'not yet' })"
  if (Test-Path $WslConfig) { ".wslconfig now:"; Read-WslConfig $WslConfig | ForEach-Object { "  $_" } }

  if ($Uninstall) {
    if ((Read-Host "Remove the desk kit from $Distro and the logon task $Task? Type yes") -ne 'yes') { 'Stopped: nothing changed.'; return }
    if ($have) { InDistro 'desk-install uninstall' '[ ! -x /opt/fork-fleet/desk-install ] || /opt/fork-fleet/desk-install uninstall' }
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue   # its `sleep infinity` too
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Made -ErrorAction SilentlyContinue   # an owed shutdown is kept: .wslconfig is shared
    $bak = @(Get-ChildItem -Path "$WslConfig.bak-*" -ErrorAction SilentlyContinue | Sort-Object Name)
    "Done. The distro and .wslconfig are kept; to remove the distro too: wsl --unregister $Distro"
    if ($bak) { ".wslconfig as it was before the first run: $($bak[0].FullName) (copy it back over .wslconfig to undo)" }
    return
  }
  if (-not $MachineFile) { throw '-MachineFile is required: the machine file with the desk section (README)' }
  if (-not $Repo -or -not $Sha) { throw '-Repo and -Sha are required (the fork and its release commit)' }
  # every input before the question, so a typo is found before anything changes
  if (-not (Test-Path -LiteralPath $MachineFile -PathType Leaf)) { throw "-MachineFile ${MachineFile}: no such file" }
  if ($FleetConfig -and -not (Test-Path -LiteralPath $FleetConfig -PathType Leaf)) { throw "-FleetConfig ${FleetConfig}: no such file" }
  # WSL1 runs no systemd: every run would restart WSL and check would never pass
  if ($have -and $version -ne 2) { throw "the distro $Distro is WSL $version; the desk needs WSL 2: wsl --set-version $Distro 2 (or pick another -Distro), then this again" }
  # A distro of that name that this script did not make: its /etc/wsl.conf is replaced below. (This starts it; it
  # changes nothing in it.) A run stopped while installing it left the Windows-side mark.
  $ours = Test-Path -LiteralPath $Made
  if ($have -and -not $ours) { wsl.exe -d $Distro -u root --cd / -- test -e /etc/fork-kit/desk-distro; $ours = $LASTEXITCODE -eq 0 }

  # -MemoryGB given: it is the cap; not given: a higher one a person set is kept, up to what leaves Windows 2 GB
  $most = if ($asked) { $MemoryGB } else { $ram - 2 }
  $want = [ordered]@{ memory = (WantMemory $WslConfig $MemoryGB $most); swap = "${SwapGB}GB"; networkingMode = 'nat'; dnsTunneling = 'true'; guiApplications = 'false' }
  $changes = @(Changes (Read-WslConfig $WslConfig) $want)
  Write-Host ''
  Write-Host "Next: the distro $Distro; .wslconfig (for ALL your WSL distros; a dated copy is kept):" -ForegroundColor Yellow
  if ($changes) { $changes | ForEach-Object { Write-Host $_ -ForegroundColor Yellow } } else { Write-Host '  no change' -ForegroundColor Yellow }
  Write-Host 'then ONE wsl --shutdown (every WSL distro and Docker Desktop stop), the desk kit, and a logon task.' -ForegroundColor Yellow
  if ($have -and -not $ours) {
    Write-Host "The distro $Distro is already there and this script did not make it. Its /etc/wsl.conf is REPLACED" -ForegroundColor Yellow
    Write-Host '(its [user] default= and every other line go; no /mnt/c, no Windows programs or PATH), and it runs fleet' -ForegroundColor Yellow
    Write-Host 'jobs from now on. Pick another -Distro to keep it as it is.' -ForegroundColor Yellow
    if ((Read-Host "Type the distro's name ($Distro) to take it over") -cne $Distro) { Write-Host 'Stopped: nothing changed.'; return }
  }
  elseif ((Read-Host 'Type yes to go on') -ne 'yes') { Write-Host 'Stopped: nothing changed.'; return }

  Head 'the distro'
  if (-not $have) {
    Mark $Made   # before the install: a run stopped inside it asks no takeover question next time
    wsl.exe --install -d Ubuntu-24.04 --name $Distro --no-launch --version 2
    if ($LASTEXITCODE -ne 0) { throw "wsl --install failed (exit $LASTEXITCODE): run wsl --update (or restart Windows after WSL's own install), then this again" }
    $version = WslVersion @(wsl.exe -l -v) $Distro
    if ($version -ne 2) { throw "the new distro $Distro is WSL $version, not 2: wsl --unregister $Distro, then wsl --set-default-version 2 and this again" }
  }
  wsl.exe -d $Distro -u root --cd / -- true
  if ($LASTEXITCODE -ne 0) {
    throw "the first start of the distro failed (exit $LASTEXITCODE). If a run was stopped while installing it: wsl --unregister $Distro (removes it and all in it), then this again"
  }

  Head '.wslconfig'
  $restart = Merge-WslConfig $WslConfig $want { Mark $Owed }   # the mark first: a run stopped after the write still restarts
  if ($restart) { 'changed: .wslconfig' }

  Head "the distro's own settings"
  # The owed mark before wsl.conf changes too: a run stopped before its shutdown restarts next time even when the
  # start of PID 1 (read from the VM's clock, which skips sleep) looks later than the change.
  if (InDistro 'reading /etc/wsl.conf' ("CHECK_ONLY=1`n" + $Prepare)) { Mark $Owed }
  $changed = InDistro "the distro's settings" $Prepare
  $changed
  Remove-Item -LiteralPath $Made -ErrorAction SilentlyContinue   # the distro's own mark is there now
  # The live state decides, not only what this run changed: a run stopped before its shutdown restarts next time
  $live = @(InDistro 'the running settings' ("WANT_KB=$(CapKb $want.memory)`n" + $Live))
  $up = [double]((wsl.exe -d $Distro -u root --cd / -- cat /proc/uptime) -split ' ')[0]
  if ((Test-Path $WslConfig) -and ((Get-Date) - (Get-Item -LiteralPath $WslConfig).LastWriteTime).TotalSeconds -lt $up) {
    $live += 'restart: .wslconfig is newer than the running WSL'
  }
  if (Test-Path -LiteralPath $Owed) { $live += 'restart: a change (.wslconfig or wsl.conf) still owes its wsl --shutdown' }
  $live
  if ($restart -or $changed -or $live) {
    Head 'wsl --shutdown (the new settings apply at the next start)'
    Run 'wsl --shutdown' { wsl.exe --shutdown }
    Remove-Item -LiteralPath $Owed -ErrorAction SilentlyContinue
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
  $cfg = Wsl2Keys (Read-WslConfig $WslConfig); $old = SectionKeys (Read-WslConfig $WslConfig) 'experimental'
  foreach ($k in $want.Keys) {
    if ($cfg[$k] -eq $want[$k]) { "PASS  wslconfig  [wsl2] $k=$($want[$k])" } else { "FAIL  wslconfig  [wsl2] no $k=$($want[$k])"; $bad++ }
  }
  foreach ($k in $Experimental) { if ($old.ContainsKey($k)) { "FAIL  wslconfig  [experimental] $k=$($old[$k]) is still there"; $bad++ } }
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
