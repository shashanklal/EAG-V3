# Discord Adapter — Test Results

**Suite:** `tests/channels/test_discord.py`
**Date:** 2026-06-25
**Result:** ✅ 7 passed in 0.39s

## Environment

- Platform: win32
- Python: 3.11.10
- pytest: 9.0.3
- pluggy: 1.6.0
- Plugins: anyio-4.13.0, asyncio-1.3.0 (mode=AUTO)

## Command

```pwsh
python -m pytest tests/channels/test_discord.py -v
```

## Test Outcomes

| # | Test | Result |
|---|------|--------|
| 1 | `test_on_message_owner_returns_valid_envelope` | PASSED |
| 2 | `test_on_message_stranger_is_untrusted` | PASSED |
| 3 | `test_send_emits_valid_wire_payload` | PASSED |
| 4 | `test_disconnect_is_handled` | PASSED |
| 5 | `test_rate_limit_propagates_429` | PASSED |
| 6 | `test_allowlist_silently_drops_stranger_in_public` | PASSED |
| 7 | `test_channel_specific_behaviour_mention_resolution` | PASSED |

## Raw Output

```text
============================= test session starts =============================
platform win32 -- Python 3.11.10, pytest-9.0.3, pluggy-1.6.0 -- C:\Program Files\Python311\python.exe
cachedir: .pytest_cache
rootdir: C:\Users\slal5\OneDrive - UHG\Work\4_VSCode\LLM Gateway\llm_gatewayV11
configfile: pyproject.toml
plugins: anyio-4.13.0, asyncio-1.3.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=session, asyncio_default_test_loop_scope=function
collecting ... collected 7 items

tests/channels/test_discord.py::test_on_message_owner_returns_valid_envelope PASSED [ 14%]
tests/channels/test_discord.py::test_on_message_stranger_is_untrusted PASSED [ 28%]
tests/channels/test_discord.py::test_send_emits_valid_wire_payload PASSED [ 42%]
tests/channels/test_discord.py::test_disconnect_is_handled PASSED        [ 57%]
tests/channels/test_discord.py::test_rate_limit_propagates_429 PASSED    [ 71%]
tests/channels/test_discord.py::test_allowlist_silently_drops_stranger_in_public PASSED [ 85%]
tests/channels/test_discord.py::test_channel_specific_behaviour_mention_resolution PASSED [100%]

============================== 7 passed in 0.39s ==============================
```

## Coverage Summary

The suite exercises both translation directions and the trust/allowlist logic:

- **Inbound translation** — Discord `MESSAGE_CREATE` dispatch frame → `ChannelMessage`
  (owner & stranger).
- **Trust-level assignment** — `owner_paired` / `untrusted` resolution on inbound messages.
- **Outbound translation** — `ChannelReply` → Discord `POST /channels/{id}/messages`
  body (`content` present, `tts` not enabled by default).
- **Resilience** — forced gateway disconnect handled without raising.
- **Rate limiting** — Discord 429 propagated to the caller.
- **Allowlist** — strangers in public channels silently dropped.
- **Mention resolution** — `<@id>` tokens resolved to handles via the mock's
  `get_user()` into `metadata["mentions"]`.
