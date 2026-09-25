import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields

INTEGER_MAXIMUMS = {
    "port": 65535,
    "max_dimension": 16384,
    "max_concurrent_jobs": 64,
    "process_timeout_seconds": 60,
    "cache_ttl_seconds": 86400,
}
DEFAULT_INTEGER_MAXIMUM = 2**31 - 1


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"  # noqa: S104 - the container must accept outside connections
    port: int = 3000
    log_level: str = "info"
    max_source_bytes: int = 10 * 1024 * 1024
    max_output_bytes: int = 10 * 1024 * 1024
    max_input_pixels: int = 40_000_000
    max_output_pixels: int = 16_000_000
    max_dimension: int = 4096
    fetch_timeout_ms: int = 10_000
    process_timeout_seconds: int = 5
    max_concurrent_jobs: int = 2
    cache_max_bytes: int = 64 * 1024 * 1024
    cache_max_entries: int = 256
    cache_ttl_seconds: int = 60
    allowed_source_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        defaults = cls()
        values = asdict(defaults)
        for field in fields(cls):
            default = getattr(defaults, field.name)
            raw = env.get(field.name.upper())
            if raw is None:
                continue
            if isinstance(default, int):
                values[field.name] = _parse_integer(field.name, raw)
            elif field.name == "allowed_source_hosts":
                values[field.name] = _parse_allowed_hosts(raw)
            else:
                values[field.name] = raw
        config = cls(**values)
        if config.log_level not in {"critical", "error", "warning", "info", "debug", "silent"}:
            raise ValueError("LOG_LEVEL is invalid")
        return config


def _parse_integer(name: str, raw: str) -> int:
    minimum = 0 if name.startswith("cache_") else 1
    maximum = INTEGER_MAXIMUMS.get(name, DEFAULT_INTEGER_MAXIMUM)
    if not re.fullmatch(r"[0-9]+", raw) or not minimum <= int(raw) <= maximum:
        raise ValueError(f"{name.upper()} must be between {minimum} and {maximum}")
    return int(raw)


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    hosts = tuple(host.strip().lower() for host in raw.split(",") if host.strip())
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) for host in hosts):
        raise ValueError("ALLOWED_SOURCE_HOSTS must contain exact hostnames")
    return hosts
