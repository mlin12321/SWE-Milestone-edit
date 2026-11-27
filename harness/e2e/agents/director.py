"""Agent-director framework: a whole org of agents works through the task queue.

The director's server cannot reach into a container, so it runs inside this
one, like every other agent here: the init script installs it from the mounted
software-director-sdk checkout, and the run command starts `director-eval
queue`. That driver gives each session of the org the milestones that are
waiting, and creates the `agent-impl-<milestone>` tags itself once the
session's work was reviewed, accepted and merged into /testbed.

What it writes lands in the trial's e2e_workspace/director/ on the host:
result.json, team_usage.json (whether and how the org used its teams),
driver.log, server.log and the server's state with every agent's events.

Set up from the environment of the process that launches the trial:

    DIRECTOR_SDK          software-director-sdk checkout (default: beside this repo)
    DIRECTOR_EVAL_DRIVER        director-eval checkout (default: beside this repo)
    DIRECTOR_MAX_DEPTH    most levels of teams; 1 forbids sub-teams (a solo run)
    DIRECTOR_COST_LIMIT   dollars one trial may spend
    DIRECTOR_BUDGET_FILE, DIRECTOR_BUDGET_USD   a cap shared by every trial
    DIRECTOR_TIMEOUT      seconds the driver gives one invocation (default 17000)
    DIRECTOR_AGENT_JSON   file with a whole agent to use instead of a model, e.g.
                          Cursor over ACP. Its command is mounted from the host.
    DIRECTOR_AGENT_HOME_FILES  files of your home folder that agent needs in the
                          container (its login), comma separated, relative to ~
    DIRECTOR_EXTRA_ARGS   further `director-eval` arguments
"""

import json
import os
import shlex
import shutil
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
        self._bench = Path(env("DIRECTOR_EVAL_DRIVER") or _PROJECT_ROOT.parent / "DirectorEvalDriver")
        self._budget_file = env("DIRECTOR_BUDGET_FILE")
        self._agent_json = Path(env("DIRECTOR_AGENT_JSON")).resolve() if env("DIRECTOR_AGENT_JSON") else None
        self._home_files = [f.strip() for f in (env("DIRECTOR_AGENT_HOME_FILES") or "").split(",") if f.strip()]

    def _agent_cli(self) -> Optional[Path]:
        """The host's program behind the agent's command, if it has one."""
        if not self._agent_json:
            return None
        command = json.loads(self._agent_json.read_text()).get("acp_command") or []
        found = shutil.which(command[0]) if command else None
        if command and not found:
            raise RuntimeError(f"{command[0]} (from {self._agent_json}) is not installed on this machine")
        return Path(found).resolve() if found else None

    def _python(self) -> Path:
        """The standalone Python the SDK checkout's own environment runs on.

        It is mounted too: uv would fetch one from GitHub, which task
        containers cannot reach.
        """
        for line in (self._sdk / ".venv" / "pyvenv.cfg").read_text().splitlines():
            if line.startswith("home"):
                return Path(line.split("=", 1)[1].strip()).resolve().parent
        raise RuntimeError(f"{self._sdk} has no synced environment; run `uv sync` in it")

    def get_container_mounts(self) -> List[str]:
        mounts = [
            "-v", f"{self._sdk.resolve()}:{_HOME}-src/sdk:ro",
            "-v", f"{self._bench.resolve()}:{_HOME}-src/bench:ro",
            "-v", f"{self._python()}:{_HOME}-src/python:ro",
        ]  # fmt: skip
        if self._budget_file:
            # The ledger is shared with every other trial, so it lives on the host.
            folder = Path(self._budget_file).resolve().parent
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o777)
            mounts += ["-v", f"{folder}:{_BUDGET_DIR}"]
        if self._agent_json:
            mounts += ["-v", f"{self._agent_json}:{_HOME}-src/agent.json:ro"]
            cli = self._agent_cli()
            if cli:
                # The whole folder: such a program ships with its own runtime.
                mounts += ["-v", f"{cli.parent}:{_HOME}-src/agent-cli:ro"]
        for name in self._home_files:
            mounts += ["-v", f"{Path.home() / name}:{_HOME}-src/home/{name}:ro"]
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
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, env=full, cwd=cwd, timeout=1800)
        except OSError as missing:
            print(f"{{cmd[0]}}: {{missing}}")
            return False
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
                # No pip in the image: take the binary out of uv's wheel on PyPI.
                import io, json, platform, urllib.request, zipfile
                arch = platform.machine()
                with urllib.request.urlopen('https://pypi.org/pypi/uv/json', timeout=60) as listing:
                    files = json.load(listing)['urls']
                wheel = next(
                    f['url'] for f in files
                    if f['filename'].endswith('.whl') and 'manylinux' in f['filename']
                    and arch in f['filename']
                )
                with urllib.request.urlopen(wheel, timeout=300) as download:
                    archive = zipfile.ZipFile(io.BytesIO(download.read()))
                name = next(n for n in archive.namelist() if n.endswith('/scripts/uv'))
                with open('/usr/local/bin/uv', 'wb') as binary:
                    binary.write(archive.read(name))
                os.chmod('/usr/local/bin/uv', 0o755)
            if not shutil.which('uv'):
                raise Exception("uv installation failed")
        skip = shutil.ignore_patterns(
            '.venv', '.git', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache',
            '.ruff_cache',
        )
        for name in ('sdk', 'bench', 'python'):
            shutil.rmtree(f'{{home}}/{{name}}', ignore_errors=True)
            shutil.copytree(f'{{src}}/{{name}}', f'{{home}}/{{name}}', ignore=skip, symlinks=True)
        with open(home + '/sdk/.director-eval-commit', 'w') as marker:
            marker.write({self._sdk_commit()!r})
        # Everything under one folder the agent user can read.
        env = {{
            'UV_PYTHON_DOWNLOADS': 'never',
            'UV_CACHE_DIR': home + '/uv-cache',
            'UV_LINK_MODE': 'copy',
        }}
        python = next(
            f'{{home}}/python/bin/{{name}}' for name in sorted(os.listdir(home + '/python/bin'))
            if name.startswith('python3.')and name[9:].isdigit()
        )
        print("Installing the director's environment (a few minutes)...")
        if not run(['uv', 'sync', '--frozen', '--no-dev', '--python', python], env=env, cwd=home + '/sdk'):
            raise Exception("uv sync of the director SDK failed")
        if not os.path.exists(home + '/sdk/.venv/bin/agent-server'):
            raise Exception("the director SDK was installed without agent-server")
        shutil.rmtree(home + '/uv-cache', ignore_errors=True)

    # The director needs git 2.36 or later (`git worktree list -z`); Ubuntu
    # 22.04 has 2.34. The newer one comes from the git maintainers' archive.
    version = subprocess.run(['git', '--version'], capture_output=True, text=True).stdout.split()
    numbers = tuple(int(n) for n in version[2].split('.')[:2]) if len(version) > 2 else (0, 0)
    if numbers < (2, 36) and os.path.exists('/etc/os-release'):
        release = dict(
            line.strip().split('=', 1) for line in open('/etc/os-release') if '=' in line
        )
        if release.get('ID', '').strip('"') == 'ubuntu':
            print(f"git {{version[2]}} is too old for the director: installing a newer one")
            with open('/etc/apt/sources.list.d/git-core.list', 'w') as source:
                source.write(
                    'deb [trusted=yes] https://ppa.launchpadcontent.net/git-core/ppa/ubuntu '
                    + release['VERSION_CODENAME'].strip('"') + ' main\n'
                )
            run(['apt-get', 'update', '-qq', '-o', 'Dir::Etc::sourcelist=sources.list.d/git-core.list',
                 '-o', 'Dir::Etc::sourceparts=-'])
            if not run(['apt-get', 'install', '-y', '-qq', 'git'], env={{'DEBIAN_FRONTEND': 'noninteractive'}}):
                raise Exception("could not install a git new enough for the director")
        else:
            raise Exception(f"git {{version[2]}} is too old for the director (2.36 needed)")

    try:
        user = pwd.getpwnam('fakeroot')
        uid, gid = user.pw_uid, user.pw_gid
    except KeyError:
        uid, gid = 1000, 1000
    os.makedirs({_OUT!r}, exist_ok=True)
    for top in (home, {_OUT!r}):
        subprocess.run(['chown', '-R', f'{{uid}}:{{gid}}', top], capture_output=True)
    cli = {self._agent_cli_name()!r}
    if cli:
        # The agent's own program, under the name its command uses.
        link = '/usr/local/bin/' + cli[0]
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(src + '/agent-cli/' + cli[1], link)
    user_home = '/home/fakeroot'
    for name in {self._home_files!r}:
        target = os.path.join(user_home, name)
        folder = os.path.dirname(target)
        os.makedirs(folder, exist_ok=True)
        shutil.copyfile(src + '/home/' + name, target)
        os.chmod(target, 0o600)
        while len(folder) > len(user_home):
            os.chown(folder, uid, gid)
            folder = os.path.dirname(folder)
        os.chown(target, uid, gid)
    print("Director ready")
except SystemExit:
    pass
except Exception as e:
    print(f"Director setup failed: {{e}}")
    raise
"""

    def _agent_cli_name(self) -> Optional[tuple]:
        """(the command's name, the program's file name), for the link in the container."""
        cli = self._agent_cli()
        if not cli:
            return None
        return (json.loads(self._agent_json.read_text())["acp_command"][0], cli.name)

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
        if self._agent_json:
            args += ["--agent-json", f"{_HOME}-src/agent.json"]
        if resume:
            args.append("--resume")
        args += shlex.split(env("DIRECTOR_EXTRA_ARGS") or "")
        python = f"{_HOME}/sdk/.venv/bin/python"
        return (
            # The org merges accepted work into the branch that is checked out.
            "(git -C /testbed symbolic-ref -q HEAD >/dev/null"
            " || git -C /testbed checkout -q -B director-eval); "
            f"PYTHONPATH={_HOME}/bench {python} -m director_eval_driver "
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
