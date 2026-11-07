# re-encore

A small docker container to re-encode your media library and save space on your home server

- Based on `ab-av1`, uses `--min-vmaf` to ensure your encodes are up to par with your quality standards
- Support for NVIDIA Hardware encoding

## Quick start

- set `MEDIA_DIR` in `.env` (copy the template first: `cp .env.example .env`).
It mounts at the same path inside the container, so the paths in
`data/config.json` work unchanged, and

```bash
make up
# Dashboard: http://localhost:7860
```

- ensure your host user is UID/GID 1000 (otherwise build with `PUID=$(id -u) PGID=$(id -g) docker compose build`).

`make up` auto-detects your hardware: NVIDIA GPU present

- it ensures the NVIDIA Container Toolkit is installed (asks for your sudo password once)
  - Plain `docker compose up` on a host without the toolkit fails with *"could not select device driver"*. Use `make up` or force CPU mode as shown below.
- uses NVENC

if no NVIDIA GPU is detected, steers to CPU (SVT-AV1) fallback, automatically.
Raw `docker compose` commands also work : see *Operations*.

On **first launch**, open the *Settings & Folders* tab and set the watched
folders and processed directory (the processed directory is created when you
save settings).

## Using the GPU (NVENC)

GPU encoding is the default. `make up` checks for an NVIDIA GPU
(`nvidia-smi -L`) and a Docker runtime that can expose it. If the GPU is
there but the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
is not, `make up` installs it first. One-time, asks for your sudo password,
then restarts Docker and continues in GPU mode. (`make gpu-setup` does just
the toolkit install.)

in case you need to verify NVENC is inside the running container:

```bash
make gpu-test
```

Then set the encoder flags in the web UI (Settings & Folders tab), e.g.
`--preset p7 -e av1_nvenc`. Without a GPU, use CPU SVT-AV1 instead: `--preset 6`.

## Force CPU mode

if, for whatever reason, you want to force re-encore to use your CPU, you can run :

`docker compose -f docker-compose.yml -f docker-compose.nogpu.yml up -d --build`.

## Operations

```bash
make up        # build & start (auto-detects GPU; see Quick start)
make logs      # follow the app/encode logs
make restart   # restart (state preserved in ./data)
make down      # stop & remove container (state preserved)
make status    # container status + healthcheck state (updates every 30s)
make gpu-test  # NVENC test inside the container (Troubleshooting)

# Plain compose commands work too; add `-f docker-compose.nogpu.yml`
# to force CPU mode (this is what `make` does when no GPU is found):
docker compose up -d --build
docker compose run --rm re-encore sh -c 'ab-av1 --version; ffmpeg -version | head -1'
```

- **Switch to the legacy UI**: set `APP_FILE=re-encore.py` under `environment:` in compose.
- **Back up or delete data**: `./data/` it holds your config, encode history, and queue state.
- **Bump pinned versions**: update the `ARG` values at the top of the
  [Dockerfile](Dockerfile) (ab-av1 version/sha, ffmpeg URL/sha) and
  [requirements.txt](requirements.txt), then rebuild.

## Troubleshooting tips

| Issue | Fix |
|---|---|
| `failed to find suitable crf` / exit 1 in logs | The file was already encoded efficiently. enable *Auto-Move/Copy* in Settings, or just ignore |
| `av1_nvenc ... cannot load libnvidia-encode` | GPU not exposed: check driver + toolkit and that `gpus: all` is set in compose (`docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu22.04 nvidia-smi`) |
| Queue never fills | Watched folder path inside container doesn't exist or is mounted read-only; check the *Settings & Folders* tab paths |
| Queue never fills (2) | The **processed directory must exist** before the watcher scans. Make sure you created it once via *Save Settings* in the UI (the app only creates its subfolders) |
| `Permission denied` writing outputs | Container UID doesn't match owner of the mounted media dir. Rebuild with matching `PUID`/`PGID` (documented how to retrieve the correct ones in the example .env file ) |
