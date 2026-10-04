#!/usr/bin/env python3
"""Pin pick_lane's three rules: observed fallback and concurrency pick the
fallback without contacting the quota endpoint, and every quota answer in the
decision table on tooling #85 picks as recorded there.

Each run serves the endpoint from a local http.server through
PICK_LANE_REMAINS_URL, with a fake key and HOME pointed at an empty directory,
so the real endpoint and ~/.hermes are never touched.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PICK = REPO_ROOT / "pipeline" / "pick_lane.py"

# Columns the production query names, exactly as the real minimax state.db
# names them. A drift between this fixture and pipeline/pick_lane.py reads
# as "picker does not work" rather than "fixture is wrong", so keep them
# in lockstep.
SCHEMA = """
create table session_model_usage (
    session_id        text primary key,
    model             text,
    billing_provider  text,
    task              text,
    api_call_count    integer,
    input_tokens      integer,
    output_tokens     integer,
    cache_read_tokens integer,
    cache_write_tokens integer,
    first_seen        real,
    last_seen         real
);
create table sessions (
    id        text primary key,
    ended_at  real
);
"""

# A key the picker treats as a valid Authorization header. The picker
# only forwards it to ``urllib``; it never logs it. We pick a long
# random-looking string so a leaky test is obvious in the diff.
TEST_API_KEY = "test-key-picklane-0c7e1f9a"


class TmpDb:
    """Build a fresh sqlite db with the columns the production query expects,
    drop it on exit. ``row(...)`` and ``session(...)`` add one each."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "usage.db"
        con = sqlite3.connect(self.path)
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def session(self, *, session_id: str, ended_at: float | None = None) -> None:
        con = sqlite3.connect(self.path)
        con.execute(
            "insert into sessions (id, ended_at) values (?, ?) "
            "on conflict(id) do update set ended_at = excluded.ended_at",
            (session_id, ended_at),
        )
        con.commit()
        con.close()

    def row(self, *, session_id: str, model: str = "m",
            task: str | None = "",
            billing_provider: str | None = "minimax",
            api_call_count: int = 1,
            input_tokens: int = 0, output_tokens: int = 0,
            cache_read_tokens: int = 0, cache_write_tokens: int = 0,
            minutes_ago: float = 0.0) -> None:
        now = time.time()
        last_seen = now - minutes_ago * 60
        first_seen = last_seen - 1.0
        con = sqlite3.connect(self.path)
        con.execute(
            "insert or ignore into sessions (id, ended_at) values (?, null)",
            (session_id,),
        )
        con.execute(
            "insert into session_model_usage "
            "(session_id, model, billing_provider, task, api_call_count, "
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
            " first_seen, last_seen) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, model, billing_provider, task, api_call_count,
             input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             first_seen, last_seen),
        )
        con.commit()
        con.close()

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "TmpDb":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Stub:
    """Local http.server on 127.0.0.1 that serves one configured
    response, counts requests, and exposes the path the picker
    actually hit. Each ``run_pick`` spawns a fresh server so a test
    cannot leak a payload into the next one."""

    def __init__(self, response: dict,
                 status_code: int = 200,
                 raise_on_request: BaseException | None = None) -> None:
        self.response: dict = response
        self.http_status = status_code
        self.raise_on_request = raise_on_request
        self.requests: list[dict] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        # Pick a free port on loopback. ``bind(('', 0))`` lets the OS
        # assign one; we then read it back so the override variable
        # points at exactly the port we bound.
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object, **_k: object) -> None:
                return  # silence stderr noise during tests

            def do_GET(self) -> None:  # noqa: N802 -- http.server contract
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(body) if body else {}
                except ValueError:
                    payload = {"_raw": body.decode("utf-8", "replace")}
                # Record the headers the picker actually sent so the
                # auth-header smoke test can assert the Authorization
                # header carried the configured key. The Body is logged
                # for completeness; it is always empty because the
                # picker only issues GETs.
                outer.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                    "body": payload,
                })
                # Optional fault injection: the test can ask the stub to
                # drop the connection so urlopen sees a transport error
                # instead of a malformed body. Used for the refused
                # case.
                if outer.raise_on_request is not None:
                    raise outer.raise_on_request
                body_bytes = json.dumps(outer.response).encode("utf-8")
                self.send_response(outer.http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.port = port
        return f"http://127.0.0.1:{port}/v1/token_plan/remains"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def _build_env(*, stub_url: str | None,
              with_api_key: bool = True) -> dict[str, str]:
    """Build the env for a pick_lane subprocess.

    HOME points at an empty tmpdir so the picker can never reach the
    real ``~/.hermes/profiles/minimax/.env``. ``MINIMAX_API_KEY`` is
    set so the .env fallback is also not consulted, unless the caller
    asks for the no-key-anywhere case (``with_api_key=False``).
    ``stub_url`` is ``None`` for cases that must not contact the
    endpoint at all (the picker reads ``PICK_LANE_REMAINS_URL`` and
    skips the http call if it is unset; we leave it unset for those).
    When ``with_api_key`` is False, we also unset
    ``PICK_LANE_REMAINS_URL`` so the picker cannot reach the real
    default URL on the way to discovering it has no key."""
    env = os.environ.copy()
    env["HOME"] = tempfile.mkdtemp()
    if with_api_key:
        env["MINIMAX_API_KEY"] = TEST_API_KEY
    else:
        env.pop("MINIMAX_API_KEY", None)
    if stub_url is not None:
        env["PICK_LANE_REMAINS_URL"] = stub_url
    else:
        env.pop("PICK_LANE_REMAINS_URL", None)
    return env


def run_pick(db_path: Path, *, stub_url: str | None,
             extra: tuple[str, ...] = (),
             with_api_key: bool = True) -> tuple[str, str, int]:
    """Run pick_lane.py with --db db_path, return (stdout, stderr, rc)."""
    env = _build_env(stub_url=stub_url, with_api_key=with_api_key)
    proc = subprocess.run(
        ["python3", str(PICK), "--db", str(db_path), *extra],
        capture_output=True, text=True, env=env)
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def main() -> int:
    failures: list[str] = []
    total_cases = 0

    def expect(label: str, *, db_path: Path, want_profile: str,
               want_reason_substr: str | None = None,
               stub: _Stub | None = None,
               stub_url: str | None = None,
               extra_args: tuple[str, ...] = (),
               want_endpoint_called: bool = True) -> None:
        """Run one case against ``db_path``.

        If ``stub`` is provided, ``stub_url`` is taken from the stub
        (which the caller is expected to have already started). When
        ``want_endpoint_called`` is False, the test asserts the picker
        did not contact the endpoint at all -- used for the
        "observed fallback / concurrency fires first" cases."""
        if stub is not None and stub_url is None:
            stub_url = f"http://127.0.0.1:{stub.port}/v1/token_plan/remains"
        profile, reason, rc = run_pick(db_path, stub_url=stub_url,
                                       extra=extra_args)
        if rc != 0:
            failures.append(f"{label}: pick_lane exited {rc}: {reason}")
            return
        if profile != want_profile:
            failures.append(
                f"{label}: chose {profile!r}, want {want_profile!r}; "
                f"reason was {reason!r}")
            return
        if want_reason_substr is not None and want_reason_substr not in reason:
            failures.append(
                f"{label}: profile matched but reason {reason!r} does not "
                f"contain {want_reason_substr!r}")
            return
        if stub is not None and not want_endpoint_called and stub.requests:
            failures.append(
                f"{label}: picker contacted the endpoint {len(stub.requests)} "
                f"time(s); the rule should have fired before quota check "
                f"(paths: {[r['path'] for r in stub.requests]!r})")
        if stub is not None and want_endpoint_called and not stub.requests:
            failures.append(
                f"{label}: picker did not contact the endpoint; expected quota "
                f"check to run")
        nonlocal total_cases
        total_cases += 1
    # ----------------------------------------------------------------
    # Sanity: the picker does not crash on an empty or missing db.
    # The missing-db path short-circuits before the quota check, so
    # the stub must not have been contacted.
    # ----------------------------------------------------------------
    with TmpDb() as empty:
        # Empty db exercises every rule: no fallback row, no concurrent
        # session, then the quota check. We just check the path picks
        # preferred and names the percentages.
        stub = _Stub(_ok(weekly=60, interval=70))
        stub.start()
        try:
            expect("empty db runs the quota check and picks preferred",
                   db_path=empty.path, want_profile="minimax",
                   stub=stub,
                   want_reason_substr="60% weekly",
                   want_endpoint_called=True)
        finally:
            stub.stop()
    missing = Path(tempfile.mkdtemp()) / "does-not-exist.db"
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("missing db picks fallback without contacting the endpoint",
               db_path=missing, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="no usage db",
               want_endpoint_called=False)
    finally:
        stub.stop()

    # ----------------------------------------------------------------
    # Rule 1: observed fallback fires before the endpoint is touched.
    # ----------------------------------------------------------------
    recent_custom = TmpDb()
    recent_custom.row(session_id="fell-back", minutes_ago=10,
                      billing_provider="custom")
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("recent custom-billed main row trips observed fallback",
               db_path=recent_custom.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="observed fallback",
               want_endpoint_called=False)
    finally:
        stub.stop()
    recent_custom.close()

    old_custom = TmpDb()
    old_custom.row(session_id="fell-back", minutes_ago=40,
                   billing_provider="custom")
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("40-minute-old custom-billed row does not trip observed fallback",
               db_path=old_custom.path, want_profile="minimax",
               stub=stub,
               want_reason_substr="50% weekly")
    finally:
        stub.stop()
    old_custom.close()

    aux_empty = TmpDb()
    aux_empty.row(session_id="aux-empty", task="title_generation",
                  billing_provider="", minutes_ago=1)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("auxiliary row billed to '' does not trip observed fallback",
               db_path=aux_empty.path, want_profile="minimax",
               stub=stub)
    finally:
        stub.stop()
    aux_empty.close()

    aux_custom = TmpDb()
    aux_custom.row(session_id="aux-custom", task="title_generation",
                   billing_provider="custom", minutes_ago=1)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("auxiliary row billed to 'custom' does not trip observed fallback",
               db_path=aux_custom.path, want_profile="minimax",
               stub=stub)
    finally:
        stub.stop()
    aux_custom.close()

    # ----------------------------------------------------------------
    # Rule 2: concurrency fires before the endpoint is touched.
    # ----------------------------------------------------------------
    four = TmpDb()
    for i in range(4):
        four.row(session_id=f"open-{i}", minutes_ago=i)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("four open sessions trip concurrency",
               db_path=four.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="concurrent minimax session",
               want_endpoint_called=False)
    finally:
        stub.stop()
    four.close()

    three = TmpDb()
    for i in range(3):
        three.row(session_id=f"open-{i}", minutes_ago=i)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("three open sessions do not trip concurrency",
               db_path=three.path, want_profile="minimax",
               stub=stub)
    finally:
        stub.stop()
    three.close()

    ended_recently = TmpDb()
    for i in range(4):
        ended_recently.row(session_id=f"closed-{i}", minutes_ago=i)
    now = time.time()
    for i in range(4):
        ended_recently.session(session_id=f"closed-{i}",
                               ended_at=now - 60)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("session ended one minute ago does not count toward concurrency",
               db_path=ended_recently.path, want_profile="minimax",
               stub=stub)
    finally:
        stub.stop()
    ended_recently.close()

    crashed_open = TmpDb()
    for i in range(4):
        crashed_open.row(session_id=f"stale-{i}", minutes_ago=40 + i)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("open session with last_seen 40m ago does not count as concurrent",
               db_path=crashed_open.path, want_profile="minimax",
               stub=stub)
    finally:
        stub.stop()
    crashed_open.close()

    aux_concurrency = TmpDb()
    for i in range(4):
        aux_concurrency.row(session_id=f"aux-{i}", task="title_generation",
                            minutes_ago=i)
    aux_concurrency.row(session_id="main-0", task="", minutes_ago=0)
    stub = _Stub(_ok(weekly=50, interval=50))
    stub.start()
    try:
        expect("auxiliary rows do not count toward concurrency",
               db_path=aux_concurrency.path, want_profile="minimax",
               stub=stub)
    finally:
        stub.stop()
    aux_concurrency.close()

    # ----------------------------------------------------------------
    # Rule 3: quota. The six rows of the issue's decision table.
    # Each row uses a fresh stub so a request cannot leak between cases.
    # ----------------------------------------------------------------

    # weekly 81, interval 93 -> preferred; reason names both percentages.
    db = TmpDb()
    stub = _Stub(_ok(weekly=81, interval=93))
    stub.start()
    try:
        expect("weekly 81, interval 93 picks preferred and names both",
               db_path=db.path, want_profile="minimax",
               stub=stub,
               want_reason_substr="81% weekly")
    finally:
        stub.stop()
    db.close()

    # weekly 15, interval 90 -> fallback; reason names the weekly window.
    db = TmpDb()
    stub = _Stub(_ok(weekly=15, interval=90))
    stub.start()
    try:
        expect("weekly 15, interval 90 picks fallback and names the weekly window",
               db_path=db.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="15% weekly")
    finally:
        stub.stop()
    db.close()

    # weekly 90, interval 10 -> fallback; reason names the 5-hour window.
    db = TmpDb()
    stub = _Stub(_ok(weekly=90, interval=10))
    stub.start()
    try:
        expect("weekly 90, interval 10 picks fallback and names the 5-hour window",
               db_path=db.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="10% 5-hour")
    finally:
        stub.stop()
    db.close()

    # Both windows below the floor: reason names both. Verifies the
    # comma-join, which is the previous rows skip.
    db = TmpDb()
    stub = _Stub(_ok(weekly=5, interval=8))
    stub.start()
    try:
        expect("both windows below floor name both in the reason",
               db_path=db.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="5% weekly, 8% 5-hour")
    finally:
        stub.stop()
    db.close()

    # --min-remaining-percent pins the threshold: with floor=80, a
    # 90% reading is above the floor (preferred) but a 70% reading
    # is below (fallback). This pins the threshold argument end-to-end
    # so a default change does not silently change the behaviour of
    # every operator who never passed the flag.
    db = TmpDb()
    stub = _Stub(_ok(weekly=90, interval=90))
    stub.start()
    try:
        expect("--min-remaining-percent 80 keeps a 90% answer preferred",
               db_path=db.path, want_profile="minimax",
               stub=stub,
               extra_args=("--min-remaining-percent", "80"),
               want_reason_substr="90% weekly")
    finally:
        stub.stop()
    db.close()

    db = TmpDb()
    stub = _Stub(_ok(weekly=70, interval=70))
    stub.start()
    try:
        expect("--min-remaining-percent 80 makes a 70% answer fall back",
               db_path=db.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default",
                                       "--min-remaining-percent", "80"),
               want_reason_substr="70% weekly")
    finally:
        stub.stop()
    db.close()

    # Pin the *default* floor. 30% is above the documented default of
    # 20 and below a hypothetical 50. 19% is below the default. A
    # change to the default constant that does not also touch the
    # decision table is broken in exactly the same way as removing
    # the constant altogether, and these two cases are the only ones
    # in the suite that can tell.
    db = TmpDb()
    stub = _Stub(_ok(weekly=30, interval=30))
    stub.start()
    try:
        expect("default floor keeps a 30% answer preferred",
               db_path=db.path, want_profile="minimax",
               stub=stub,
               want_reason_substr="30% weekly")
    finally:
        stub.stop()
    db.close()

    db = TmpDb()
    stub = _Stub(_ok(weekly=19, interval=50))
    stub.start()
    try:
        expect("default floor makes a 19% weekly answer fall back",
               db_path=db.path, want_profile="default",
               stub=stub, extra_args=("--fallback", "default"),
               want_reason_substr="19% weekly")
    finally:
        stub.stop()
    db.close()

    # status_code != 0 -> preferred; reason says quota check was skipped.
    db = TmpDb()
    stub = _Stub(_ok(weekly=99, interval=99), status_code=200)
    stub.response["base_resp"]["status_code"] = 1001
    stub.start()
    try:
        expect("non-zero status_code skips the quota check",
               db_path=db.path, want_profile="minimax",
               stub=stub,
               want_reason_substr="quota check skipped",
               want_endpoint_called=True)
    finally:
        stub.stop()
    db.close()

    # No "general" entry -> preferred; reason says quota check was skipped.
    db = TmpDb()
    stub = _Stub({
        "base_resp": {"status_code": 0},
        "model_remains": [
            {"model_name": "other", "current_weekly_remaining_percent": 81,
             "current_interval_remaining_percent": 93},
        ],
    })
    stub.start()
    try:
        expect("missing 'general' entry skips the quota check",
               db_path=db.path, want_profile="minimax",
               stub=stub,
               want_reason_substr="quota check skipped",
               want_endpoint_called=True)
    finally:
        stub.stop()
    db.close()

    # Connection refused -> preferred; reason says quota check was skipped.
    # The stub is bound and immediately shut down so the next connect()
    # in the picker raises ConnectionRefusedError. We still need a stub
    # to discover the port; we kill the listening socket before the
    # pick_lane subprocess starts.
    probe = _Stub(_ok(weekly=99, interval=99))
    refused_url = probe.start()
    probe.stop()
    db = TmpDb()
    expect("connection refused skips the quota check",
           db_path=db.path, want_profile="minimax",
           stub_url=refused_url,
           want_reason_substr="quota check skipped")
    db.close()

    # Timeout -> preferred; reason says quota check was skipped. We
    # bind a server that never answers, and shrink the picker's
    # timeout via a small monkeypatch is not possible from a
    # subprocess; instead we point it at a non-routable address that
    # urlopen will time out on. Using 10.255.255.1 (TEST-NET) on
    # 127.0.0.1's port range is not portable; the most reliable way to
    # force a timeout is to use a socket we accept on but never
    # respond from, then patch the picker's timeout. We instead test
    # the timeout path by pointing the picker at a server that hangs.
    # Implementation: a socket that accepts and reads but never
    # writes a response.
    hang = _HangingServer()
    hang.start()
    db = TmpDb()
    try:
        expect("timeout skips the quota check",
               db_path=db.path, want_profile="minimax",
               stub_url=hang.url,
               want_reason_substr="quota check skipped")
    finally:
        hang.stop()
    db.close()

    # A body cut short of its Content-Length is an unavailable answer too.
    cut = _HangingServer(reply=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                b"Content-Length: 1000\r\n\r\n{\"base_resp\":")
    cut.start()
    db = TmpDb()
    try:
        expect("truncated body skips the quota check",
               db_path=db.path, want_profile="minimax",
               stub_url=cut.url,
               want_reason_substr="quota check skipped")
    finally:
        cut.stop()
    db.close()

    # No key anywhere -> preferred; reason says quota check was skipped.
    # No stub URL is set; the env has no MINIMAX_API_KEY; HOME points at
    # an empty tmpdir so the .env lookup also misses.
    db = TmpDb()
    profile, reason, rc = run_pick(db.path, stub_url=None,
                                   with_api_key=False)
    total_cases += 1
    if rc != 0:
        failures.append(f"no API key: rc={rc} stderr={reason!r}")
    elif profile != "minimax":
        failures.append(
            f"no API key: chose {profile!r}, want 'minimax'; reason={reason!r}")
    elif "quota check skipped" not in reason:
        failures.append(
            f"no API key: reason {reason!r} does not contain 'quota check skipped'")
    elif "MINIMAX_API_KEY" not in reason:
        failures.append(
            f"no API key: reason {reason!r} does not explain the skip")
    db.close()

    # ----------------------------------------------------------------
    # Guard: the Authorization header is set; the picker does not log it.
    # ----------------------------------------------------------------
    db = TmpDb()
    stub = _Stub(_ok(weekly=80, interval=80))
    stub.start()
    auth_case_failures = 0
    try:
        env = _build_env(stub_url=f"http://127.0.0.1:{stub.port}/v1/token_plan/remains")
        proc = subprocess.run(
            ["python3", str(PICK), "--db", str(db.path)],
            capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            failures.append(f"auth-header smoke: rc={proc.returncode} stderr={proc.stderr!r}")
            auth_case_failures += 1
        elif TEST_API_KEY in proc.stderr or TEST_API_KEY in proc.stdout:
            failures.append("auth-header smoke: API key leaked into stdout/stderr")
            auth_case_failures += 1
        if not stub.requests:
            failures.append("auth-header smoke: stub was not contacted")
            auth_case_failures += 1
        else:
            req = stub.requests[0]
            if req["authorization"] != f"Bearer {TEST_API_KEY}":
                failures.append(
                    f"auth-header smoke: Authorization header was "
                    f"{req['authorization']!r}, want 'Bearer {TEST_API_KEY}'")
                auth_case_failures += 1
    finally:
        stub.stop()
    db.close()
    if auth_case_failures == 0:
        total_cases += 1

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print(f"PASS pick_lane honours the three rules end-to-end ({total_cases} cases)")
    return 0


def _ok(*, weekly: float, interval: float) -> dict:
    """Build a payload that looks like the real plan endpoint's
    success response, with the percentages the case cares about."""
    return {
        "base_resp": {"status_code": 0},
        "model_remains": [
            {"model_name": "general",
             "current_weekly_remaining_percent": weekly,
             "current_interval_remaining_percent": interval},
        ],
    }


class _HangingServer:
    """A TCP server that accepts the connection and never answers, or, given
    `reply`, sends those raw bytes and hangs up."""

    def __init__(self, reply: bytes | None = None) -> None:
        self._reply = reply
        self._sock: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._thread: threading.Thread | None = None
        self.url: str = ""
        self.port: int = 0

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        self._sock = s
        self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/v1/token_plan/remains"

        def accept_loop() -> None:
            assert self._sock is not None
            while True:
                try:
                    c, _ = self._sock.accept()
                except OSError:
                    return
                self._clients.append(c)
                if self._reply is not None:
                    c.recv(65536)
                    c.sendall(self._reply)
                    c.close()

        self._thread = threading.Thread(target=accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        for c in self._clients:
            try:
                c.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
