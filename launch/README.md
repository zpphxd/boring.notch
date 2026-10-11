# Launch video

A 61-second 1080p launch video, built as code so you can change any word and re-render.

| File | What |
|---|---|
| `timeline.json` | The script: every text bubble, when it appears, the outreach examples, scene timings and sound cues. Edit copy here. |
| `video.html` | The animation. Open it in Chrome with `?t=12.5` to see any moment. |
| `render.mjs` | Renders the video frame by frame (deterministic, so there are no dropped frames) and pipes the frames to ffmpeg. |
| `audio.py` | Synthesizes the soundtrack from the same timeline: message pops, keyboard taps, the riser and beat drop, printer ticks. Everything is generated, with no samples or licensed music. |

## Re-render

```bash
npm i playwright            # once (uses Chromium)
python3 launch/audio.py launch/soundtrack.wav
node launch/render.mjs --out launch/silent.mp4                 # ~15 min at 1080p
node launch/render.mjs --preview 3,21,33,45,53 --dir /tmp      # quick stills
ffmpeg -i launch/silent.mp4 -i launch/soundtrack.wav -c:v copy -c:a aac -b:a 192k \
  -af loudnorm=I=-14:TP=-1:LRA=11 -shortest launch/bruno-launch.mp4
```

To use your own music instead, mix it under `soundtrack.wav`, or replace the music part of `audio.py`. The UI sounds are in the `sfx` buffer, so you can keep them.

## Notes

- The opening conversation is the script you wrote. Bruno's real engine produces the same flow: run `python -m bruno chat --outreach` with Claude configured. The live version recaps the price before placing the order, which the video skips for pacing.
- The data, outreach and kitchen scenes are labelled "Demo data". The numbers there show what the product does, not customer results.
