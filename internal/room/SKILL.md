---
description: Use to create, join, list, or close tmux rooms (windows) within a workspace.
---

# room

> **Prerequisites**
> - Runtime: `tmux` on PATH; `libtmux` Python package installed (per requirements.txt)
> - Mode: Memex or Local (mode-symmetric — `workspace.py` dispatches via `backend.py` for any DB writes; tmux session state is mode-independent)
> - Required tables: none — pure tmux session management

Manages rooms (tmux windows) within a workspace. Rooms are created dynamically. Every workspace always has a main room.

## Commands

- `internal/room/SKILL.md` (`create`) — Create a new room in the current workspace
- `internal/room/SKILL.md` (`join`) — Switch to a room
- `internal/room/SKILL.md` (`list`) — List all rooms in the workspace
- `internal/room/SKILL.md` (`close`) — Close a room (not allowed for main)

## Procedure

### room:create
1. Ask: "Workspace name?" / "Room name (e.g. 'standup', 'auth-sprint', 'security-review')?"
2. Run: `python3 atelier/scripts/workspace.py room:create <workspace> <room_name>`
3. Confirm: "Room '[room_name]' created in workspace '[workspace]'."

### room:join
1. Ask: "Workspace name?" / "Room name?"
2. Run: `python3 atelier/scripts/workspace.py room:join <workspace> <room_name>`
3. Confirm: "Joined room '[room_name]'."

### room:list
1. Ask: "Workspace name?"
2. Run: `python3 atelier/scripts/workspace.py room:list <workspace>`
3. Display the list of rooms.

### room:close
1. Ask: "Workspace name?" / "Room name?"
2. Run: `python3 atelier/scripts/workspace.py room:close <workspace> <room_name>`
3. Confirm: "Room '[room_name]' closed."

## Hard rules
- The main room cannot be closed. Refuse with an explanation if the user tries.
- Room names must describe purpose (e.g. 'standup', 'auth-sprint'), not be a bare personal name. If a proposed name is ambiguous (e.g. a feature named after a founder, like 'ada-refactor'), ask the user to confirm intent before refusing.
