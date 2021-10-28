Convenient tool to manage multiple VMs at once using libvirt

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

# TODO

* Add revert to snapshot
* Add snapshot delete
* Add connection URL parameter
