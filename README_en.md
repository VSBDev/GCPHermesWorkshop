# Workshop: Hermes + Gemma 4 on Google Cloud

This repo is used to run Hermes on a Google Cloud VM and use `Gemma 4` through `Vertex AI`.

The flow is simple:

1. Create a Google Cloud project.
2. Enable Vertex and Gemma 4.
3. Create a small VM.
4. Clone this repo.
5. Run one script.
6. Connect Hermes to the local proxy.

## What this repo sets up

- `Hermes Agent` on the VM
- a local OpenAI-compatible proxy on `127.0.0.1:8080`
- `Vertex AI` as the real inference backend

## Requirements

- A Google Cloud project with billing enabled
- A Compute Engine VM
- SSH access to the VM
- Vertex AI enabled
- `Gemma 4 26B A4B IT API Service` enabled in Model Garden
- The VM must use a `service account`
- That `service account` must have permission to use Vertex AI
- In the VM `Access scopes`, select `Allow full access to all Cloud APIs`

Recommended VM:

- `4 vCPU`
- `8 GB RAM`

Important:

- Creating the VM is not enough by itself.
- If the VM does not have `Allow full access to all Cloud APIs`, the proxy may start but Vertex will fail with permission or scope errors.
- In Google Cloud Console this is configured when editing the VM, in the `Service account` and `Access scopes` section.

## Step 1: create the Google Cloud project

In Google Cloud Console:

1. Open `IAM & Admin` → `Manage resources`
2. Click `Create Project`
3. Give it a name
4. Enter the new project
5. Make sure `Billing` is enabled

What you are doing here:

- creating the container where the VM, Vertex AI, and permissions will live

## Step 2: enable the required APIs

In the console:

1. Go to `APIs & Services` → `Enabled APIs & services`
2. Click `Enable APIs and Services`
3. Enable `Vertex AI API`
4. Enable `Compute Engine API`

What you are doing here:

- enabling Vertex AI for inference
- enabling Compute Engine so you can create the VM

## Step 3: enable Gemma 4 in Vertex

In the console:

1. Go to `Vertex AI`
2. Open `Model Garden`
3. Search for `Gemma 4 26B A4B IT API Service`
4. Open that card
5. Click `Enable` if it appears

If you want to test it before touching the VM, use `Cloud Shell`:

```bash
# stores the active gcloud project id in a variable
PROJECT_ID="$(gcloud config get-value project)"

# sets the global region used by this model
REGION="global"

# sets the Vertex AI host
ENDPOINT="aiplatform.googleapis.com"

# makes a direct call to Vertex to verify model access
curl \
  -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://${ENDPOINT}/v1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/openapi/chat/completions" \
  -d '{
    "model": "google/gemma-4-26b-a4b-it-maas",
    "stream": false,
    "messages": [
      {
        "role": "user",
        "content": "Reply with exactly: Vertex Gemma 4 is working."
      }
    ]
  }'
```

What should happen:

- Vertex should return a valid JSON response
- the text should say `Vertex Gemma 4 is working.`

## Step 4: create the VM

In the console:

1. Go to `Compute Engine` → `VM instances`
2. Click `Create instance`
3. Choose a machine with `4 vCPU` and `8 GB RAM`
4. Use a recent Ubuntu or Debian image
5. In `Service account`, keep a valid project service account
6. In `Access scopes`, select `Allow full access to all Cloud APIs`
7. Create the VM

What you are doing here:

- creating the machine where Hermes runs
- giving the VM enough scopes so the proxy can talk to Vertex

## Step 5: SSH into the VM and clone the repo

Run this inside the VM:

```bash
# updates the VM package index
sudo apt update

# installs git so the repo can be cloned
sudo apt install -y git

# clones this repo into a local folder
git clone https://github.com/VSBDev/GCPHermesWorkshop.git

# enters the project folder
cd GCPHermesWorkshop

# runs the main installer
# the script will ask for the project id in the terminal
bash scripts/bootstrap-vm.sh

# reloads the current shell PATH in case Hermes was just installed
source ~/.bashrc

# checks that the shell can already find Hermes
command -v hermes

# checks that Hermes responds correctly
hermes --version
```

What this block does:

- prepares the VM
- downloads this repo
- installs Hermes if needed, but without launching the interactive setup inside the installer
- creates the local proxy
- starts the `vertex-openai-proxy` service

Important:

- do not use `--skip-hermes` unless Hermes really already exists on that VM
- if `hermes` is not found after bootstrap, run `source ~/.bashrc`

## Step 6: verify Hermes and the proxy are ready

```bash
# checks that the shell can find the Hermes binary
command -v hermes

# checks that Hermes responds
hermes --version

# shows the systemd status of the proxy service
systemctl status vertex-openai-proxy.service --no-pager

# verifies that the proxy responds locally
curl http://127.0.0.1:8080/healthz

# verifies which model the proxy exposes to Hermes
curl http://127.0.0.1:8080/v1/models
```

What should happen:

- `command -v hermes` should return a path
- `hermes --version` should work
- the service should appear as `active (running)`
- `/healthz` should return your `project_id`
- `/v1/models` should return `google/gemma-4-26b-a4b-it-maas`

## Step 7: connect Hermes

```bash
# opens the Hermes model configuration
hermes model
```

Inside Hermes choose:

- `Custom endpoint`
- URL: `http://127.0.0.1:8080/v1`
- API key: blank
- Model: `google/gemma-4-26b-a4b-it-maas`
- Context window: `256000`

Then start Hermes:

```bash
# starts Hermes already pointing to the local proxy
hermes
```

## Step 8: test Vertex from Hermes

```text
Reply with exactly: Hermes is using Vertex Gemma 4.
```

If it replies, it is working.

## Step 9: connect Telegram

First create a bot with `@BotFather` and get your user id from `@userinfobot`.

Then on the VM:

```bash
# opens the gateway setup wizard
hermes gateway setup
```

When prompted:

- select `Telegram`
- paste the `bot token`
- paste your `Telegram user ID`

Then install the service:

```bash
# installs the gateway as a system service
sudo "$(command -v hermes)" gateway install --system --run-as-user "$USER"

# starts the Telegram gateway
sudo "$(command -v hermes)" gateway start --system

# checks that the gateway is running
sudo systemctl status hermes-gateway.service --no-pager
```

What this block does:

- registers Hermes Gateway as a service
- keeps it running after you disconnect SSH
- allows you to talk to Hermes from Telegram

## Quick explanation of the main script

The important file is:

- [scripts/bootstrap-vm.sh](/Users/victor/Desktop/HermesGemma4/scripts/bootstrap-vm.sh)

It does this, in order:

1. installs system packages
2. installs Hermes if it is not already present
3. creates a virtualenv for the proxy
4. installs Python dependencies
5. writes `proxy.env`
6. copies `vertex_openai_proxy.py`
7. creates the `systemd` service
8. starts the proxy and verifies it

## Important files

- [README.md](/Users/victor/Desktop/HermesGemma4/README.md)
- [README_en.md](/Users/victor/Desktop/HermesGemma4/README_en.md)
- [scripts/bootstrap-vm.sh](/Users/victor/Desktop/HermesGemma4/scripts/bootstrap-vm.sh)
- [vertex_openai_proxy.py](/Users/victor/Desktop/HermesGemma4/vertex_openai_proxy.py)
- [requirements-proxy.txt](/Users/victor/Desktop/HermesGemma4/requirements-proxy.txt)

## If something fails

```bash
# checks the Vertex proxy log
journalctl -u vertex-openai-proxy.service -n 100 --no-pager

# checks the Telegram gateway log
journalctl -u hermes-gateway -n 100 --no-pager
```

Those two logs usually show the problem.
