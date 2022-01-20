#!/usr/bin/python3

from contextlib import contextmanager
from datetime import datetime
import click
import json
import libvirt
import re
import sys
from rich.console import Console
from rich.table import Table
from rich.text import Text
from xml.etree import ElementTree


SOURCE_ATTRIBUTES = {"file", "dir", "name", "dev", "volume"}
STATES = {
    libvirt.VIR_DOMAIN_RUNNING: {"label": "running", "style": "green"},
    libvirt.VIR_DOMAIN_SHUTDOWN: {"label": "shutting down"},
    libvirt.VIR_DOMAIN_SHUTOFF: {"label": "stopped"},
    libvirt.VIR_DOMAIN_PAUSED: {"label": "paused", "style": "blue"},
    libvirt.VIR_DOMAIN_NOSTATE: {"label": "unknown", "style": "red"},
    libvirt.VIR_DOMAIN_BLOCKED: {"label": "blocked", "style": "red"},
    libvirt.VIR_DOMAIN_CRASHED: {"label": "crashed", "style": "red"},
    libvirt.VIR_DOMAIN_PMSUSPENDED: {"label": "suspended", "style": "blue"},
}


def complete_domain_pattern(ctx, param, incomplete):
    """
    Provide completion for VM names
    """
    domains = []
    connect = ctx.find_root().params["connect"]
    with connect_libvirt(connect) as cnx:
        domains = [
            dom.name()
            for dom in cnx.listAllDomains()
            if dom.name().startswith(incomplete)
        ]
    return domains


@click.group(invoke_without_command=True)
@click.option("-c", "--connect", help="libvirt URL to connect to", default=None)
@click.pass_context
def cli(ctx, connect):
    ctx.obj = ctx.with_resource(connect_libvirt(connect))
    if ctx.invoked_subcommand is None:
        ctx.invoke(list)


@cli.command()
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.option(
    "--format", "format", type=click.Choice(["json", "table"]), default="table"
)
@click.pass_context
def list(ctx, format, patterns):
    """
    list the virtual machines (default command)

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    domains = [
        [dom.name(), STATES[dom.state()[0]]]
        for dom in ctx.obj.listAllDomains()
        if matches(dom.name(), patterns)
    ]
    console = Console()
    if format == "json":
        domains = [[domain[0], domain[1]["label"]] for domain in domains]
        console.print(json.dumps(domains))
    else:
        table = Table(show_header=True)
        table.add_column("Name")
        table.add_column("State")
        for name, state in sorted(domains, key=lambda d: d[0]):
            formatted_state = Text()
            formatted_state.append(state["label"], style=state.get("style"))
            table.add_row(name, formatted_state)
        console.print(table)


@cli.command()
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def start(ctx, patterns):
    """
    start all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    console = Console()
    with console.status("[bold green]Starting domains..."):
        for dom in ctx.obj.listAllDomains():
            if dom.state()[0] != libvirt.VIR_DOMAIN_RUNNING and matches(
                dom.name(), patterns
            ):
                try:
                    dom.create()
                    console.print(dom.name() + " started")
                except libvirt.libvirtError as err:
                    console.print(
                        "Failed to start {}: {}".format(dom.name(), err),
                        style="bold red",
                    )


@cli.command(help="")
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def stop(ctx, patterns):
    """
    stop all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    console = Console()
    with console.status("[bold green]Stopping domains..."):
        for dom in ctx.obj.listAllDomains():
            if dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING and matches(
                dom.name(), patterns
            ):
                try:
                    dom.shutdown()
                    console.print("Triggered shutdown of " + dom.name())
                except libvirt.libvirtError as err:
                    console.print(
                        "Failed to shut down {}: {}".format(dom.name(), err),
                        style="bold red",
                    )


@cli.command()
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def delete(ctx, patterns):
    """
    delete all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    domains = [dom for dom in ctx.obj.listAllDomains() if matches(dom.name(), patterns)]
    console = Console()
    with console.status("[bold green]Deleting domains..."):
        for dom in domains:
            name = dom.name()
            if dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING:
                dom.destroy()
                console.print("Stopped " + name)

            volumes_to_delete = []
            xml_desc = ElementTree.fromstring(dom.XMLDesc())
            for disk in xml_desc.findall("./devices/disk"):
                target_node = disk.find("target")
                if target_node is None:
                    console.print(
                        "Missing target in disk definition of vm " + name,
                        style="bold red",
                    )
                    continue
                target = target_node.get("dev")

                source_node = disk.find("source")
                if source_node is not None:
                    attr = SOURCE_ATTRIBUTES.intersection(set(source_node.keys()))
                    if len(attr) != 1:
                        continue
                    source = source_node.get(attr.pop())
                    pool = source_node.get("pool")
                    volume = None
                    if pool is not None:
                        try:
                            pool_obj = ctx.obj.storagePoolLookupByName(pool)
                        except libvirt.libvirtError:
                            console.print(
                                "Storage pool {} not found for disk {} of vm {}".format(
                                    pool, target, name
                                ),
                                style="bold red",
                            )
                            continue

                        try:
                            volume = pool_obj.storageVolLookupByName(source)
                        except libvirt.libvirtError:
                            console.print(
                                "Storage volume {}/{} not found for disk {} of vm {}".format(
                                    pool, source, target, name
                                ),
                                style="bold red",
                            )
                            continue

                    else:
                        try:
                            volume = ctx.obj.storageVolLookupByPath(source)
                        except libvirt.libvirtError:
                            console.print(
                                "Storage volume {} of vm {} not managed by libvirt, delete manually".format(
                                    source, name
                                ),
                                style="bold red",
                            )
                            continue

                    if volume:
                        volumes_to_delete.append(
                            {"volume": volume, "target": target, "source": source}
                        )

            try:
                dom.undefineFlags(
                    libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
                    | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
                    | libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA
                    | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                )
                console.print("Deleted " + name)
            except libvirt.libvirtError as err:
                console.print(
                    "Failed to delete {}: {}".format(name, err), style="bold red"
                )
                continue

            for volume in volumes_to_delete:
                try:
                    vol_source = volume["source"]
                    volume["volume"].delete()
                    console.print("Volume {} deleted".format(vol_source), style="dim")
                except libvirt.libvirtError as err:
                    console.print(
                        "Failed to delete volume {}({}): {}".format(
                            volume["target"], volume["source"], err
                        ),
                        style="bold red",
                    )


@cli.group(help="Snapshots management", invoke_without_command=True)
@click.pass_context
def snapshot(ctx):
    if ctx.invoked_subcommand is None:
        ctx.invoke(snapshot_list)


@snapshot.command(name="list")
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.option(
    "--format", "format", type=click.Choice(["json", "table"]), default="table"
)
@click.pass_context
def snapshot_list(ctx, format, patterns):
    """
    list all snapshots of all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    snapshots = []
    for dom in ctx.obj.listAllDomains():
        if not matches(dom.name(), patterns):
            continue
        for snap in dom.listAllSnapshots():
            xml_desc = ElementTree.fromstring(snap.getXMLDesc())
            desc_node = xml_desc.find("description")
            created_node = xml_desc.find("creationTime")
            created_time = (
                datetime.fromtimestamp(int(created_node.text)).strftime("%c")
                if created_node is not None
                else ""
            )
            state_node = xml_desc.find("state")
            snapshots.append(
                [
                    dom.name(),
                    snap.getName(),
                    bool(snap.isCurrent()),
                    state_node.text if state_node is not None else "",
                    created_time,
                    desc_node.text if desc_node is not None else "",
                ]
            )

    console = Console()
    if format == "json":
        console.print(json.dumps(snapshots))
    else:
        table = Table(show_header=True)
        for header in [
            "Domain",
            "Name",
            "Current",
            "State",
            "Created",
            "Description",
        ]:
            table.add_column(header)
        for snapshot in snapshots:
            table.add_row(*snapshot)
        console.print(table)


@snapshot.command(name="create")
@click.argument("name", nargs=1)
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def snapshot_create(ctx, name, patterns):
    """
    create a snapshot on all vms matching a pattern

    NAME: the name of the snapshot to create

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    name_node = "<name>{}</name>".format(name) if name else ""
    snapshotXml = "<domainsnapshot>{}</domainsnapshot>".format(name_node)
    console = Console()
    with console.status("[bold green]Creating snapshots..."):
        for dom in ctx.obj.listAllDomains():
            if not matches(dom.name(), patterns):
                continue
            try:
                dom.snapshotCreateXML(snapshotXml)
                console.print("Created snapshot for " + dom.name())
            except libvirt.libvirtError as err:
                console.print(
                    "Failed to create snapshot on {}: {}".format(dom.name(), err),
                    style="bold red",
                )


@snapshot.command(name="delete")
@click.argument("name", nargs=1)
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def snapshot_delete(ctx, name, patterns):
    """
    delete a snapshot on all vms matching a pattern

    NAME: the name of the snapshot to delete

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    console = Console()
    with console.status("[bold green]Deleting snapshots..."):
        for dom in ctx.obj.listAllDomains():
            if not matches(dom.name(), patterns):
                continue
            try:
                snapshots = [
                    snap for snap in dom.listAllSnapshots() if snap.getName() == name
                ]
                if len(snapshots) == 1:
                    snapshots[0].delete()
                    console.print("Deleted snapshot for " + dom.name())
                else:
                    console.print(
                        "No snapshot to delete for " + dom.name(), style="orange"
                    )
            except libvirt.libvirtError as err:
                console.print(
                    "Failed to delete snapshot on {}: {}".format(dom.name(), err),
                    style="bold red",
                )


@snapshot.command(name="revert")
@click.argument("name", nargs=1)
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def snapshot_revert(ctx, name, patterns):
    """
    revert to a snapshot on all vms matching a pattern

    NAME: the name of the snapshot to revert to

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    console = Console()
    with console.status("[bold green]Reverting snapshots..."):
        for dom in ctx.obj.listAllDomains():
            if not matches(dom.name(), patterns):
                continue
            try:
                snapshots = [
                    snap for snap in dom.listAllSnapshots() if snap.getName() == name
                ]
                if len(snapshots) == 1:
                    dom.revertToSnapshot(snapshots[0])
                    console.print("Reverted to snapshot for " + dom.name())
                else:
                    console.print(
                        "No snapshot to revert to for " + dom.name(), style="orange"
                    )
            except libvirt.libvirtError as err:
                console.print(
                    "Failed to revert to snapshot {} on {}: {}".format(
                        name, dom.name(), err
                    ),
                    style="bold red",
                )


def matches(name, patterns):
    """
    Return whether the name matches at least a pattern or if no pattern is set
    """
    return any([re.search(p, name) for p in patterns]) or not patterns


@contextmanager
def connect_libvirt(connect):
    """
    Connect to libvirt daemon
    """
    cnx = libvirt.open(connect)
    try:
        yield cnx
    finally:
        cnx.close()


if __name__ == "__main__":
    cli()
