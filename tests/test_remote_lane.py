#!/usr/bin/env python3
"""Pin that REMOTE_LANE picks the lane's host and lock, whatever the env file says.

Every tool sources an env file that may set HOST_BUILDER unconditionally, so the
cases write one and assert the lane still wins, or refuses outright.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib" / "common.sh"


def source_common(env: dict, script: str) -> tuple[int, str, str]:
    """Run `script` after sourcing common.sh, under the `set -eu` remote-task uses."""
    proc = subprocess.run(
        ["bash", "-c", f'set -eu; . "{LIB}" >/dev/null 2>&1; {script}'],
        capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def write_env(lines: list[str]) -> Path:
    """Write a scratch env file under TMPDIR so the real one cannot interfere."""
    p = Path(os.environ.get("TMPDIR", "/tmp")) / "test_remote_lane.env"
    p.write_text("\n".join(lines) + ("\n" if lines else ""))
    return p


def base_env(envfile: Path, **extra) -> dict:
    env = {**os.environ, "TOOLS_ENV": str(envfile),
           "HOME": "/home/dispatcher",
           "PATH": "/home/dispatcher/.cargo/bin:/usr/bin:/bin"}
    env.pop("REMOTE_LANE", None)
    env.pop("REMOTE_TASK_LOCK", None)
    env.pop("HOST_BUILDER", None)
    env.pop("HOST_CLUSTER", None)
    env.pop("HOST_BUILDER_FAST_LANE", None)
    env.pop("HOST_BUILDER_SOLO", None)
    env.pop("LANE_POOL_SOLO", None)
    env.update(extra)
    return env


def pick(home: Path, lanes: str, wait: int = 0, **extra) -> tuple[int, str, str]:
    """Run pick_builder_lane for tree `t` and print the lane and lock it took."""
    envfile = write_env([f'export BUILDER_LANES="{lanes}"',
                         'export LANE_POOL_A=a', 'export LANE_POOL_B=b'])
    env = base_env(envfile, HOME=str(home), LOCK_WAIT=str(wait), PICK_LANE_POLL="0",
                   **extra)
    return source_common(env, 'pick_builder_lane t; printf "%s|%s" "$REMOTE_LANE" "$BUILD_LOCK"')


def hold(lock: Path) -> subprocess.Popen:
    """Hold `lock` from another process, as a concurrent run would."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["flock", str(lock), "sleep", "60"])
    deadline = time.monotonic() + 5
    while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
        if time.monotonic() > deadline:
            raise RuntimeError(f"could not hold {lock}")
        time.sleep(0.05)
    return proc


def main() -> int:
    failures: list[str] = []

    # 1. No lane: unchanged behaviour.
    envfile = write_env(['export HOST_BUILDER=user@build-host'])
    rc, out, err = source_common(base_env(envfile), 'host=$(host_for builder); lock=$(lock_for); printf "%s|%s" "$host" "$lock"')
    if rc != 0:
        failures.append(f"no-lane host_for/lock_for refused: rc={rc} stderr={err!r}")
    elif out != "user@build-host|/home/dispatcher/.local/state/agent-tools/build.lock":
        failures.append(f"no-lane defaults diverged from today: {out!r}")

    # 2. REMOTE_LANE=fast-lane -> HOST_BUILDER_FAST_LANE.
    envfile = write_env(['export HOST_BUILDER_FAST_LANE=user@fast-builder'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="fast-lane"),
                                 'host=$(host_for builder); printf "%s" "$host"')
    if rc != 0:
        failures.append(f"lane host refused: rc={rc} stderr={err!r}")
    elif out != "user@fast-builder":
        failures.append(f"lane host was not the lane assignment: {out!r}")

    # 3. An env file assigning HOST_BUILDER unconditionally cannot defeat it.
    envfile = write_env(['export HOST_BUILDER=user@build-host',
                         'export HOST_BUILDER_FAST_LANE=user@fast-builder'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="fast-lane"),
                                 'host=$(host_for builder); printf "%s" "$host"')
    if rc != 0:
        failures.append(f"host fell back despite a lane: rc={rc} stderr={err!r}")
    elif out == "user@build-host":
        failures.append("env file defeated the lane: host_for returned HOST_BUILDER")
    elif out != "user@fast-builder":
        failures.append(f"host is neither the lane host nor the default: {out!r}")

    # 4. A lane with no host refuses and names the lane's variable.
    envfile = write_env(['export HOST_BUILDER=user@build-host'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="fast-lane"),
                                 'host_for builder >/dev/null')
    if rc == 0:
        failures.append("unset lane host succeeded silently")
    if "HOST_BUILDER_FAST_LANE" not in err:
        failures.append(f"missing-lane message omitted the lane variable: err={err!r}")

    # 5. LANE_POOL_<LANE>=solo locks on .../solo.lock; an undeclared pool
    # locks on the shared /build.lock even when REMOTE_LANE is set.
    envfile = write_env(['export HOST_BUILDER_FAST_LANE=user@fast-builder',
                         'export LANE_POOL_SOLO=solo',
                         'export HOST_BUILDER_SOLO=user@solo-builder'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="solo"),
                                 'printf "%s" "$(lock_for)"')
    if rc != 0:
        failures.append(f"declare-pool lock refused: rc={rc} stderr={err!r}")
    elif out != "/home/dispatcher/.local/state/agent-tools/solo.lock":
        failures.append(f"declared pool not honoured: {out!r}")

    envfile = write_env(['export HOST_BUILDER_FAST_LANE=user@fast-builder'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="fast-lane"),
                                 'printf "%s" "$(lock_for)"')
    if rc != 0:
        failures.append(f"undeclared-pool lock refused: rc={rc} stderr={err!r}")
    elif out != "/home/dispatcher/.local/state/agent-tools/build.lock":
        failures.append(f"undeclared pool should default to shared: {out!r}")

    # 6. REMOTE_TASK_LOCK overrides the lane pool AND the no-lane default.
    envfile = write_env(['export HOST_BUILDER=user@build-host',
                         'export LANE_POOL_SOLO=solo',
                         'export HOST_BUILDER_SOLO=user@solo-builder'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="solo",
                                         REMOTE_TASK_LOCK="/tmp/override.lock"),
                                 'printf "%s" "$(lock_for)"')
    if rc != 0:
        failures.append(f"override lock refused: rc={rc} stderr={err!r}")
    elif out != "/tmp/override.lock":
        failures.append(f"REMOTE_TASK_LOCK did not win: {out!r}")

    envfile = write_env(['export HOST_BUILDER=user@build-host'])
    rc, out, err = source_common(base_env(envfile,
                                         REMOTE_TASK_LOCK="/tmp/override.lock"),
                                 'printf "%s" "$(lock_for)"')
    if rc != 0:
        failures.append(f"override-without-lane refused: rc={rc} stderr={err!r}")
    elif out != "/tmp/override.lock":
        failures.append(f"REMOTE_TASK_LOCK did not win without a lane: {out!r}")

    # 7. A lane selects a builder only; other roles resolve as before.
    envfile = write_env(['export HOST_CLUSTER=user@cluster-host',
                         'export HOST_BUILDER_FAST_LANE=user@fast-builder'])
    rc, out, err = source_common(base_env(envfile, REMOTE_LANE="fast-lane"),
                                 'printf "%s" "$(host_for cluster)"')
    if rc != 0 or out != "user@cluster-host":
        failures.append(f"a lane rebound the cluster role: rc={rc} out={out!r} err={err!r}")

    # 8. BUILDER_LANES: the first idle lane wins, a held one is skipped, the
    # tree's last lane goes first while idle, and all-busy gives up.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        locks = home / ".local/state/agent-tools"
        rc, out, err = pick(home, "a b")
        if rc != 0 or out != f"a|{locks}/a.lock":
            failures.append(f"idle lanes did not pick the first: rc={rc} out={out!r} err={err!r}")

        (locks / "builder-lane/t").write_text("b\n")
        rc, out, err = pick(home, "a b")
        if rc != 0 or out != f"b|{locks}/b.lock":
            failures.append(f"the tree's last lane was not preferred: rc={rc} out={out!r} err={err!r}")

        holder = hold(locks / "b.lock")
        try:
            rc, out, err = pick(home, "a b")
            if rc != 0 or out != f"a|{locks}/a.lock":
                failures.append(f"a busy last lane was not skipped: rc={rc} out={out!r} err={err!r}")
            if (locks / "builder-lane/t").read_text() != "a\n":
                failures.append("the pick was not recorded for the tree")

            other = hold(locks / "a.lock")
            try:
                rc, out, err = pick(home, "a b")
                if rc == 0:
                    failures.append(f"every lane busy still picked one: {out!r}")
            finally:
                other.kill()
                other.wait()
        finally:
            holder.kill()
            holder.wait()

    # 9. An override lock's directory is created; an unopenable lock returns 2.
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        override = home / "new/dir/x.lock"
        rc, out, err = pick(home, "a", REMOTE_TASK_LOCK=str(override))
        if rc != 0 or out != f"a|{override}":
            failures.append(f"override lock in a new directory: rc={rc} out={out!r} err={err!r}")

        (home / "file").write_text("")
        rc, out, err = pick(home, "a", REMOTE_TASK_LOCK=str(home / "file/x.lock"))
        if rc != 2:
            failures.append(f"an unopenable lock did not return 2: rc={rc} out={out!r} err={err!r}")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print("PASS REMOTE_LANE selects the lane host and the lane lock; BUILDER_LANES picks an idle lane")
    return 0


if __name__ == "__main__":
    sys.exit(main())
