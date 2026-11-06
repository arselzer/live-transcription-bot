"""Owned libvirt VM provisioning, verified downloads and private cloud-init data.

This module never adopts an existing domain or replaces an existing disk. A
failed setup is resumable; its guest disk and diagnostics are never removed.
"""

import hashlib
from importlib.resources import files
import ipaddress
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import urllib.request
from xml.etree import ElementTree as ET

from live_transcript.core.files import atomic_text

IMAGE_URL = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256 = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
OWNER_NAMESPACE = "urn:live-transcript:installation"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download(url, destination, expected):
    """Reuse verified content; publish only complete, checksum-matching downloads."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("Download destination must not be a symlink")
    if destination.exists():
        if sha256(destination) != expected:
            raise ValueError("Cached download checksum differs: " + destination.name)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(destination.name + ".download")
    if temporary.is_symlink():
        raise ValueError("Partial download must not be a symlink")
    # A partial download is deliberately replaced on retry, never treated as valid.
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "live-transcription-bot-setup"}
        )
        deadline = time.monotonic() + 1800
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            os.fdopen(
                os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600),
                "wb",
            ) as output,
        ):
            os.fchmod(output.fileno(), 0o600)
            while block := response.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("Download exceeded its 30-minute budget")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if sha256(temporary) != expected:
            raise ValueError("Download checksum differs: " + destination.name)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


class Runner:
    def __init__(self, log):
        self.log = Path(log)

    def run(self, args, *, timeout=600, check=True, capture=False, **kwargs):
        with self.log.open("ab") as stream:
            os.fchmod(stream.fileno(), 0o600)
            result = subprocess.run(
                list(map(str, args)),
                stdout=subprocess.PIPE if capture else stream,
                stderr=stream,
                timeout=timeout,
                **kwargs,
            )
        if check and result.returncode:
            raise RuntimeError(
                f"{Path(str(args[0])).name} failed ({result.returncode}); see {self.log}"
            )
        return result

    def virsh(self, *args, check=True):
        return self.run(
            ["sudo", "-n", "virsh", "-c", "qemu:///system", *args],
            capture=True,
            check=check,
            timeout=30,
        )


def domain_xml(name, ident, disk, seed, network, memory, cpus, mac):
    root = ET.Element("domain", type="kvm")
    ET.SubElement(root, "name").text = name
    metadata = ET.SubElement(root, "metadata")
    ET.SubElement(metadata, "{" + OWNER_NAMESPACE + "}installation", id=ident)
    ET.SubElement(root, "memory", unit="MiB").text = str(memory)
    ET.SubElement(root, "vcpu").text = str(cpus)
    boot = ET.SubElement(root, "os")
    ET.SubElement(boot, "type", arch="x86_64", machine="q35").text = "hvm"
    ET.SubElement(boot, "boot", dev="hd")
    features = ET.SubElement(root, "features")
    for feature in ("acpi", "apic"):
        ET.SubElement(features, feature)
    ET.SubElement(root, "cpu", mode="host-passthrough")
    ET.SubElement(root, "on_reboot").text = "restart"
    devices = ET.SubElement(root, "devices")
    for path, kind, target, bus in (
        (disk, "disk", "vda", "virtio"),
        (seed, "cdrom", "sda", "sata"),
    ):
        element = ET.SubElement(devices, "disk", type="file", device=kind)
        ET.SubElement(element, "driver", name="qemu", type="qcow2" if kind == "disk" else "raw")
        ET.SubElement(element, "source", file=str(path))
        ET.SubElement(element, "target", dev=target, bus=bus)
        if kind == "cdrom":
            ET.SubElement(element, "readonly")
    interface = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(interface, "source", network=network)
    ET.SubElement(interface, "mac", address=mac)
    ET.SubElement(interface, "model", type="virtio")
    ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(devices, "console", type="pty")
    rng = ET.SubElement(devices, "rng", model="virtio")
    ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"
    return ET.tostring(root, encoding="unicode")


def assert_domain_owner(xml, ident):
    owner = ET.fromstring(xml).find("metadata/{" + OWNER_NAMESPACE + "}installation")
    if owner is None or owner.get("id") != ident:
        raise ValueError(
            "VM name already belongs to another installation; choose another --vm-name"
        )


def cloud_config(public_key, host_private, host_public, ident, admin_key):
    script = files("live_transcript.runtime").joinpath("guest-bootstrap.sh").read_text()
    return (
        "#cloud-config\n"
        + json.dumps(
            dict(
                hostname="live-transcript",
                manage_etc_hosts=True,
                ssh_pwauth=False,
                disable_root=False,
                ssh_deletekeys=True,
                ssh_keys=dict(ed25519_private=host_private, ed25519_public=host_public),
                users=[
                    dict(name="root", lock_passwd=True, ssh_authorized_keys=[admin_key]),
                    dict(
                        name="transcript",
                        shell="/bin/bash",
                        lock_passwd=True,
                        groups=["audio", "video"],
                        ssh_authorized_keys=[public_key],
                        sudo=[
                            "ALL=(root) NOPASSWD: /usr/sbin/modprobe snd-dummy, /usr/sbin/rmmod snd-aloop"
                        ],
                    ),
                ],
                write_files=[
                    dict(
                        path="/opt/live-transcript/guest-bootstrap.sh",
                        permissions="0700",
                        content=script,
                    )
                ],
                runcmd=[["bash", "/opt/live-transcript/guest-bootstrap.sh", ident]],
                power_state=dict(mode="reboot", delay="now", timeout=30, condition=True),
            ),
            indent=2,
        )
        + "\n"
    )


class BrowserVM:
    def assert_idle(self, ip):
        code = """import fcntl,pathlib,subprocess,os
p=pathlib.Path('/home/transcript/logs/meeting-owner.lock')
lock=p.open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
r=subprocess.run(['systemctl','--user','list-units','--no-legend',
 '--state=active,activating,deactivating','live-transcript-meeting-*'],
 capture_output=True,check=True,timeout=10,
 env=dict(os.environ,XDG_RUNTIME_DIR='/run/user/'+str(os.getuid())))
assert not r.stdout.strip(),'A meeting owns the guest; setup refused'
"""
        self.ssh(ip, "/opt/live-transcript/venv/bin/python -c " + shlex.quote(code))

    def __init__(self, runner, state, workspace, options):
        self.runner, self.state = runner, state
        self.root, self.options = Path(workspace), options
        self.name = options["vm_name"]
        self.images = Path(options["vm_storage"]) / self.name

    def network(self):
        name = self.options["network"]
        result = self.runner.virsh("net-dumpxml", name, check=False)
        if result.returncode:
            if name != "default":
                raise ValueError("Configured libvirt network does not exist")
            self.runner.virsh("net-define", "/usr/share/libvirt/networks/default.xml")
            result = self.runner.virsh("net-dumpxml", name)
        xml = ET.fromstring(result.stdout)
        ip = next(
            (
                e.get("address")
                for e in xml.findall("ip")
                if e.get("address") and ipaddress.ip_address(e.get("address")).version == 4
            ),
            None,
        )
        if not ip or xml.find("ip/dhcp") is None:
            raise ValueError("Provisioning requires a libvirt IPv4 network with DHCP")
        active = self.runner.virsh("net-list", "--name").stdout.decode().splitlines()
        if name not in active:
            self.runner.virsh("net-start", name)
        self.runner.virsh("net-autostart", name)
        return ip

    def keys(self):
        for name in ("ssh-key", "host-key", "admin-key"):
            path = self.root / name
            if not path.exists():
                self.runner.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "ed25519",
                        "-N",
                        "",
                        "-C",
                        "live-transcript-managed",
                        "-f",
                        path,
                    ]
                )
            path.chmod(0o600)
        return (self.root / "ssh-key.pub").read_text().strip()

    def create(self):
        existing = self.runner.virsh("dumpxml", self.name, check=False)
        if existing.returncode == 0:
            assert_domain_owner(existing.stdout, self.state["id"])
            return
        # Do not mistake a permission/daemon failure for permission to create resources.
        self.runner.virsh("list", "--all")
        disk, seed = self.images / "system.qcow2", self.images / "seed.iso"
        if self.runner.run(["sudo", "-n", "test", "-e", self.images], check=False).returncode == 0:
            marker = (
                self.runner.run(["sudo", "-n", "cat", self.images / "owner"], capture=True)
                .stdout.decode()
                .strip()
            )
            if marker != self.state["id"]:
                raise ValueError("VM image directory belongs to another installation")
        if self.runner.run(["sudo", "-n", "test", "-e", disk], check=False).returncode == 0:
            # A retained disk from an interrupted define is never replaced.
            marker = (
                self.runner.run(["sudo", "-n", "cat", self.images / "owner"], capture=True)
                .stdout.decode()
                .strip()
            )
            if marker != self.state["id"]:
                raise ValueError("An unowned VM disk already exists")
        else:
            image = download(IMAGE_URL, self.root / "downloads/browser.img", IMAGE_SHA256)
            user_data = self.root / "user-data"
            atomic_text(
                user_data,
                cloud_config(
                    self.keys(),
                    (self.root / "host-key").read_text(),
                    (self.root / "host-key.pub").read_text(),
                    self.state["id"],
                    (self.root / "admin-key.pub").read_text().strip(),
                ),
            )
            metadata = self.root / "meta-data"
            atomic_text(
                metadata, json.dumps({"instance-id": self.state["id"], "local-hostname": self.name})
            )
            local_seed = self.root / "seed.iso"
            self.runner.run(["cloud-localds", local_seed, user_data, metadata])
            self.runner.run(
                [
                    "sudo",
                    "-n",
                    "install",
                    "-d",
                    "-m",
                    "0750",
                    "-o",
                    "root",
                    "-g",
                    "kvm",
                    self.images,
                ]
            )
            owner = self.root / "owner"
            atomic_text(owner, self.state["id"] + "\n")
            self.runner.run(["sudo", "-n", "install", "-m", "0640", owner, self.images / "owner"])
            self.runner.run(["sudo", "-n", "install", "-m", "0640", "-g", "kvm", local_seed, seed])
            temporary = self.images / "system.incoming.qcow2"
            self.runner.run(
                ["sudo", "-n", "qemu-img", "convert", "-O", "qcow2", image, temporary], timeout=600
            )
            self.runner.run(
                ["sudo", "-n", "qemu-img", "resize", temporary, str(self.options["disk_gib"]) + "G"]
            )
            self.runner.run(["sudo", "-n", "chown", "libvirt-qemu:kvm", temporary])
            self.runner.run(["sudo", "-n", "chmod", "0640", temporary])
            self.runner.run(["sudo", "-n", "mv", "-n", temporary, disk])
        xml = domain_xml(
            self.name,
            self.state["id"],
            disk,
            seed,
            self.options["network"],
            self.options["memory_mib"],
            self.options["vcpus"],
            self.state["mac"],
        )
        path = self.root / "domain.xml"
        atomic_text(path, xml)
        self.runner.virsh("define", str(path))

    def ssh(self, ip, command, *, timeout=20, check=True, capture=False, admin=False, input=None):
        return self.runner.run(
            [
                "ssh",
                "-i",
                self.root / ("admin-key" if admin else "ssh-key"),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + str(self.root / "known_hosts"),
                ("root@" if admin else "transcript@") + ip,
                command,
            ],
            timeout=timeout,
            check=check,
            capture=capture,
            input=input,
        )

    def ready(self, timeout=1800):
        running = self.runner.virsh("list", "--name").stdout.decode().splitlines()
        if self.name not in running:
            self.runner.virsh("start", self.name)
        deadline = time.monotonic() + timeout
        ip = None
        repaired = False
        while time.monotonic() < deadline:
            result = self.runner.virsh("domifaddr", self.name, "--source", "lease")
            for line in result.stdout.decode().splitlines():
                columns = line.split()
                if self.state["mac"] in columns and "ipv4" in columns:
                    ip = str(ipaddress.ip_interface(columns[-1]).ip)
                    break
            if ip:
                key = (self.root / "host-key.pub").read_text().split()
                atomic_text(self.root / "known_hosts", ip + " " + " ".join(key[:2]) + "\n")
                probe = self.ssh(
                    ip,
                    "test -f /var/lib/live-transcript/ready && test -c /dev/video0 && grep -q '^snd_dummy ' /proc/modules",
                    check=False,
                )
                if probe.returncode == 0:
                    self.ssh(
                        ip,
                        "/opt/live-transcript/venv/bin/python -c "
                        + shlex.quote(
                            "import playwright.sync_api,PIL; from pathlib import Path; assert Path.home().joinpath('.cache/ms-playwright').is_dir()"
                        ),
                    )
                    return ip
                failure = self.ssh(ip, "test -f /var/lib/live-transcript/failed", check=False)
                if failure.returncode == 0:
                    self.ssh(ip, "tail -n 100 /var/log/cloud-init-output.log", admin=True)
                    if repaired:
                        raise RuntimeError(
                            "Guest setup failed after repair; cloud-init diagnostics are in the setup log"
                        )
                    # Repair only an idle owned guest, with the operator-only key.
                    guard = """import pathlib,subprocess,pwd
p=pathlib.Path('/home/transcript/logs');p.mkdir(exist_ok=True)
uid=str(pwd.getpwnam('transcript').pw_uid)
r=subprocess.run(['runuser','-u','transcript','--','env','XDG_RUNTIME_DIR=/run/user/'+uid,
 'systemctl','--user','list-units','--no-legend','--state=active,activating,deactivating','live-transcript-meeting-*'],capture_output=True,check=True)
assert not r.stdout.strip(),'A meeting owns the guest; repair refused'
"""
                    script = (
                        files("live_transcript.runtime").joinpath("guest-bootstrap.sh").read_bytes()
                    )
                    payload = ("set -e\npython3 -c " + shlex.quote(guard) + "\n").encode() + script
                    self.ssh(
                        ip,
                        "install -d -m 0700 -o transcript -g transcript /home/transcript/logs && "
                        "touch /home/transcript/logs/meeting-owner.lock && "
                        "chown transcript:transcript /home/transcript/logs/meeting-owner.lock && "
                        "flock -n /home/transcript/logs/meeting-owner.lock bash -s -- "
                        + shlex.quote(self.state["id"]),
                        admin=True,
                        input=payload,
                        timeout=1200,
                    )
                    self.ssh(ip, "shutdown -r +1", admin=True)
                    repaired = True
            time.sleep(5)
        raise TimeoutError(
            "VM provisioning timed out; disk retained. Inspect the setup log and rerun setup."
        )

    def reserve_address(self, ip):
        """Keep the recorded SSH address stable across guest and host reboots."""
        network = self.options["network"]
        xml = ET.fromstring(self.runner.virsh("net-dumpxml", network).stdout)
        for host in xml.findall("ip/dhcp/host"):
            if host.get("mac") == self.state["mac"]:
                if host.get("ip") != ip:
                    raise ValueError("Owned DHCP reservation changed unexpectedly")
                return
            if host.get("ip") == ip:
                raise ValueError("DHCP address already reserved for another machine")
        host = ET.Element("host", mac=self.state["mac"], name=self.name, ip=ip)
        self.runner.virsh(
            "net-update",
            network,
            "add-last",
            "ip-dhcp-host",
            ET.tostring(host, encoding="unicode"),
            "--live",
            "--config",
        )
