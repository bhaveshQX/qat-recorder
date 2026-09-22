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
