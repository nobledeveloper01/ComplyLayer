"""Phase 0's own gates: the guards must work before there is anything to guard.

A gate nobody has watched fail is not a gate. Each test here drives its script
into the failing case as well as the passing one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import no_eval_guard
import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "no_eval_guard.py"
README_CHECK = ROOT / "scripts" / "check-readme-phase.sh"


def run(script: Path, *args: str) -> subprocess.CompletedProcess:
    command = [sys.executable, str(script)] if script.suffix == ".py" else [str(script)]
    return subprocess.run([*command, *args], capture_output=True, text=True, check=False, cwd=ROOT)


class TestNoEvalGuard:
    def test_passes_on_the_real_package(self):
        result = run(GUARD)
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "source",
        [
            "result = eval(rule.expression, {}, facts)",
            "exec(compile(src, '<rule>', 'exec'))",
            "value = eval (expr)",
            "if flag:\n    eval(payload)",
            "outcome = [eval(e) for e in exprs]",
        ],
    )
    def test_catches_forbidden_calls(self, source: str):
        assert no_eval_guard.scan_source(source), f"missed: {source!r}"

    def test_reports_the_file_and_the_reason(self, tmp_path: Path):
        (tmp_path / "sneaky.py").write_text("result = eval(rule.expression, {}, facts)\n")
        result = run(GUARD, str(tmp_path))
        assert result.returncode == 1
        assert "forbidden call found" in result.stderr
        assert "adr/0001" in result.stderr

    @pytest.mark.parametrize(
        "source",
        [
            # The reason the guard tokenises rather than greps: the DSL validator
            # documents why eval is forbidden, and its own rationale must not
            # fail the build.
            "# never call eval( here\n",
            '"""Never use eval( in this module."""\n',
            "MESSAGE = 'do not call eval(expr)'\n",
            "import ast\nvalue = ast.literal_eval(text)",
            "self.evaluate(node)",
            "runner.exec_command(cmd)",
            "interpreter.eval(node)",
        ],
    )
    def test_does_not_fire_on_lookalikes(self, source: str):
        """A guard that cries wolf gets disabled, which is the failure it exists to prevent."""
        assert no_eval_guard.scan_source(source) == [], f"false positive: {source!r}"

    def test_unparseable_python_is_left_to_the_linter(self):
        assert no_eval_guard.scan_source("def broken(:\n") == []


class TestReadmePhaseCheck:
    def test_passes_in_the_current_state(self):
        result = run(README_CHECK)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_readme_declares_the_same_phase_as_the_phase_file(self):
        declared = (ROOT / "README.md").read_text()
        current = (ROOT / "PHASE").read_text().strip()
        assert f"<!-- phase: {current} -->" in declared
