# Agent commands

Pass these arguments after --json to the installed player. Indices start at 1;
paths and saved names are separate quoted arguments.

| Arguments | Behavior |
| --- | --- |
| playlist list [TEXT] | Return paths, titles, indices, and the current track; optionally filter by path or filename |
| playlist add PATH | Append a file or recursively scanned folder; skip duplicates |
| playlist remove N | Remove a queue entry; removing the current track selects its successor |
| playlist move FROM TO | Reorder entries without interrupting the current track |
| playlist play N | Select and play a queue entry |
| playlist clear | Empty the queue and stop playback |
| playlist shuffle | Shuffle the queue once, keeping the current audio playing |
| playlist repeat all / one / off | Set automatic advancement; next still changes tracks in repeat-one |
| playlist save NAME | Save the current queue and repeat mode |
| playlist saved | List saved names |
| playlist load NAME | Restore a queue; keep the current track if present, otherwise select the first paused |
| playlist delete NAME | Delete the named queue file, preserving audio files |
| eq / eq status | Read ten bands, enabled state, gains, and gain compensation |
| eq presets | List flat, bass, treble, and vocal curves |
| eq preset NAME | Apply a preset and enable EQ |
| eq set HZ DB | Set one band and enable EQ; Hz may be 1000 or 1k |
| eq curve DB1 ... DB10 | Set all ten gains and enable EQ |
| eq off / on | Bypass or re-enable EQ, preserving gains |
| eq reset | Enable a flat curve |

Bands are 31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, and 16000 Hz.
Gains must be finite and within -12 to +12 dB. Boosted curves apply automatic
gain compensation, so raising a band can reduce overall loudness. This is
not a peak limiter. Flat and disabled EQ preserve the original PCM.

Queue order, current position, volume, repeat mode, and EQ persist on exit.
Saved queues contain local file paths, not copied audio. Missing or duplicate
entries are skipped on load; an entirely unavailable saved queue is rejected.
