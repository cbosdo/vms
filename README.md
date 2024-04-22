<!--
SPDX-FileCopyrightText: 2024 SUSE LLC

SPDX-License-Identifier: LGPL-2.1-or-later
-->

[![REUSE status](https://api.reuse.software/badge/git.fsfe.org/reuse/api)](https://api.reuse.software/info/git.fsfe.org/reuse/api)

Convenient tool to manage multiple VMs at once using libvirt

![vms usage](./vms.gif)

# Installing

To install the tool and its dependencies:

```
pip install -e .
```

Getting completion for your shell is fairly easy.
Follow the instructions for your shell in [the click documentation](https://click.palletsprojects.com/en/8.0.x/shell-completion/#enabling-completion) and replace `FOO_BAR` with `VMS` and `foo-bar` with `vms`.

For instance for Fish completion just run:

```
_VMS_COMPLETE=fish_source vms > ~/.config/fish/completions/vms.fish
```
