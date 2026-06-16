# Agentic Arc S10 - Architecture, Query Runs, Cascade Behavior, and Failures

This README summarizes the current computer-skill architecture 

## 1) Five-Layer Architecture

The computer skill uses a layered cascade with verification and fallback:

1. Layer 1 - Extract / Plan
- Build a structured task plan from node metadata/query intent.
- For maps tasks, route parameters are resolved from explicit metadata or parsed from free-text instructions.

2. Layer 2a - Deterministic Actions
- Execute known, repeatable UI sequences without LLM-in-the-loop action generation.
- Examples:
  - Calculator: type expression and read result.
  - VS Code: File -> New File flow, then type snippet.

3. Layer 2b - Accessibility Tree (AX)
- Read `get_window_state(capture_mode="ax")` and attempt semantic extraction.
- Used as the workhorse when actionable text exists in the accessibility tree.

4. Layer 3 - Vision Fallback
- If AX is missing/insufficient, capture a screenshot and use vision extraction.
- Screenshot capture now prioritizes the active window region to increase signal quality.

5. Layer 4 - Verify/Recover Envelope
- Verify expected artifacts/outcomes and return structured success/failure.
- If downstream formatting is empty, orchestrator fallback now generates a deterministic completion summary.

## 2) Three Session Queries and Results

### Session A: s8-1fd50abb (Calculator)
Source files:
- `code/state/sessions/s8-1fd50abb/query.txt`
- `code/state/sessions/s8-1fd50abb/graph.json`

Query:
- Use computer skill to calculate `(25*4)/5` and confirm deterministic layer.

Observed result:
- Task: `calculator_hotkeys`
- Path: `deterministic`
- Result: `20`
- Actions: 2 turns (`type_text`, `get_window_state`)
- Formatter output: valid completion summary

### Session B: s8-764f9b7e (Google Maps Delhi -> Mumbai)
Source files:
- `code/state/sessions/s8-764f9b7e/query.txt`
- `code/state/sessions/s8-764f9b7e/graph.json`

Query:
- Open Chrome, use Google Maps, find travel options from Delhi to Mumbai.

Observed result:
- Task: `maps_distance`
- Path: `vision`
- Extracted distance: `1,390 km`
- URL: `https://www.google.com/maps/dir/Delhi/Mumbai`
- Actions: 5 turns (`hotkey`, `type_text`, `press_key`, AX read, vision read)
- Formatter output: valid completion summary

### Session C: s8-d2ceb4ed (VS Code CSV Code)
Source files:
- `code/state/sessions/s8-d2ceb4ed/query.txt`
- `code/state/sessions/s8-d2ceb4ed/graph.json`

Query:
- Open VS Code, create new Python file, write CSV-reading code, confirm deterministic layer, fix indentation issues if any.

Observed result:
- Task: `vscode_csv_code`
- Path: `deterministic`
- Snippet typed: CSV reader function with valid indentation and `if __name__ == '__main__':` block.
- Actions: 4 turns
  - Turn 1: `hotkey` (`alt+f`)
  - Turn 2: `press_key` (`n`) -> File -> New File behavior
  - Turn 3: `type_text`
  - Turn 4: `get_window_state`
- Formatter output in this session: empty object (`{}`)
- Orchestrator now includes fallback summarization to avoid empty final text in this case.

## 3) Cascade Decisions (Why Each Layer Was Chosen)

Across the three sessions:

- Calculator (`s8-1fd50abb`):
  - Decision: stop at Layer 2a deterministic.
  - Reason: expression execution is fully scriptable and verifiable.

- Maps (`s8-764f9b7e`):
  - Decision: Layer 2b attempted, then escalated to Layer 3 vision.
  - Reason: AX did not provide reliable route-distance text; vision successfully extracted distance.

- VS Code (`s8-d2ceb4ed`):
  - Decision: stop at Layer 2a deterministic.
  - Reason: creation and typing flow is deterministic and verifiable via AX query for `import csv`.

## 4) Failure Modes Encountered and Current Handling

1. Empty formatter output (`FINAL: {}`)
- Seen in session `s8-d2ceb4ed`.
- Impact: user-facing final answer was blank/placeholder.
- Mitigation: flow fallback now rejects non-meaningful formatter payloads (`{}`, `[]`, `null`, empty string) and synthesizes a deterministic completion summary from completed skill outputs.

2. AX insufficiency for map distances
- Seen in maps runs where UIA/AX tree lacked usable route text.
- Impact: Layer 2b could not confidently extract distance.
- Mitigation: enforced Layer 3 vision fallback with screenshot capture and structured vision extraction.

3. Deterministic intent mismatch for VS Code new file flow
- Earlier behavior used `Ctrl+N` directly; expectation was explicit File -> New File behavior.
- Impact: action trace did not match expected menu-driven sequence.
- Mitigation: deterministic VS Code flow now does `Alt+F` then `N` before typing.

4. Browser window visibility affecting vision reliability
- Some runs showed poor screenshot framing due to window state changes.
- Mitigation: maximize-window handling plus window-focused screenshot capture to improve visual extraction reliability.

## 5) Practical Notes

- Logs:
  - `logs/s8-1fd50abb.log`
  - `logs/s8-764f9b7e.log`
  - `logs/s8-d2ceb4ed.log`

- Session graph data:
  - `code/state/sessions/<session_id>/graph.json`

- Query source:
  - `code/state/sessions/<session_id>/query.txt`


