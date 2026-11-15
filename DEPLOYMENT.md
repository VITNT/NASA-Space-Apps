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

## Migrate an existing deployment to Luminite

Run the following in the VM's SSH terminal with the existing container name,
checkout path, and archive path. It tags the image the live container is already
using, moves the checkout and archive in place, and keeps the stopped container
for rollback. The site is briefly unavailable while the replacement starts.

```bash
(
  set -e
  read -r -p 'Existing container name: ' old_container
  read -r -p 'Existing checkout path: ' old_checkout
  read -r -p 'Existing archive path: ' old_archive
  cd /opt
  sudo test -d "$old_checkout/.git"
  sudo test -d "$old_archive/data"
  sudo test ! -e /opt/luminite
  sudo test ! -e /srv/luminite-data
  ! sudo docker container inspect luminite >/dev/null 2>&1 || exit 1
  ! sudo docker container inspect luminite-rollback >/dev/null 2>&1 || exit 1

  image=$(sudo docker container inspect --format '{{.Image}}' "$old_container")
  data_mount=$(sudo docker container inspect --format '{{range .Mounts}}{{if eq .Destination "/tmp/firms"}}{{.Source}}{{end}}{{end}}' "$old_container")
  test "$data_mount" = "$old_archive"
  maps=()
  if sudo test -f /etc/luminite-maps.env; then
    maps=(--env-file /etc/luminite-maps.env)
  fi

  sudo docker tag "$image" luminite
  sudo docker stop "$old_container"
  sudo docker rename "$old_container" luminite-rollback
  sudo mv "$old_checkout" /opt/luminite
  sudo mv "$old_archive" /srv/luminite-data
  sudo docker run -d --name luminite --restart unless-stopped \
    -p 80:7860 \
    --mount type=bind,source=/srv/luminite-data,target=/tmp/firms \
    --env FIRMS_DATASET_REPO=Plutonians/luminite-data \
    "${maps[@]}" \
    luminite
)
```

Once the homepage and `/health` work, remove the stopped backup with
`sudo docker rm luminite-rollback`. If the original image tag still exists,
remove it with `sudo docker image rm` followed by that tag.

To roll back after the directories have moved, stop and remove the new
container, move the directories back to their original paths, then rename
`luminite-rollback` back to the original container name and start it.
