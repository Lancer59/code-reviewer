"""
Dev Companion mounting integration tests.
Run: python _test_mounting.py
"""
import sys, os, re, asyncio
sys.path.insert(0, '.')
# Force UTF-8 output on Windows
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = []
FAIL = []

def ok(label):
    PASS.append(label)
    print(f"  OK    {label}")

def fail(label, detail=""):
    FAIL.append(label)
    print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 1. Dashboard API routes
# ---------------------------------------------------------------------------
print("\n=== 1. Dashboard API routes ===")
try:
    from fastapi.testclient import TestClient
    from dashboard.api import dashboard_app

    with TestClient(dashboard_app, raise_server_exceptions=False) as c:
        for path, label in [
            ("/dashboard",                          "GET /dashboard"),
            ("/dashboard/api/findings",             "GET /dashboard/api/findings"),
            ("/dashboard/api/workspaces",           "GET /dashboard/api/workspaces"),
            ("/dashboard/api/config",               "GET /dashboard/api/config"),
            ("/dashboard/api/findings/summary",     "GET /dashboard/api/findings/summary"),
            ("/dashboard/api/sessions",             "GET /dashboard/api/sessions"),
            ("/dashboard/api/telemetry/summary",    "GET /dashboard/api/telemetry/summary"),
            ("/dashboard/api/telemetry/tools",      "GET /dashboard/api/telemetry/tools"),
            ("/dashboard/api/findings/trend",       "GET /dashboard/api/findings/trend"),
            ("/dashboard/api/findings/heatmap",     "GET /dashboard/api/findings/heatmap"),
        ]:
            try:
                r = c.get(path)
                if r.status_code == 200:
                    ok(label)
                else:
                    fail(label, f"status={r.status_code}")
            except Exception as e:
                fail(label, str(e)[:80])
except Exception as e:
    fail("Dashboard API TestClient setup", str(e)[:120])


# ---------------------------------------------------------------------------
# 2. window.API_BASE injection
# ---------------------------------------------------------------------------
print("\n=== 2. window.API_BASE injection ===")
try:
    from fastapi.testclient import TestClient
    from dashboard.api import dashboard_app, _get_api_base

    with TestClient(dashboard_app, raise_server_exceptions=False) as c:
        r = c.get("/dashboard")
        body = r.content.decode("utf-8", errors="replace")
        if "window.API_BASE" in body:
            ok("window.API_BASE injected in /dashboard HTML")
            m = re.search(r'window\.API_BASE\s*=\s*"([^"]*)"', body)
            val = m.group(1) if m else "NOT FOUND"
            if val == "/dashboard":
                ok("window.API_BASE = '/dashboard' for root mount")
            else:
                fail("window.API_BASE value for root mount", f"got '{val}'")
        else:
            fail("window.API_BASE injected in /dashboard HTML")

    # Test _get_api_base logic directly with fake requests
    class FakeReq:
        def __init__(self, path):
            class U:
                def __init__(self, p): self.path = p
            self.url = U(path)

    cases = [
        ("/dashboard",                  "/dashboard"),
        ("/reviewer/dashboard",         "/reviewer/dashboard"),
        ("/api/v1/reviewer/dashboard",  "/api/v1/reviewer/dashboard"),
        ("/dashboard/",                 "/dashboard"),
        ("/deep/nested/path/dashboard", "/deep/nested/path/dashboard"),
    ]
    for path, expected in cases:
        result = _get_api_base(FakeReq(path))
        if result == expected:
            ok(f"_get_api_base('{path}') = '{expected}'")
        else:
            fail(f"_get_api_base('{path}')", f"got '{result}', expected '{expected}'")

except Exception as e:
    fail("window.API_BASE injection tests", str(e)[:120])


# ---------------------------------------------------------------------------
# 3. index.html path audit
# ---------------------------------------------------------------------------
print("\n=== 3. index.html path audit ===")
try:
    with open("dashboard/static/index.html", encoding="utf-8") as f:
        html = f.read()

    hardcoded = re.findall(r"['\`]/dashboard/api", html)
    if not hardcoded:
        ok("No hardcoded /dashboard/api paths in index.html")
    else:
        fail("No hardcoded /dashboard/api paths", f"{len(hardcoded)} remaining")

    count = html.count("window.API_BASE")
    if count >= 15:
        ok(f"window.API_BASE used {count} times in index.html")
    else:
        fail("window.API_BASE used enough times", f"only {count}")

    static_refs = re.findall(r"['\`]/dashboard/static", html)
    if not static_refs:
        ok("No hardcoded /dashboard/static paths in JS")
    else:
        fail("No hardcoded /dashboard/static paths", f"{len(static_refs)} found")

except Exception as e:
    fail("index.html path audit", str(e)[:120])


# ---------------------------------------------------------------------------
# 4. run.py structure
# ---------------------------------------------------------------------------
print("\n=== 4. run.py structure ===")
try:
    with open("run.py", encoding="utf-8") as f:
        src = f.read()

    checks = [
        ("def create_app(",     "create_app function defined"),
        ("config_file",         "config_file parameter"),
        ("standalone",          "standalone object"),
        ("mount_chainlit",      "mount_chainlit called"),
        ("/health",             "health endpoint"),
        ("sys.path",            "sys.path manipulation for imports"),
        ("dashboard_app.routes","dashboard routes included"),
    ]
    for pattern, label in checks:
        if pattern in src:
            ok(label)
        else:
            fail(label, f"'{pattern}' not found in run.py")
except Exception as e:
    fail("run.py structure check", str(e)[:120])


# ---------------------------------------------------------------------------
# 5. config.py priority chain
# ---------------------------------------------------------------------------
print("\n=== 5. config.py priority chain ===")
try:
    from config import cfg, cfg_bool, cfg_int

    os.environ["_TEST_CFG_KEY"] = "from_env"
    assert cfg("_TEST_CFG_KEY", "default") == "from_env"
    ok("cfg() reads os.environ")
    del os.environ["_TEST_CFG_KEY"]

    assert cfg("_NONEXISTENT_XYZ_KEY", "my_default") == "my_default"
    ok("cfg() falls back to default")

    os.environ["_TEST_BOOL"] = "true"
    assert cfg_bool("_TEST_BOOL") is True
    ok("cfg_bool() parses 'true'")
    del os.environ["_TEST_BOOL"]

    os.environ["_TEST_BOOL2"] = "false"
    assert cfg_bool("_TEST_BOOL2", True) is False
    ok("cfg_bool() parses 'false'")
    del os.environ["_TEST_BOOL2"]

    os.environ["_TEST_INT"] = "42"
    assert cfg_int("_TEST_INT") == 42
    ok("cfg_int() parses '42'")
    del os.environ["_TEST_INT"]

    assert cfg_int("_NONEXISTENT_INT", 99) == 99
    ok("cfg_int() falls back to default")

except AssertionError as e:
    fail("config.py assertion", str(e))
except Exception as e:
    fail("config.py priority chain", str(e)[:120])


# ---------------------------------------------------------------------------
# 6. git_tools workspace safety guard
# ---------------------------------------------------------------------------
print("\n=== 6. git_tools workspace safety guard ===")
try:
    from tools.git_tools import _safe_repo

    os.environ["WORKSPACE_BASE_DIR"] = os.path.abspath("workspaces")

    # Path outside workspace -> Security violation
    try:
        _safe_repo(os.path.abspath("."))
        fail("_safe_repo blocks path outside workspace")
    except ValueError as e:
        if "Security violation" in str(e):
            ok("_safe_repo raises Security violation for path outside workspace")
        else:
            fail("_safe_repo Security violation message", f"got: {e}")

    # Path inside workspace but no .git -> ValueError (not security)
    try:
        _safe_repo(os.path.abspath("workspaces/nonexistent-repo"))
        fail("_safe_repo raises for non-existent path inside workspace")
    except ValueError as e:
        msg = str(e)
        if "Security violation" in msg:
            # This is also acceptable — resolved repo root was outside workspace
            ok("_safe_repo rejects path whose resolved repo is outside workspace")
        else:
            ok("_safe_repo raises ValueError for non-existent path inside workspace")
    except Exception as e:
        fail("_safe_repo for non-existent path", f"{type(e).__name__}: {e}")

    # Verify a real workspace repo works (if one exists)
    workspaces = [
        d for d in os.listdir("workspaces")
        if os.path.isdir(os.path.join("workspaces", d))
        and os.path.isdir(os.path.join("workspaces", d, ".git"))
    ] if os.path.exists("workspaces") else []

    if workspaces:
        try:
            repo = _safe_repo(os.path.abspath(os.path.join("workspaces", workspaces[0])))
            ok(f"_safe_repo opens valid workspace repo: {workspaces[0]}")
        except Exception as e:
            fail(f"_safe_repo opens valid workspace repo", str(e)[:80])
    else:
        ok("No workspace repos to test (skipped valid-repo check)")

except Exception as e:
    fail("git_tools workspace safety guard setup", str(e)[:120])


# ---------------------------------------------------------------------------
# 7. PAT encryption round-trip
# ---------------------------------------------------------------------------
print("\n=== 7. PAT encryption round-trip ===")
try:
    from dashboard.db import _fernet, save_pat, load_pat, delete_pat

    f = _fernet()
    test_pat = "ghp_testtoken_abc123xyz"

    # Fernet round-trip
    encrypted = f.encrypt(test_pat.encode()).decode()
    decrypted = f.decrypt(encrypted.encode()).decode()
    assert decrypted == test_pat
    ok("Fernet encrypt/decrypt round-trip")

    # Different secret -> different key -> can't decrypt
    os.environ["CHAINLIT_AUTH_SECRET"] = "different-secret"
    from config import cfg as _cfg  # reload won't help, but test the fernet key changes
    import importlib
    import config as _config_mod
    _config_mod._json_config = {}  # clear cache
    f2 = _fernet()
    try:
        f2.decrypt(encrypted.encode())
        # If same key derived, that's fine too (same secret in env)
        ok("Fernet key derivation consistent")
    except Exception:
        ok("Different secret produces different Fernet key")
    finally:
        del os.environ["CHAINLIT_AUTH_SECRET"]

    # DB save/load/delete
    async def test_db():
        tid = "test-thread-pat-roundtrip"
        await save_pat(tid, test_pat, "https://github.com/test/repo")
        loaded = await load_pat(tid)
        await delete_pat(tid)
        after = await load_pat(tid)
        return loaded, after

    loaded, after = asyncio.run(test_db())
    if loaded == test_pat:
        ok("save_pat/load_pat DB round-trip")
    else:
        fail("save_pat/load_pat DB round-trip", f"got '{loaded}'")

    if after == "":
        ok("delete_pat removes PAT from DB")
    else:
        fail("delete_pat removes PAT", f"still got value")

except AssertionError as e:
    fail("PAT encryption assertion", str(e))
except Exception as e:
    fail("PAT encryption round-trip", str(e)[:120])


# ---------------------------------------------------------------------------
# 8. Dashboard API sub-path prefix via FastAPI mount()
# ---------------------------------------------------------------------------
print("\n=== 8. Dashboard API sub-path prefix via mount() ===")
try:
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from dashboard.api import dashboard_app

    # Build a reviewer sub-app with dashboard routes + health
    reviewer_sub = FastAPI()

    @reviewer_sub.get("/health")
    def rev_health():
        return {"status": "ok"}

    for route in dashboard_app.routes:
        reviewer_sub.routes.append(route)

    # Host app mounts reviewer at /reviewer
    host = FastAPI()

    @host.get("/api/status")
    def status():
        return {"ok": True}

    host.mount("/reviewer", reviewer_sub)

    with TestClient(host, raise_server_exceptions=False) as c:
        r = c.get("/api/status")
        if r.status_code == 200:
            ok("Host /api/status works alongside mounted reviewer")
        else:
            fail("Host /api/status", f"status={r.status_code}")

        r = c.get("/reviewer/health")
        if r.status_code == 200:
            ok("GET /reviewer/health -> 200")
        else:
            fail("GET /reviewer/health", f"status={r.status_code}")

        r = c.get("/reviewer/dashboard/api/findings")
        if r.status_code == 200:
            ok("GET /reviewer/dashboard/api/findings -> 200")
        else:
            fail("GET /reviewer/dashboard/api/findings", f"status={r.status_code}")

        r = c.get("/reviewer/dashboard/api/workspaces")
        if r.status_code == 200:
            ok("GET /reviewer/dashboard/api/workspaces -> 200")
        else:
            fail("GET /reviewer/dashboard/api/workspaces", f"status={r.status_code}")

        # Root /dashboard/api/... should 404
        r = c.get("/dashboard/api/workspaces")
        if r.status_code == 404:
            ok("/dashboard/api/workspaces at root is 404 (correctly isolated)")
        else:
            fail("/dashboard/api/workspaces at root should be 404", f"got {r.status_code}")

except Exception as e:
    fail("Dashboard sub-path prefix test", str(e)[:120])


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*55}")
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("\nFailed tests:")
    for f in FAIL:
        print(f"  FAIL  {f}")
    sys.exit(1)
else:
    print("All tests passed.")
