---
description: Use to create, join, list, or close tmux rooms (windows) within a workspace.
user-invocable: false
---

# room

Manages rooms (tmux windows) within a workspace. Rooms are created dynamically. Every workspace always has a main room.

## Commands

- `room:create` — Create a new room in the current workspace
- `room:join` — Switch to a room
- `room:list` — List all rooms in the workspace
- `room:close` — Close a room (not allowed for main)

## Procedure

### room:create
1. Ask: "Workspace name?" / "Room name (e.g. 'standup', 'auth-sprint', 'security-review')?"
2. Run: `python atelier/scripts/workspace.py room:create <workspace> <room_name>`
3. Confirm: "Room '[room_name]' created in workspace '[workspace]'."

### room:join
1. Ask: "Workspace name?" / "Room name?"
2. Run: `python atelier/scripts/workspace.py room:join <workspace> <room_name>`
3. Confirm: "Joined room '[room_name]'."

### room:list
1. Ask: "Workspace name?"
2. Run: `python atelier/scripts/workspace.py room:list <workspace>`
3. Display the list of rooms.

### room:close
1. Ask: "Workspace name?" / "Room name?"
2. Run: `python atelier/scripts/workspace.py room:close <workspace> <room_name>`
3. Confirm: "Room '[room_name]' closed."

## Hard rules
- The main room cannot be closed. Refuse with an explanation if the user tries.
- Room names should describe purpose (e.g. 'standup', 'auth-sprint') not people.
