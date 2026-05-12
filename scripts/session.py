# scripts/session.py
from pathlib import Path


_TEMPLATE = """\
## Task state
current-task: {current_task}
status: {status}
blocking-reason: {blocking_reason}

## Session close
last-session: {last_session}
accomplished: {accomplished}
next-action: {next_action}
"""

_KEYS = {
    "current-task": "current_task",
    "status": "status",
    "blocking-reason": "blocking_reason",
    "last-session": "last_session",
    "accomplished": "accomplished",
    "next-action": "next_action",
}


def write_session(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE.format(**data))


def read_session(path: Path) -> dict | None:
    if not path.exists():
        return None
    result = {}
    for line in path.read_text().splitlines():
        for prefix, key in _KEYS.items():
            if line.startswith(f"{prefix}:"):
                result[key] = line[len(prefix) + 1:].strip()
    return result


if __name__ == "__main__":
    import sys
    from datetime import date

    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    work_md = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".ai/work.md")

    if cmd == "read":
        data = read_session(work_md)
        if data:
            for k, v in data.items():
                print(f"{k}: {v}")
        else:
            print("No session state found.")
    elif cmd == "write":
        data = {
            "current_task": input("current-task: "),
            "status": input("status (in-progress/blocked/complete): "),
            "blocking_reason": input("blocking-reason (leave blank if none): "),
            "last_session": date.today().isoformat(),
            "accomplished": input("accomplished: "),
            "next_action": input("next-action: "),
        }
        write_session(work_md, data)
        print(f"Saved to {work_md}")
