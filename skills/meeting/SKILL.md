---
name: atelier:meeting
description: Use to create, read, update, delete, list, or search meetings — writes both a DB record and a markdown file.
---

# meeting

Manages meetings in Atelier. Each meeting creates both a DB record and a markdown file in `.ai/meetings/`.

## Commands

- `meeting:create` — Record a meeting
- `meeting:read` — Get meeting details and participants
- `meeting:update` — Update meeting summary or decisions
- `meeting:delete` — Remove meeting record and file
- `meeting:list` — List meetings by date or participant
- `meeting:search` — Search meetings by title, summary, or decisions

## Procedure

### meeting:create
1. Ask: "Meeting title?" / "Date (YYYY-MM-DD)?" / "Summary?" / "Decisions made?" / "Your agent ID?" / "Participant agent IDs? (comma-separated)"
2. Run: `python atelier/scripts/meetings.py create "<title>" "<date>" "<summary>" "<decisions>" "<created_by>" --meetings-dir .ai/meetings`
3. For each participant: `python atelier/scripts/meetings.py add-participant <meeting_id> <agent_id>`
4. Confirm: "Meeting recorded: [title] → .ai/meetings/[filename]"

### meeting:read
1. Ask: "Meeting ID?"
2. Run: `python atelier/scripts/meetings.py get <id>`
3. Run: `python atelier/scripts/meetings.py participants <id>`
4. Display meeting details and participant list.

### meeting:update
1. Ask: "Meeting ID?" / "What to update (summary / decisions / title / date)?"
2. Run: `python atelier/scripts/meetings.py update <id> [--summary "..."] [--decisions "..."] [--title "..."] [--date "..."]`
3. Confirm: "Meeting updated. Note: the markdown file is not automatically updated — update it manually if needed."

### meeting:delete
1. Ask: "Meeting ID? This removes both the DB record and the markdown file."
2. Run: `python atelier/scripts/meetings.py delete <id> --meetings-dir .ai/meetings`
3. Confirm: "Meeting deleted."

### meeting:list
1. Run: `python atelier/scripts/meetings.py list`
2. Display results as a table: id | title | date | filename

### meeting:search
1. Ask: "Search query?"
2. Run: `python atelier/scripts/meetings.py search "<query>"`
3. Display matching meetings as a table.

## Hard rules
- Always capture meeting knowledge to Memex via `ingest` after recording a meeting. Prompt the user: "Meeting recorded. Capture key insights to the knowledge base? (y/n)"
