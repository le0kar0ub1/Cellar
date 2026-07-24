"""Hermetic end-to-end tests for the cellar CLI.

These drive the real command implementations through `cli.main()`:

  * real subprocess boundary — pacman, pacman-conf, vercmp, and paru are
    stub executables on a private PATH;
  * real HTTP — the AUR RPC is a local http.server on 127.0.0.1; the
    only "network" is loopback;
  * real filesystem — config, state, and holds files live in a temp dir
    (via XDG_* and the `holds_file` config key).

No AUR, no pacman, and no root required, so the suite runs anywhere
Python does, including makepkg's check() in a clean chroot:

    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from cellar import aur, cli

DAY = 86400

# Stub executables. The vercmp stub implements a simplified comparison
# ([epoch:]dotted.numbers[-rel]); keep fixture versions numeric.
STUBS = {
    "pacman": """\
#!/usr/bin/env python3
import os, sys
if sys.argv[1:] == ["-Qm"]:
    warn = os.environ.get("CELLAR_TEST_QM_STDERR")
    if warn:
        print(warn, file=sys.stderr)
    out = ""
    path = os.environ.get("CELLAR_TEST_QM_FILE")
    if path and os.path.exists(path):
        out = open(path).read()
    if not out.strip():
        sys.exit(1)  # pacman -Qm: no foreign packages
    sys.stdout.write(out)
    sys.exit(0)
sys.exit(2)
""",
    "pacman-conf": """\
#!/usr/bin/env python3
import os, sys
if sys.argv[1:] == ["IgnorePkg"]:
    ignored = os.environ.get("CELLAR_TEST_IGNOREPKG", "").split()
    for name in ignored:
        print(name)
    sys.exit(0)
sys.exit(2)
""",
    "vercmp": """\
#!/usr/bin/env python3
import sys
def parse(v):
    epoch, sep, rest = v.partition(":")
    if not sep:
        epoch, rest = "0", v
    pkgver, _, pkgrel = rest.partition("-")
    return (int(epoch), [int(p) for p in pkgver.split(".")], int(pkgrel or 0))
a, b = parse(sys.argv[1]), parse(sys.argv[2])
print((a > b) - (a < b))
""",
    "paru": """\
#!/usr/bin/env python3
import os, sys
log = os.environ.get("CELLAR_TEST_HELPER_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("CELLAR_TEST_HELPER_EXIT", "0")))
""",
}


class _AurServer(ThreadingHTTPServer):
    daemon_threads = True
    packages: dict = {}
    requests: list = []
    mode: str = "ok"  # "ok" | "http500" | "aur-error"


class _AurHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        names = parse_qs(urlparse(self.path).query).get("arg[]", [])
        self.server.requests.append(names)
        if self.server.mode == "http500":
            self.send_error(500)
            return
        if self.server.mode == "aur-error":
            body = {"type": "error", "error": "Incorrect by request."}
        else:
            results = [self.server.packages[n] for n in names if n in self.server.packages]
            body = {"type": "multiinfo", "resultcount": len(results), "results": results}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # keep test output clean


class CliTest(unittest.TestCase):
    """Base: private PATH with stubs, local AUR server, temp XDG dirs."""

    @classmethod
    def setUpClass(cls):
        cls.server = _AurServer(("127.0.0.1", 0), _AurHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.packages = {}
        self.server.requests = []
        self.server.mode = "ok"

        self.tmp = Path(tempfile.mkdtemp(prefix="cellar-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        for name, body in STUBS.items():
            stub = self.bin_dir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.helper_log = self.tmp / "helper.log"
        self.holds_file = self.tmp / "cellar-holds.conf"
        self.state_file = self.tmp / "state" / "cellar" / "holds.json"

        env = {
            # Stubs shadow any real pacman/paru; the tail keeps python3
            # resolvable for the stubs' shebangs.
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.tmp / "home"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "CELLAR_TEST_QM_FILE": str(self.tmp / "qm.txt"),
            "CELLAR_TEST_HELPER_LOG": str(self.helper_log),
            "CELLAR_TEST_IGNOREPKG": "",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("CELLAR_CONFIG", "CELLAR_TEST_QM_STDERR", "CELLAR_TEST_HELPER_EXIT"):
            os.environ.pop(var, None)

        port = self.server.server_address[1]
        rpc = mock.patch.object(aur, "RPC_URL", f"http://127.0.0.1:{port}/rpc/v5/info")
        rpc.start()
        self.addCleanup(rpc.stop)

        # Default config pointing the holds file into the temp dir, so no
        # test ever looks at the real /etc/pacman.d.
        self.write_config("")

    # ------------------------------------------------------------ fixtures

    def run_cli(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def set_installed(self, packages: dict):
        lines = "".join(f"{name} {version}\n" for name, version in packages.items())
        Path(os.environ["CELLAR_TEST_QM_FILE"]).write_text(lines)

    def add_aur(self, name, version, age_days):
        self.server.packages[name] = {
            "Name": name,
            "Version": version,
            "LastModified": int(time.time() - age_days * DAY),
        }

    def write_config(self, text):
        path = Path(os.environ["XDG_CONFIG_HOME"]) / "cellar" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'holds_file = "{self.holds_file}"\n' + textwrap.dedent(text))
        return path


class CheckCommand(CliTest):
    def test_full_status_spread(self):
        self.set_installed(
            {
                "alpha": "1.0-1",
                "beta": "2.0-1",
                "gamma": "3.0-1",
                "local-only": "0.1-1",
                "pinned": "1.0-1",
            }
        )
        self.add_aur("alpha", "1.0-1", 0.1)  # same version, fresh push → up-to-date
        self.add_aur("beta", "2.1-1", 2)  # aging
        self.add_aur("gamma", "3.1-1", 10)  # ready
        self.add_aur("pinned", "2.0-1", 50)  # outdated but in IgnorePkg
        os.environ["CELLAR_TEST_IGNOREPKG"] = "pinned"

        code, out, err = self.run_cli("check")
        self.assertEqual(code, 0)  # holds are a normal outcome
        self.assertIn("aging (5.0d left)", out)
        self.assertIn("ready", out)
        self.assertIn("ignored (pacman IgnorePkg; update available)", out)
        self.assertNotIn("alpha", out)  # up-to-date hidden by default
        self.assertNotIn("local-only", out)
        self.assertIn("local-only", err)  # warned and skipped

        code, out, err = self.run_cli("check", "--all")
        self.assertEqual(code, 0)
        self.assertIn("alpha", out)
        self.assertIn("up-to-date", out)
        self.assertIn("not in AUR", out)

    def test_version_gate_beats_fresh_metadata_push(self):
        self.set_installed({"alpha": "1.0-1"})
        self.add_aur("alpha", "1.0-1", 0.01)
        code, out, _ = self.run_cli("check", "--all")
        self.assertEqual(code, 0)
        self.assertIn("up-to-date", out)
        self.assertNotIn("aging", out)

    def test_epoch_versions_compare_correctly(self):
        self.set_installed({"spot": "1.2.80.0-1"})
        self.add_aur("spot", "1:1.2.92.147-1", 10)
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("ready", out)

    def test_no_foreign_packages(self):
        self.set_installed({})
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("No foreign packages installed.", out)

    def test_pacman_warning_on_stderr_is_not_fatal(self):
        # Regression: pacman may warn on stderr while succeeding.
        os.environ["CELLAR_TEST_QM_STDERR"] = "warning: database file does not exist"
        self.set_installed({"alpha": "1.0-1"})
        self.add_aur("alpha", "1.0-1", 5)
        code, _, _ = self.run_cli("check", "--all")
        self.assertEqual(code, 0)

    def test_aur_error_response_fails_safe(self):
        self.set_installed({"alpha": "1.0-1"})
        self.server.mode = "aur-error"
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("fail safe", err)

    def test_batching_splits_large_package_sets(self):
        installed = {f"some-fairly-long-package-name-{i:03}": "1.0-1" for i in range(300)}
        self.set_installed(installed)
        for name in installed:
            self.add_aur(name, "1.0-1", 30)
        code, _, _ = self.run_cli("check", "--all")
        self.assertEqual(code, 0)
        self.assertGreater(len(self.server.requests), 1)  # actually batched
        requested = {name for batch in self.server.requests for name in batch}
        self.assertEqual(requested, set(installed))  # nothing dropped


class UpgradeCommand(CliTest):
    def setUp(self):
        super().setUp()
        self.set_installed({"beta": "2.0-1", "gamma": "3.0-1"})
        self.add_aur("beta", "2.1-1", 2)  # aging
        self.add_aur("gamma", "3.1-1", 10)  # ready

    def test_dry_run_ignores_aging_packages(self):
        code, out, _ = self.run_cli("upgrade", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--ignore=beta", out)
        self.assertNotIn("--ignore=gamma", out)
        self.assertIn("2.0-1 -> 2.1-1", out)
        self.assertFalse(self.helper_log.exists())  # dry run runs nothing

    def test_force_bypasses_hold_for_one_run(self):
        code, out, _ = self.run_cli("upgrade", "--dry-run", "--force", "beta")
        self.assertEqual(code, 0)
        self.assertNotIn("--ignore", out)
        self.assertIn("Bypassed this run (--force): beta", out)

    def test_force_unheld_package_warns(self):
        code, _, err = self.run_cli("upgrade", "--dry-run", "--force", "gamma")
        self.assertEqual(code, 0)
        self.assertIn("not currently held", err)

    def test_helper_runs_and_exit_code_propagates(self):
        os.environ["CELLAR_TEST_HELPER_EXIT"] = "7"
        code, _, _ = self.run_cli("upgrade")
        self.assertEqual(code, 7)
        self.assertEqual(self.helper_log.read_text().strip(), "-Syu --ignore=beta")

    def test_aur_unreachable_never_runs_helper(self):
        self.server.mode = "http500"
        code, _, err = self.run_cli("upgrade")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was upgraded", err)
        self.assertFalse(self.helper_log.exists())

    def test_missing_helper_fails_fast(self):
        (self.bin_dir / "paru").unlink()
        os.environ["PATH"] = str(self.bin_dir)  # no real paru/yay reachable
        code, _, err = self.run_cli("upgrade", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("AUR helper not found", err)
        self.assertEqual(self.server.requests, [])  # failed before the network


class StatusCommand(CliTest):
    def test_aging_package_detail(self):
        self.set_installed({"beta": "2.0-1"})
        self.add_aur("beta", "2.1-1", 2)
        code, out, _ = self.run_cli("status", "beta")
        self.assertEqual(code, 0)
        self.assertIn("Installed:", out)
        self.assertIn("2.0-1", out)
        self.assertIn("aging (5.0d left)", out)
        self.assertIn("Remaining:", out)
        self.assertIn("aur.archlinux.org/packages/beta", out)

    def test_unknown_package_fails(self):
        self.set_installed({"beta": "2.0-1"})
        code, _, err = self.run_cli("status", "nope")
        self.assertEqual(code, 1)
        self.assertIn("not an installed foreign package", err)


class SyncHoldsCommand(CliTest):
    def setUp(self):
        super().setUp()
        self.set_installed({"beta": "2.0-1"})
        self.add_aur("beta", "2.1-1", 2)  # aging → held

    def test_writes_managed_ignore_file(self):
        code, out, _ = self.run_cli("sync-holds")
        self.assertEqual(code, 0)
        content = self.holds_file.read_text()
        self.assertIn("Managed by cellar", content)
        self.assertIn("IgnorePkg = beta", content)
        self.assertIn("Generated-epoch:", content)

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_cli("sync-holds", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("IgnorePkg = beta", out)
        self.assertFalse(self.holds_file.exists())

    def test_no_holds_writes_empty_file(self):
        self.add_aur("beta", "2.1-1", 10)  # now aged → nothing held
        code, out, _ = self.run_cli("sync-holds")
        self.assertEqual(code, 0)
        self.assertIn("No packages currently held", out)
        self.assertNotIn("IgnorePkg =", self.holds_file.read_text())

    def test_refuses_to_overwrite_foreign_file(self):
        self.holds_file.write_text("IgnorePkg = my-manual-entry\n")
        code, _, err = self.run_cli("sync-holds")
        self.assertEqual(code, 1)
        self.assertIn("refusing", err)
        # The manual entry survived untouched.
        self.assertEqual(self.holds_file.read_text(), "IgnorePkg = my-manual-entry\n")

    def test_stale_holds_file_warns_on_check(self):
        self.run_cli("sync-holds")
        stale = self.holds_file.read_text().replace(
            self.holds_file.read_text().splitlines()[1],
            f"# Generated-epoch: {int(time.time() - 11 * DAY)}",
        )
        self.holds_file.write_text(stale)
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("days old", err)
        self.assertIn("stale holds", err)


class ConfigResolution(CliTest):
    def test_config_path_prints_xdg_file(self):
        code, out, _ = self.run_cli("config", "path")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.write_config("")))

    def test_cellar_config_env_wins_over_xdg(self):
        alt = self.tmp / "alt.toml"
        alt.write_text("days = 3\n")
        os.environ["CELLAR_CONFIG"] = str(alt)
        code, out, _ = self.run_cli("config", "path")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(alt))

    def test_config_flag_wins_over_env(self):
        alt = self.tmp / "alt.toml"
        alt.write_text("days = 3\n")
        flag = self.tmp / "flag.toml"
        flag.write_text("days = 4\n")
        os.environ["CELLAR_CONFIG"] = str(alt)
        code, out, _ = self.run_cli("config", "path", "--config", str(flag))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(flag))

    def test_missing_config_flag_file_fails(self):
        code, _, err = self.run_cli("check", "--config", str(self.tmp / "nope.toml"))
        self.assertEqual(code, 1)
        self.assertIn("no such file", err)

    def test_malformed_config_fails_before_network(self):
        self.set_installed({"beta": "2.0-1"})
        self.write_config("dyas = 3\n")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("unknown key", err)
        self.assertEqual(self.server.requests, [])

    def test_trust_and_per_package_days_from_config(self):
        self.set_installed({"beta": "2.0-1", "gamma": "3.0-1"})
        self.add_aur("beta", "2.1-1", 0.5)
        self.add_aur("gamma", "3.1-1", 2)
        self.write_config(
            """
            [packages.beta]
            trust = true

            [packages.gamma]
            days = 1
            """
        )
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("ready (trusted)", out)
        self.assertNotIn("aging", out)  # gamma's 1-day override already met

    def test_days_flag_overrides_config(self):
        self.set_installed({"gamma": "3.0-1"})
        self.add_aur("gamma", "3.1-1", 10)
        code, out, _ = self.run_cli("check", "--days", "60")
        self.assertEqual(code, 0)
        self.assertIn("aging (50.0d left)", out)


class StaleHoldTracking(CliTest):
    def test_hold_lifecycle(self):
        self.set_installed({"beta": "2.0-1"})
        self.add_aur("beta", "2.1-1", 2)

        # First sighting records the hold; no stale warning yet.
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertNotIn("clock keeps resetting", err)
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["beta"]["installed"], "2.0-1")

        # Pre-age the hold past stale_days → prominent warning.
        state["beta"]["since"] = time.time() - 40 * DAY
        self.state_file.write_text(json.dumps(state))
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("held for 40 days", err)
        self.assertIn("--force beta", err)

        # Upgrading (installed now matches latest) clears the entry.
        self.set_installed({"beta": "2.1-1"})
        code, _, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.state_file.read_text()), {})


class EntryPoint(unittest.TestCase):
    def test_python_m_cellar_runs(self):
        proc = subprocess.run(
            [sys.executable, "-m", "cellar", "--version"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("cellar", proc.stdout)


if __name__ == "__main__":
    unittest.main()
