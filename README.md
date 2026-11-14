# CLI Audio Player

Full-screen terminal audio player with a scrollable browser and now-playing pane.

## Requirements

- Python 3
- `mpv` installed and available on `PATH` for play/pause support
- `ffplay` is supported as a fallback, but pause will not work with it
- Supported formats include `.mp3` and `.m4a`
- A UTF-8 terminal locale for non-English filenames, metadata, and search input

## Run

From a checkout:

```bash
./mp3player
```

To install the command for your user:

```bash
install -Dm755 mp3player "$HOME/.local/bin/mp3player"
```

Then run:

```bash
mp3player [--loop]
```

By default, it scans `~/Media/music`.
Track metadata is loaded in the background so the player opens faster on larger libraries.
The UI and search now handle Unicode track names and metadata directly.

To use another folder:

```bash
mp3player /path/to/your/music
```

To keep replaying the current track when it ends:

```bash
mp3player --loop
```

## Controls

- `j` / `k` or arrow keys to move through the list
- `Enter` to play the selected track
- `Space` to pause or resume playback
- `n` / `p` for next or previous track
- `l` to toggle looping for the current track
- `x` to stop playback
- `/` to search by title, artist, album, or file name
- `Esc` while searching to cancel and restore the previous filter
- `q` to quit
