OnTrak — range host installer
=============================

This ISO installs Ubuntu Server 24.04 LTS and prepares the machine to run the
OnTrak range: Incus, the lab network and the student portal. It was built by
infra/build-installer-iso.sh in github.com/innotelinc/OnTrak.

WHAT HAPPENS
------------

1.  Boot the machine from this ISO (BIOS or UEFI, USB stick or virtual media).
    The installer starts by itself.

2.  It stops on one screen: identity. Choose the admin username, the hostname and
    the password, and paste an SSH public key if you want one. Everything else —
    locale, keyboard, DHCP, LVM on the largest disk, the SSH server, security
    updates — is already decided.

3.  The install runs, then the machine reboots into the new system. The first
    boot is when the rest happens, unattended:

      * the range host's own packages (Docker, git)
      * the OnTrak checkout, cloned from GitHub to /opt/ontrak
      * infra/bootstrap-host.sh — Incus, the ontrak0 bridge, the ontrak project
      * make setup — the venv, dependencies and .env
      * the portal stack — portal, guacd, Guacamole and the trunk gateway

    Suppose the machine has no KVM (virtualisation disabled in firmware, or a
    nested VM without it): the portal still comes up, but it cannot create
    machines, and the script says so.

4.  First boot takes as long as the internet does — apt, the checkout and the
    portal image build. Watch it on the console, or afterwards:

      journalctl -fu ontrak-firstboot
      cat /var/log/ontrak-firstboot.log

5.  When it finishes, the console prints the portal URL for this machine.

AFTERWARDS
----------

    cd /opt/ontrak
    make check                     # preflight: Python, Incus, KVM, storage, secrets
    make provision-plan            # DNS + TLS + edge through Cerulean (writes nothing)
    make provision                 # ... and apply it (needs CERULEAN_API_TOKEN)
    make golden && make templates  # the golden Windows image, then every scenario
    infra/lab-services.sh          # the intranet targets the scenarios test against

SETTINGS
--------

First-boot settings are optional and live in /etc/ontrak/firstboot.env — a private
git remote and its token, a ZFS pool instead of the default dir storage, or
building the range's templates as part of the first boot. /etc/ontrak/
firstboot.env.example documents every one of them.

To apply a change, re-run the script. It is idempotent:

    sudo /usr/local/sbin/ontrak-firstboot.sh --force

If the first boot failed outright (no network yet, a private remote without a
token), fixing the cause and re-running is all that is needed: the host is left
armed to provision itself again.

Sign-in to the portal is Authentik's. Set the ONTRAK_PORTAL__OIDC_* values in
/opt/ontrak/.env, and see docs/operations.md for the estate around it: DNS and
certificates are Cerulean's, not this host's.
