import urllib.request
import json
from datetime import datetime

def fetch_prs():
    url = "https://api.github.com/repos/bot523h/nexus-ai-agent/pulls?state=open&per_page=100"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())

def load_board():
    with open(".agents/board.json") as f:
        return json.load(f)

prs = fetch_prs()
board = load_board()

claims = board.get("claims", [])
next_work = board.get("next_work", [])
all_tasks = claims + next_work

# Map tasks by PR number
task_by_pr = {}
for t in all_tasks:
    task_id = t.get("task", "")
    if task_id.startswith("pr-repair-"):
        parts = task_id.split("-")
        if len(parts) >= 3 and parts[2].isdigit():
            pr_num = int(parts[2])
            task_by_pr[pr_num] = t

# We should also check for general tasks that reference PRs
for t in all_tasks:
    task_id = t.get("task", "")
    note = t.get("note", "")
    for pr in prs:
        num = pr["number"]
        if f"PR #{num}" in note or f"PR#{num}" in note or f"#{num}" in note:
            if num not in task_by_pr:
                task_by_pr[num] = t

inventory = []
for pr in prs:
    num = pr["number"]
    title = pr["title"]
    head_sha = pr["head"]["sha"]
    base_ref = pr["base"]["ref"]
    
    t = task_by_pr.get(num)
    if t:
        task_id = t.get("task")
        status = t.get("status")
        zone = t.get("zone")
        exclusive_paths = t.get("exclusive_paths", [])
        prereqs = t.get("prerequisites", [])
        owner = t.get("agent_branch") or t.get("owner_branch")
        ttl = t.get("ttl_hours")
    else:
        task_id = None
        status = None
        zone = None
        exclusive_paths = []
        prereqs = []
        owner = None
        ttl = None

    inventory.append({
        "pr_number": num,
        "title": title,
        "head_sha": head_sha,
        "base_branch": base_ref,
        "task_id": task_id,
        "task_status": status,
        "zone": zone,
        "exclusive_paths": exclusive_paths,
        "prerequisites": prereqs,
        "claim_owner": owner,
        "claim_expiry": ttl,
    })

print(json.dumps(inventory, indent=2))
