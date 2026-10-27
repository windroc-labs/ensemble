from tests.guard_checks.removed_interfaces import removed_interfaces
from tests.guard_samples import Sample

BASE = "src/ensemble/session/hosts/base.py"


class RemovedInterfaces(Sample):
    def found(self, files):
        return removed_interfaces(self.tree({BASE: "class Host: ...\n", **files}))

    def test_a_removed_class_or_function_defined_or_imported_is_found(self):
        self.assertEqual(
            self.found(
                {
                    BASE: "class Host: ...\nclass Process: ...\ndef check_sid(): ...\n",
                    "src/ensemble/session/x.py": "from ensemble.session.hosts.base import Interactive\n",
                }
            ),
            [
                f"{BASE}:2: defines Process",
                f"{BASE}:3: defines check_sid",
                "src/ensemble/session/x.py:1: imports Interactive",
            ],
        )

    def test_a_host_or_handle_subclass_defining_a_removed_member_is_found(self):
        source = (
            "from ensemble.session.hosts.base import Host, InteractiveProcess\n"
            "class Mid(Host): ...\n"
            "class Leaf(Mid):\n    def attach(self, name): ...\n"
            "class Handle(InteractiveProcess):\n    def alive(self): ...\n"
            "class Other:\n    def attach(self, name): ...\n"
        )
        self.assertEqual(
            self.found({"src/ensemble/session/hosts/leaf.py": source}),
            [
                "src/ensemble/session/hosts/leaf.py:4: Leaf defines removed Host member attach",
                "src/ensemble/session/hosts/leaf.py:6: Handle defines removed "
                "InteractiveProcess member alive",
            ],
        )

    def test_a_removed_call_on_a_host_is_found_others_are_not(self):
        source = (
            "host.attach('w')\nself._host.detach('w', launch)\ncls.host(n).events_path(e)\n"
            "Session.attach('w')\nrun.hook_callback()\nself._controls.attach(path)\n"
        )
        self.assertEqual(
            self.found({"src/ensemble/delegate/y.py": source}),
            [
                "src/ensemble/delegate/y.py:1: calls host.attach",
                "src/ensemble/delegate/y.py:2: calls self._host.detach",
                "src/ensemble/delegate/y.py:3: calls cls.host(n).events_path",
            ],
        )

    def test_tests_and_go_code_are_out(self):
        self.assertEqual(
            self.found(
                {
                    "tests/session/fake.py": "class Process: ...\nhost.attach('w')\n",
                    "src/broker/main.go": "cmd.Process.Pid\n",
                }
            ),
            [],
        )
