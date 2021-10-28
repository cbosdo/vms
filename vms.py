#!/usr/bin/python3

from contextlib import contextmanager
import click
import json
import libvirt
import re
import sys
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


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    ctx.obj = ctx.with_resource(connect_libvirt())
    if ctx.invoked_subcommand is None:
        ctx.invoke(list)


@cli.command(help="list virtual machines (default command)")
@click.argument("patterns", nargs=-1)
@click.option(
    "--format", "format", type=click.Choice(["json", "table"]), default="table"
)
@click.pass_context
def list(ctx, format, patterns):
    domains = [
        {"name": dom.name(), "state": STATES[dom.state()[0]]}
        for dom in ctx.obj.listAllDomains()
        if matches(dom.name(), patterns)
    ]
    if format == "json":
        json.dumps(domains)
    else:
        name_size = max([len(dom["name"]) for dom in domains])
        state_size = max([len(dom["state"]) for dom in domains])
        format_str = "{:<" + str(name_size) + "}    {:<" + str(state_size) + "}"
        print(format_str.format("Name", "State"))
        print("-" * (name_size + state_size + 4))

        for dom in sorted(domains, key=lambda d: d["name"]):
            print(format_str.format(dom["name"], dom["state"]))


@cli.command(help="start all vms matching a pattern")
@click.argument("patterns", nargs=-1)
@click.pass_context
def start(ctx, patterns):
    """
    Start VMs which name matches any of the patterns
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


@cli.command(help="stop all vms matching a pattern")
@click.argument("patterns", nargs=-1)
@click.pass_context
def stop(ctx, patterns):
    """
    Stop VMs which name matches any of the patterns
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


@cli.command(help="delete all vms matching a pattern")
@click.argument("patterns", nargs=-1)
@click.pass_context
def delete(ctx, patterns):
    """
    Delete VMs which name matches any of the patterns
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


@cli.command(help="create a snapshot on all vms matching a pattern")
@click.option("--name", "name", help="the name of the snapshot to create")
@click.argument("patterns", nargs=-1)
@click.pass_context
def snapshot(ctx, name, patterns):
    """
    Create a snapshot on VMs which name matches any of the patterns
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


def matches(name, patterns):
    """
    Return whether the name matches at least a pattern or if no pattern is set
    """
    return any([re.search(p, name) for p in patterns]) or not patterns


@contextmanager
def connect_libvirt():
    """
    Connect to libvirt daemon
    """
    cnx = libvirt.open()
    try:
        yield cnx
    finally:
        cnx.close()


if __name__ == "__main__":
    cli()