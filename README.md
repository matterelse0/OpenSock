# OpenSock

OpenSock is a local system-management interface. The overview reports operating
system, kernel, CPU, memory, root-disk, Secure Boot, and bootloader information.
Kernel rebuilding and GRUB configuration are separate workspaces. The kernel
workspace downloads official Linux release archives from kernel.org over HTTPS,
builds Ubuntu/Debian x86_64 packages, optionally applies a unified diff, and
installs the resulting kernel packages.
The GRUB workspace edits its timeout/menu visibility, default entry, kernel
command line, menu colors, and optional PNG/JPEG background.
The Network workspace reads NetworkManager device and Wi-Fi scan data, and can
change active-profile IPv4, DNS, and MAC settings or create a Wi-Fi profile.

## Requirements

- Ubuntu or Debian on x86_64
- Python 3.10+
- `apt-get` and `pkexec` for confirmed dependency installation
- NetworkManager and `nmcli` for network management
- An internet connection to `cdn.kernel.org`

OpenSock checks for required build packages when you start a build. If any are
missing, it asks before requesting system authorization to update APT metadata
and install the fixed set of build dependencies. It does not upgrade already
installed packages.

## Run

From the project directory:

```sh
python3 -m opensock.server
```

Open the one-time URL printed in the terminal. It contains a session token;
do not share it. The web server refuses to run as root. Enter an upstream
kernel version such as `6.12.56` or `6.13-rc1`; OpenSock downloads that release
from `cdn.kernel.org` and builds packages as the current user under
`~/.local/state/opensock/builds/`. Kernel compilation can take a long time and
requires substantial free disk space.

After a successful build, review the package list, then choose **Install kernel
packages**. OpenSock does not edit `/etc/default/grub` during kernel
installation, though the package's own maintainer scripts may refresh available
boot entries. To customize the boot menu,
open **Загрузчик GRUB**, review the timeout/default entry/colors and optional
wallpaper, then confirm **Save GRUB settings**. OpenSock asks `pkexec` for
system authorization and invokes its bundled, argument-validated helper.
Keep a known-good kernel available and make sure you can recover the machine
before changing boot settings. Secure Boot may reject an unsigned custom
kernel. A supplied patch is trusted code and may affect the packages produced
for installation.

The **Сеть и Wi-Fi** workspace uses NetworkManager and does not rewrite
`/etc/network/interfaces` or systemd-networkd files. Reading interface status
and scanning Wi-Fi are unprivileged. Changing an active profile, disconnecting,
or joining Wi-Fi requests `pkexec` authorization and may briefly interrupt the
browser connection. Wi-Fi profiles are stored by NetworkManager as system
profiles; passwords are not written to OpenSock logs or passed as process
arguments. OpenSock supports open networks and WPA/WPA2/WPA3-Personal, not
enterprise authentication.

GRUB's current configuration is backed up as
`/etc/default/grub.opensock-<id>.bak`. If `update-grub` fails, OpenSock
restores the configuration and attempts to regenerate the menu from the
restored file. An installed kernel package is not rolled back automatically.
PNG/JPEG backgrounds are copied to `/boot/grub/opensock-background.*`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
