"""No-op framework: submits every waiting milestone unchanged, and spends nothing.

It calls no model. A trial with it exercises the data checkout, the images,
the tag watcher and the evaluator end to end, which is how to check a setup
before paying for a real agent. Its scores are those of the untouched tree.
"""

from typing import List

from harness.e2e.agents.base import AgentFramework, register_framework

_SUBMIT_WAITING = (
    "for m in $(sed -n 's/^- \\([^:]*\\): See SRS.*/\\1/p' /e2e_workspace/TASK_QUEUE.md); do "
    "git -C /testbed tag agent-impl-$m 2>/dev/null && echo \"noop: submitted $m\"; "
    "done; echo 'noop: done'"
)


@register_framework("noop")
class NoopFramework(AgentFramework):
    FRAMEWORK_NAME = "noop"

    def __init__(self, **kwargs):
        pass

    def get_container_mounts(self) -> List[str]:
        return self.get_quarantine_mounts()

    def get_container_init_script(self, agent_name: str) -> str:
        return "print('noop agent: nothing to install')\n"

    def build_run_command(self, model: str, session_id: str, prompt_path: str) -> str:
        return _SUBMIT_WAITING

    def build_resume_command(self, model: str, session_id: str, message_path: str) -> str:
        return _SUBMIT_WAITING
