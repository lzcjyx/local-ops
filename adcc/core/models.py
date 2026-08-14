"""Structural type aliases for legacy-compatible ADCC state.

M1 intentionally models only records that already exist. Workspace, Project,
ResourceDefinition, and ManagedRun are introduced by later milestones.
"""

from typing import Any, Dict, List, Optional, Tuple, TypedDict


class ProcessInfo(TypedDict, total=False):
    uid: int
    comm: str
    args: str
    cpu: float
    mem: float
    etime: int


ProcessSnapshot = Dict[int, ProcessInfo]
ListenerSnapshot = Dict[Tuple[int, int], set[str]]
ProcessGroups = Dict[int, List[int]]
OriginTable = Dict[int, Tuple[int, str]]


class LastExit(TypedDict, total=False):
    status: str
    code: Optional[int]
    at: float
    startedAt: float
    durationSec: float


JsonObject = Dict[str, Any]

