# FTD Backup Automation

FDM can schedule backups on its own, but it can't send them anywhere except
the device itself -- backups stay stored locally, with no automated way to
get a copy off-box. Cisco's own upgrade guidance recommends downloading a
backup before upgrading, which says a lot on its own about how much you
want to rely on what's sitting on the appliance. That's exactly what this
tool fixes: it triggers a backup on the FTD, downloads it, then uploads it
to an FTP (or FTPS) server of your choice -- ready to drop on a scheduled
task (cron at night, Windows Task Scheduler, etc.) for real off-box
retention.

Everything needed to run it is already in this repo; no other tooling
required.

## What you need before starting

1. **A machine to run it from.** Not the FTD itself -- FDM can't run scripts
   on the appliance. Any server or workstation with Python 3 works, as long
   as it stays on and reachable for scheduled runs.
2. **Network access** from that machine to:
   - the FTD's management interface, over HTTPS (port 443);
   - your FTP/FTPS server, on port 21 (and the data ports in passive mode,
     if your firewall filters them).
3. **An FDM account with API access** -- the same kind of admin account used
   to log into the web UI.
4. **An FTP or FTPS server** you control, with an account able to write
   files to it.
5. **A way to schedule the run** on your machine -- `cron` on Linux/macOS,
   Task Scheduler on Windows.
6. **FTP or FTPS?** Decide this before you start: plain FTP sends the backup
   unencrypted over the network. If that's a concern for you, use an
   FTPS-capable server and set `tls: true` in the config.

## Installation

1. Python 3 installed, then the two dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Copy the config file and fill in your own values:

   ```
   cp config.example.yaml config.yaml
   ```

   In `config.yaml`:
   - `ftd.host` / `username` / `password`: the FTD's management address and
     your FDM API account;
   - `ftp.host` / `port` / `username` / `remote_dir`: your FTP server's
     details;
   - `ftp.tls`: set to `true` if your FTP server supports FTPS;
   - keep `options.dry_run: true` for the first try -- more on that below.

3. Same for the secrets file:

   ```
   cp backup_secrets.example.yaml backup_secrets.yaml
   ```

   - `ftp_password`: the FTP account's password;
   - `encryption_key` (optional, but recommended): encrypts the backup file
     itself, independently of the FTP transfer.

   Both of these files (`config.yaml` and `backup_secrets.yaml`) hold real
   credentials -- keep them to yourself, never share them or drop them
   somewhere publicly accessible. Both are already excluded via
   `.gitignore`.

## Running it

**Dry run first**, with `dry_run: true` still set:

```
python backup_to_ftp.py --config config.yaml
```

Nothing is actually sent anywhere -- the script just prints what it would
do. This is the moment to check that the host and paths it prints match
what you expect.

**Then for real**: set `dry_run: false` and rerun the same command, keeping
an eye on the output. This time a backup is actually triggered on the FTD,
the script waits for it to complete, downloads it, and uploads it to your
FTP server. Run it once by hand before scheduling it, to make sure
everything works cleanly against your specific setup.

And if you want to keep a local copy alongside the upload:

```
python backup_to_ftp.py --config config.yaml --keep-local
```

## Scheduling it

Once a manual run goes through cleanly, you can automate it. Example with
`cron` on Linux, every day at 2am:

```
0 2 * * * cd /path/to/ftd-fdm-backup && python backup_to_ftp.py --config config.yaml >> backup.log 2>&1
```

On Windows, a scheduled task running:

```
python C:\path\to\ftd-fdm-backup\backup_to_ftp.py --config config.yaml
```

with "Start in" pointed at the `ftd-fdm-backup` folder.

## If something goes wrong

Rerun the script by hand (not through the scheduler) and read the output --
each step is announced as it happens (triggering, waiting, downloading,
uploading), and the actual error is printed as soon as a step fails. A few
things to check first:
- Can this machine reach the FTD over HTTPS, and the FTP server on its
  port?
- Are the credentials in `config.yaml` and `backup_secrets.yaml` correct and
  still valid?
- Is there enough disk space to hold the backup while it's being uploaded?
