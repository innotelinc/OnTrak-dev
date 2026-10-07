# main.ps1 -- the range's own first-logon step inside the golden image build.
#
# incus-windows' OEM/main.ps1 dot-sources this file, from the unattended ISO's
# `local/` directory, near the end of the build VM's first logon:
#
#     if (test-path "${setupdrive}\local\main.ps1") { . "${setupdrive}\local\main.ps1" }
#
# infra/build-golden-image.sh passes infra/windows/golden-local as tools/pack.sh's
# optional seventh argument, and pack.sh copies it to `local/` on the ISO. It is
# upstream's own extension point -- do not read this as a patch to upstream.
#
# WHAT IT FIXES
#
# A clone of this image comes up on the lab bridge, which Windows classifies as a
# **Public** network. The image's WinRM firewall rules are only active on Domain
# and Private, so a clone is unreachable: it has an address, it answers ARP, and
# every port -- 5985, 3389, 445, 135 -- times out *without a RST*, with an empty
# qemu log. OnTrak provisions Windows over WinRM, so that image is unusable.
#
# infra/windows/post-install.ps1 already contains this repair, but the build applies
# that *over WinRM to a clone of the image*: it can only fix a guest that is already
# reachable. The repair has to happen where no network is involved, which is here.
#
# WHY A FILE, NOT A LINE IN THE AUTOUNATTEND
#
# This started as a `RunSynchronousCommand` in the autounattend's specialize pass and
# it broke the build in a way worth remembering. The `Path` of a RunSynchronousCommand
# is length-limited; a 490-character one-liner made Windows reject the *whole answer
# file*:
#
#     [setup.exe] SMI data results dump: Source = .../RunSynchronousCommand/[Order="4"]/Path
#     [setup.exe] SMI data results dump: Description = Value is invalid.
#     Error [0x060432] IBS  The provided unattend file is not valid; hrResult = 0x80220005
#     Windows could not parse or process unattend answer file
#       [C:\WINDOWS\Panther\unattend.xml] for pass [specialize]. The answer file is invalid.
#
# Setup then blocked the installation: no reboot, nothing visible from outside the
# guest, `unattendgc` never written, sysprep never run, a half-installed Windows
# burning 0.6 of a core forever, and tools/click.py waiting for a STOPPED that never
# comes. A file has no such limit. infra/incus-windows-pack.sh keeps the note.
#
# IT MUST NOT END THE CALLER
#
# main.ps1 goes on to run sysprep.bat, and `sysprep /shutdown` is what stops the
# build VM so pack.sh can publish it. There is deliberately no `exit` in this file,
# every step is wrapped, and it always returns to its caller.

$ErrorActionPreference = 'Continue'

$logPath = 'C:\Windows\Temp\ontrak-local.log'
$marker = 'ONTRAK-LOCAL-OK'

function Step {
    param([string] $Message)
    $line = '[ontrak][local] ' + $Message
    Write-Output $line
    try { Add-Content -Path $logPath -Value $line -ErrorAction SilentlyContinue } catch { }
}

function StepFail {
    param([string] $Message)
    $line = '[ontrak][local][error] ' + $Message
    Write-Output $line
    try { Add-Content -Path $logPath -Value $line -ErrorAction SilentlyContinue } catch { }
}

Step ('starting on ' + $env:COMPUTERNAME)

# --------------------------------------------------------------- the listener --
# `-SkipNetworkProfileCheck` is the whole point: without it Enable-PSRemoting
# refuses to configure WinRM at all on a machine whose interfaces are in the Public
# zone, which is exactly the case this file exists for.
try {
    Enable-PSRemoting -SkipNetworkProfileCheck -Force | Out-Null
    Step 'WinRM enabled (Enable-PSRemoting -SkipNetworkProfileCheck)'
} catch {
    StepFail ('Enable-PSRemoting: ' + $_.Exception.Message)
}

# ------------------------------------------------------- the rules, every profile --
# Enable-PSRemoting creates or enables rules scoped to the profile the machine is on
# when it runs, which is not necessarily the profile a clone boots on. These two are
# explicit about `-Profile Any`, and they are named so they cannot be confused with
# Windows' own rules -- sysprep seals the image with the built-in WinRM rules
# *blocked* and a clone re-enables them from SetupComplete.cmd, but nothing in that
# path turns these off.
#
# Remove-then-add rather than add-because-it-might-not-exist: re-running this on an
# image that already has the rule would otherwise stack a second one.
foreach ($spec in @(
    @{ Label = 'OnTrak WinRM (5985)'; Port = 5985 },
    @{ Label = 'OnTrak RDP (3389)'; Port = 3389 }
)) {
    try {
        Remove-NetFirewallRule -DisplayName $spec.Label -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName $spec.Label -Direction Inbound -Protocol TCP `
            -LocalPort $spec.Port -Action Allow -Profile Any -ErrorAction Stop | Out-Null
        Step ('firewall: ' + $spec.Label + ' allowed on every profile')
    } catch {
        StepFail ('firewall rule for port ' + $spec.Port + ': ' + $_.Exception.Message)
    }
}

# -------------------------------------------------------------- what it added ---
# Printed rather than asserted: this runs inside a guest nobody can reach yet, and
# the console and C:\Windows\Temp\ontrak-local.log are the only way to see it. The
# marker is what a later `grep` looks for.
try {
    $rules = Get-NetFirewallRule -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like 'OnTrak *' } |
        Select-Object -ExpandProperty DisplayName
    Step ('rules now present: ' + (($rules | Sort-Object) -join ', '))
} catch {
    StepFail ('could not list the OnTrak rules: ' + $_.Exception.Message)
}

Step $marker
