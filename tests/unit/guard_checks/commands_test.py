from tests.guard_checks import commands
from tests.guard_samples import PKG, Sample

OPKG = "src/ensemble/orchestrator"


class LayerCommands(Sample):
    SESSION, DELEGATE = (f"{PKG}/{layer}" for layer in ("session", "delegate"))
    LINEAR = f"{OPKG}"

    def test_a_layer_command_in_a_spawn_argv_and_only_there(self):
        for files, expected in (
            (
                {
                    f"{self.DELEGATE}/a.py": (
                        'import subprocess\nsubprocess.run(["ensemble-session", "x"])\nsubprocess.Popen(cwd="/", args=("x", "ensemble-role"))\n'
                    ),
                    f"{self.LINEAR}/b.py": 'import os\nfrom subprocess import check_output as out\nout("ensemble-issue run", shell=True)\nos.execvp("ensemble-x", ["ensemble-x"])\nos.spawnl(os.P_WAIT, "/bin/ensemble-y", f"ensemble-{n}")\n',
                    f"{self.SESSION}/c.py": 'import subprocess as sp\nsp.run(["ensemble-session"])\nsp.call(["ensemble-role"])\n',
                    f"{self.DELEGATE}/team/tools/d.py": (
                        'import subprocess\nsubprocess.run(["ensemble-x"])\n'
                    ),
                },
                sorted(
                    [
                        f'{self.DELEGATE}/a.py:2: runs "ensemble-session"',
                        f'{self.DELEGATE}/a.py:3: runs "ensemble-role"',
                        f'{self.DELEGATE}/team/tools/d.py:2: runs "ensemble-x"',
                        f'{self.SESSION}/c.py:3: runs "ensemble-role"',
                        f'{self.LINEAR}/b.py:3: runs "ensemble-issue"',
                        f'{self.LINEAR}/b.py:4: runs "ensemble-x"',
                        f'{self.LINEAR}/b.py:5: runs "ensemble-"',
                    ]
                ),
            ),
            (
                {
                    f"{self.DELEGATE}/a.py": (
                        'import subprocess\nNAME = "ensemble-progress"\nprint("ensemble-x")\nsubprocess.run(["tmux", NAME, "/ensemble-list"], env={"A": "ensemble-x"})\n'
                    ),
                    f"{PKG}/manager/crew.py": (
                        'import subprocess\nsubprocess.run(["ensemble-x"])\n'
                    ),
                    "tests/b.py": 'import subprocess\nsubprocess.run(["ensemble-x"])\n',
                },
                [],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(commands.layer_commands(self.tree(files)), expected)


class PythonCommands(Sample):
    def test_a_python_command_other_than_p_m_ensemble_or_a_team_tool(self):
        team = f"{PKG}/delegate/team"
        for files, expected in (
            (
                {
                    "src/ensemble/delegate/config.toml": 'commands = ["python3 x.py", "python3 -m ensemble.x"]\n# python3 -P {{team}}/tools/repo.py\ngate = "python3 -P {{team}}/x.py"\n',
                    f"{OPKG}/config.toml": 'gate = "python3 -P -m ensemblex.y"\n',
                    "skills/a/SKILL.md": "Run `python3.11 x.py`.\n- Bash(python -P -m ensemble.delegate.cli --list)\n",
                    "skills/b/SKILL.md": "Run `python3 y.py`.\n",
                    f"{team}/roles/r.md": (
                        "1. `python3 -P ensemble.x`\n2. /usr/bin/python3 -I -m ensemble.x\n3. `env PYTHONPATH=x python3 -P -m ensemble.orchestrator.engine.run`\n4. `python3 -P <team>/tools/repo.py status --dir x`\n5. `python3 -P <team>/tools/../repo.py`\n6. `python3 -P <team>/tools/repo.pyc`\n7. `python3 -P <role team>/tools/x.py`\n"
                    ),
                },
                [
                    'skills/a/SKILL.md:1: python command "python3.11 x.py"',
                    'src/ensemble/delegate/config.toml:1: python command "python3 -m ensemble.x"',
                    'src/ensemble/delegate/config.toml:1: python command "python3 x.py"',
                    'src/ensemble/delegate/config.toml:3: python command "python3 -P {{team}}/x.py"',
                    f'{team}/roles/r.md:1: python command "python3 -P ensemble.x"',
                    f'{team}/roles/r.md:2: python command "python3 -I -m ensemble.x"',
                    f'{team}/roles/r.md:5: python command "python3 -P <team>/tools/../repo.py"',
                    f'{team}/roles/r.md:6: python command "python3 -P <team>/tools/repo.pyc"',
                    'skills/b/SKILL.md:1: python command "python3 y.py"',
                    f'{OPKG}/config.toml:1: python command "python3 -P -m ensemblex.y"',
                ],
            ),
            (
                {
                    f"{team}/a.md": "pythonic, my_python3 x.py, Python 3.11\n",
                    "skills/a/notes.txt": "python3 x.py\n",
                    "delegate/README.md": "python3 x.py\n",
                    "orchestrator/other.toml": 'x = "python3 x.py"\n',
                    "tests/a.md": "python3 x.py\n",
                },
                [],
            ),
        ):
            with self.subTest(files=sorted(files)):
                self.assertEqual(commands.python_commands(self.tree(files)), sorted(expected))


class BinCommands(Sample):
    SCRIPT = '#!/bin/sh\nexport PYTHONPATH=x\nexec python3 -P -m ensemble.{0}.cli "$@"\n'
    SKILL = "---\nname: a\nallowed-tools:\n  - Read\n  - Bash({0})\n---\n\nRun `{0}`.\n"

    def shipped(self, files=()):
        return self.tree(
            {
                "bin/ensemble-delegate": self.SCRIPT.format("delegate"),
                f"{PKG}/delegate/cli.py": "",
                "skills/a/SKILL.md": self.SKILL.format("ensemble-delegate run --list"),
                f"{PKG}/delegate/config.toml": 'commands = ["python3 -P {{team}}/tools/repo.py *"]\n',
                f"{OPKG}/config.toml": '[core.roles.r]\ngate = "ensemble-delegate run --list"\n',
                **dict(files),
            }
        )

    def test_shipped_commands_name_bin_scripts_each_running_one_module_of_its_own(self):
        config = f"{OPKG}/config.toml"
        two = self.SCRIPT.format("delegate") + "exec python3 -P -m ensemble.session.cli\n"
        for name, files, expected in (
            ("shipped", {}, []),
            *(
                (
                    rule,
                    {"skills/b/SKILL.md": self.SKILL.format(rule)},
                    [f'skills/b/SKILL.md: pre-approves "{rule}", not a bin/ command'],
                )
                for rule in ("ensemble-nope run", "python3 -P -m ensemble.delegate.cli run --list")
            ),
            (
                "config",
                {
                    config: '[core.roles.r]\ngate = "ensemble-orchestrator router --brake r"\ncommands = ["ensemble-crew x *", "true"]\n'
                },
                [
                    f'{config}: runs "ensemble-crew x *", not a bin/ command',
                    f'{config}: runs "ensemble-orchestrator router --brake r", not a bin/ command',
                ],
            ),
            *(
                (want, {"bin/ensemble-x": script}, [want])
                for script, want in (
                    ("#!/bin/sh\nexec true\n", "bin/ensemble-x: runs 0 modules, not 1"),
                    (two, "bin/ensemble-x: runs 2 modules, not 1"),
                    (self.SCRIPT.format("nope"), "bin/ensemble-x: no module ensemble.nope.cli"),
                    (
                        self.SCRIPT.format("delegate"),
                        "bin/ensemble-x: runs ensemble.delegate.cli, as bin/ensemble-delegate does",
                    ),
                )
            ),
        ):
            with self.subTest(name):
                self.assertEqual(commands.bin_commands(self.shipped(files)), expected)
