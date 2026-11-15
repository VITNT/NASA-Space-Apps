# Deploy Luminite on a VM

Use `/opt/luminite` for the Git checkout, `/srv/luminite-data` for the cached
Hugging Face dataset, and `luminite` for the Docker image and container. The
dataset is `Plutonians/luminite-data`. Keep the Maps browser key in
`/etc/luminite-maps.env` if Google 3D is enabled.

From the VM checkout, build the image before replacing the running container:

```bash
cd /opt/luminite
sudo git pull --ff-only
sudo docker build -t luminite .
```

When creating or replacing the container, use the following settings. Omit
the `--env-file` line if Google 3D is not configured. Create `/srv/luminite-data`
and make it writable by UID 1000 on a fresh install.

```bash
sudo docker run -d --name luminite --restart unless-stopped \
  -p 80:7860 \
  --mount type=bind,source=/srv/luminite-data,target=/tmp/firms \
  --env FIRMS_DATASET_REPO=Plutonians/luminite-data \
  --env-file /etc/luminite-maps.env \
  luminite
```

Check `curl -fsS http://127.0.0.1/health` after startup. The existing HTTPS
proxy can keep using the same VM address and port.

## Rename an existing Burnline deployment

Run the following in the VM's SSH terminal if it still uses `burnline` for the
container and the two old directories. It tags the image the live container is
already using, moves the archive in place, and keeps the stopped container for
rollback. The site is briefly unavailable while the replacement starts.

```bash
(
  set -e
  cd /opt
  sudo test -d /opt/burnline/.git
  sudo test -d /srv/burnline-data/data
  sudo test ! -e /opt/luminite
  sudo test ! -e /srv/luminite-data
  ! sudo docker container inspect luminite >/dev/null 2>&1 || exit 1
  ! sudo docker container inspect luminite-rollback >/dev/null 2>&1 || exit 1

  image=$(sudo docker inspect --format '{{.Image}}' burnline)
  data_mount=$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/tmp/firms"}}{{.Source}}{{end}}{{end}}' burnline)
  test "$data_mount" = /srv/burnline-data
  maps=()
  if sudo test -f /etc/luminite-maps.env; then
    maps=(--env-file /etc/luminite-maps.env)
  fi

  sudo docker tag "$image" luminite
  sudo docker stop burnline
  sudo docker rename burnline luminite-rollback
  sudo mv /opt/burnline /opt/luminite
  sudo mv /srv/burnline-data /srv/luminite-data
  sudo docker run -d --name luminite --restart unless-stopped \
    -p 80:7860 \
    --mount type=bind,source=/srv/luminite-data,target=/tmp/firms \
    --env FIRMS_DATASET_REPO=Plutonians/luminite-data \
    "${maps[@]}" \
    luminite
)
```

Once the homepage and `/health` work, remove the stopped backup with
`sudo docker rm luminite-rollback`. If the old `burnline:latest` image tag
still exists, remove it with `sudo docker image rm burnline:latest`.

To roll back after the directories have moved, stop and remove the new
container, move the directories back to their original paths, then rename
`luminite-rollback` to `burnline` and start it.
