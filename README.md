# Bird Audio Identifier

An always-on Raspberry Pi with a USB microphone that identifies wildlife by sound and logs it live. It records 15-second clips, runs them through the open-source BirdNET model, and posts confident detections to a web API that a dashboard reads.

<a href="https://copezetic.github.io/bird-audio-identifier/media/red-fox-radio.mp4"><img src="media/red-fox-radio.gif" width="420" alt="Red Fox Radio video"></a>

**[Watch the video with sound](https://copezetic.github.io/bird-audio-identifier/media/red-fox-radio.mp4).** This is Red Fox Radio, the dashboard this listener feeds, and its Logbook. Each bird the microphone hears lands in a painted scene, and the Blue Jay call in the video is the deck mic's own recording. Figures are from 2026-09-22. The owl count at the top of the Logbook is inflated by sirens, which is explained below.

I direct this work through Claude Code and own the design, testing and upkeep.

## What it shows

**Working with an AI model that gets things wrong.** The hard part was not getting detections. It was knowing which ones to trust.

- **Filtering the input.** Outdoors, hum and wind below 200 Hz held most of the signal's energy and buried the 2-4 kHz band where birdsong lives. A high-pass filter fixed it.
- **Two ways to pass.** A clip clears a high confidence bar on its own, or a species earns its way in by being heard repeatedly at lower confidence.
- **False positives.** A police siren's slide in pitch looks like a screech owl's call to the model. A tree crew's steady noise was logged as a swan. Repeated detections reward any noise that lasts for minutes, so the repeat rule is also what let those through.
- **Keeping a person in the loop.** No audio clip gets featured until a person has listened to it.

**Making it run unattended.** The listener used to go silent for days, and each time the evidence was gone. Now each layer has a guard:

| Failure | What happens |
| --- | --- |
| Mic stalls or drops off | Recording has a timeout. Repeated failures exit, and systemd restarts the listener. |
| Listener hangs | systemd watchdog kills and restarts it if no clip completes in 5 minutes. |
| Restarts don't help | Repeated restarts reboot the Pi, which brings the USB bus back. |
| Wi-Fi drops | `network-watchdog.sh` restarts Wi-Fi, then reboots if that fails. |
| Token rotated | The check-in reports the rejected token instead of failing silently. |
| Pi goes offline | The server notices the missing check-in and sends an alert. |

Every check-in carries a health report covering the mic, Wi-Fi signal, power, temperature, disk and recent errors, so an alert can tell "the Pi is off" from "the Pi is on and the mic is broken."

**Staying inside free limits.** Each post is a metered database write, so a species only re-posts after a cooldown and only uploads audio once a day. If the network is down, detections queue locally.

## Files

| File | Purpose |
| --- | --- |
| `listener.py` | Records, filters, classifies, applies the confidence rules and posts results |
| `health.py` | Health report, systemd watchdog and remote restart commands |
| `network-watchdog.sh` | Recovers Wi-Fi |
| `.env.example` | Settings. Real values never go in the repo. |

The server and dashboard are private and not included.
