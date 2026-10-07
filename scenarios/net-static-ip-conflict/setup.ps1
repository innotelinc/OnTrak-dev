# Fault: the adapter is left on a manual (static) address with a gateway that does
# not exist. The guest stays reachable on-link on the address it already answers on
# -- which is what the ticket promises ("the machine does answer to a remote
# session, which is how you are connected to it") -- while every off-subnet
# destination times out: no intranet, no file server, no browsing.
#
# The address *value* is deliberately left alone, and that is not cosmetic. A guest
# that moves ends up at an address nothing else knows: Incus reports a virtual
# machine's address from the DHCP lease (the golden image carries no Incus agent),
# so a guest that leaves DHCP without keeping its address becomes unreachable to the
# range that has to connect to it. Keeping the value means the template, its clones,
# the console and the grader all still agree on where the machine is.
#
# Turning the adapter off DHCP still ends the session this script's own result
# travels over -- Windows tears the interface down with the lease -- so the build
# reading us never hears ONTRAK-SETUP-OK. The build therefore reconnects and asks
# again (see the retry in ontrak/sessions.py:_run_setup), and the guard below is what
# makes the second ask cheap: the fault is already in place, and re-applying it would
# cut that connection too.
#
# Reversible by: Set-NetIPInterface -Dhcp Enabled; ipconfig /renew
#                (or "Obtain an IP address automatically" in the GUI)

. "$PSScriptRoot\..\..\lib\OnTrak.Common.ps1"

$badGateway = '10.20.0.254'
$dns = '10.20.0.1'
$adapter = Get-OnTrakPrimaryAdapterName

if (-not $adapter) {
    Write-OnTrakStep 'no active adapter found; cannot inject addressing fault'
} else {
    $config = Get-NetIPConfiguration -InterfaceAlias $adapter -ErrorAction SilentlyContinue
    $gateway = if ($config -and $config.IPv4DefaultGateway) { $config.IPv4DefaultGateway.NextHop } else { '' }
    $manual = @(Get-NetIPAddress -InterfaceAlias $adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -eq 'Manual' })

    if ($manual.Count -gt 0 -and $gateway -eq $badGateway) {
        Write-OnTrakStep ('already static at ' + $manual[0].IPAddress + ' gw ' + $badGateway +
            '; the fault is in place')
    } else {
        $lease = @(Get-NetIPAddress -InterfaceAlias $adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.PrefixOrigin -ne 'WellKnown' } |
            Sort-Object -Property SkipAsSource, ifIndex |
            Select-Object -First 1)
        if (-not $lease) {
            Write-OnTrakStep 'the adapter holds no address to pin; cannot inject addressing fault'
        } else {
            $address = $lease.IPAddress
            $prefix = [int]$lease.PrefixLength
            Write-OnTrakStep ("pinning '" + $adapter + "' to static " + $address + "/" + $prefix +
                " gw " + $badGateway)

            # One `netsh set address` converts the live lease into a manual address of
            # the same value. A Remove-NetIPAddress/New-NetIPAddress pair is not
            # equivalent: it drops the address in between, and on a re-run it can race
            # itself over an address Windows has not finished releasing.
            $maskBytes = [BitConverter]::GetBytes([uint32](4294967296 - [math]::Pow(2, 32 - $prefix)))
            [array]::Reverse($maskBytes)
            $mask = ($maskBytes -join '.')
            netsh interface ipv4 set address name="$adapter" source=static address=$address `
                mask=$mask gateway=$badGateway gwmetric=1 | Out-Null
            Set-DnsClientServerAddress -InterfaceAlias $adapter -ServerAddresses $dns -ErrorAction SilentlyContinue
            try { Clear-DnsClientCache -ErrorAction SilentlyContinue } catch { }

            $reachable = Test-OnTrakDefaultGatewayReachable
            Write-OnTrakStep ("gateway reachable after fault: " + $reachable + " (expected False)")
        }
    }
}

# Read the state back rather than trusting the writes: every one of them needs an
# elevated session, and a fault that silently did not apply would ship a template
# with nothing wrong with it. DHCP must be off, the address must still be there, and
# the gateway must be the one that is not.
$config = Get-NetIPConfiguration -InterfaceAlias $adapter -ErrorAction SilentlyContinue
$gateway = if ($config -and $config.IPv4DefaultGateway) { $config.IPv4DefaultGateway.NextHop } else { '' }
$manual = @(Get-NetIPAddress -InterfaceAlias $adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.PrefixOrigin -eq 'Manual' })
$addresses = @($manual | Select-Object -ExpandProperty IPAddress)
$dhcp = if ($adapter) { Test-OnTrakDhcpEnabled -InterfaceAlias $adapter } else { $true }
Write-OnTrakStep ("adapter=" + $adapter + "; addresses=" + ($addresses -join ', ') + "; DHCP=" + $dhcp +
    " (expected False); gateway=" + $gateway + " (expected " + $badGateway + ")")

if (($addresses.Count -eq 0) -or $dhcp -or ($gateway -ne $badGateway)) {
    Write-OnTrakStep 'the addressing fault did not apply; refusing to report success'
    exit 1
}

Write-OnTrakSetupOk -Note ('static=' + ($addresses -join ',') + ' gw=' + $badGateway)
