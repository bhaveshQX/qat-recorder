# Commands

## Windows — build the wheel

```powershell
.\.venv\Scripts\python.exe -m build --wheel --outdir dist
```

Send `dist\qat_recorder-0.1.0-py3-none-any.whl` and `install.sh` to the VM.

## VM — install

```bash
cd ~/Downloads

# only if the transfer renamed the wheel to .whl.zip
mv qat_recorder-0.1.0-py3-none-any.whl.zip qat_recorder-0.1.0-py3-none-any.whl

# only if bash complains about $'\r'
sed -i 's/\r$//' install.sh

bash install.sh vm --agent
```

## VM — the two paths the panel asks for

```bash
readlink -f ~/qatrec-filter/libqatrec*.so
readlink -f "$(command -v qbittorrent)"     # or the launch script: .../bin/start_spine.sh
```

## VM — applications started by a launch script

Nothing extra to do: give the panel the script itself. `build-filter` also builds
`~/qatrec-filter/libqatgate.so`, which the recorder picks up automatically; it is
what keeps Qat's injector out of the shells the script runs (their `$(...)` lines
break otherwise) and lets the recorder follow the application into the process the
script actually starts.

To see which processes were instrumented, start the agent with:

```bash
QATREC_GATE_DEBUG=1 ~/qatrec/bin/python -m qat_recorder.agent.cli ...
```

## VM — start the agent

```bash
~/qatrec/bin/python -m qat_recorder.agent.cli \
    --token-file ~/.qatrec/token \
    --ngrok --ngrok-authtoken <your-ngrok-authtoken>
```

Open the ngrok link it prints. The token it asks for:

```bash
cat ~/.qatrec/token
```

## VM — an application that has to run as root

If the application is normally started with `sudo` (for example because its
data folder belongs to root), start the agent with `QATREC_SUDO=1`. The agent
stays your user; only the application is started as root, with the injection
passed through `sudo`:

```bash
QATREC_SUDO=1 ~/qatrec/bin/python -m qat_recorder.agent.cli \
    --token-file ~/.qatrec/token \
    --ngrok --ngrok-authtoken <your-ngrok-authtoken>
```

sudo must not ask for a password, because nobody is there to type it. Check with
`sudo -k; sudo -n true && echo ok`. If it asks, that is for the machine's admin
to decide: what the recorder runs through sudo is `env`, so a password-free rule
for it amounts to password-free root for that user.

Start the agent from a terminal on the desktop, so `DISPLAY` and `XAUTHORITY`
point at the screen the application should open on.

## VM — an application that resumes where it was left

Some applications save their state and restore it on the next start. Set
`QATREC_RESET` to a command that puts it back to one fixed starting point; the
recorder runs it before every launch -- recording, replay and probe -- and does
not start the application if it fails.

```bash
# once: with the application closed and in the state every test should start from
sudo cp -a /opt/stryker/data/mako_shoulder_1_5/patients ~/mako-patients-start

# then start the agent with:
QATREC_RESET='sudo -n rm -rf /opt/stryker/data/mako_shoulder_1_5/patients && sudo -n cp -a /home/ronit/mako-patients-start /opt/stryker/data/mako_shoulder_1_5/patients' \
QATREC_SUDO=1 ~/qatrec/bin/python -m qat_recorder.agent.cli ...
```

## VM — install a new wheel later

```bash
cd ~/Downloads
~/qatrec/bin/pip install --force-reinstall qat_recorder-0.1.0-py3-none-any.whl
~/qatrec/bin/python -m qat_recorder build-filter --out ~/qatrec-filter

# --force-reinstall reinstalls qat too, which puts back any server library that
# was standing in for an unloadable one:
~/qatrec/bin/python -m qat_recorder qat-servers --fix
```

## VM — the filter has to match the application's Qt

A filter built against the wrong major version loads without complaining and
then sees none of the application's widgets: the session records nothing and
says nothing. An application that ships its own Qt (a `lib/Qt/lib` beside its
`bin/`) has nothing to do with the Qt this machine has installed.

```bash
sudo apt install qt6-base-dev         # RHEL: sudo dnf install qt6-qtbase-devel

# let the application decide which Qt, rather than this machine:
~/qatrec/bin/python -m qat_recorder build-filter --out ~/qatrec-filter     --app /path/to/start_app.sh
```

The minor version matters too, and only in one direction: a filter built
against Qt 6.2 works inside a 6.8 application, one built against 6.10 does not.
Where several Qts are installed, say which:

```bash
~/qatrec/bin/python -m qat_recorder build-filter --out ~/qatrec-filter     --app /path/to/start_app.sh --qt-prefix /usr/lib/x86_64-linux-gnu/cmake
```

Then give the panel the `libqatrec.6.*.so` it built. Each build starts from
scratch, so no stale library or cached Qt choice is left behind.

## VM — when the application starts but never answers

Qat ships one prebuilt server per Qt version, and the Qt 6.8+ ones need a newer
glibc than Ubuntu 22.04 or RHEL 9 has. The application then starts perfectly and
is never reachable, and the only sign is a line in its own output:

```
Failed to load Qat server: .../libQatServer.6.8.so
/lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found
```

`install.sh vm` repairs this automatically. To check or repair it by hand:

```bash
~/qatrec/bin/python -m qat_recorder qat-servers          # what needs what
~/qatrec/bin/python -m qat_recorder qat-servers --fix    # stand in one that loads
~/qatrec/bin/python -m qat_recorder qat-servers --restore
```

## VM — remove everything

```bash
bash install.sh uninstall --yes
```

## Windows — desktop panel (only if you are not using the browser)

```powershell
.\install.ps1 local
.\install.ps1 uninstall
```
