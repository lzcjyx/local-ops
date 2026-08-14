"""Platform adapters: OS facts and control primitives behind one interface."""

from adcc.platform.base import (
    PlatformAdapter,
    PlatformCapabilityError,
    PlatformUnsupportedError,
    ProcessControlError,
    get_platform_adapter,
    run_cmd,
)

__all__ = [
    "PlatformAdapter",
    "PlatformCapabilityError",
    "PlatformUnsupportedError",
    "ProcessControlError",
    "get_platform_adapter",
    "run_cmd",
]
