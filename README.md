# Patchy

<img src="patchy/static/icon.svg" width="96" align="right" alt="Patchy logo">

**A live map of your AV system's signal flow.** Every device, port and route in one place. Dante routes and
Blackmagic Videohub crosspoints are read live from the network, and Patchy flags anything that has drifted from
what you documented.

- **Live Dante reads.** Patchy finds Dante devices, their channels and every subscription on its own.
- **Drift.** It shows where the live routing differs from the documented routing: a new route nobody documented, or a documented route that's gone.
- **Videohubs.** It reads labels and routing from Blackmagic Videohubs. A change on a locked output counts as drift.
- **Gear that isn't on the network:** analog, AES, MADI, SDI, HDMI, DMX and more. Add it by hand and patch it with cable labels.
- **Device library.** Pick a model (Yamaha DM7, Rio, Tio, TF, RUio16-D; Allen & Heath ME-U; Shure ULX-D; Dante Virtual Soundcard and Via…) and its inputs, outputs, Dante channels and option cards are filled in for you. This works offline: you can plan a system before the gear is on the network.
- **Patch table** for each room, with CSV export and print or PDF.
- **Works on phones and tablets.** You can add it to your home screen as an app.
- **Read-only toward your gear.** Patchy only listens. It never changes a Dante subscription, a Videohub route or any other setting.

## Download

Get the latest version from **[Releases](../../releases/latest)**:

| System | File | How to start it |
|---|---|---|
| Windows 10/11 | `Patchy-…-windows-x64.exe` | Double-click it. If Windows SmartScreen says it "protected your PC", click **More info → Run anyway**. The app isn't code-signed yet. |
| macOS 12+ (Apple silicon) | `Patchy-…-macos-arm64.zip` | Unzip it and move **Patchy** to Applications. The first time, right-click Patchy → **Open** → **Open**. It isn't notarized yet. Allow **Local Network** access when macOS asks. |
| Linux x64 | `Patchy-…-linux-x64.tar.gz` | Extract it and run `Patchy/Patchy`. Use `Patchy/Patchy --headless` on a machine with no desktop. |

Patchy opens a small window and your browser. Close the window to stop Patchy.

The computer running Patchy must be connected to your **Dante network**: the primary Dante network, or a
network that can see Dante's mDNS. If it has more than one network adapter, choose the Dante one in **Settings → Network adapter**.

## First run

1. Patchy starts reading Dante right away. The first read takes about a minute. Devices appear on the map and the
   **first three reads are accepted as documented**, so today's routing becomes your starting point.
2. Open **Settings** (⚙). You can:
   - name your site;
   - add **room rules**, e.g. `FOH => Main Hall`, which put new devices into rooms by their Dante name;
   - add **Videohubs**, one per line: `192.168.1.50, Main Videohub, Control Room`.
3. Add the gear Dante can't see (amps, speakers, analog boxes, video gear) with **+ Device**, give it ports, then
   **+** on a port to patch it.
4. **Arrange** lays each room out left to right by signal flow.

### Planning offline with the device library

1. **+ Device → Start from**: choose the model, and pick any option card (e.g. a PY64-D in a DM7, an NY64-D in a TF).
   The device gets all its local ports and its Dante channels, and you can patch them right away.
2. When the gear is on the network, open the device, pick its **Dante device** in Details and save. The live
   channels take over the placeholders by channel number, and your planned routes are checked against the live ones.
3. For a device Dante already found (e.g. a console), use **+ Ports from a model** to add its local inputs and
   outputs. Dante channels are skipped because they come from the network.

The library is `patchy/static/library.json`, built from manufacturers' spec sheets. Models with a note have a
number the spec sheet didn't fully confirm.

## Phones, tablets and other computers

By default, only the computer running Patchy can open it. To use it from other devices:

1. Turn on **Settings → Allow phones, tablets and other computers**.
2. Quit and start Patchy again.
3. Open the address shown in the Patchy window, e.g. `http://192.168.1.20:8760`.

Anyone on your network can then *view* the map. *Editing* from another device asks once for the **edit token**,
which Settings shows on the computer running Patchy.

## How drift works

- **Matches live**: documented and seen on the network.
- **Live, not documented** (amber): someone patched it in Dante Controller. Click ✓ to accept it.
- **Documented, not live** (red, dashed): the receiver no longer has that route. A route counts as gone only after
  the receiver returns its **full** channel table on **two reads in a row** without it, because mDNS is lossy.
- **Live error**: Dante reports a problem with the subscription, e.g. unresolved.
- **Videohub → Rerouted**: a *locked* output is now fed from a different input than documented. Unlocked outputs
  are shown but not counted, because they get re-routed during shows.

Devices are never removed automatically. Hide one from its details panel.

## Where your data lives

| System | Folder |
|---|---|
| Windows | `%APPDATA%\Patchy` |
| macOS | `~/Library/Application Support/Patchy` |
| Linux | `~/.local/share/patchy` |

- `patchy.db` is the whole map (SQLite). Back it up by copying it while Patchy is stopped.
- `settings.json` holds your settings and edit token.
- `patchy.log` is the log.
- **Portable mode:** create a folder named `patchy-data` next to the app, and Patchy keeps everything there instead.
- You can also start Patchy with `--data <folder>`.

## Run from source

Needs Python 3.10 or newer, with tkinter for the window.

```
pip install -r requirements.txt
python run.py                 # window + browser
python run.py --headless      # no window
python run.py sync            # one Dante read, then exit
```

## Build the apps

`python -m pip install -r requirements-build.txt && python build.py` builds the app for the system you're on into
`release/`.

GitHub Actions builds all three. Push a tag like `v0.1.0` and the **Build** workflow creates a Release with the
Windows, macOS and Linux downloads.

## How it works

- **Data model:** rooms → devices → ports → connections, in SQLite.
- **Front end:** vanilla JS, served by Python's standard-library HTTP server. No build step.
- **Dante:** read through [netaudio](https://github.com/chris-ritsen/network-audio-controller) (Unlicense), run as a
  child process. Yamaha consoles that also announce their control port on Dante are folded into the console's card,
  using a read-only Yamaha RCP `devinfo` query.
- **Videohub:** the Videohub Ethernet protocol on TCP 9990. Patchy reads the status dump the hub sends on connect,
  then disconnects. It sends nothing.

## Roadmap

- Network: switch port and VLAN per device
- More video: ATEM, NDI
- Lighting: sACN and Art-Net universes
- Floor pockets and tie lines
