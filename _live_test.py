import urllib.request, json, re, sys

base = 'http://localhost:8080'
ok_count = 0
fail_count = 0

def check(path, label, expect_api_base=None):
    global ok_count, fail_count
    try:
        r = urllib.request.urlopen(base + path, timeout=5)
        body = r.read().decode('utf-8', errors='replace')
        status = r.status
        if expect_api_base:
            m = re.search(r'window\.API_BASE\s*=\s*"([^"]+)"', body)
            val = m.group(1) if m else 'NOT FOUND'
            if val == expect_api_base:
                print(f'  OK    {label} ({status}) [API_BASE="{val}"]')
                ok_count += 1
            else:
                print(f'  FAIL  {label} - API_BASE="{val}", expected "{expect_api_base}"')
                fail_count += 1
        else:
            print(f'  OK    {label} ({status})')
            ok_count += 1
    except Exception as e:
        print(f'  FAIL  {label} - {e}')
        fail_count += 1

print('\n=== Host app routes ===')
check('/', 'GET /')
check('/api/status', 'GET /api/status')

print('\n=== Reviewer health ===')
check('/reviewer/health', 'GET /reviewer/health')

print('\n=== Dashboard HTML + API_BASE ===')
check('/reviewer/dashboard', 'GET /reviewer/dashboard', expect_api_base='/reviewer/dashboard')

print('\n=== Dashboard API routes ===')
check('/reviewer/dashboard/api/findings',           'GET /reviewer/dashboard/api/findings')
check('/reviewer/dashboard/api/workspaces',         'GET /reviewer/dashboard/api/workspaces')
check('/reviewer/dashboard/api/config',             'GET /reviewer/dashboard/api/config')
check('/reviewer/dashboard/api/findings/summary',   'GET /reviewer/dashboard/api/findings/summary')
check('/reviewer/dashboard/api/sessions',           'GET /reviewer/dashboard/api/sessions')
check('/reviewer/dashboard/api/telemetry/summary',  'GET /reviewer/dashboard/api/telemetry/summary')
check('/reviewer/dashboard/api/telemetry/tools',    'GET /reviewer/dashboard/api/telemetry/tools')
check('/reviewer/dashboard/api/findings/trend',     'GET /reviewer/dashboard/api/findings/trend')
check('/reviewer/dashboard/api/findings/heatmap',   'GET /reviewer/dashboard/api/findings/heatmap')

print('\n=== Isolation check (root /dashboard should 404) ===')
try:
    urllib.request.urlopen(base + '/dashboard/api/workspaces', timeout=5)
    print('  FAIL  /dashboard/api/workspaces at root should be 404 but got 200')
    fail_count += 1
except urllib.error.HTTPError as e:
    if e.code == 404:
        print(f'  OK    /dashboard/api/workspaces at root is 404 (correctly isolated)')
        ok_count += 1
    else:
        print(f'  FAIL  /dashboard/api/workspaces at root got {e.code}')
        fail_count += 1
except Exception as e:
    print(f'  FAIL  isolation check - {e}')
    fail_count += 1

print(f'\n{"="*50}')
print(f'PASSED: {ok_count}   FAILED: {fail_count}')
sys.exit(0 if fail_count == 0 else 1)
