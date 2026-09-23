#!/usr/bin/env python3
import os
import re
import sys
from datetime import datetime
from pathlib import Path
 
ONCONFIG_FILE = Path(sys.argv[2] if len(sys.argv) > 2 else "/opt/IBM/informix/etc/onconfig.local")
SYSCTL_FILE = Path("/etc/sysctl.d/99-informix-hugepages.conf")
LIMITS_FILE = Path("/etc/security/limits.d/99-informix-memlock.conf")
PROFILE_FILE = Path("/etc/profile.d/profile.local.sh")
 
 
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
 
 
def main():
    ram_kb = ram_kb_from_args_or_host()
    if ram_kb < 4 * 1024 * 1024:
        print("Fehler: RAM < 4 GB. Beispiel: {} 32".format(sys.argv[0]), file=sys.stderr)
        sys.exit(1)
 
    info = meminfo()
    hp_kb = info.get("Hugepagesize", 2048) or 2048
    informix_gid = int(os.popen("id -g informix").read().strip())
 
    inform_ram_kb = ram_kb * 80 // 100
    shmtotal_kb = (inform_ram_kb + hp_kb - 1) // hp_kb * hp_kb
    buffer_start_mb = inform_ram_kb * 30 // 100 // 1024
    buffer_max_mb = inform_ram_kb * 75 // 100 // 1024
    if buffer_start_mb < 2048:
        buffer_start_mb = 2048
    if buffer_max_mb < 4096:
        buffer_max_mb = 4096
    inform_ram_mb = inform_ram_kb // 1024
    if buffer_max_mb > inform_ram_mb:
        buffer_max_mb = inform_ram_mb
    if buffer_start_mb > buffer_max_mb:
        buffer_start_mb = buffer_max_mb
 
    if ram_kb >= 32 * 1024 * 1024:
        shmadd, extshmadd = 262144, 65536
    else:
        shmadd, extshmadd = 131072, 32768
 
    n_ext = ((buffer_max_mb - buffer_start_mb) * 1024 + extshmadd - 1) // extshmadd
    if n_ext < 4:
        n_ext = 4
    nr_hp = shmtotal_kb // hp_kb + n_ext + (shmadd + hp_kb - 1) // hp_kb
    max_hp = ram_kb * 85 // 100 // hp_kb
    if nr_hp > max_hp:
        nr_hp = max_hp
 
    if not ONCONFIG_FILE.is_file():
        print("Fehler: {} nicht gefunden".format(ONCONFIG_FILE), file=sys.stderr)
        sys.exit(1)
 
    backup = Path("{}.bak_{}".format(ONCONFIG_FILE, datetime.now().strftime("%Y%m%d_%H%M%S")))
    backup.write_bytes(ONCONFIG_FILE.read_bytes())
    print("Backup:", backup)
 
    text = ONCONFIG_FILE.read_text()
    text = comment_shmvisize(text)
    text = comment_unmanaged_bufferpool(text)
    text = replace_or_append(text, "SHMTOTAL", "SHMTOTAL    {}".format(shmtotal_kb))
    text = replace_or_append(text, "SHMADD", "SHMADD      {}".format(shmadd))
    text = replace_or_append(text, "EXTSHMADD", "EXTSHMADD   {}".format(extshmadd))
    text = replace_or_append(text, "RESIDENT", "RESIDENT    -1")
    text = replace_or_append(
        text,
        "BUFFERPOOL",
        "BUFFERPOOL  default,extendable=1,start_memory={}MB,memory={}MB".format(
            buffer_start_mb, buffer_max_mb
        ),
    )
    ONCONFIG_FILE.write_text(text)
 
    LIMITS_FILE.write_text(
        "informix soft memlock unlimited\ninformix hard memlock unlimited\n"
    )
    set_profile_export("IFX_LARGE_PAGES", "1")
 
    cur_hp = int(Path("/proc/sys/vm/nr_hugepages").read_text().strip() or "0")
    SYSCTL_FILE.write_text(
        "vm.nr_hugepages = {}\nvm.hugetlb_shm_group = {}\n".format(nr_hp, informix_gid)
    )
    if cur_hp < nr_hp:
        Path("/proc/sys/vm/drop_caches").write_text("3")
    os.system("sysctl -p {}".format(SYSCTL_FILE))
    got = int(Path("/proc/meminfo").read_text().split("HugePages_Total:")[1].split()[0])
    if got < nr_hp:
        print("Fehler: HugePages_Total={} < {}".format(got, nr_hp), file=sys.stderr)
        sys.exit(1)
 
    print(
        "RAM {} KB | SHMTOTAL {} | BUFFER {}-{} MB | nr_hugepages {} | RESIDENT -1 | hugetlb_shm_group {}".format(
            ram_kb, shmtotal_kb, buffer_start_mb, buffer_max_mb, nr_hp, informix_gid
        )
    )
    print("Aktualisiert:", ONCONFIG_FILE)
    print("oninit mit: export IFX_LARGE_PAGES=1; ulimit -l unlimited; oninit")
 
 
if __name__ == "__main__":
    main()
