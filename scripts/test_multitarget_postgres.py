"""Create a UTF-8, socket-only disposable database, test, then always shut it down."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="oneaction-multitarget-pg-") as folder:
    temp = Path(folder)
    data = temp / "data"
    env = {**os.environ, "LC_ALL": "C"}
    subprocess.run(["initdb", "-D", str(data), "-A", "trust", "-U", "local_test", "--locale=C", "-E", "UTF8"],
                   check=True, env=env, stdout=subprocess.DEVNULL)
    subprocess.run(["pg_ctl", "-D", str(data), "-l", str(temp / "postgres.log"), "-o",
                    f"-k {temp} -h '' -p 55439", "-w", "start"], check=True, env=env)
    try:
        env["MULTITARGET_TEST_DSN"] = f"host={temp} port=55439 dbname=postgres user=local_test"
        env["PYTHONPATH"] = os.pathsep.join(str(root / p) for p in ("ai", "common", "api", "worker"))
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_request_postgres.py"],
                                env=env, cwd=root / "worker")
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"], check=True, env=env)
    raise SystemExit(result.returncode)
