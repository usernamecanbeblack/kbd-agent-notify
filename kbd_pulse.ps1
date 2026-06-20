<#
.SYNOPSIS
  Pulse the Dell keyboard backlight as an agent notification, then restore the
  exact prior state. Runs ELEVATED (the Dell BFn/BDat WMI interface is admin-only).

  This is the elevated worker behind kbd_agent_notify.py's "kbd" backend. A non-elevated
  CLI hook drops a small request JSON, then triggers the registered Scheduled Task
  (which runs this with -RequestPath), so there is no per-notification UAC prompt.

  Hardware path (confirmed on Dell XPS 13 9350 / platform 0CC9):
    WMI class BFn / data BDat, SMBIOS class 4 (CLASS_KBD_BACKLIGHT),
    select 11 (SELECT_KBD_BACKLIGHT). Field layout from upstream dell-laptop.c:
      cbArg1=1 Get State:  cbRES2.word0 = mode bitmap, cbRES3.byte2 = level
      cbArg1=2 Set State:  cbArg2.word0 = mode bitmap, cbArg2.byte2 = triggers,
                           cbArg2.byte3 = batt timeout, cbArg3.byte2 = level,
                           cbArg3.byte3 = AC timeout
    mode bit0 = Always off, bit1 = Always on.

  SAFETY: reads and saves the original mode+level+triggers+timeouts first, and
  ALWAYS restores them in a finally block, even on error or interrupt.
#>
[CmdletBinding()]
param(
  # Path to a JSON request: { "event": "done|waiting|test", "kbd": { ...pattern cfg... } }
  # Used by the Scheduled Task path. Omit for direct -Event or -ReadOnly runs.
  [string]$RequestPath,

  # Or drive it directly (for calibration / testing).
  [ValidateSet("done", "waiting", "test")]
  [string]$Event,

  # Read current state and print it; make no changes.
  [switch]$ReadOnly,

  # Optional per-run timing overrides (calibration). 0 = use pattern defaults.
  [int]$CountOverride = 0,
  [int]$OnMsOverride = 0,
  [int]$OffMsOverride = 0,

  [string]$LogPath = ".\captures\kbd-pulse.jsonl"
)

$ErrorActionPreference = "Stop"

# ---- low-level BFn/BDat call (class 4 / select 11) ---------------------------

function Invoke-Bfn {
  param([uint16]$Class, [uint16]$Select, [uint32[]]$InArgs)

  [byte[]]$buffer = New-Object byte[] 32768
  [Array]::Copy([BitConverter]::GetBytes($Class),  0, $buffer, 0, 2)
  [Array]::Copy([BitConverter]::GetBytes($Select), 0, $buffer, 2, 2)
  for ($i = 0; $i -lt 4; $i++) {
    $v = if ($i -lt $InArgs.Count) { $InArgs[$i] } else { [uint32]0 }
    [Array]::Copy([BitConverter]::GetBytes([uint32]$v), 0, $buffer, 4 + ($i * 4), 4)
  }

  $scope = New-Object System.Management.ManagementScope("\\.\root\WMI")
  $scope.Connect()
  $bdatClass = New-Object System.Management.ManagementClass($scope, (New-Object System.Management.ManagementPath("BDat")), $null)
  $bfnClass  = New-Object System.Management.ManagementClass($scope, (New-Object System.Management.ManagementPath("BFn")),  $null)
  $query    = New-Object System.Management.ObjectQuery("SELECT * FROM BFn WHERE InstanceName='ACPI\\PNP0C14\\0_0'")
  $searcher = New-Object System.Management.ManagementObjectSearcher($scope, $query)
  $instance = @($searcher.Get())[0]
  if (-not $instance) { throw "BFn instance ACPI\PNP0C14\0_0 not found (is this elevated?)" }

  $data = $bdatClass.CreateInstance()
  # Force a genuine byte[] (not a PSObject-wrapped array) so the WMI property
  # marshaller can map it to a CIM uint8 array. Inside a function the unwrapped
  # assignment can arrive as PSObject and throw InvalidCastException.
  $data.Properties["Bytes"].Value = [byte[]]$buffer
  $inParams = $bfnClass.GetMethodParameters("DoBFn")
  $inParams["Data"] = $data
  $result = $instance.InvokeMethod("DoBFn", $inParams, $null)
  $outBytes = [byte[]]@($result.Properties["Data"].Value.Properties["Bytes"].Value)

  return @(
    [BitConverter]::ToUInt32($outBytes, 20),
    [BitConverter]::ToUInt32($outBytes, 24),
    [BitConverter]::ToUInt32($outBytes, 28),
    [BitConverter]::ToUInt32($outBytes, 32)
  )
}

$CLASS_KBD = 4
$SELECT_KBD = 11

function Get-KbdInfo {
  $o = Invoke-Bfn -Class $CLASS_KBD -Select $SELECT_KBD -InArgs @(0, 0, 0, 0)
  # cbRES3 (o[2]) byte2 = number of brightness levels
  $levels = ($o[2] -shr 16) -band 0xFF
  return @{ raw = $o; levelCount = [int]$levels }
}

function Get-KbdState {
  $o = Invoke-Bfn -Class $CLASS_KBD -Select $SELECT_KBD -InArgs @(1, 0, 0, 0)
  $res2 = $o[1]; $res3 = $o[2]
  return @{
    raw       = $o
    modeWord  = [uint32]($res2 -band 0xFFFF)              # cbRES2 word0: mode bitmap
    triggers  = [uint32](($res2 -shr 16) -band 0xFF)       # cbRES2 byte2
    battTo    = [uint32](($res2 -shr 24) -band 0xFF)       # cbRES2 byte3
    alsThresh = [uint32]($res3 -band 0xFF)                 # cbRES3 byte0
    level     = [uint32](($res3 -shr 16) -band 0xFF)       # cbRES3 byte2
    acTo      = [uint32](($res3 -shr 24) -band 0xFF)       # cbRES3 byte3
  }
}

function Set-KbdState {
  param([uint32]$ModeWord, [uint32]$Triggers, [uint32]$Level, [uint32]$BattTo, [uint32]$AlsThresh, [uint32]$AcTo)
  # cbArg2 = modeWord(word0) | triggers(byte2) | battTo(byte3)
  $arg2 = ($ModeWord -band 0xFFFF) -bor (($Triggers -band 0xFF) -shl 16) -bor (($BattTo -band 0xFF) -shl 24)
  # cbArg3 = alsThresh(byte0) | level(byte2) | acTo(byte3)
  $arg3 = ($AlsThresh -band 0xFF) -bor (($Level -band 0xFF) -shl 16) -bor (($AcTo -band 0xFF) -shl 24)
  [void](Invoke-Bfn -Class $CLASS_KBD -Select $SELECT_KBD -InArgs @(2, [uint32]$arg2, [uint32]$arg3, 0))
}

# ==========================================================================
#  CALIBRATE PER MACHINE. These two mode-bitmap values decide the flash.
#  On the reference machine (Dell XPS 13 9350 / 0CC9) the "Always-on" mode
#  bit (0x0002) did NOT visibly light the keyboard; the visible "lit" states
#  were the trigger/level mode bits (0x0040 = TRIGGER_50, 0x0100 = TRIGGER_100).
#  Other Dell models may differ. Use kbd_raw_toggle.ps1 to find which value
#  visibly toggles YOUR keyboard, then set $MODE_ON accordingly. See README
#  ("Adapt it to your keyboard"). $MODE_OFF = 0x0001 (Always-off) is standard.
# ==========================================================================
$MODE_OFF = 0x0001   # bit0 = Always off  (dark)        -- standard across Dell
$MODE_ON  = 0x0040   # bit6 = TRIGGER_50  (visibly lit) -- VERIFY for your machine

# ---- pulse patterns ---------------------------------------------------------

function Get-Pattern {
  param([string]$Ev, $KbdCfg)
  # defaults; overridable via the request JSON's "kbd.patterns.<event>"
  # 2 flashes, ~0.3 second each, with a short gap between them.
  $defaults = @{
    done    = @{ count = 2; onMs = 300; offMs = 300; level = 0 }   # level 0 => keep saved level
    waiting = @{ count = 2; onMs = 300; offMs = 300; level = 0 }
    test    = @{ count = 2; onMs = 300; offMs = 300; level = 0 }
  }
  $p = $defaults[$Ev]
  if ($KbdCfg -and $KbdCfg.patterns -and $KbdCfg.patterns.$Ev) {
    foreach ($k in @("count", "onMs", "offMs", "level")) {
      if ($null -ne $KbdCfg.patterns.$Ev.$k) { $p[$k] = [int]$KbdCfg.patterns.$Ev.$k }
    }
  }
  return $p
}

# ---- main -------------------------------------------------------------------

$record = [ordered]@{ startedAt = (Get-Date).ToString("o"); event = $Event; readOnly = [bool]$ReadOnly }
$kbdCfg = $null

# Load the request JSON only when a path was actually supplied (the Scheduled
# Task path). Read-only dumps and direct -Event runs don't need it.
if ($RequestPath) {
  if (-not (Test-Path -LiteralPath $RequestPath)) {
    throw "RequestPath not found: $RequestPath"
  }
  $req = Get-Content -LiteralPath $RequestPath -Raw | ConvertFrom-Json
  $Event = [string]$req.event
  $kbdCfg = $req.kbd
  $record.event = $Event
}

$saved = $null
try {
  $info  = Get-KbdInfo
  $saved = Get-KbdState
  $record.savedState  = $saved
  $record.levelCount  = $info.levelCount

  if ($ReadOnly) {
    $record.note = "read-only; no changes made"
  }
  else {
    if (-not $Event) { throw "No event specified" }
    $pat = Get-Pattern -Ev $Event -KbdCfg $kbdCfg
    if ($CountOverride -gt 0) { $pat.count = $CountOverride }
    if ($OnMsOverride  -gt 0) { $pat.onMs  = $OnMsOverride }
    if ($OffMsOverride -gt 0) { $pat.offMs = $OffMsOverride }
    $record.pattern = $pat

    # Visibility on this firmware comes from the MODE bit, not a numeric level:
    # the "lit" state is a trigger/level mode bit ($MODE_ON = 0x0040, confirmed
    # visible by direct test); "Always-on" (0x0002) does NOT light. We INVERT the
    # resting state so a flash is visible either way:
    #   - resting dark (Always-off) -> pulse = $MODE_ON (lit),  between = dark
    #   - resting lit/auto          -> pulse = Always-off (dark), between = lit
    # Triggers/level/timeout bytes are preserved from the saved state on every
    # write, matching the exact word that was confirmed to toggle the light.
    $restingDark = ($saved.modeWord -eq $MODE_OFF)
    if ($restingDark) {
      $pulseMode   = $MODE_ON;  $betweenMode = $MODE_OFF
    } else {
      $pulseMode   = $MODE_OFF; $betweenMode = $MODE_ON
    }
    $record.restingDark = $restingDark
    $record.pulseMode = $pulseMode

    # Each flash is a full pulse->between cycle (between state runs after EVERY
    # flash, including the last). steps[] timestamps each write for diagnostics.
    $steps = New-Object System.Collections.ArrayList
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    for ($i = 0; $i -lt [int]$pat.count; $i++) {
      Set-KbdState -ModeWord $pulseMode -Triggers $saved.triggers -Level $saved.level -BattTo $saved.battTo -AlsThresh $saved.alsThresh -AcTo $saved.acTo
      [void]$steps.Add(@{ step = "flash$i-on"; tMs = [int]$sw.ElapsedMilliseconds })
      Start-Sleep -Milliseconds ([int]$pat.onMs)
      Set-KbdState -ModeWord $betweenMode -Triggers $saved.triggers -Level $saved.level -BattTo $saved.battTo -AlsThresh $saved.alsThresh -AcTo $saved.acTo
      [void]$steps.Add(@{ step = "flash$i-off"; tMs = [int]$sw.ElapsedMilliseconds })
      Start-Sleep -Milliseconds ([int]$pat.offMs)
    }
    $sw.Stop()
    $record.steps = $steps
    $record.emitted = $true
  }
}
catch {
  $record.error = $_.Exception.ToString()
}
finally {
  # ALWAYS restore the exact prior state.
  if ($saved -and -not $ReadOnly) {
    try {
      Set-KbdState -ModeWord $saved.modeWord -Triggers $saved.triggers -Level $saved.level `
                   -BattTo $saved.battTo -AlsThresh $saved.alsThresh -AcTo $saved.acTo
      $record.restored = $true
    }
    catch {
      $record.restoreError = $_.Exception.ToString()
      $record.restored = $false
    }
  }
}

$record.finishedAt = (Get-Date).ToString("o")
try {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
  $record | ConvertTo-Json -Depth 6 -Compress | Add-Content -LiteralPath $LogPath -Encoding UTF8
} catch { }
$record | ConvertTo-Json -Depth 6
if ($record.error) { exit 1 }
