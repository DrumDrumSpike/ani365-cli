"""Exercise the real updater with isolated fake Docker/GitHub, never the host daemon."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MOCK_COMMAND = r'''#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["FAKE_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (root / "calls").open("a") as f:
    f.write(json.dumps([name, args]) + "\n")
if name == "curl":
    print(json.dumps([
        {"tag_name": "v9.9.9", "draft": False, "prerelease": False, "published_at": "2026-09-09"},
        {"tag_name": "bot-v0.2.0", "draft": False, "prerelease": False, "published_at": "2026-09-08"},
        {"tag_name": "bot-v0.3.0", "draft": False, "prerelease": True, "published_at": "2026-09-10"}
    ]))
    sys.exit(0)
if name == "sleep":
    sys.exit(0)
if name != "docker":
    sys.exit(2)
if args[0] == "pull":
    sys.exit(1 if os.environ.get("FAIL_PULL") else 0)
if args[:2] == ["image", "inspect"]:
    if "RepoDigests" in args[-1]:
        print("ghcr.io/drumdrumspike/ani365-bot@sha256:new")
    else:
        print("sha256:old" if args[2] == "ani365-bot:rollback" else "sha256:new")
    sys.exit(0)
if args[:2] == ["image", "ls"]:
    print("sha256:old\nsha256:new\nsha256:unused")
    sys.exit(0)
if args[0] == "compose":
    if "images" in args:
        print("sha256:old")
    if "control" in " ".join(args):
        action = args[-1]
        if action == "drain":
            (root / "drain").touch()
        elif action == "resume":
            (root / "drain").unlink(missing_ok=True)
        elif action == "ready":
            sys.exit(1 if os.environ.get("BUSY") else 0)
    if "up" in args:
        candidate = (root / "deploy.env").read_text()
        with (root / "installed").open("a") as f:
            f.write(candidate)
        if "sha256:new" in candidate and os.environ.get("FAIL_HEALTH"):
            sys.exit(1)
sys.exit(0)
'''


@unittest.skipUnless(shutil.which("jq") and shutil.which("flock"), "Updater requires jq and flock")
class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "bin").mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/update.sh", self.root / "scripts/update.sh")
        for name in ("docker", "curl", "sleep"):
            path = self.root / "bin" / name
            path.write_text(MOCK_COMMAND)
            path.chmod(0o755)
        (self.root / "deploy.env").write_text("BOT_IMAGE=ani365-bot:local\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_update(self, **flags):
        env = dict(os.environ, FAKE_ROOT=str(self.root), PATH=str(self.root / "bin") + ":" + os.environ["PATH"], **flags)
        result = subprocess.run(["bash", str(self.root / "scripts/update.sh")], env=env,
                                text=True, capture_output=True, timeout=30)
        self.calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        return result

    def test_installs_stable_bot_release_and_preserves_rollback_image(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sha256:new", (self.root / "deploy.env").read_text())
        self.assertFalse((self.root / "drain").exists())
        self.assertIn(["docker", ["pull", "ghcr.io/drumdrumspike/ani365-bot:bot-v0.2.0"]], self.calls)
        self.assertIn(["docker", ["tag", "sha256:old", "ani365-bot:rollback"]], self.calls)
        removed = [args[-1] for name, args in self.calls if name == "docker" and args[:2] == ["image", "rm"]]
        self.assertEqual(removed, ["sha256:unused"])

    def test_failed_pull_never_stops_or_drains_bot(self):
        result = self.run_update(FAIL_PULL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "deploy.env").read_text(), "BOT_IMAGE=ani365-bot:local\n")
        self.assertFalse(any("drain" in args or "up" in args for _, args in self.calls))

    def test_bad_health_rolls_back_and_resumes_requests(self):
        result = self.run_update(FAIL_HEALTH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "deploy.env").read_text(), "BOT_IMAGE=ani365-bot:rollback\n")
        self.assertEqual((self.root / "installed").read_text().splitlines(),
                         ["BOT_IMAGE=ghcr.io/drumdrumspike/ani365-bot@sha256:new", "BOT_IMAGE=ani365-bot:rollback"])
        self.assertFalse((self.root / "drain").exists())

    def test_same_digest_does_not_restart(self):
        (self.root / "deploy.env").write_text("BOT_IMAGE=ghcr.io/drumdrumspike/ani365-bot@sha256:new\n")
        result = self.run_update()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(any("up" in args or "drain" in args for _, args in self.calls))


if __name__ == "__main__":
    unittest.main()
