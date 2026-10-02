#!/usr/bin/env python3
"""Pin the GPU group lock's three contracts: serialise, don't serialise, don't lock.

The lock has three behaviours a caller relies on and which a silent regression
breaks each in its own way.

  Two callers on one group serialise. The second waits outside any timeout,
  and only starts when the first has released. A lock that does not wait
  (a silent flock -n 8 that returns failure on contention) would let two
  reviews land on the same GPUs at once and reproduce the original thrash.

  Two callers on different groups run together. A lock that conflated groups
  -- hashing on the basename rather than the value, or
  keying on the profile rather than the GPU group -- would still serialise
  across unrelated jobs and waste the parallelism the issue was filed to
  recover.

  No group configured means no lock file and no wait. A profile that
  doesn't name a GPU group should not acquire a lock on anything; a
  function that creates the lock file as a side effect of being called
  would litter the state directory with empty locks for every profile
  the operator ever configured, and a "wait" loop with no lock to wait
  on would never return.

Run directly: `python3 tests/test_gpu_lock.py`.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib" / "common.sh"


def run_scenario(env: dict, body: str) -> subprocess.CompletedProcess:
    """Source common.sh under the given env and run `body`. set -eu matches
    the bin/ scripts' defaults; otherwise a script that exits non-zero on a
    missing variable would mask the case being tested."""
    proc = subprocess.run(
        ["bash", "-c", f'set -eu; . "{LIB}" >/dev/null 2>&1; {body}'],
        capture_output=True, text=True, env=env)
    return proc


def base_env(envfile: Path | None, **extra) -> dict:
    """An env without the GPU_GROUP_* vars, so a test that sets only one does
    not collide with the operator's own. HOME is set to a scratch path so the
    default AGENT_STATE_DIR doesn't reach into the real ~/.local tree."""
    env = {**os.environ, "HOME": "/nonexistent",
           "PATH": "/usr/bin:/bin", "TOOLS_ENV": str(envfile) if envfile else ""}
    # Clear anything that might leak into the test.
    for k in list(env):
        if k.startswith("GPU_GROUP_") or k == "AGENT_STATE_DIR":
            env.pop(k, None)
    env.update(extra)
    return env


def write_env(lines: list[str] | None = None) -> Path:
    """Write an empty env file under TMPDIR so the real ~/.config one
    cannot leak a value into the test."""
    p = Path(os.environ.get("TMPDIR", "/tmp")) / "test_gpu_lock.env"
    p.write_text("\n".join(lines or []) + ("\n" if lines else ""))
    return p


def same_group_serialise() -> tuple[bool, str]:
    """Two callers on the same GPU group: the second starts only after the
    first releases. Run under two bash subshells that race for the lock;
    the timestamps they print via `date +%s.%N` decide whether they
    overlapped."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        envfile = write_env()
        env = base_env(envfile, AGENT_STATE_DIR=str(tmp),
                       GPU_GROUP_PROFILE="logan-3099")
        # Hold the lock from a third process: the second subshell has to
        # wait for it to release before it can take its own lock. Four
        # seconds is long enough that the first attempt has clearly started
        # (the timestamp + "start" before the wait) and short enough that
        # the test stays fast.
        holder = subprocess.Popen(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock profile; '
             f'echo start $(date +%s.%N); sleep 4; '
             f'echo release $(date +%s.%N); exec 8>&-'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Give the holder a moment to acquire its lock.
        time.sleep(0.3)
        waiter = subprocess.run(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock profile; '
             f'echo acquired $(date +%s.%N); exec 8>&-'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        holder_out, holder_err = holder.communicate(timeout=15)
        assert holder.returncode == 0, \
            f"holder failed: rc={holder.returncode} stderr={holder_err!r}"
        assert waiter.returncode == 0, \
            f"waiter failed: rc={waiter.returncode} stderr={waiter.stderr!r}"
        # Holder timestamps.
        holder_release = float(re.search(r"release ([\d.]+)", holder_out).group(1))
        waiter_acquired = float(re.search(r"acquired ([\d.]+)", waiter.stdout).group(1))
        if waiter_acquired < holder_release:
            return False, (f"waiter acquired at {waiter_acquired} but holder "
                           f"released at {holder_release}")
        # And the "waiting for GPU group" line must appear on stderr
        # exactly once, like acquire_review_slot's.
        waiting = [line for line in waiter.stderr.splitlines()
                   if "waiting for GPU group" in line]
        if len(waiting) != 1:
            return False, (f"expected exactly one waiting line, got "
                           f"{len(waiting)}: {waiter.stderr!r}")
        return True, ""


def different_groups_run_concurrently() -> tuple[bool, str]:
    """Two callers on different GPU groups must both acquire their lock
    without waiting on each other."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        envfile = write_env()
        env = base_env(envfile, AGENT_STATE_DIR=str(tmp),
                       GPU_GROUP_PROFILE="logan-3099")
        # Holder of group A
        holder = subprocess.Popen(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock profile; '
             f'echo start $(date +%s.%N); sleep 4; '
             f'echo release $(date +%s.%N); exec 8>&-'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.3)
        # Different group; should NOT wait for holder.
        env_b = base_env(envfile, AGENT_STATE_DIR=str(tmp),
                         GPU_GROUP_PROFILE="another-3099")
        waiter = subprocess.run(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock profile; '
             f'echo acquired $(date +%s.%N); exec 8>&-'],
            env=env_b, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        holder_out, holder_err = holder.communicate(timeout=15)
        assert holder.returncode == 0, \
            f"holder failed: rc={holder.returncode} stderr={holder_err!r}"
        assert waiter.returncode == 0, \
            f"waiter failed: rc={waiter.returncode} stderr={waiter.stderr!r}"
        waiter_acquired = float(re.search(r"acquired ([\d.]+)", waiter.stdout).group(1))
        holder_release = float(re.search(r"release ([\d.]+)", holder_out).group(1))
        if waiter_acquired >= holder_release:
            return False, (f"different-group waiter acquired at "
                           f"{waiter_acquired} only after holder released at "
                           f"{holder_release}; should have been concurrent")
        # No wait message either.
        if any("waiting for GPU group" in line for line in waiter.stderr.splitlines()):
            return False, f"different group should not have logged a wait: {waiter.stderr!r}"
        return True, ""


def no_group_no_file_no_wait() -> tuple[bool, str]:
    """No GPU_GROUP_* in scope: acquire_gpu_lock returns at once without
    creating a lock file or printing a wait line."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        envfile = write_env()
        env = base_env(envfile, AGENT_STATE_DIR=str(tmp))
        proc = subprocess.run(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock default; '
             f'echo rc=$? done $(date +%s.%N)'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.returncode == 0, f"acquire_gpu_lock refused: {proc.stderr!r}"
        # The function returned at once -- the "done" line should be very
        # close to the function-call timestamp. We don't pin a hard
        # number, but we do assert no "waiting" line.
        if any("waiting" in line for line in proc.stderr.splitlines()):
            return False, f"no-group caller logged a wait: {proc.stderr!r}"
        # No lock file under state dir.
        locks = list(tmp.glob("*.lock"))
        if locks:
            return False, f"no-group caller created lock files: {locks}"
        return True, ""


def root_profile_routes_to_default() -> tuple[bool, str]:
    """Empty PROFILE (the root profile's case in hermes-implement) resolves
    to GPU_GROUP_DEFAULT, not GPU_GROUP_."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        envfile = write_env()
        env = base_env(envfile, AGENT_STATE_DIR=str(tmp),
                       GPU_GROUP_DEFAULT="logan-3099")
        # Two callers: one on default group (via empty profile), one on
        # a *different* group. They should run concurrently -- proves the
        # empty profile maps to GPU_GROUP_DEFAULT, not to GPU_GROUP_.
        holder = subprocess.Popen(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock ""; '
             f'echo start $(date +%s.%N); sleep 4; '
             f'echo release $(date +%s.%N); exec 8>&-'],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.3)
        env_b = base_env(envfile, AGENT_STATE_DIR=str(tmp),
                         GPU_GROUP_OTHER="not-the-default")
        waiter = subprocess.run(
            ["bash", "-c", f'. "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock other; '
             f'echo acquired $(date +%s.%N); exec 8>&-'],
            env=env_b, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        holder_out, holder_err = holder.communicate(timeout=15)
        assert holder.returncode == 0, \
            f"holder failed: rc={holder.returncode} stderr={holder_err!r}"
        assert waiter.returncode == 0, \
            f"waiter failed: rc={waiter.returncode} stderr={waiter.stderr!r}"
        waiter_acquired = float(re.search(r"acquired ([\d.]+)", waiter.stdout).group(1))
        holder_release = float(re.search(r"release ([\d.]+)", holder_out).group(1))
        if waiter_acquired >= holder_release:
            return False, (f"empty-profile waiter acquired at "
                           f"{waiter_acquired} only after holder released at "
                           f"{holder_release}; should have been concurrent "
                           f"(empty profile should resolve to GPU_GROUP_DEFAULT)")
        # And the lock file should be gpu-logan-3099.lock, not gpu-.lock.
        if not (tmp / "gpu-logan-3099.lock").exists():
            return False, (f"expected gpu-logan-3099.lock in {tmp}, got "
                           f"{list(tmp.iterdir())}")
        return True, ""


def main() -> int:
    failures: list[tuple[str, str]] = []
    for name, fn in [
        ("two callers on one group serialise", same_group_serialise),
        ("two callers on different groups run concurrently",
         different_groups_run_concurrently),
        ("no group configured: no lock file and no wait",
         no_group_no_file_no_wait),
        ("empty profile resolves to GPU_GROUP_DEFAULT",
         root_profile_routes_to_default),
    ]:
        try:
            ok, why = fn()
        except AssertionError as e:
            ok, why = False, str(e)
        if not ok:
            failures.append((name, why))
            print(f"FAIL {name}: {why}")
        else:
            print(f"ok   {name}")
    if failures:
        return 1
    print("PASS acquire_gpu_lock serialises on group, runs across groups, "
          "and is a no-op without one")
    return 0


if __name__ == "__main__":
    sys.exit(main())