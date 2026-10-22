"""Small, dependency-free Linux process/container memory diagnostics."""
from pathlib import Path


def memory_snapshot():
    result = {"rss_mb": None, "peak_rss_mb": None, "container_mb": None, "limit_mb": None}
    try:
        status = {}
        for line in Path("/proc/self/status").read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("VmRSS", "VmHWM"):
                status[key] = round(int(value.split()[0]) / 1024, 1)
        result.update(rss_mb=status.get("VmRSS"), peak_rss_mb=status.get("VmHWM"))
    except (OSError, ValueError, IndexError):
        pass
    for key, paths in {
        "container_mb": ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        "limit_mb": ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    }.items():
        for name in paths:
            try:
                value = Path(name).read_text().strip()
                if value != "max":
                    count = int(value)
                    if 0 <= count < 2**60:
                        result[key] = round(count / 1024**2, 1)
                break
            except (OSError, ValueError):
                continue
    return result
