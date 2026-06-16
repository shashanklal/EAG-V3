# Computer Skill Task Write-Up (Primary OS)

This runbook documents the three real desktop tasks now supported by the
`computer` skill, implemented per the scan -> act -> verify discipline in
CUA_DRIVER_GUIDE.md.

## Task 1: Calculator deterministic hotkeys (Layer 2a)

Goal:
- Compute an arithmetic expression using deterministic keyboard actions.

Planner node metadata:
```json
{
  "skill": "computer",
  "inputs": [],
  "metadata": {
    "label": "calc",
    "task": "calculator_hotkeys",
    "expression": "25*4"
  }
}
```

Expected output signals:
- output.path = "deterministic"
- output.result = "100"
- output.trajectory_dir contains the recording trajectory

## Task 2: Electron app task (VS Code) - write Python CSV code

Goal:
- Open VS Code and type a Python snippet that reads a CSV file.

Planner node metadata:
```json
{
  "skill": "computer",
  "inputs": [],
  "metadata": {
    "label": "vscode",
    "task": "vscode_csv_code"
  }
}
```

Expected output signals:
- output.path = "deterministic"
- output.snippet contains `import csv` and `csv.DictReader`
- output.trajectory_dir contains the recording trajectory

## Task 3: Vision task - Google Maps distance (home -> office)

Goal:
- Open Google Maps directions and extract the displayed distance.

Planner node metadata:
```json
{
  "skill": "computer",
  "inputs": [],
  "metadata": {
    "label": "maps",
    "task": "maps_distance",
    "home": "Times Square, New York",
    "office": "JFK Airport, New York"
  }
}
```

Expected output signals:
- output.path = "a11y" (if semantic AX parse succeeds) or "vision"
- output.distance contains a value like `14 km` or `9.2 mi`
- output.screenshot present when Layer 3 is used
- output.trajectory_dir contains the recording trajectory

## Evidence and replay

For every `computer` run:
- recording starts with `start_recording`
- recording stops with `stop_recording`
- `output.trajectory_dir` stores the trajectory directory for evidence
- Flow summary prints the chosen path and trajectory
- Replay shows output.path and full output JSON per node
