import subprocess
from unittest import mock


def finished(returncode=0, stderr=""):
    proc = mock.MagicMock(returncode=returncode)
    proc.__enter__.return_value = proc
    proc.communicate.return_value = (None, stderr)
    return proc


def answered(run, *starts):
    """Popen side effect: `starts` (else every argv) answer as a subprocess.run fake."""
    real = subprocess.Popen

    def popen(argv, **options):
        if starts and tuple(argv) not in starts:
            return real(argv, **options)
        proc, outcome = finished(), []

        def communicate(timeout):
            if not outcome:
                try:
                    with mock.patch("subprocess.Popen", real):
                        outcome.append(run(argv, timeout=timeout, **options))
                except (OSError, subprocess.SubprocessError) as error:
                    outcome.append(error)
            if isinstance(outcome[0], BaseException):
                raise outcome[0]
            proc.returncode = outcome[0].returncode
            return None, outcome[0].stderr

        proc.communicate.side_effect = communicate
        return proc

    return popen
