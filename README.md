## Creating an issue from LINE

The bot records a stretch of chat on demand and turns it into a GitHub issue.
In the group:

1. `ai start` — the bot starts recording
2. Send or forward every message and photo that belongs to the issue
3. `ai end` — everything between the two markers becomes one GitHub issue

`ai cancel` throws the recording away, `ai help` prints the steps. The command
has to be the whole message (an @-mention in front is fine), so "ai end of the
month" won't trigger anything. Only one recording runs per conversation at a
time; one left open is expired after `CAPTURE_SESSION_TTL_MINUTES` (default
120).

Venv Setup

```
python3 -m venv .venv
source .venv/bin/activate
```

To update Pip Packages
```
pip install requests          # installs into .venv only
pip freeze > requirements.txt # snapshot your deps
```