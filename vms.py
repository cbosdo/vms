#!/usr/bin/python3

from contextlib import contextmanager
from datetime import datetime
import click
import json
import libvirt
import re
import sys
import tabulate
from xml.etree import ElementTree


SOURCE_ATTRIBUTES = {"file", "dir", "name", "dev", "volume"}
STATES = {
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_NOSTATE: "unknown",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
}


def complete_domain_pattern(ctx, param, incomplete):
    """
    Provide completion for VM names
    """
    domains = []
    connect = ctx.find_root().params["connect"]
    with connect_libvirt(connect) as cnx:
        domains = [dom.name() for dom in cnx.listAllDomains() if dom.name().startswith(incomplete)]
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
    if format == "json":
        print(json.dumps(domains))
    else:
        print(
            tabulate.tabulate(
                sorted(domains, key=lambda d: d[0]),
                headers=["Name", "State"],
                tablefmt="simple",
            )
        )


@cli.command()
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def start(ctx, patterns):
    """
    start all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    for dom in ctx.obj.listAllDomains():
        if dom.state()[0] != libvirt.VIR_DOMAIN_RUNNING and matches(
            dom.name(), patterns
        ):
            try:
                print("Starting " + dom.name())
                dom.create()
            except libvirt.libvirtError as err:
                print(
                    "Failed to start {}: {}".format(dom.name(), err),
                    file=sys.stderr,
                )


@cli.command(help="")
@click.argument("patterns", nargs=-1, shell_complete=complete_domain_pattern)
@click.pass_context
def stop(ctx, patterns):
    """
    stop all vms matching a pattern

    PATTERNS: the list of patterns matching the VM name. If none is set matches all VMs.
    """
    for dom in ctx.obj.listAllDomains():
        if dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING and matches(
            dom.name(), patterns
        ):
            try:
                print("Stopping " + dom.name())
                dom.shutdown()
            except libvirt.libvirtError as err:
                print(
                    "Failed to shut down {}: {}".format(dom.name(), err),
                    file=sys.stderr,
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
    for dom in domains:
        name = dom.name()
        if dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING:
            print("Stopping " + name)
            dom.destroy()

        volumes_to_delete = []
        xml_desc = ElementTree.fromstring(dom.XMLDesc())
        for disk in xml_desc.findall("./devices/disk"):
            target_node = disk.find("target")
            if target_node is None:
                print(
                    "Missing target in disk definition of vm " + name,
                    file=sys.stderr,
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
                        print(
                            "Storage pool {} not found for disk {} of vm {}".format(
                                pool, target, name
                            ),
                            file=sys.stderr,
                        )
                        continue

                    try:
                        volume = pool_obj.storageVolLookupByName(source)
                    except libvirt.libvirtError:
                        print(
                            "Storage volume {}/{} not found for disk {} of vm {}".format(
                                pool, source, target, name
                            ),
                            file=sys.stderr,
                        )
                        continue

                else:
                    try:
                        volume = ctx.obj.storageVolLookupByPath(source)
                    except libvirt.libvirtError:
                        print(
                            "Storage volume {} of vm {} not managed by libvirt, delete manually".format(
                                source, name
                            ),
                            file=sys.stderr,
                        )
                        continue

                if volume:
                    volumes_to_delete.append(
                        {"volume": volume, "target": target, "source": source}
                    )

        try:
            print("Deleting " + name)
            dom.undefineFlags(
                libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
                | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
                | libvirt.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA
                | libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
            )
        except libvirt.libvirtError as err:
            print("Failed to delete {}: {}".format(name, err), file=sys.stderr)
            continue

        for volume in volumes_to_delete:
            try:
                volume["volume"].delete()
            except libvirt.libvirtError as err:
                print(
                    "Failed to delete volume {}({}): {}".format(
                        volume["target"], volume["source"], err
                    ),
                    file=sys.stderr,
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

    if format == "json":
        print(json.dumps(snapshots))
    else:
        print(
            tabulate.tabulate(
                snapshots,
                headers=[
                    "Domain",
                    "Name",
                    "Current",
                    "State",
                    "Created",
                    "Description",
                ],
                tablefmt="simple",
            )
        )


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
    for dom in ctx.obj.listAllDomains():
        if not matches(dom.name(), patterns):
            continue
        try:
            print("Creating snapshot for " + dom.name())
            dom.snapshotCreateXML(snapshotXml)
        except libvirt.libvirtError as err:
            print("Failed to create snapshot on {}: {}".format(dom.name(), err))


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
    for dom in ctx.obj.listAllDomains():
        if not matches(dom.name(), patterns):
            continue
        try:
            snapshots = [snap for snap in dom.listAllSnapshots() if snap.getName() == name]
            if len(snapshots) == 1:
                print("Deleting snapshot for " + dom.name())
                snapshots[0].delete()
            else:
                print("No snapshot to delete for " + dom.name())
        except libvirt.libvirtError as err:
            print("Failed to delete snapshot on {}: {}".format(dom.name(), err))


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
    for dom in ctx.obj.listAllDomains():
        if not matches(dom.name(), patterns):
            continue
        try:
            snapshots = [snap for snap in dom.listAllSnapshots() if snap.getName() == name]
            if len(snapshots) == 1:
                print("Reverting to snapshot for " + dom.name())
                dom.revertToSnapshot(snapshots[0])
            else:
                print("No snapshot to revert to for " + dom.name())
        except libvirt.libvirtError as err:
            print("Failed to revert to snapshot {} on {}: {}".format(name, dom.name(), err))


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
