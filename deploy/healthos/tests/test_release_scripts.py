"""Exercise rollback without a Docker daemon, network, or real credentials."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts/deploy_yggdrasil.sh"
OLD_ID = "sha256:" + "a" * 64


class ReleaseScriptsTests(unittest.TestCase):
    def test_rollback_uses_captured_image_and_original_environment(self):
        bash = shutil.which("bash")
        if os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
            bash = str(candidate) if candidate.is_file() else None
        if not bash:
            self.skipTest("Bash is required for deployment-script rollback regression")
        for failure in ("run", "health"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix="healthos-release-test-") as directory:
                temp = Path(directory)
                trace = temp / "trace"
                docker = r'''#!/usr/bin/env bash
set -eu
case "$1" in
 inspect)
   case "$*" in
    *Config.Env*) printf 'HEALTHOS_API_TOKEN=synthetic-only\nHEALTHOS_IRONBOOK_BASE_URL=http://synthetic-old\n' ;;
    *Config.Image*) echo docker.io/ziyaolin/healthos:latest ;;
    *'{{.Image}}'*) echo '__OLD_ID__' ;;
   esac ;;
 run)
   printf 'run %s\n' "$*" >> "$TRACE_FILE"
   last=''
   for arg in "$@"; do
     if [ "$last" = '--env-file' ]; then
       if grep -q '^HEALTHOS_IRONBOOK_BASE_URL=' "$arg"; then echo 'original_env=yes' >> "$TRACE_FILE"; else echo 'original_env=no' >> "$TRACE_FILE"; fi
     fi
     last="$arg"
   done
   case "$*" in
     *__OLD_ID__*) echo old > "$STATE_FILE" ;;
     *) echo new > "$STATE_FILE"; if [ "$FAILURE" = run ]; then exit 1; fi ;;
   esac ;;
 ps) ;;
 *) exit 0 ;;
esac
'''.replace("__OLD_ID__", OLD_ID)
                curl = r'''#!/usr/bin/env bash
if [ "$(cat "$STATE_FILE")" = old ]; then echo '{"ok":true}'; else exit 22; fi
'''
                for name, body in (("docker", docker), ("curl", curl), ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
                    path = temp / name
                    path.write_text(body, encoding="utf-8", newline="\n")
                    path.chmod(0o755)
                env = dict(os.environ, PATH=str(temp) + os.pathsep + os.environ.get("PATH", ""),
                           TRACE_FILE=trace.as_posix(), STATE_FILE=(temp / "state").as_posix(), FAILURE=failure,
                           HEALTHOS_IMAGE="docker.io/ziyaolin/healthos:0.10.0", HEALTHOS_PRUNE_OLD_IMAGE="0")
                shell = 'export PATH="$1:/usr/bin:/bin"; exec bash "$2"'
                if os.name == "nt":
                    shell = 'export PATH="$(/usr/bin/cygpath -u "$1"):/usr/bin:/bin"; exec bash "$2"'
                result = subprocess.run([bash, "-c", shell,
                                         "release-test", temp.as_posix(), SCRIPT.resolve().as_posix()], env=env,
                                        capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 1, result.stderr)
                lines = trace.read_text().splitlines()
                runs = [line for line in lines if line.startswith("run ")]
                self.assertEqual(len(runs), 2)
                self.assertIn("docker.io/ziyaolin/healthos:0.10.0 serve", runs[0])
                self.assertIn(OLD_ID + " serve", runs[1])
                self.assertEqual([line for line in lines if line.startswith("original_env=")],
                                 ["original_env=no", "original_env=yes"])
                self.assertIn("restored healthos from " + OLD_ID, result.stderr)


if __name__ == "__main__":
    unittest.main()
