import subprocess, pathlib, os, sys

branches = [
    "proposal/sophia-prime/20260828-044459",
    "proposal/sophia-prime/20260828-044724",
    "proposal/sophia-prime/20260828-044810",
    "proposal/sophia-prime/20260828-044930",
]


def run(cmd, cwd=r"S:\New folder\AgentLand_Agent2_Gemini\repo"):
    print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        print(f"FAILED {result.returncode}")
        # don't exit, continue
    return result


for br in branches:
    print("\n" + "=" * 80)
    print(f"Processing {br}")
    run(f"git checkout {br}")
    # try merge
    res = run("git merge origin/main --no-commit")
    # Check if merge had conflicts? If so, print and abort?
    # Check status
    run("git status --porcelain | head -n 20")
    # Run ruff format
    run("ruff format .")
    run("ruff check .")
    # Check diff after format
    run("git diff --stat")
    # If any changes, add and commit
    # Check if there are staged changes from merge plus unstaged from format
    status = subprocess.run(
        "git status --porcelain",
        shell=True,
        cwd=r"S:\New folder\AgentLand_Agent2_Gemini\repo",
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        # There is merge in progress, need to add all and commit
        run("git add -A")
        run("git diff --cached --stat")
        run("python tests/run_all.py")
        run('git commit -m "merge main into {} (format)"'.format(br))
        run("git push")
    else:
        # No merge needed? But we did merge, so if status empty, maybe already up to date?
        print("No changes, already clean")
        # Ensure we commit merge if in merging state
        # If still merging, need to commit
        # Check if .git/MERGE_HEAD exists
        if pathlib.Path(
            r"S:\New folder\AgentLand_Agent2_Gemini\repo\.git\MERGE_HEAD"
        ).exists():
            run('git commit -m "merge main into {}"'.format(br))
            run("git push")
