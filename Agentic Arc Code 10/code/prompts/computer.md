The Computer skill controls host desktop apps through cua-driver using a
five-layer execution contract.

Execution layers:
  Layer 1   plan and normalize a concrete computer task from metadata
  Layer 2a  deterministic hotkeys / scripted actions
  Layer 2b  semantic accessibility interpretation through V9 /v1/chat
  Layer 3   vision fallback through V9 /v1/vision
  Layer 4   verify/recover and return typed evidence

Required metadata:
  metadata.task must be one of:
    - calculator_hotkeys
    - vscode_csv_code
    - maps_distance

Optional metadata:
  expression (for calculator_hotkeys, default: 25*4)
  home, office (for maps_distance)

The skill always starts cua-driver recording via start_recording and stops it
at the end. It returns the trajectory directory in output.trajectory_dir.

Output contract:
  - output.path: layer used (deterministic | a11y | vision)
  - output.turns: number of driver calls
  - output.actions: driver actions performed
  - output.trajectory_dir: evidence path for replay
