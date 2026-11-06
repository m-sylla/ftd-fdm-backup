#!/usr/bin/env python3
"""
Trigger an on-demand FDM configuration backup, download it, and push it to
an FTP (or FTPS) server -- FDM itself has no remote-backup-destination option
at all (confirmed via Cisco's own docs: backups are local-only, stored on the
box under /ngfw/var/sf/backup/, non-durable -- purged under disk pressure and
wiped entirely on any non-hotfix upgrade; see README.md's backup section).
This script is this project's answer to that gap: it's meant to be run on a
schedule from a jump host/management server the customer controls (cron,
Scheduled Task, etc.) -- FDM can't run arbitrary scripts on-box.

*** NOTHING IN THIS SCRIPT HAS BEEN CONFIRMED AGAINST A REAL DEVICE. ***
Unlike this toolkit's other migrate_*.py scripts, every endpoint and field
name below comes from Cisco's own FDM REST API reference (developer.cisco.com)
and a Cisco Community backup/restore writeup, NOT a live test against this
project's FTD. Before trusting a scheduled/automated run, do a manual run
against the real box first and check each step against
https://<host>/#/api-explorer:

  - POST action/backup's body fields (name/description/backupLocation/
    encryptionKey/scheduleType). Cisco's own docs contradict themselves on
    scheduleType: the field description says the only allowed value is
    "IMMEDIATE", but Cisco's own example payload in the same reference uses
    "NONE". This script sends "IMMEDIATE" per the field description -- flip
    to "NONE" if the live API rejects it.
  - The response is documented to include a jobHistoryUuid, but the response
    schema itself isn't shown in Cisco's docs -- confirm the actual field
    name live (this script also falls back to a bare "id" if present).
  - The job-status path (jobs/backups/{id}) is inferred by analogy with the
    deployment-job path (jobs/deployments/{id}) that this project's own
    common/fdm_client.py:FdmClient.deploy() already uses successfully against
    the real device -- the backup-specific path itself is NOT independently
    confirmed. Cisco's docs also mention a "latest" alias for {id} that
    returns the most recent run of a job type; not used here but worth
    knowing if a jobHistoryUuid never comes back.
  - GET managedentity/archivedbackups returning an "archiveName" field per
    archived-backup entry, and GET action/downloadbackup/{archiveName}
    serving the actual file -- both from Cisco Community documentation, not
    independently verified here.
  - client.resolve_endpoint() (this project's live-spec confirmation helper)
    only checks POST operations in the device's own OpenAPI spec, so it's
    used below for the backup-trigger call but NOT for the GET-only job
    status / archived-backups / download calls -- those three use their
    hardcoded default paths directly. If any of them 404s, check
    /#/api-explorer by hand for the real path on your build.

Also worth deciding before relying on this for anything real: plain FTP
sends the backup file unencrypted over the network. Set backup.ftp.tls: true
in config.yaml for FTPS, and/or set encryption_key in backup_secrets.yaml so
the backup file itself is encrypted regardless of transport.

This folder (backup_automation/) is self-contained on purpose -- script,
config/secrets templates, requirements.txt, its own README, and its own copy
of the FDM client -- so it can be handed to the customer by itself, without
the rest of the asa-to-ftd-migration toolkit. See README.md in this same
folder for setup/usage. Run it from inside this folder:

Usage:
    cd backup_automation
    python backup_to_ftp.py --config config.yaml
    python backup_to_ftp.py --config config.yaml --keep-local
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from ftplib import FTP, FTP_TLS

import yaml

from common.fdm_client import load_config, build_client, FdmApiError


def load_backup_secrets(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return {k: v for k, v in data.items() if k != "_comment"}

BACKUP_ACTION_PATH = "action/backup"
BACKUP_JOB_PATH_TMPL = "jobs/backups/{job_id}"
ARCHIVED_BACKUPS_PATH = "managedentity/archivedbackups"
DOWNLOAD_BACKUP_PATH_TMPL = "action/downloadbackup/{archive_name}"


def trigger_backup(client, name, description, encryption_key):
    path = client.resolve_endpoint(BACKUP_ACTION_PATH, ["action", "backup"])
    body = {
        "type": "backupimmediate",
        "name": name,
        "description": description,
        "backupLocation": "DEFAULT",
        "scheduleType": "IMMEDIATE",
    }
    if encryption_key:
        body["encryptionKey"] = encryption_key
    else:
        print("  ! no encryption_key set in backup_secrets.yaml -- the backup file will be "
              "unencrypted (Cisco's own docs recommend setting one)")
    client.validate_body_fields(path, body)
    result = client.post(path, body)
    job_id = result.get("jobHistoryUuid") or result.get("id")
    if not job_id:
        raise FdmApiError(f"No jobHistoryUuid/id in backup-trigger response: {result}")
    return job_id


def wait_for_backup_job(client, job_id, poll_interval=10, timeout=900):
    path = BACKUP_JOB_PATH_TMPL.format(job_id=job_id)
    start = time.time()
    while time.time() - start < timeout:
        status = client.get(path)
        state = status.get("status") or status.get("state")
        print(f"  backup job state: {state}")
        if state in ("COMPLETED", "SUCCESS", "DEPLOYED"):
            return status
        if state in ("FAILED", "ERROR"):
            raise FdmApiError(f"Backup job failed: {status}")
        time.sleep(poll_interval)
    raise FdmApiError("Timed out waiting for backup job to finish")


def find_archive_name(client, backup_name):
    items = client.get_all_pages(ARCHIVED_BACKUPS_PATH)
    matches = [i for i in items if i.get("name") == backup_name]
    if not matches:
        if not items:
            raise FdmApiError("No archived backups found at all -- check "
                               f"{ARCHIVED_BACKUPS_PATH} manually")
        print(f"  ! no archived-backup entry named '{backup_name}' -- using the most "
              "recent entry instead, verify this is the right one")
        matches = [items[-1]]
    archive_name = matches[-1].get("archiveName")
    if not archive_name:
        raise FdmApiError(f"Matched archived-backup entry has no archiveName field: {matches[-1]}")
    return archive_name


def download_backup_file(client, archive_name, dest_path):
    path = DOWNLOAD_BACKUP_PATH_TMPL.format(archive_name=archive_name)
    url = client.base_url + path.lstrip("/")
    resp = client.session.get(url, verify=client.verify, timeout=300, stream=True)
    if resp.status_code != 200:
        raise FdmApiError(f"GET {path} -> {resp.status_code}: {resp.text[:500]}")
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    return dest_path


def upload_via_ftp(local_path, ftp_cfg, ftp_password):
    cls = FTP_TLS if ftp_cfg.get("tls") else FTP
    ftp = cls()
    ftp.connect(ftp_cfg["host"], ftp_cfg.get("port", 21), timeout=60)
    ftp.login(ftp_cfg["username"], ftp_password)
    if ftp_cfg.get("tls"):
        ftp.prot_p()
    remote_dir = ftp_cfg.get("remote_dir")
    if remote_dir:
        ftp.cwd(remote_dir)
    remote_name = os.path.basename(local_path)
    with open(local_path, "rb") as f:
        ftp.storbinary(f"STOR {remote_name}", f)
    ftp.quit()
    return remote_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--secrets-file", default="backup_secrets.yaml")
    ap.add_argument("--keep-local", action="store_true",
                     help="also keep a copy of the downloaded backup in the current directory")
    args = ap.parse_args()

    cfg = load_config(args.config)
    client = build_client(cfg)
    secrets = load_backup_secrets(args.secrets_file)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"auto-{timestamp}"[:32]
    description = cfg.get("description", "Automated backup")
    ftp_cfg = cfg.get("ftp")

    if client.dry_run:
        dest = f"ftp://{ftp_cfg['host']}{ftp_cfg.get('remote_dir', '')}" if ftp_cfg else "(no backup.ftp configured)"
        print(f"[DRY-RUN] would trigger backup '{name}', wait for completion, download it, "
              f"and upload to {dest}")
        return

    print(f"Triggering backup '{name}'...")
    job_id = trigger_backup(client, name, description, secrets.get("encryption_key"))
    print(f"Backup job started (id={job_id}), waiting for completion...")
    wait_for_backup_job(client, job_id)

    archive_name = find_archive_name(client, name)
    print(f"Backup archived as '{archive_name}', downloading...")

    with tempfile.TemporaryDirectory() as tmp:
        local_path = os.path.join(tmp, f"{name}.zip")
        download_backup_file(client, archive_name, local_path)
        size = os.path.getsize(local_path)
        print(f"Downloaded {size} bytes to {local_path}")

        if args.keep_local:
            dest = os.path.join(os.getcwd(), os.path.basename(local_path))
            shutil.copy(local_path, dest)
            print(f"Kept local copy at {dest}")

        if not ftp_cfg:
            print("No ftp: section in config.yaml -- nothing uploaded. "
                  "Re-run with --keep-local if you just wanted the file on disk.")
            return

        ftp_password = secrets.get("ftp_password")
        if not ftp_password:
            print("  ! no ftp_password in backup_secrets.yaml -- cannot upload, aborting")
            sys.exit(1)

        remote_name = upload_via_ftp(local_path, ftp_cfg, ftp_password)
        print(f"Uploaded to ftp://{ftp_cfg['host']}{ftp_cfg.get('remote_dir', '')}/{remote_name}")


if __name__ == "__main__":
    main()
