"""目标机三项指标的显示投影；派单拒绝仍由 cli.assess 决定。"""
import math
from typing import Any, Optional


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def host_metrics(host: dict[str, str], info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """只返回数值与固定枚举，不透传配置或探针对象。"""
    def metric(value: Any, unit: str, warning: str, pressure: str, *, lower: bool = False) -> dict[str, Any]:
        value = number(value) if info.get("reachable") else None
        warning_value, pressure_value = float(host[warning]), float(host[pressure])
        level = "unknown"
        if value is not None:
            if (value < pressure_value) if lower else (value > pressure_value):
                level = "pressure"
            elif (value <= warning_value) if lower else (value >= warning_value):
                level = "warning"
            else:
                level = "normal"
        return dict(value=value, unit=unit, warning=warning_value, pressure=pressure_value, level=level)

    if host["platform"] == "windows":
        cpu = metric(info.get("cpu_pct"), "cpu_pct", "warn_cpu_pct", "max_cpu_pct")
    else:
        load, cores = number(info.get("load1")), number(info.get("ncpu"))
        value = load / cores if load is not None and cores else None
        cpu = metric(value, "load_per_core", "warn_load_per_core", "max_load_per_core")
    memory = metric(info.get("memfree"), "free_pct", "warn_free_mem_pct", "min_free_mem_pct", lower=True)
    disk = metric(info.get("disk"), "free_gb", "warn_free_disk_gb", "min_free_disk_gb", lower=True)
    disk["total"] = number(info.get("disk_total")) if info.get("reachable") else None
    return dict(cpu=cpu, memory=memory, disk=disk)
