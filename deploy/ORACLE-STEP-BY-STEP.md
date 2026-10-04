# Oracle Cloud deployment — the click-by-click

From "no cloud account" to "my trader is on the internet with HTTPS", in about
20 minutes — most of it waiting for Oracle's signup e-mail.

**Your route:** create the account (Part 1), then let **Cloud Shell build the VM
for you** (Part 2), then one command from this PC deploys the app (Part 5).
Part 3 is the same thing by clicking, in case Cloud Shell misbehaves.

| Already prepared for you | Where |
| --- | --- |
| SSH keypair for the server | `C:\Users\iones\.ssh\oracle-paper-trader` (+ `.pub`) |
| Builds the whole VM (network, firewall, instance) | [oci-create-instance.sh](oci-create-instance.sh) |
| One-command uploader + installer | [deploy-from-windows.ps1](deploy-from-windows.ps1) |
| Your invite code, and the command to run | [my-deployment.ps1](my-deployment.ps1) |
| Runs on the VM: Docker, firewalls, DuckDNS, HTTPS | [oracle-cloud-setup.sh](oracle-cloud-setup.sh) |

The one thing I cannot do is create the account: Oracle's signup needs your
identity, phone and card check, and it must be *your* tenancy with *your* terms
accepted. That is Part 1, and it is the only part that needs you.

---

## Part 1 — Create the Oracle Cloud account (10 min, you)

Oracle's wording shifts slightly now and then; the sequence is always this one.

1. Open **https://signup.oraclecloud.com**.
2. **Country** (yours), **first and last name**, **e-mail**. Submit — a
   verification link or code arrives within a minute or two; click/enter it.
3. **Choose a password.** It must be 8+ characters with upper case, lower case,
   a number and a special character.
4. **Cloud Account Name** — this becomes your tenancy name. **Write it down**:
   signing in later on `cloud.oracle.com` asks for it (it is also the
   `?tenant=...` part of your console URL).
5. **Home Region** — *permanent* for your free resources, so pick the one
   nearest you. For the free ARM shape, `Netherlands Northwest (Amsterdam)`,
   `Germany Central (Frankfurt)` and `UK South (London)` usually have capacity;
   for the AMD micro shape almost any region works. (Romania has no region;
   Frankfurt or Amsterdam are the closest.)
6. **Address and mobile number**, then the **SMS/phone verification**.
7. **Payment method.** This is required even for Always Free — it is an identity
   check, and Always Free resources are never charged. A refundable hold of
   about $1 may appear and disappear. Debit cards usually work; some virtual and
   prepaid cards are declined, in which case try another card.
8. Accept the agreement and click **Create Account**. Provisioning takes 5–15
   minutes; you get a "your account is ready" e-mail.

Then sign in at **https://cloud.oracle.com** with your Cloud Account Name.
**Do not click "Upgrade to Pay As You Go"** — nothing in this guide needs it and
Always Free stays free exactly as long as you stay on it.

> Stuck? The two usual ones are a declined card (try a different card or a
> different region) and a region without free capacity (the region is fixed to
> the tenancy, so if capacity is exhausted everywhere there, the only fix is a
> new tenancy in another region).

**While you wait for that e-mail, do Part 4** — DuckDNS needs no Oracle account.

---

## Part 2 — Build the VM from Cloud Shell (your route, 5 min)

Cloud Shell is Oracle's browser terminal (the **`>_`** icon, top right of the
console). It is already signed in as you, so no API keys are involved and
nothing secret leaves your tenancy.

**2a. Upload the build script.** First check the **region selector at the top
right of the console is your *home* region** (the one you chose at signup) —
Always Free resources only exist there, and Cloud Shell opens in whichever
region is selected. Then, in Cloud Shell: **⋮ (menu) → Upload →** choose
[oci-create-instance.sh](oci-create-instance.sh) from this project's `deploy`
folder.

**2b. Run it.** Paste this into Cloud Shell, changing nothing except the last
line (the key is already yours — the private half stays on this PC):

```bash
SSH_PUBLIC_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICmH5qjj+wDmByBybdwxbONzD6HfkgVjWJiPAoECY+JK paper-trader-deploy" \
bash oci-create-instance.sh
```

It creates, in order: the VCN, an internet gateway, the route table, a security
list with **22, 80 and 443** open, a public subnet, and a `VM.Standard.A1.Flex`
instance (1 OCPU / 6 GB, always free) with a **public IPv4 address assigned** —
then prints the **public IP**.

Notes:
- It works out your compartment automatically; if it cannot, copy the OCID from
  **Identity → Compartments → your root compartment** and re-run with
  `COMPARTMENT_ID=ocid1.tenancy... ` in front.
- "Out of host capacity" is normal on the free ARM shape: the script already
  retries each availability domain, and if all are full it tells you to re-run
  with `SHAPE=VM.Standard.E2.1.Micro` (also free).
- Nothing is created twice: re-running it finds the instance and just reports
  its IP.

**2c.** Copy the printed **public IP** — Part 5 needs it.

---

## Part 3 — (alternative) the same thing by clicking

Only if you would rather not use Cloud Shell. Menu **☰ → Compute → Instances →
Create instance**:

| Field | What to choose |
| --- | --- |
| **Name** | `paper-trader` |
| **Image** | *Change image* → **Canonical Ubuntu** → **24.04** |
| **Shape** | *Change shape* → **Ampere** → `VM.Standard.A1.Flex`, 1 OCPU / 6 GB (or **AMD** → `VM.Standard.E2.1.Micro`) |
| **Primary VNIC** | *Create new virtual cloud network* (accept the defaults) |
| **Assign a public IPv4 address** | **YES — tick this.** Without it the VM is unreachable |
| **Add SSH keys** | **Paste public keys** → the line in Part 2b |

Then open the ports: **☰ → Networking → Virtual Cloud Networks →** your VCN **→
Security Lists → Default Security List → Add Ingress Rules**, and add `0.0.0.0/0`
TCP **80**, then **+ Another Ingress Rule** for TCP **443** (port 22 is already
open). This is the step people skip — the site stays unreachable until both this
*and* the instance firewall (the installer fixes that one) allow the port.

---

## Part 4 — DuckDNS: a free name with HTTPS (3 min, you)

Caddy needs a hostname to get a free Let's Encrypt certificate. Do this while
Oracle provisions your account:

1. Open **https://www.duckdns.org** and sign in (GitHub / Google / Reddit).
2. Under **create a domain**, type a short name (e.g. `mytrader`) and click
   **add domain**. You now own `mytrader.duckdns.org`.
3. Scroll to the top: your **token** is in that box. Copy it.

Keep the name and the token — Part 5 uses them.

---

## Part 5 — Deploy (2 min, one command)

In **PowerShell, in this project folder**:

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\deploy-from-windows.ps1 `
    -Server <the public IP from Part 2> `
    -DuckDnsName mytrader -DuckDnsToken <your DuckDNS token> `
    -OwnerEmail <your e-mail> -RegistrationCode paper-9k4m2p
```

> `-ExecutionPolicy Bypass` is only because Windows blocks local `.ps1` files by
> default; it applies to this single command and changes no setting. (Or run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and drop it.)

That one command: waits for SSH → packs the project and uploads it (no git
needed on the server, and **no local secrets** — your `web_config.json`,
invite-code file, database and price caches are deliberately left behind) →
installs Docker → opens the instance `iptables` chain for 80/443 → points
DuckDNS here and installs a 5-minute refresh job → builds and starts the
container with `restart: unless-stopped` (survives reboots) → puts Caddy in
front for the automatic HTTPS certificate → **prints your owner password**.

Add `-SkipUpload` to re-run the installer without re-uploading, `-DryRun` to see
every command without touching the server, `-WithTalib` for the full 61-pattern
TA-Lib engine (a few minutes of compiling).

---

## Part 6 — Add the AI (optional, 2 min, you)

The trader, its candle patterns and its risk engine need no AI at all. AI
*analysis* needs an OpenAI-compatible endpoint, because a 1 GB free VM cannot
run a model. Get a key from [platform.deepseek.com](https://platform.deepseek.com)
(cheap) or [platform.openai.com](https://platform.openai.com), then on the VM:

```powershell
ssh -i "$env:USERPROFILE\.ssh\oracle-paper-trader" ubuntu@<public-ip>
sudo nano /opt/paper-trader/.env      # LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
cd /opt/paper-trader && sudo docker compose up -d
```

`LLM_BASE_URL` is everything before `/chat/completions`, e.g.
`https://api.deepseek.com/v1`.

---

## Part 7 — First sign-in

1. Open `https://mytrader.duckdns.org` (give the certificate a few seconds on
   the first visit).
2. Sign in with the **owner password** the deploy printed, leaving the e-mail
   box **empty** — that means "the owner account", which owns the portfolio that
   existed before the app became multi-user.
3. Press **Trading Mode** when you want it to trade. Until then the engine sits
   idle; afterwards it is remembered and resumes by itself on restarts.
4. Share the address and the invite code with whoever should get their own
   account. (Change the code any time in `/opt/paper-trader/.env`.)

---

## Part 8 — Looking after it

```bash
# on the VM
cd /opt/paper-trader
sudo docker compose logs -f          # watch it work
sudo docker compose restart          # restart the app
sudo docker compose up -d --build    # update after uploading new code
sudo docker compose down             # stop everything
sudo docker compose logs paper-trader | grep -A3 "OWNER PASSWORD"   # the password

# back up accounts + portfolios (data/ lives outside the container)
tar czf ~/paper-trader-$(date +%F).tar.gz -C /opt/paper-trader data

# if the owner password is lost, print a new one
sudo docker compose exec paper-trader python app_web.py --reset-password
```

---

## When something is wrong

| Symptom | Cause and fix |
| --- | --- |
| `deploy-from-windows.ps1 cannot be loaded because running scripts is disabled` | Use the `powershell -ExecutionPolicy Bypass -File …` form in Part 5. |
| SSH says "Connection refused/timed out" | The instance is not **Running**, or the IP is wrong. |
| The page never loads, but SSH works | The **VCN Security List** (Part 2b/3). Then on the VM: `sudo iptables -L INPUT --line-numbers -n \| head` — 80 and 443 must be ACCEPTed *above* the REJECT line. |
| `docker compose ps` shows it restarting | `sudo docker compose logs paper-trader` — usually a bad `LLM_*` value or a port clash. |
| The certificate never arrives | `dig +short mytrader.duckdns.org` must return the VM's IP. Fix the record with `curl "https://www.duckdns.org/update?domains=mytrader&token=TOKEN&ip="`. |
| "Out of host capacity" | Normal for the free ARM shape: re-run with `SHAPE=VM.Standard.E2.1.Micro`, or try again in a few minutes. |
| Cloud Shell lost my upload | Sessions expire after ~20 idle minutes; upload again and re-run (it reuses what exists). |
