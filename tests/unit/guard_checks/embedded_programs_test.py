import ast
import gc
import hashlib
from pathlib import Path

from tests.case import REPO
from tests.guard_checks.embedded_programs import embedded_programs
from tests.guard_data.session import EMBEDDED_PROGRAMS_ALLOWED as SESSION_ALLOWED
from tests.guard_samples import Sample

PATH = "tests/delegate/example.py"
HOST = "src/ensemble/session/hosts/docker/host.py"


class EmbeddedPrograms(Sample):
    def checked(self, source, allowed=None):
        return embedded_programs(self.tree({PATH: source}), allowed=allowed or {})

    def test_interpreters_and_subprocess_list_tuple_and_keyword_arguments(self):
        for interpreter in ("python", "python3", "/venv/bin/python3.13", "sh", "/bin/bash"):
            for argv in (
                "[{interpreter!r}, '-c', 'print(1)']",
                "({interpreter!r}, '-c', 'print(1)')",
            ):
                for call in ("subprocess.run({argv})", "run(args={argv})"):
                    with self.subTest(interpreter=interpreter, argv=argv, call=call):
                        source = call.format(argv=argv.format(interpreter=interpreter))
                        (found,) = self.checked(source)
                        self.assertIn("interpreter -c", found)
                        self.assertIn("own file, passed by path or stdin", found)

    def test_old_worker_named_multiline_program_through_docker_wrapper(self):
        source = (
            "PYTHON = '/opt/ensemble/venv/bin/python'\n"
            "TOOL_PROBE = 'import json\\nprint(json.dumps({}))\\n'\n"
            "def docker(*args):\n    return subprocess.run(['docker', *args])\n"
            "docker('exec', name, PYTHON, '-I', '-c', TOOL_PROBE)\n"
        )
        self.assertTrue(self.checked(source))

    def docker_program(self, program, *, execute=True):
        source = (
            "from ensemble.session.hosts.docker.daemon import docker_call as build\n"
            "IMAGE_PYTHON = '/opt/ensemble/venv/bin/python'\n"
            f"PROMPT_STAGE = {program!r}\n"
            "class Worker:\n"
            "    def stage(self):\n"
            "        argv, env = build(self.docker,\n"
            "            *('exec', '-i', '--user', 'agent', container),\n"
            "            *(IMAGE_PYTHON, '-I', '-c', PROMPT_STAGE, path))\n"
        )
        return source + ("        self.run(argv, env)\n" if execute else "")

    def test_docker_builder_tuple_result_reaches_self_run_but_inert_data_does_not(self):
        source = self.docker_program("print('static fixture')")
        (found,) = self.checked(source)
        self.assertIn("interpreter -c", found)
        self.assertEqual(
            self.checked(self.docker_program("print('static fixture')", execute=False)), []
        )

    def test_real_prompt_stage_has_one_exact_exception_and_changed_body_is_rejected(self):
        source = Path(REPO, HOST).read_text()
        program = next(
            ast.literal_eval(node.value)
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "PROMPT_STAGE"
                for target in node.targets
            )
        )
        allowed = {key: reason for key, reason in SESSION_ALLOWED.items() if key[0] == HOST}
        self.assertEqual(len(allowed), 1)
        self.assertIn((HOST, hashlib.sha256(program.encode()).hexdigest()), allowed)
        root = self.tree({HOST: source})
        self.assertTrue(embedded_programs(root, allowed={}))
        self.assertEqual(embedded_programs(root, allowed=allowed), [])
        changed = self.tree({HOST: self.docker_program("print('unregistered static fixture')")})
        found = embedded_programs(changed, allowed=allowed)
        self.assertTrue(any("embedded program (interpreter -c)" in line for line in found), found)
        self.assertTrue(any("stale embedded program exception" in line for line in found), found)

    def test_inert_command_lists_in_assertions_and_data_calls_are_allowed(self):
        for call in ("self.assertEqual(actual, ARGV)", "print(ARGV)", "record(ARGV)"):
            with self.subTest(call=call):
                self.assertEqual(self.checked("ARGV = ['python3', '-c', 'print(1)']\n" + call), [])

    def test_aliases_assigned_argv_concatenation_and_dedent(self):
        source = (
            "import sys as system\nfrom textwrap import dedent as clean\n"
            "PY = system.executable\nCODE = clean(' print(1)\\n') + 'print(2)'\n"
            "ALIAS = CODE\nOPTIONS = [PY, '-I', '-c', ALIAS]\n"
            "ARGS = OPTIONS\nsubprocess.run(ARGS)\n"
        )
        self.assertEqual(len(self.checked(source)), 1)

    def test_exec_direct_builtin_and_assigned_alias(self):
        for name in ("exec", "builtins.exec", "execute"):
            with self.subTest(name=name):
                source = (
                    f"import builtins\nexecute = builtins.exec\nCODE = 'print(1)'\n{name}(CODE)"
                )
                (found,) = self.checked(source)
                self.assertIn("exec()", found)

    def test_function_and_lambda_forwarding_keyword_and_varargs_wrappers(self):
        for definition, call in (
            (
                "def launch(code):\n    subprocess.run(['python3', '-c', code])",
                "launch('print(1)')",
            ),
            ("def launch(*args):\n    subprocess.run(args)", "launch('python3', '-c', 'print(1)')"),
            ("launch = lambda code: exec(code)", "launch(code='print(1)')"),
            ("def launch(code):\n    exec(code)\nalias = launch", "alias('print(1)')"),
        ):
            with self.subTest(definition=definition):
                self.assertTrue(self.checked(f"{definition}\n{call}"))

    def test_nested_scopes_keep_globals_and_closures_but_respect_shadowing(self):
        source = (
            "CODE = 'print(1)'\n"
            "def outer():\n    local = 'print(2)'\n"
            "    def inner():\n        exec(local)\n    return inner\n"
            "class Example:\n    CODE = 'print(3)'\n"
            "    def method(self):\n        exec(CODE)\n"
        )
        self.assertEqual(len(self.checked(source)), 2)
        self.assertEqual(self.checked("CODE = 'print(1)'\ndef run(CODE):\n    exec(CODE)"), [])

    def test_path_write_text_then_run_with_path_or_executable(self):
        for call in ("subprocess.run(['python3', str(script)])", "subprocess.run([script])"):
            with self.subTest(call=call):
                source = f"from pathlib import Path\nscript = Path('probe.py')\nscript.write_text('print(1)')\n{call}"
                (found,) = self.checked(source)
                self.assertIn("written to a file and run", found)

    def test_a_write_and_a_run_in_different_methods_never_match_after_collection(self):
        self.addCleanup(gc.set_threshold, *gc.get_threshold())
        gc.set_threshold(1)
        methods = "".join(
            f"    def test_write_{i}(self):\n        Path(self.home, 'state').write_text('x=1')\n"
            f"    def test_run_{i}(self):\n        subprocess.run([str(Path(self.home, 'bin'))])\n"
            for i in range(50)
        )
        self.assertEqual(self.checked(f"class Example:\n{methods}"), [])

    def test_open_write_then_interpreter_run_and_wrapped_write(self):
        for write in (
            "with open(path, 'w') as stream:\n    stream.write(CODE)",
            "stream = open(path, 'w')\nstream.write(CODE)",
            "def save(target, code):\n    target.write_text(code)\nsave(path, CODE)",
        ):
            with self.subTest(write=write):
                source = f"path = Path('probe.sh')\nCODE = 'echo sample'\n{write}\nsubprocess.run(['bash', path])"
                self.assertTrue(self.checked(source))

    def test_file_stdin_git_config_and_unexecuted_fixture_data_are_allowed(self):
        for source in (
            "subprocess.run(['git', '-c', 'user.name=Example'])",
            "subprocess.run(['python3', 'probe.py'])",
            "subprocess.run(['sh', '-s'], stdin=open('probe.sh'))",
            "subprocess.run(['python3', '-'], stdin=open('probe.py'))",
            "argv = ['python3', '-c', 'print(1)']",
            "sample = \"exec('print(1)')\"",
            "Path('fixture.txt').write_text('fixture contents')",
            "Path('other.py').write_text('print(1)')\nsubprocess.run(['python3', 'probe.py'])",
            "def exec(code): pass\nexec('print(1)')",
        ):
            with self.subTest(source=source):
                self.assertEqual(self.checked(source), [])

    def test_exact_path_and_literal_hash_exceptions_and_stale_entries(self):
        code = "print(1)"
        digest = hashlib.sha256(code.encode()).hexdigest()
        allowed = {(PATH, digest): "Interpreter isolation fixture."}
        self.assertEqual(self.checked(f"exec({code!r})", allowed), [])
        edited = self.checked("exec('print(2)')", allowed)
        self.assertEqual(len(edited), 2)
        self.assertTrue(any("stale embedded program exception" in line for line in edited))
        other = embedded_programs(
            self.tree({"tests/session/example.py": "exec('print(1)')"}), allowed=allowed
        )
        self.assertEqual(len(other), 2)

    def test_empty_and_multiline_exception_reasons_fail(self):
        digest = hashlib.sha256(b"print(1)").hexdigest()
        for reason in ("", " ", "fixture\nreason", None):
            with self.subTest(reason=reason):
                (found,) = self.checked("exec('print(1)')", {(PATH, digest): reason})
                self.assertIn("needs one-line reason", found)
