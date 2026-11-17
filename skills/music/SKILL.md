---
name: music
description: Control the Windows T3 Code Music Theme player when the user invokes /music or asks to play local music, manage its playlists, or adjust its equalizer.
---

# Music

Operate the installed T3 Code Music Theme player. The theme shows playback and
spectrum controls; playlist and EQ controls are available through the agent.
A bare /music request means show status.

## Entry point

In PowerShell, use the installed launcher with JSON output:

    $musicRoot = Join-Path $env:USERPROFILE '.t3\tools\t3-music'
    & (Join-Path $musicRoot 'music.ps1') --json status

The launcher reads python-path.txt and calls Python directly. Do not use
music.cmd: cmd.exe can reinterpret music filenames containing shell characters.
Use argument arrays or single-quoted PowerShell strings for paths; double an
apostrophe inside a single-quoted string. --json precedes the command.

In Git Bash, resolve the same interpreter:

    music_root="$HOME/.t3/tools/t3-music"
    music_python="$(cygpath -u "$(cat "$music_root/python-path.txt")")"
    "$music_python" "$music_root/t3music.py" --json status

For a custom installation, use the directory provided by the user. If the
player is missing, direct setup requests to the
[theme repository](https://github.com/RAKiQWQ/t3-code-music-theme).

## Operate

Check the JSON ok field and process exit code before reporting success. If the
player is stopped and the request needs playback or a mutation, run start and
retry the requested command once. Start restores the queue, position, volume,
and EQ with playback paused; issue play only when requested. Stop exits the
background process; pause retains it.

Read [commands.md](references/commands.md) for playlist or EQ operations.
Ordinary commands are play, pause, toggle, next, prev, seek, vol, open, and status.
Seek accepts seconds, m:ss, or a percentage. Prev restarts a track after three
seconds; another prev moves to the previous track.

Open replaces the queue with the chosen folder's music and starts playback;
opening one file includes its sibling tracks. For a curated queue, use playlist
add instead. Query playlist indices before removing, moving, or choosing a track;
use playlist list TEXT to find matches in a large library without printing it all.
Do not adjust the library, queue, or volume beyond the musical request.

If the user asks to show the interface, use t3_theme_read and then t3_theme_set
to select mono-player. If it has not been published, pass the installed
mono-player.json as the theme. Playback works while another theme is selected.
Do not edit T3 settings.json directly.

Report the requested result briefly. Use title, playing, position_seconds,
track_index, track_count, volume, repeat, and eq from JSON as needed.
