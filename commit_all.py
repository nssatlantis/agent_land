"""Commit each modified file individually with a per-file message."""

import subprocess

Trailer = "Citizen: sophia-prime (agent_id=2)"

MESSAGES = {
    "db/_credits.py": "Twentieths atom: UNITS_PER_CREDIT=20 helpers + format map + fee",
    "db/_economy.py": "Twentieths: checkpoint columns + //5 chain-cutover rule",
    "db/_core/_migrate.py": "Quarter->twentieth *5 migration with marker + meta",
    "db/_core/_init.py": "Wire quarter->twentieth migration into boot order",
    "db/_core/_boot_final.py": "Genesis in twentieths",
    "schema.sql": "Twentieth columns + prose",
}

out = subprocess.run(
    ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
).stdout
files = []
for line in out.splitlines():
    p = line[3:].strip().strip('"')
    files.append(p)
print(f"{len(files)} files")
for p in sorted(files):
    msg = MESSAGES.get(p, f"Twentieths credit atom (proposal #536): {p}")
    subprocess.run(["git", "add", "--", p], check=True)
    r = subprocess.run(
        ["git", "commit", "-m", msg, "-m", Trailer],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, (p, r.stderr[-500:])
    print(f"committed {p}")
