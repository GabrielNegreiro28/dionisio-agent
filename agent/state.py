from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Literal
import time
import uuid


@dataclass
class StepResult:
    step_id: int
    tool: str
    params: dict
    result: Any
    success: bool
    error: Optional[str] = None
    kind: str = "api"  # api | compute | verify | fanout
    timestamp: float = field(default_factory=time.time)


@dataclass
class LogEntry:
    timestamp: float
    event: str
    data: dict = field(default_factory=dict)


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    original_query: str = ""
    rewritten_query: str = ""
    domains: list[str] = field(default_factory=list)
    selected_tools: list[str] = field(default_factory=list)
    plan: Optional[dict] = None
    step_results: list[StepResult] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    replan_count: int = 0
    # UI hook: called with a short PT-BR message as each stage/step starts, so
    # the operator sees activity during a multi-second run. None = silent.
    on_progress: Optional[Callable[[str], None]] = None
    # Step ids of 'find' computes whose output feeds a destructive write —
    # set by the executor; an ambiguous match there must ask, not guess.
    ambiguity_sensitive_ids: set = field(default_factory=set)

    def log(self, event: str, data: dict = None) -> None:
        self.logs.append(LogEntry(
            timestamp=time.time(),
            event=event,
            data=data or {}
        ))

    def add_step_result(self, result: StepResult) -> None:
        self.step_results = [r for r in self.step_results if r.step_id != result.step_id]
        self.step_results.append(result)

    def get_step_result(self, step_id: int) -> Optional[StepResult]:
        for r in self.step_results:
            if r.step_id == step_id:
                return r
        return None

    def has_successful_result(self, step_id: int) -> bool:
        r = self.get_step_result(step_id)
        return r is not None and r.success

    def get_results_as_context(self) -> dict:
        """Returns step results as a dict for reference resolution and replanning."""
        return {
            f"step{r.step_id}": r.result
            for r in self.step_results
            if r.success
        }
