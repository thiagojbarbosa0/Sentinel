"""
Agente de coleta.

Reúne métricas reais do host usando psutil (CPU, memória, disco, processos)
e sondas de rede via socket. Não depende de nenhum serviço externo além dos
hosts usados no diagnóstico de rede (gateway / DNS / internet).
"""
from __future__ import annotations

import platform
import re
import socket
import subprocess
import time

import psutil

from app.config import DISK_PATH, DNS_PROBE_HOST, INTERNET_PROBE_HOST, PROBE_PORT, PROBE_TIMEOUT, TOP_N_PROCESSES

_last_disk_io = None
_last_net_io = None
_last_io_ts = None


def collect_system() -> dict:
    """CPU, memória e disco — visão geral do host (seção 5 do projeto)."""
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(DISK_PATH)
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_per_core": psutil.cpu_percent(interval=None, percpu=True),
        "load_avg": _load_avg(),
        "ram": vm.percent,
        "ram_used_gb": round(vm.used / (1024**3), 2),
        "ram_total_gb": round(vm.total / (1024**3), 2),
        "swap": psutil.swap_memory().percent,
        "disk": disk.percent,
        "disk_free_gb": round(disk.free / (1024**3), 2),
        "uptime_seconds": time.time() - psutil.boot_time(),
    }


def _load_avg() -> list[float] | None:
    try:
        return list(psutil.getloadavg())
    except (AttributeError, OSError):
        return None  # não disponível no Windows


def collect_io_rates() -> dict:
    """Taxas de I/O de disco e rede (delta entre chamadas, em MB/s)."""
    global _last_disk_io, _last_net_io, _last_io_ts
    now = time.time()
    disk_io = psutil.disk_io_counters()
    net_io = psutil.net_io_counters()

    rates = {"disk_read_mbs": 0.0, "disk_write_mbs": 0.0, "net_rx_mbs": 0.0, "net_tx_mbs": 0.0}
    if _last_disk_io and _last_net_io and _last_io_ts:
        dt = max(now - _last_io_ts, 0.001)
        rates["disk_read_mbs"] = round(max(disk_io.read_bytes - _last_disk_io.read_bytes, 0) / dt / 1e6, 3)
        rates["disk_write_mbs"] = round(max(disk_io.write_bytes - _last_disk_io.write_bytes, 0) / dt / 1e6, 3)
        rates["net_rx_mbs"] = round(max(net_io.bytes_recv - _last_net_io.bytes_recv, 0) / dt / 1e6, 3)
        rates["net_tx_mbs"] = round(max(net_io.bytes_sent - _last_net_io.bytes_sent, 0) / dt / 1e6, 3)

    _last_disk_io, _last_net_io, _last_io_ts = disk_io, net_io, now
    return rates


def collect_processes(top_n: int = TOP_N_PROCESSES) -> list[dict]:
    """
    Top-N processos por CPU. psutil.cpu_percent() por processo precisa de uma
    primeira chamada "de aquecimento" (feita no primeiro tick do scheduler)
    para não retornar 0.0 sempre.
    """
    procs = []
    for p in psutil.process_iter(attrs=["pid", "name", "cpu_percent", "memory_percent", "nice"]):
        try:
            info = p.info
            if info["name"] in (None, ""):
                continue
            procs.append({
                "pid": info["pid"],
                "name": info["name"],
                "cpu": round(info["cpu_percent"] or 0.0, 2),
                "ram": round(info["memory_percent"] or 0.0, 2),
                "nice": info.get("nice"),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    return procs[:top_n]


def _default_gateway() -> str | None:
    """
    Resolve o gateway padrão do host. A abordagem muda por SO porque nenhuma
    delas depende de bibliotecas externas (evita puxar `netifaces` só por
    causa disso):
      - Linux: lê /proc/net/route diretamente (rápido, sem subprocess).
      - macOS: `route -n get default`.
      - Windows: `ipconfig` (procura o primeiro "Default Gateway" não vazio).
    Qualquer falha retorna None e o hop de "Gateway" some do diagnóstico em
    vez de quebrar a coleta.
    """
    system = platform.system()
    try:
        if system == "Linux":
            return _gateway_linux()
        if system == "Darwin":
            return _gateway_macos()
        if system == "Windows":
            return _gateway_windows()
    except Exception:
        return None
    return None


def _gateway_linux() -> str | None:
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) < 3 or fields[1] != "00000000":
                    continue
                gw_hex = fields[2]
                return ".".join(str(int(gw_hex[i:i + 2], 16)) for i in range(6, -1, -2))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _gateway_macos() -> str | None:
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        m = re.search(r"gateway:\s*(\S+)", out)
        return m.group(1) if m else None
    except (subprocess.SubprocessError, OSError):
        return None


def _gateway_windows() -> str | None:
    try:
        out = subprocess.run(
            ["ipconfig"], capture_output=True, text=True, timeout=2,
        ).stdout
        for line in out.splitlines():
            if "Default Gateway" in line:
                m = re.search(r":\s*([\d.]+)\s*$", line)
                if m and m.group(1) != "0.0.0.0":
                    return m.group(1)
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def _tcp_probe_latency_ms(host: str, port: int, timeout: float) -> float | None:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.perf_counter() - start) * 1000, 1)
    except OSError:
        return None


def collect_network_latency() -> dict:
    """
    Escada gateway → DNS → internet (seção 7). Usa conexões TCP curtas em vez
    de ICMP para não exigir privilégios de root.
    """
    gateway = _default_gateway()
    gw_latency = _tcp_probe_latency_ms(gateway, 80, PROBE_TIMEOUT) if gateway else None
    dns_latency = _tcp_probe_latency_ms(DNS_PROBE_HOST, PROBE_PORT, PROBE_TIMEOUT)
    internet_latency = _tcp_probe_latency_ms(INTERNET_PROBE_HOST, 443, PROBE_TIMEOUT)
    return {
        "gateway_host": gateway,
        "gateway_latency_ms": gw_latency,
        "dns_latency_ms": dns_latency,
        "internet_latency_ms": internet_latency,
    }
