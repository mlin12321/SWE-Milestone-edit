"""Agent-director framework: a whole org of agents works through the task queue.

The director's server cannot reach into a container, so it runs inside this
one, like every other agent here: the init script installs it from the mounted
software-director-sdk checkout, and the run command starts `director-bench
queue`. That driver gives each session of the org the milestones that are
waiting, and creates the `agent-impl-<milestone>` tags itself once the
session's work was reviewed, accepted and merged into /testbed.

What it writes lands in the trial's e2e_workspace/director/ on the host:
result.json, team_usage.json (whether and how the org used its teams),
driver.log, server.log and the server's state with every agent's events.

Set up from the environment of the process that launches the trial:

    DIRECTOR_SDK          software-director-sdk checkout (default: beside this repo)
    DIRECTOR_BENCH        director-bench checkout (default: beside this repo)
    DIRECTOR_MAX_DEPTH    most levels of teams; 1 forbids sub-teams (a solo run)
    DIRECTOR_COST_LIMIT   dollars one trial may spend
    DIRECTOR_BUDGET_FILE, DIRECTOR_BUDGET_USD   a cap shared by every trial
    DIRECTOR_TIMEOUT      seconds the driver gives one invocation (default 17000)
    DIRECTOR_EXTRA_ARGS   further `director-bench` arguments
"""

import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_OUT = "/e2e_workspace/director"
_HOME = "/opt/director"
_BUDGET_DIR = "/director-budget"


@register_framework("director")
class DirectorFramework(AgentFramework):
    FRAMEWORK_NAME = "director"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs,
    ):
        env = os.environ.get
        self._api_key = api_key or env("UNIFIED_API_KEY") or env("ANTHROPIC_API_KEY")
        self._base_url = base_url or env("UNIFIED_BASE_URL")
        self._model = model
        self._sdk = Path(env("DIRECTOR_SDK") or _PROJECT_ROOT.parent / "software-director-sdk")
        self._bench = Path(env("DIRECTOR_BENCH") or _PROJECT_ROOT.parent / "director-bench")
        self._budget_file = env("DIRECTOR_BUDGET_FILE")

    def get_container_mounts(self) -> List[str]:
        mounts = [
            "-v", f"{self._sdk.resolve()}:{_HOME}-src/sdk:ro",
            "-v", f"{self._bench.resolve()}:{_HOME}-src/bench:ro",
        ]  # fmt: skip
        if self._budget_file:
            # The ledger is shared with every other trial, so it lives on the host.
            folder = Path(self._budget_file).resolve().parent
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o777)
            mounts += ["-v", f"{folder}:{_BUDGET_DIR}"]
        return mounts + self.get_quarantine_mounts()

    def get_container_env_vars(self) -> List[str]:
        env_vars = []
        if self._api_key:
            env_vars += ["-e", f"DIRECTOR_API_KEY={self._api_key}"]
        if self._base_url:
            env_vars += ["-e", f"DIRECTOR_BASE_URL={self._base_url}"]
        return env_vars + self.get_quarantine_env_vars()

    def get_network_endpoint_url(self) -> Optional[str]:
        return self._base_url or "https://api.anthropic.com"

    def _sdk_commit(self) -> str:
        done = subprocess.run(
            ["git", "-C", str(self._sdk), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        return done.stdout.strip()

    def get_container_init_script(self, agent_name: str) -> str:
        return f"""
# === Agent-director: install the server from the mounted checkout ===
try:
    import os
    import pwd
    import shutil
    import subprocess

    home = {_HOME!r}
    src = home + '-src'

    def run(cmd, env=None, cwd=None):
        full = os.environ.copy()
        full.update(env or {{}})
        done = subprocess.run(cmd, capture_output=True, text=True, env=full, cwd=cwd, timeout=1800)
        if done.returncode:
            print(f"{{' '.join(cmd)}} failed: {{(done.stderr or done.stdout)[-800:]}}")
        return done.returncode == 0

    if os.path.exists(home + '/sdk/.venv/bin/agent-server'):
        print("Director already installed")
    else:
        os.environ['PATH'] = '/root/.local/bin:' + os.environ.get('PATH', '')
        if not shutil.which('uv'):
            print("Installing uv...")
            if not run(['pip3', 'install', '--break-system-packages', 'uv']):
                run(['apt-get', 'update'])
                run(['apt-get', 'install', '-y', 'curl', 'ca-certificates'])
                subprocess.run('curl -LsSf https://astral.sh/uv/install.sh | sh', shell=True, capture_output=True)
            if not shutil.which('uv'):
                raise Exception("uv installation failed")
        skip = shutil.ignore_patterns(
            '.venv', '.git', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache',
            '.ruff_cache',
        )
        for name in ('sdk', 'bench'):
            shutil.rmtree(f'{{home}}/{{name}}', ignore_errors=True)
            shutil.copytree(f'{{src}}/{{name}}', f'{{home}}/{{name}}', ignore=skip, symlinks=True)
        with open(home + '/sdk/.director-bench-commit', 'w') as marker:
            marker.write({self._sdk_commit()!r})
        # Everything under one folder the agent user can read.
        env = {{
            'UV_PYTHON_INSTALL_DIR': home + '/python',
            'UV_CACHE_DIR': home + '/uv-cache',
            'UV_LINK_MODE': 'copy',
        }}
        print("Installing the director's environment (a few minutes)...")
        if not run(['uv', 'sync', '--frozen', '--no-dev', '--python', '3.13'], env=env, cwd=home + '/sdk'):
            raise Exception("uv sync of the director SDK failed")
        if not os.path.exists(home + '/sdk/.venv/bin/agent-server'):
            raise Exception("the director SDK was installed without agent-server")
        shutil.rmtree(home + '/uv-cache', ignore_errors=True)

    try:
        user = pwd.getpwnam('fakeroot')
        uid, gid = user.pw_uid, user.pw_gid
    except KeyError:
        uid, gid = 1000, 1000
    os.makedirs({_OUT!r}, exist_ok=True)
    for top in (home, {_OUT!r}):
        subprocess.run(['chown', '-R', f'{{uid}}:{{gid}}', top], capture_output=True)
    print("Director ready")
except SystemExit:
    pass
except Exception as e:
    print(f"Director setup failed: {{e}}")
    raise
"""

    def _normalize_model(self, model: str) -> str:
        """A LiteLLM model name: as given when it names a provider, through
        the proxy when a base URL is set, else Anthropic's own API."""
        if "/" in model:
            return model
        return f"litellm_proxy/{model}" if self._base_url else f"anthropic/{model}"

    def _command(self, model: str, rules: str, resume: bool) -> str:
        env = os.environ.get
        args = [
            "queue",
            "--queue", "/e2e_workspace/TASK_QUEUE.md",
            "--repo", "/testbed",
            "--prompt-file", rules,
            "--out", _OUT,
            "--sdk", f"{_HOME}/sdk",
            "--model", self._normalize_model(model),
            "--api-key-env", "DIRECTOR_API_KEY",
            "--timeout", env("DIRECTOR_TIMEOUT") or "17000",
        ]  # fmt: skip
        if env("DIRECTOR_MAX_DEPTH"):
            args += ["--max-depth", env("DIRECTOR_MAX_DEPTH")]
        if env("DIRECTOR_COST_LIMIT"):
            args += ["--cost-limit", env("DIRECTOR_COST_LIMIT")]
        if self._budget_file and env("DIRECTOR_BUDGET_USD"):
            ledger = f"{_BUDGET_DIR}/{Path(self._budget_file).name}"
            args += ["--budget-file", ledger, "--budget-usd", env("DIRECTOR_BUDGET_USD")]
        if resume:
            args.append("--resume")
        args += shlex.split(env("DIRECTOR_EXTRA_ARGS") or "")
        python = f"{_HOME}/sdk/.venv/bin/python"
        return (
            # The org merges accepted work into the branch that is checked out.
            "(git -C /testbed symbolic-ref -q HEAD >/dev/null"
            " || git -C /testbed checkout -q -B director-bench); "
            f"PYTHONPATH={_HOME}/bench {python} -m director_bench "
            + " ".join(shlex.quote(arg) for arg in args)
        )

    def build_run_command(self, model: str, session_id: str, prompt_path: str) -> str:
        # Kept for the resumes: they are handed a reminder, not the instructions.
        rules = f"{_OUT}/rules.md"
        return f"mkdir -p {_OUT} && cp {shlex.quote(prompt_path)} {rules} && " + self._command(
            model, rules, resume=False
        )

    def build_resume_command(self, model: str, session_id: str, message_path: str) -> str:
        return self._command(model, f"{_OUT}/rules.md", resume=True)
