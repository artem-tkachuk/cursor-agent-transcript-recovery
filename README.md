# Cursor Agent Transcript Recovery

Recover Cursor Agent chats that still exist as local transcript files but do not
show up in the Agent picker.

## Problem

Cursor stores Agent chat data in two layers:

- Raw transcript JSONL files:

```text
~/.cursor/projects/<workspace-slug>/agent-transcripts/<composer-id>/<composer-id>.jsonl
```

- Local sidebar/index state in SQLite:

```text
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
~/Library/Application Support/Cursor/User/workspaceStorage/<workspace-id>/state.vscdb
```

The transcript can still be present and individually openable, while the Agent
picker only shows one chat or none. In the recovered case, the old chats were
indexed to stale workspace identities even though the JSONL files were already
under the right project.

The important keys were:

- `composer.composerHeaders`
- `glass.localAgentProjectMembership.v1`
- workspace `composer.composerData`

## Critical Detail

Run the repair from macOS Terminal after quitting Cursor completely.

Running the patch from inside Cursor can be reverted because Cursor may rewrite
`composer.composerHeaders` from its in-memory state.

## Basic Usage

Clone or copy this recovery tool anywhere. The script does not need to live in
the workspace being fixed.

Run the script from the Cursor workspace whose chats you want to recover:

```bash
git clone <repo-with-this-tool> /tmp/cursor-agent-transcript-recovery
cd "/path/to/Cursor/workspace/to/fix"

python3 /tmp/cursor-agent-transcript-recovery/fix_cursor_agent_index.py
```

Or run it from any directory by passing the workspace explicitly:

```bash
python3 /tmp/cursor-agent-transcript-recovery/fix_cursor_agent_index.py \
  --workspace-path "/path/to/Cursor/workspace/to/fix"
```

Only add `--apply` after the dry run output looks correct and Cursor is fully
quit.

## Dry Run

```bash
cd "/path/to/workspace"
python3 "/path/to/cursor-agent-transcript-recovery/fix_cursor_agent_index.py"
```

The dry run reports:

- workspace path and detected workspace id
- transcript root
- number of transcript JSONLs
- missing composer headers
- stale workspace header mappings
- stale local Agent project memberships

## Apply

Quit Cursor:

```bash
osascript -e 'quit app "Cursor"'
sleep 5
```

Run the repair:

```bash
cd "/path/to/workspace"
python3 "/path/to/cursor-agent-transcript-recovery/fix_cursor_agent_index.py" --apply
open -a Cursor "/path/to/workspace"
```

The script refuses to apply while Cursor is open unless `--allow-running` is
passed. Avoid `--allow-running` unless you are debugging.

## Useful Overrides

If the transcript root cannot be auto-detected:

```bash
python3 fix_cursor_agent_index.py \
  --workspace-path "/path/to/workspace" \
  --transcript-root "$HOME/.cursor/projects/<workspace-slug>/agent-transcripts" \
  --apply
```

If the workspace id or local Agent project id cannot be auto-detected:

```bash
python3 fix_cursor_agent_index.py \
  --workspace-path "/path/to/workspace" \
  --workspace-id "<workspace-storage-id>" \
  --agent-project-id "<glass-local-agent-project-id>" \
  --apply
```

## Backups

Every `--apply` run backs up both SQLite DBs beside the originals:

```text
state.vscdb.backup-before-agent-reindex-YYYYMMDD-HHMMSS
```

To roll back, quit Cursor and restore the relevant backup files.

## Tests

Run the test suite from the tool directory:

```bash
cd "/path/to/cursor-agent-transcript-recovery"
python3 -m unittest discover -s tests
```

The tests use temporary fake Cursor directories and SQLite databases. They do
not read or modify real Cursor state.

## Example Recovery Result

In a real recovery run, the final successful repair was done externally from
Terminal after quitting Cursor. It patched:

- 17 transcript composer IDs
- 16 stale `composer.composerHeaders` workspace references
- 9 stale local Agent project memberships

After reopening Cursor, the old chats appeared in the Agent picker again.
