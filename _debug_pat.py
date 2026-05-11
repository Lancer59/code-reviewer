"""
Run this after starting a session to check PAT storage.
Usage: python _debug_pat.py <thread_id>
"""
import asyncio, sys, os
sys.path.insert(0, '.')

async def main():
    from dashboard.db import load_pat, DB_PATH
    print("DB_PATH:", DB_PATH)
    print("DB exists:", os.path.exists(DB_PATH))

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if table exists
        tables = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='session_pats'")
        t = await tables.fetchone()
        print("session_pats table exists:", t is not None)

        if t:
            rows = await db.execute("SELECT thread_id, repo_url, created_at, length(token) as tlen FROM session_pats ORDER BY created_at DESC LIMIT 5")
            all_rows = await rows.fetchall()
            print("Stored PATs (%d):" % len(all_rows))
            for r in all_rows:
                print("  thread=%-20s  repo=%-40s  created=%s  token_len=%d" % (
                    str(r[0])[:20], str(r[1] or '')[:40], str(r[2])[:19], r[3] or 0
                ))

            if len(sys.argv) > 1:
                tid = sys.argv[1]
                pat = await load_pat(tid)
                print("\nload_pat('%s...'): %r" % (tid[:8], '***' if pat else '(empty)'))

    print("\nos.environ _SESSION_GIT_PAT:", repr(os.environ.get('_SESSION_GIT_PAT', 'NOT SET')))
    print("os.environ _SESSION_GIT_THREAD_ID:", repr(os.environ.get('_SESSION_GIT_THREAD_ID', 'NOT SET')))

asyncio.run(main())
