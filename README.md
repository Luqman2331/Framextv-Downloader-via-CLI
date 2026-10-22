# framextvdownloader

This tool downloads videos from framextv.cc.

## What it does

- It downloads movies and TV shows.
- First, you pick the video quality.
- Then, you pick the subtitles.
- Subtitles are saved as separate `.srt` files.
- The subtitles are NOT inside the video file.
- It works with `/watch/` links and `/movie/` links. Example: https://framextv.cc/watch/1101412 and https://framextv.cc/movie/1101412
- For TV shows, use `/watch/<id>/<season>/<episode>`.
- If a download stops, you can run it again. It will continue.
- After a download finishes, the video pieces (`.ts` files) are deleted by themselves. Use `--no-clean` to keep them.
- If a video part fails, the tool will try again by itself.

## What you need

- Python 3
- ffmpeg (it must be in your PATH)
- These Python packages:

```bash
pip install requests textual
```

## How to use it (easy way)

Run this command:

```bash
python3 framextvdownload.py
```

Then:
1. Paste a framextv link.
2. Pick a quality.
3. Pick subtitles.
4. Wait for the download to finish.

The video will be in a folder called `dl_framextv_<id>/`.

## How to use it (command line)

Movie:

```bash
python3 framextvdownload.py "https://framextv.cc/watch/962697" --quality 1080
```

TV show:

```bash
python3 framextvdownload.py "https://framextv.cc/watch/335032/1/1" --quality 720
```

Pick subtitles without asking:

```bash
python3 framextvdownload.py "https://framextv.cc/watch/962697" --quality 1080 --subs ms,en,id
python3 framextvdownload.py URL --subs all
python3 framextvdownload.py URL --no-subs
```

## Run it on another computer

Linux:

```bash
cd ~/Downloads
sudo xbps-install -S ffmpeg
pip install requests textual
python3 framextvdownload.py
```

Windows:

```cmd
cd /d "%USERPROFILE%\Downloads"
winget install -e --id Gyan.FFmpeg
pip install requests textual
python framextvdownload.py
```

Note for Windows: after you install ffmpeg, close the terminal and open a new one. Then run the tool.
