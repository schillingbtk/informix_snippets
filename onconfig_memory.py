#!/usr/bin/env python3
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SYSCTL_FILE = Path("/etc/sysctl.d/99-informix-hugepages.conf")
LIMITS_FILE = Path("/etc/security/limits.d/99-informix-memlock.conf")
PROFILE_FILE = Path("/etc/profile.d/profile.local.sh")
SHMADD_KB = 256000
EXTSHMADD_KB = 8192


def meminfo():
    data = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v, *rest = line.split()
            data[k.rstrip(":")] = int(v)
    return data


def ram_kb_from_args_or_host():
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        return int(sys.argv[1]) * 1024 * 1024
    return meminfo().get("MemTotal", 0)


def dimension(ram_kb):
    buffer_2k_mb = ram_kb * 40 // 64 // 1024
    buffer_16k_mb = ram_kb * 10 // 64 // 1024
    shmvirtsize_kb = ram_kb * 4 // 64
    return {
        "shmtotal_kb": 0,
        "shmadd": SHMADD_KB,
        "extshmadd": EXTSHMADD_KB,
        "buffer_2k_mb": buffer_2k_mb,
        "buffer_16k_mb": buffer_16k_mb,
        "shmvirtsize_kb": shmvirtsize_kb,
        "shmvirt_allocseg": "0,3",
        "nr_hugepages": 0,
    }


def replace_or_append(text, key, line):
    pat = re.compile(rf"^{re.escape(key)}\s+.*$", re.M)
    if pat.search(text):
        return pat.sub(line, text, count=1)
    return text.rstrip() + "\n" + line + "\n"


def comment_bufferpool(text):
    return re.sub(r"^BUFFERPOOL\s+.*$", lambda m: "# " + m.group(0), text, flags=re.M)


def comment_shmvisize(text):
    return re.sub(r"^SHMVSIZE(\s+.*)$", r"# SHMVSIZE\1", text, flags=re.M)


def set_profile_export(name, value):
    if not PROFILE_FILE.is_file():
        return
    text = PROFILE_FILE.read_text()
    line = "export {}={}".format(name, value)
    pat = re.compile(r"^(export\s+)?" + re.escape(name) + r"=.*$", re.M)
    if pat.search(text):
        text = pat.sub(line, text)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    PROFILE_FILE.write_text(text)


def self_check():
    d8 = dimension(8 * 1024 * 1024)
    assert d8["shmtotal_kb"] == 0
    assert d8["nr_hugepages"] == 0
    assert d8["shmadd"] == SHMADD_KB
    assert d8["extshmadd"] == EXTSHMADD_KB
    assert d8["shmvirt_allocseg"] == "0,3"
    assert d8["buffer_2k_mb"] == 5120
    assert d8["buffer_16k_mb"] == 1280
    assert d8["shmvirtsize_kb"] == 524288
    assert (d8["buffer_2k_mb"] + d8["buffer_16k_mb"]) * 1024 + d8["shmvirtsize_kb"] < 8 * 1024 * 1024
    d64 = dimension(64 * 1024 * 1024)
    assert d64["buffer_2k_mb"] == 40960
    assert d64["buffer_16k_mb"] == 10240
    assert d64["shmvirtsize_kb"] == 4 * 1024 * 1024
    assert d64["shmtotal_kb"] == 0
    assert (d64["buffer_2k_mb"] + d64["buffer_16k_mb"]) * 1024 + d64["shmvirtsize_kb"] < 64 * 1024 * 1024
    print("self-check ok")


def main():
    ram_kb = ram_kb_from_args_or_host()
    if ram_kb < 4 * 1024 * 1024:
        print("Fehler: RAM < 4 GB. Beispiel: {} 32".format(sys.argv[0]), file=sys.stderr)
        sys.exit(1)

    informix_gid = int(os.popen("id -g informix").read().strip())
    d = dimension(ram_kb)
    used_kb = (d["buffer_2k_mb"] + d["buffer_16k_mb"]) * 1024 + d["shmvirtsize_kb"]
    if d["buffer_2k_mb"] < 64 or used_kb >= ram_kb:
        print("Fehler: Bufferpools passen nicht in das RAM", file=sys.stderr)
        sys.exit(1)

    onconfig = Path(sys.argv[2] if len(sys.argv) > 2 else "/opt/IBM/informix/etc/onconfig.local")
    if not onconfig.is_file():
        print("Fehler: {} nicht gefunden".format(onconfig), file=sys.stderr)
        sys.exit(1)

    backup = Path("{}.bak_{}".format(onconfig, datetime.now().strftime("%Y%m%d_%H%M%S")))
    backup.write_bytes(onconfig.read_bytes())
    print("Backup:", backup)

    text = onconfig.read_text()
    text = comment_shmvisize(text)
    text = comment_bufferpool(text)
    text = replace_or_append(text, "SHMTOTAL", "SHMTOTAL    0")
    text = replace_or_append(text, "SHMADD", "SHMADD      {}".format(d["shmadd"]))
    text = replace_or_append(text, "EXTSHMADD", "EXTSHMADD   {}".format(d["extshmadd"]))
    text = replace_or_append(text, "RESIDENT", "RESIDENT    1")
    text = replace_or_append(text, "SHMVIRTSIZE", "SHMVIRTSIZE {}".format(d["shmvirtsize_kb"]))
    text = replace_or_append(text, "SHMVIRT_ALLOCSEG", "SHMVIRT_ALLOCSEG {}".format(d["shmvirt_allocseg"]))
    text = text.rstrip() + "\n" + "\n".join([
        "BUFFERPOOL  size=2K,extendable=0,start_memory={0}MB,memory={0}MB".format(d["buffer_2k_mb"]),
        "BUFFERPOOL  size=16K,extendable=0,start_memory={0}MB,memory={0}MB".format(d["buffer_16k_mb"]),
    ]) + "\n"
    onconfig.write_text(text)

    LIMITS_FILE.write_text(
        "informix soft memlock unlimited\ninformix hard memlock unlimited\n"
    )
    set_profile_export("IFX_LARGE_PAGES", "0")

    SYSCTL_FILE.write_text(
        "vm.nr_hugepages = 0\nvm.hugetlb_shm_group = {}\n".format(informix_gid)
    )
    os.system("sysctl -p {}".format(SYSCTL_FILE))
    got = int(Path("/proc/meminfo").read_text().split("HugePages_Total:")[1].split()[0])
    if got > 0:
        print("Hinweis: HugePages_Total={} bleibt belegt, bis Informix beendet ist".format(got))

    print(
        "RAM {} KB | SHMTOTAL 0 | 2K {} MB | 16K {} MB | SHMVIRTSIZE {} KB | RESIDENT 1 | nr_hugepages 0".format(
            ram_kb,
            d["buffer_2k_mb"],
            d["buffer_16k_mb"],
            d["shmvirtsize_kb"],
        )
    )
    print("Aktualisiert:", onconfig)
    print("oninit mit: export IFX_LARGE_PAGES=0; ulimit -l unlimited; oninit")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
        sys.exit(0)
    main()
