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
readlink -f "$(command -v qbittorrent)"
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
