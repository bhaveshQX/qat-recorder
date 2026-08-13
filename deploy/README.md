# Deploying the agent

One agent per Linux VM. Testers on Windows or Linux then point the panel at it.

The quickest route is `bash install.sh vm --agent` from the repository root,
which does everything below. This document explains what that script sets up.

---

## 1. On the VM

```bash
sudo useradd --system --create-home --shell /bin/bash qatrec
sudo install -d -o qatrec -g qatrec /etc/qatrec /var/lib/qatrec /opt/qatrec

sudo -u qatrec python3 -m venv /opt/qatrec/venv
sudo -u qatrec /opt/qatrec/venv/bin/pip install qat qat-recorder
```

The agent needs only the standard library plus `qat` — no web framework, so
there is nothing else to get through review.

### Token and certificate

```bash
sudo -u qatrec /opt/qatrec/venv/bin/qat-recorder-agent --generate-token \
    | sudo tee /etc/qatrec/token > /dev/null
sudo chmod 600 /etc/qatrec/token && sudo chown qatrec:qatrec /etc/qatrec/token

sudo -u qatrec /opt/qatrec/venv/bin/qat-recorder-agent --generate-cert \
    --cert /etc/qatrec/agent.crt --key /etc/qatrec/agent.key \
    --common-name "$(hostname -f)"
```

The second command prints a **fingerprint**. Keep it — that is what testers pin,
and it is why there is no CA to run.

### Service

```bash
sudo install -D -m644 deploy/qat-recorder-agent.service \
    /etc/systemd/system/qat-recorder-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now qat-recorder-agent
```

### The display

**Recording is interactive** — a human drives the application, so its window has
to be somewhere a tester can see and click. `DISPLAY` in the unit must point at a
session that exists: the machine's own desktop, or a VNC server the testers
connect to.

This does not affect capture fidelity. `QEvent::spontaneous()` is about whether
the *window system* delivered the event, not where the person sits, so clicks
arriving over VNC are treated exactly like local ones.

Replay needs no display beyond `Xvfb` or `QT_QPA_PLATFORM=offscreen`, which is
why CI can run headless while recording cannot.

### Firewall

```bash
sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload
```

---

## 2. On each tester's machine

```bash
pip install "qat-recorder[ui]"

qat-recorder hosts add vm-01 10.0.0.5:8765 \
    --fingerprint 2c6ee2d60460632a662e01f2dd23b86dc359799f87f59006a55cc928cda304af \
    --token-file ~/.qatrec/token \
    --note "HMI test rig, Qt 6.6"

qat-recorder hosts check vm-01      # free, or who is using it
qat-recorder hosts list
```

Then:

```bash
qat-recorder panel                  # pick the host from the drop-down
qat-recorder panel --agent vm-01    # or go straight there
```

Headless, for a scripted capture:

```bash
qat-recorder record --agent vm-01 --app /opt/acme/bin/hmi \
    --lib /opt/qatrec/libqatrec.6.6.so --seconds 60 --out ./recorded
```

### Distributing the token

The token is shared per host, and the config file references it rather than
storing it — `--token-file`, or `--token-env` to take it from the environment.
Putting the token inline in `hosts.json` works but is flagged, because a
credential sitting beside the address it unlocks is the most common way these
leak.

---

## 3. Sharing a host

Recording is exclusive: the operator drives the real UI, so two sessions on one
machine would fight over one display. A second tester is told who holds it:

```
host is in use by 'alice' since 2026-08-12T21:40:11+00:00
```

The lock frees itself when recording stops — the session stays alive only long
enough to fetch its artifacts, and another tester can claim the host immediately
after.

---

## 4. Security posture

- The agent **launches processes on request**. That is its job, and it is why the
  token and TLS are not optional: it refuses to start without a token, refuses
  TLS without a certificate, and refuses plaintext on anything but loopback.
- Certificates are self-signed and **pinned by fingerprint**, so a swapped
  certificate cannot pass unnoticed.
- `401` responses carry no detail — an unauthenticated caller cannot even learn
  the host name.
- Tokens are compared in constant time; generated keys are written `0600`.
- The unit runs unprivileged with `NoNewPrivileges`, `PrivateTmp` and a read-only
  system.

Rotating a token means regenerating `/etc/qatrec/token`, restarting the service,
and redistributing it. There is deliberately no remote administration surface.
