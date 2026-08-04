# Installing pve-monitor

Everything here needs either root or a Telegram client, which is why it is
written down instead of scripted. The rest — the webhook target and the
matchers — is `bin/pve-notify-setup`.

`pve-monitor` runs on a host with network reach to the Proxmox API, not on a
cluster node. It only reads the API, so it needs no privileges there.

## 1. Create the bot

`@BotFather` on Telegram, `/newbot`. Then message the new bot once and read
`message.chat.id` from:

    https://api.telegram.org/bot<TOKEN>/getUpdates

A group chat id is negative. Put both values in `~/.config/pve-monitor/env`
(see `pve-monitor.env.example`) and `chmod 600` it.

The bot token ends up in the Proxmox webhook URL in cleartext, readable to
anyone holding `Sys.Audit` on the cluster or root on a node. Telegram takes its
token only in the URL path, so there is no way to keep it in PVE's encrypted
`secret` field. `/revoke` in `@BotFather` rotates it.

## 2. Install

    cp bin/pve-monitor ~/bin/
    cp install/pve-monitor/* ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now pve-monitor-health.timer pve-monitor-digest.timer

`pve-monitor-digest.timer` names a timezone (`America/Los_Angeles`) rather than
a bare hour. Hosts commonly run UTC, where a bare `08:00` is the middle of the
night, and a named zone also tracks the DST shift. Edit it to taste; it needs
systemd 252 or newer.

## 3. Enable lingering — needs root

    sudo loginctl enable-linger <user>

These are systemd **user** units. Without lingering they stop at logout, and
the failure is silent: `systemctl --user list-timers` still shows them armed
while a session exists.

## Verifying

    pve-monitor health  --dry-run
    pve-monitor digest  --dry-run
    pve-monitor backups --dry-run

`--dry-run` prints instead of sending, and does not consume the state
transition that `health` deduplicates against.

`health` exits 1 when it reports something, which is why the unit sets
`SuccessExitStatus=0 1`.

## Verifying the alert path end to end

`bin/pve-notify-setup` finishes by asking PVE to send a test notification. A
`404` in that response means PVE reached Telegram and Telegram rejected the bot
token; a connection or resolution error means the nodes have no egress. The two
look nothing alike, which is the only reason the test is worth running
separately from the first real alert.
