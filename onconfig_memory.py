#!/usr/bin/env python3
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SYSCTL_FILE = Path("/etc/sysctl.d/99-informix-hugepages.conf")
LIMITS_FILE = Path("/etc/security/limits.d/99-informix-memlock.conf")
PROFILE_FILE = Path("/etc/profile.d/profile.local.sh")
OS_MIN_KB = 3 * 1024 * 1024
WG_SHM_KB = 16 * 1024 * 1024
BUFFER_POOLS = 2


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


def dimension(ram_kb, hp_kb):
    hp_kb = hp_kb or 2048
    os_reserve_kb = max(OS_MIN_KB, ram_kb * 25 // 100)
    inform_ram_kb = ram_kb - os_reserve_kb
    capped_kb = inform_ram_kb if inform_ram_kb < WG_SHM_KB else WG_SHM_KB
    shmtotal_kb = capped_kb // hp_kb * hp_kb
    if ram_kb >= 32 * 1024 * 1024:
        shmadd, extshmadd = 262144, 65536
    else:
        shmadd, extshmadd = 131072, 32768
    buffer_total_mb = shmtotal_kb * 70 // 100 // 1024
    buffer_max_mb = buffer_total_mb // BUFFER_POOLS
    buffer_start_mb = 512 if buffer_max_mb > 512 else buffer_max_mb
    nr_hugepages = shmtotal_kb // hp_kb + (shmadd + hp_kb - 1) // hp_kb
    return {
        "os_reserve_kb": os_reserve_kb,
        "inform_ram_kb": inform_ram_kb,
        "shmtotal_kb": shmtotal_kb,
        "shmadd": shmadd,
        "extshmadd": extshmadd,
        "buffer_start_mb": buffer_start_mb,
        "buffer_max_mb": buffer_max_mb,
        "nr_hugepages": nr_hugepages,
        "hp_kb": hp_kb,
    }


def replace_or_append(text, key, line):
    pat = re.compile(rf"^{re.escape(key)}\s+.*$", re.M)
    if pat.search(text):
        return pat.sub(line, text, count=1)
    return text.rstrip() + "\n" + line + "\n"


def comment_unmanaged_bufferpool(text):
    def repl(m):
        line = m.group(0)
        if re.match(r"^BUFFERPOOL\s+default,extendable=1", line):
            return line
        return "# " + line

    return re.sub(r"^BUFFERPOOL\s+.*$", repl, text, flags=re.M)


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
    hp = 2048
    d8 = dimension(8 * 1024 * 1024, hp)
    os8 = max(OS_MIN_KB, 8 * 1024 * 1024 * 25 // 100)
    assert d8["os_reserve_kb"] == os8 == OS_MIN_KB
    assert d8["shmtotal_kb"] == (8 * 1024 * 1024 - os8) // hp * hp
    assert d8["shmtotal_kb"] % hp == 0
    assert d8["nr_hugepages"] == d8["shmtotal_kb"] // hp + (d8["shmadd"] + hp - 1) // hp
    assert d8["nr_hugepages"] * hp <= 8 * 1024 * 1024 - os8 + d8["shmadd"] + hp - 1
    assert d8["buffer_max_mb"] == (d8["shmtotal_kb"] * 70 // 100 // 1024) // BUFFER_POOLS
    assert d8["buffer_max_mb"] * BUFFER_POOLS * 1024 <= d8["shmtotal_kb"] * 70 // 100
    assert d8["buffer_start_mb"] == 512
    assert d8["shmtotal_kb"] < WG_SHM_KB
    assert d8["shmadd"] == 131072
    d64 = dimension(64 * 1024 * 1024, hp)
    os64 = max(OS_MIN_KB, 64 * 1024 * 1024 * 25 // 100)
    assert d64["os_reserve_kb"] == os64 == 16 * 1024 * 1024
    assert d64["shmtotal_kb"] == WG_SHM_KB
    assert d64["shmtotal_kb"] < (64 * 1024 * 1024 - os64)
    assert d64["buffer_start_mb"] == 512
    assert d64["shmadd"] == 262144
    assert d64["buffer_max_mb"] == (d64["shmtotal_kb"] * 70 // 100 // 1024) // BUFFER_POOLS
    assert d64["buffer_max_mb"] * BUFFER_POOLS * 1024 <= d64["shmtotal_kb"] * 70 // 100
    print("self-check ok")


def main():
    ram_kb = ram_kb_from_args_or_host()
    if ram_kb < 4 * 1024 * 1024:
        print("Fehler: RAM < 4 GB. Beispiel: {} 32".format(sys.argv[0]), file=sys.stderr)
        sys.exit(1)

    info = meminfo()
    hp_kb = info.get("Hugepagesize", 2048) or 2048
    informix_gid = int(os.popen("id -g informix").read().strip())
    d = dimension(ram_kb, hp_kb)
    if d["shmtotal_kb"] <= 0:
        print("Fehler: SHMTOTAL 0 nach OS-Reserve", file=sys.stderr)
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
    text = comment_unmanaged_bufferpool(text)
    text = replace_or_append(text, "SHMTOTAL", "SHMTOTAL    {}".format(d["shmtotal_kb"]))
    text = replace_or_append(text, "SHMADD", "SHMADD      {}".format(d["shmadd"]))
    text = replace_or_append(text, "EXTSHMADD", "EXTSHMADD   {}".format(d["extshmadd"]))
    text = replace_or_append(text, "RESIDENT", "RESIDENT    -1")
    text = replace_or_append(
        text,
        "BUFFERPOOL",
        "BUFFERPOOL  default,extendable=1,start_memory={}MB,memory={}MB".format(
            d["buffer_start_mb"], d["buffer_max_mb"]
        ),
    )
    onconfig.write_text(text)

    LIMITS_FILE.write_text(
        "informix soft memlock unlimited\ninformix hard memlock unlimited\n"
    )
    set_profile_export("IFX_LARGE_PAGES", "1")

    cur_hp = int(Path("/proc/sys/vm/nr_hugepages").read_text().strip() or "0")
    SYSCTL_FILE.write_text(
        "vm.nr_hugepages = {}\nvm.hugetlb_shm_group = {}\n".format(d["nr_hugepages"], informix_gid)
    )
    if cur_hp < d["nr_hugepages"]:
        Path("/proc/sys/vm/drop_caches").write_text("3")
    os.system("sysctl -p {}".format(SYSCTL_FILE))
    got = int(Path("/proc/meminfo").read_text().split("HugePages_Total:")[1].split()[0])
    if got < d["nr_hugepages"]:
        print("Fehler: HugePages_Total={} < {}".format(got, d["nr_hugepages"]), file=sys.stderr)
        sys.exit(1)

    print(
        "RAM {} KB | OS-Reserve {} | SHMTOTAL {} | BUFFER {}-{} MB | nr_hugepages {} | RESIDENT -1 | hugetlb_shm_group {}".format(
            ram_kb,
            d["os_reserve_kb"],
            d["shmtotal_kb"],
            d["buffer_start_mb"],
            d["buffer_max_mb"],
            d["nr_hugepages"],
            informix_gid,
        )
    )
    print("Aktualisiert:", onconfig)
    print("oninit mit: export IFX_LARGE_PAGES=1; ulimit -l unlimited; oninit")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        self_check()
        sys.exit(0)
    main()
