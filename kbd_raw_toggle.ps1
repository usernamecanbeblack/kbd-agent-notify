<#
  Minimal raw toggle test. Sends exact cbArg2/cbArg3 words to BFn class4/select11,
  alternating between two raw states N times. Used to find which transition the
  EC actually shows as a visible blink. No decoding, no inversion, no restore
  logic beyond returning to the start state.

  Usage (ELEVATED):
    .\kbd_raw_toggle.ps1 -OnArg2 0x41070040 -OffArg2 0x41070001 -Count 4 -OnMs 350 -OffMs 350
#>
param(
  [uint32]$OnArg2  = 0x41070040,   # a visibly-confirmed "on/bright" word from the original session
  [uint32]$OffArg2 = 0x41070001,   # the resting/off word
  [uint32]$Arg3    = 0x41000000,   # cbArg3 as used in the original session
  [int]$Count = 4,
  [int]$OnMs  = 350,
  [int]$OffMs = 350
)
$ErrorActionPreference = "Stop"

function Invoke-Bfn {
  param([uint32]$A0,[uint32]$A1,[uint32]$A2,[uint32]$A3)
  [byte[]]$buffer = New-Object byte[] 32768
  [Array]::Copy([BitConverter]::GetBytes([uint16]4),  0,$buffer,0,2)   # class
  [Array]::Copy([BitConverter]::GetBytes([uint16]11), 0,$buffer,2,2)   # select
  [Array]::Copy([BitConverter]::GetBytes([uint32]$A0),0,$buffer,4,4)
  [Array]::Copy([BitConverter]::GetBytes([uint32]$A1),0,$buffer,8,4)
  [Array]::Copy([BitConverter]::GetBytes([uint32]$A2),0,$buffer,12,4)
  [Array]::Copy([BitConverter]::GetBytes([uint32]$A3),0,$buffer,16,4)
  $scope = New-Object System.Management.ManagementScope("\\.\root\WMI"); $scope.Connect()
  $bdat = New-Object System.Management.ManagementClass($scope,(New-Object System.Management.ManagementPath("BDat")),$null)
  $bfn  = New-Object System.Management.ManagementClass($scope,(New-Object System.Management.ManagementPath("BFn")),$null)
  $q = New-Object System.Management.ObjectQuery("SELECT * FROM BFn WHERE InstanceName='ACPI\\PNP0C14\\0_0'")
  $inst = @((New-Object System.Management.ManagementObjectSearcher($scope,$q)).Get())[0]
  if (-not $inst) { throw "BFn instance not found (elevated?)" }
  $d = $bdat.CreateInstance(); $d.Properties["Bytes"].Value = [byte[]]$buffer
  $p = $bfn.GetMethodParameters("DoBFn"); $p["Data"] = $d
  $r = $inst.InvokeMethod("DoBFn",$p,$null)
  $ob = [byte[]]@($r.Properties["Data"].Value.Properties["Bytes"].Value)
  return ("0x{0:x8} 0x{1:x8}" -f [BitConverter]::ToUInt32($ob,24),[BitConverter]::ToUInt32($ob,28))
}

# Save current (get state), so we can restore at the end.
Write-Host "toggling On=$('0x{0:x8}' -f $OnArg2) Off=$('0x{0:x8}' -f $OffArg2) count=$Count onMs=$OnMs offMs=$OffMs"
for ($i=0; $i -lt $Count; $i++) {
  $o = Invoke-Bfn 2 $OnArg2 $Arg3 0
  Write-Host ("  [{0}] ON  -> {1}" -f $i,$o)
  Start-Sleep -Milliseconds $OnMs
  $o = Invoke-Bfn 2 $OffArg2 $Arg3 0
  Write-Host ("  [{0}] OFF -> {1}" -f $i,$o)
  Start-Sleep -Milliseconds $OffMs
}
Write-Host "done (left at Off state $('0x{0:x8}' -f $OffArg2))"
