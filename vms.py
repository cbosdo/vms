#!/usr/bin/python3

# SPDX-FileCopyrightText: 2024 SUSE LLC
#
# SPDX-License-Identifier: LGPL-2.1-or-later

from contextlib import contextmanager
from datetime import datetime
import click
import json
import libvirt
import re
import time
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
        ctx.invoke(vms_list)


@cli.command(name="list")
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.option(
    "--format", "format", type=click.Choice(["json", "table"]), default="table"
)
@click.pass_context
def vms_list(ctx, format, patterns):
    """
    list the virtual machines (default command)

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    def getTime(domain):
        """
        Safely get the time of the domain
        """
        if domain.state()[0] == libvirt.VIR_DOMAIN_RUNNING:
            try:
                return datetime.fromtimestamp(domain.getTime()["seconds"])
            except libvirt.libvirtError:
                # The vm is likely to have no guest agent
                return None
        return None

    domains = [
        [dom.name(), STATES[dom.state()[0]], getTime(dom)]
        for dom in ctx.obj.listAllDomains()
        if matches(dom.name(), patterns)
    ]
    console = Console()
    if format == "json":
        domains = [[domain[0], domain[1]["label"], domain[2].isoformat() if domain[2] else None] for domain in domains]
        console.print(json.dumps(domains))
    else:
        table = Table(show_header=True)
        table.add_column("Name")
        table.add_column("State")
        table.add_column("Time")
        for name, state, dom_time in sorted(domains, key=lambda d: d[0]):
            formatted_state = Text()
            formatted_state.append(state["label"], style=state.get("style"))
            table.add_row(name, formatted_state, dom_time.isoformat(sep=" ") if dom_time else "")
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
@click.option("-f", "--force", help="Power off instead of gentle shut down", is_flag=True)
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def stop(ctx, force, patterns):
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
                    if force:
                        dom.destroy()
                        console.print("Powered off " + dom.name())
                    else:
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


def do_synctime(cnx, patterns):
    """
    Shared code actually synchronizing time in VMs
    """
    domains = [dom for dom in cnx.listAllDomains() if matches(dom.name(), patterns) and dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING]
    console = Console()
    with console.status("[bold green]Synchronizing time on domains..."):
        for dom in domains:
            if getattr(time, "time_ns"):
                now_ns = time.time_ns()
                now = {"seconds": int(now_ns / 10 ** 9), "nseconds": now_ns % 10 ** 9}
            else:
                now_ts = time.time()
                now = {"seconds": int(now_ts), "nseconds": int((now_ts % 1) * 10 ** 9)}
            console.print("{} time set to {}.{}".format(dom.name(), datetime.fromtimestamp(now["seconds"]), now["nseconds"]))
            try:
                dom.setTime(now)
            except libvirt.libvirtError as err:
                console.print(f"[bold red]Failed to set time {datetime.fromtimestamp(now['seconds'])} on domain {dom.name()}")


@cli.command()
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def synctime(ctx, patterns):
    """
    Set the host time on all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    do_synctime(ctx.obj, patterns)


@cli.command()
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.option(
    "--format", "format", type=click.Choice(["json", "table"]), default="table"
)
@click.pass_context
def addresses(ctx, format, patterns):
    """
    Get the IP addresses of all the vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    cnx = ctx.obj
    domains = [dom for dom in cnx.listAllDomains() if matches(dom.name(), patterns) and dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING]
    def convert_data(data):
        return {value["hwaddr"]: {"names": [name], "addrs": [addr["addr"] for addr in value["addrs"]]} for name, value in data.items() if name != "lo"}

    all_addresses = {}
    for dom in domains:
        leases = dom.interfaceAddresses(source=libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
        agent = dom.interfaceAddresses(source=libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT)
        arp = dom.interfaceAddresses(source=libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP)

        # Merge the 3 data. The returned dict looks like this
        #{'vnet1': {'addrs': [{'addr': '192.168.122.110', 'prefix': 24, 'type': 0}],
        #  'hwaddr': '2a:c3:a7:a6:01:00'}}
        data = merge_dicts(convert_data(leases), convert_data(agent))
        data = merge_dicts(data, convert_data(arp))

        all_addresses[dom.name()] = data

    console = Console()
    if format == "json":
        console.print(json.dumps(all_addresses))
    else:
        table = Table(show_header=True)
        for header in ["Domain", "MAC", "Interface", "IPv4", "IPv6"]:
            table.add_column(header)

        for domain, ifaces in all_addresses.items():
            for mac, iface in ifaces.items():
                names = iface["names"]
                names.sort(key=iface_name_key)
                ipv4 = [addr for addr in iface["addrs"] if "." in addr]
                ipv6 = [addr for addr in iface["addrs"] if ":" in addr]
                row = [domain,
                       mac,
                       ", ".join(names),
                       ", ".join(ipv4),
                       ", ".join(ipv6)]
                table.add_row(*row)
        console.print(table)

def iface_name_key(name):
    prefix_order = ["eth", "vnet"]
    prefix = name.rstrip('0123456789')
    try:
        return prefix_order.index(prefix)
    except ValueError:
        return len(prefix_order)


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
            row = snapshot
            row[2] = "\u2714" if row[2] else ""
            table.add_row(*snapshot, style="bold green" if row[2] else None)
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

    NAME: the pattern matching the name of the snapshots to delete

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    console = Console()
    with console.status("[bold green]Deleting snapshots..."):
        for dom in ctx.obj.listAllDomains():
            if not matches(dom.name(), patterns):
                continue
            try:
                snapshots = [
                    snap for snap in dom.listAllSnapshots() if matches(snap.getName(), [name])
                ]
                if not snapshots:
                    console.print(
                        "No snapshot to delete for " + dom.name(), style="dark_red"
                    )
                else:
                    for snapshot in snapshots:
                        snapshot.delete()
                        console.print("Deleted snapshot {} on {}".format(snapshot.getName(), dom.name()))
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
                        "No snapshot to revert to for " + dom.name(), style="red"
                    )
            except libvirt.libvirtError as err:
                console.print(
                    "Failed to revert to snapshot {} on {}: {}".format(
                        name, dom.name(), err
                    ),
                    style="bold red",
                )

    do_synctime(ctx.obj, patterns)


def matches(name, patterns):
    """
    Return whether the name matches at least a pattern or if no pattern is set
    """
    return any([re.search(p, name) for p in patterns]) or not patterns


def merge_dicts(dict1, dict2):
    """
    Merge two dictionaries. Concatenates included lists and dictionaries.
    """
    merged = dict1
    for key, value in dict2.items():
        if key in merged and isinstance(value, list) and isinstance(merged[key], list):
            merged[key] = list(set(merged[key] + value))
        elif key in merged and isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


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
