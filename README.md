# GDh ZR campaign: voter portal for many candidates

A private, login-only voter list: search and filter voters, view photos, canvass, assign agents,
and (for admins) edit records, export lists, manage users and read the activity log.
It started as the Gadhdhoo portal on `vote.seenu.online` and now serves **any number of candidates
from one code base**: each candidate gets **their own container, database, photos, domain and
admin account**, so one campaign can never see another's voters.

**Stack:** Python 3 · Flask · SQLite (WAL) · Gunicorn · Docker behind the VPS's shared Traefik
(or Nginx + systemd for a single portal). The front end is server-rendered HTML with its own CSS
(light and dark themes, mobile layout). It uses no CDN or third-party scripts, so a strict CSP is possible.

```
                      ┌─► vp-gadhdhoo  (container) ─► volume vp-gadhdhoo_data  (database + photos)
browser ──HTTPS──► Traefik ─► vp-gdh-zr    (container) ─► volume vp-gdh-zr_data
  (Host: …)           └─► vp-<slug>    (container) ─► volume vp-<slug>_data
                     all built from the same image  vote-portal:<version>
```

What differs per candidate, and where it's set:

| | Where |
|---|---|
| Domain, version, secret key, first admin password | `candidates/<slug>/.env` on the server (`deploy/vp.sh new`) |
| Campaign name (title bar, sign-in, PDFs) | `.env` at first, then **Settings → Campaign name** |
| Parties shown, colour, WhatsApp/SMS/Viber messages, picture, Dhivehi font | **Settings** in that candidate's portal |
| Voters, photos, users, agents, canvassing, 100% confirmed list | That candidate's own database (Import page) |

See [Many candidates on one VPS](#many-candidates-on-one-vps) for setup.

## Call centre (reachability campaigns)

For a candidate whose main work is phoning voters across **several islands** (e.g. GDh ZR). Turn it on
under **Settings → Call centre and photos** (untick *Show photos* if the list has none). Your one-island
portal keeps this off and doesn't change.

**Setup (admin)**
1. **Import** the voter list. These headers are read as they are:
   `nationalID, DisplayName, House, Address, Phone, LivesIn, Atoll, Island, Constituency, BoxName, BoxCode`
   (a Phone cell may hold several numbers: `7771234 / 9991234`).
2. **Users**: add each call agent (a *user*, or an *agent* given a sign-in) and hand them their password.
3. **Calls → Islands**: tick which agents call each island. One agent can have several islands.

**Users and agents see only their own islands** while the call centre is on: search, voter pages
and the call list leave out every other island (and voters with no island). Admins see everything.
Each has **My dashboard**: their islands' progress, their calls today / this week, and their latest
calls and comments.

**Agents** sign in straight to **Calls**: the next voter of their islands, a **Call** button per number,
then one tap for the outcome (**Will vote · Will not vote · Reached · No answer · Switched off · Wrong number · Call back later ·
Refused**), a comment, and **Save and next**.
- The voter on screen is held for that agent (10 min by default) so two agents never ring the same person.
- *Call back later* takes a time; the voter comes back to the top of the list when it's due.
- *No answer* / *Switched off* come back after the retry gap (60 min) until the try limit (3);
  then the voter counts as **hard to reach**. *Reached*, *Refused* and *Wrong number* end the calls
  (the admin can put a voter back).
- Every call is logged (who, when, number, outcome, comment) and also sets the voter's canvassing
  status and note, so search, the voter page and exports show it.

**The candidate (admin)** gets:
- **Reachability**: % reached overall and per island, called at least once, call-backs due, hard to
  reach, wrong numbers, never tried, no phone; each number opens the list of those voters (CSV export).
  Plus calls per day for 14 days and every agent's calls and reached today / this week.
- **Comments**: every call comment, newest first, filtered by island, agent, outcome, date or text, with
  CSV export.
- Search gets an **Island** filter and sort; cards show the island and where the voter lives now.

Try limit, retry gap and hold time are under **Settings**. A new candidate can start with it on:
add `VOTER_PORTAL_CALL_CENTER=1` and `VOTER_PORTAL_SHOW_PHOTOS=0` to `candidates/<slug>/.env`
before the first start.

## Roles

| | Admin | User |
|---|---|---|
| Search, filter (name, ID, house, phone, party, sex, age range), view voter + photo + phone | ✓ | ✓ |
| Add, edit, delete voters; upload photos | ✓ | — |
| Export the current filtered list to CSV / Excel | ✓ | — |
| Create users, activate/deactivate, change role, reset password | ✓ | — |
| Activity log | ✓ | — |

Each forbidden request returns 403 and is written to the activity log.

## Canvassing

Every voter has a canvassing status (**Not contacted**, **Contacted**, **Follow up**, **No answer**)
and free-text notes. Every signed-in user can set them, including read-only users, so canvassers
can record visits; they still can't change any other voter detail.
- **On a voter card:** pick the status from its dropdown. It saves in the background and the list keeps its place.
- **On the voter page:** set the status and write notes.
- **Status chips** at the top of the search page show live counts and filter the list.
- **Sort by House** groups the list under house headings, for door-to-door rounds.
- **Search** also looks inside notes.
- **Tracking:** each change records who made it and when, and is written to the activity log as `voter_canvass`.
- **Exports** include status, when it was last changed, and notes.
- **Imports never touch canvassing data.** Replace mode removes voters missing from the files and
  overwrites the rest in place, so their status and notes are kept.

## Campaign parties (e.g. MDP only)

**Settings → Show voters of these parties**: tick the parties the campaign covers (for example MDP)
and save.
- **Who's affected:** users and agents then never see other voters. That covers search, voter pages
  and photos (direct links give "not found"), status updates, the dashboard, My houses and the
  assignment lists.
- **Admins** see the same campaign view, with a banner button **Show all parties**. It changes the
  view for that admin only, until they switch back, and it's logged. Exports follow the current view.
- **Hidden voters are not deleted.** Imports keep updating them, and anyone whose party changes to
  a campaign party (for example by joining the MDP list) appears automatically.
- **"No party recorded"** can be included too. **Show all parties** on the settings page turns the
  filter off for everyone.

## WhatsApp, SMS and Viber

Every voter with a mobile number (7 digits starting with 7 or 9) gets these buttons, on their card
and on the voter page:
- **WhatsApp:** opens a chat (+960 added) with the campaign message already typed.
- **Viber:** copies the Viber message, then opens the voter's Viber chat; the canvasser pastes it
  (Viber can't pre-fill text for a number). Works only if the voter uses Viber on that number.
- **SMS:** opens the phone's messaging app with the number and the SMS message. It's sent from the
  canvasser's own phone, so it costs whatever their plan charges.
- **Call.**
- **Send picture** (voter page, phones only): shares the campaign picture plus the WhatsApp text through
  the phone's share sheet; pick WhatsApp, then the voter.

Landlines only get Call. Admins set **three separate messages** under **Settings → Messages**, using
`{first_name}`, `{name}`, `{house}` and `{id}`. SMS and Viber use the WhatsApp text until they're given
their own. Nothing is sent automatically: the canvasser always presses Send.

**WhatsApp picture:** upload a flyer under Settings → Messages. A WhatsApp link can't attach a file, so
the WhatsApp message ends with a link (`/c/<random>`) that WhatsApp shows as a picture preview. That
picture is public (no sign-in) so voters can see it; it holds no voter data, and each upload gets a new
random address (the old one stops working). Untick "Add the picture link" to leave it out.

Sending one SMS to many voters at once (bulk SMS) needs an account with an SMS gateway (e.g. Dhiraagu
or Ooredoo bulk SMS, or Twilio) and is not set up.

## Who can see the voter list

- **Admins** see the whole list, can page through it (25/50/100/200 per page, or type a page number),
  export it, and are the only ones who can add, edit or delete voters.
- **Read-only users** can browse and search the list (within the campaign's parties) and record
  canvassing, but can't add, edit, delete or export.
- **Agents** see only their own voters (their houses plus voters handed to them).
- Each voter page lists everyone else at the same address (within what that person may see).

## 100% confirmed voters (private to the `admin` account)

Voters who will vote for the candidate. Only the account named **admin** can see or change them;
other admins, read-only users and agents never see them (every route is behind
`can_manage_confirmed_voters`, and they live in their own `confirmed_voters` table that no other page
or export reads).
- **Mark / unmark:** the "Mark confirmed" pill on each voter card (saves in the background), or the
  box on the voter page, which also takes a private note.
- **Numbers:** **Confirmed** page (top bar or account menu) and a widget on the Dashboard: confirmed
  out of the campaign's voters (e.g. MDP), this week, still to confirm, out of all voters, per house.
  JSON at `/confirmed/metrics.json`.
- **List and export:** Search → More filters → **100% confirmed**; CSV/Excel then include
  "100% confirmed" and "Confirmed on" columns. Only `admin` gets these.
- **Privacy:** confirm/unconfirm and confirmed-list exports are hidden from other admins' activity log,
  and other admins can't reset the `admin` password, change its role or deactivate it.

## Colours

- **Settings → Portal colour** sets the main colour for everyone: MDP yellow, teal, blue, green,
  purple or red. Each comes with a dark-mode version and a darker shade for text, so links stay
  readable even in yellow.
- **Appearance** (each person, in the account menu): Auto (follows the phone), Light or Dark. It's
  remembered on that device.

## Dashboard and agents

**Dashboard** (admins and users): overall progress (voters reached, contacted, follow up, no answer,
not contacted, houses finished, updates today and this week), then one progress bar per house with
its agent. You can filter by party or agent (including "Unassigned houses") and sort by least
progress, most voters left, biggest house, recently updated or name. Tap a house to canvass its
voters. "Most active this week" ranks people by their canvassing updates.

**Dashboards:** only admins see the full **Dashboard** (every house, every agent, the leaderboard).
Everyone else gets **My dashboard**: the voters they personally reached (by status, with links), their
updates today and this week, and their latest contacts. For agents it also shows their houses, their
one-by-one voters and a suggested next house. Admins can open anyone's from **Users → Dashboard ›**
or the Agents page.
- **Date range:** both dashboards have **All time / Today / Last 7 days / Last 30 days** and From/To
  dates (Maldives days). Only status changes made in that period count as reached, and "Updates" and
  "Most active" cover the same period.
- **Reached by (admin dashboard):** pick a person to see only the voters they reached and only the
  houses where they reached someone. Tap a name under "Most active" to do the same.

**Agents** are canvassers. Create a user with the role **agent**, then assign houses under **Agents**:
- **Assigning:** pick an agent for each house in the list (type to filter), or use **Auto-assign**.
  Auto-assign shares out the unassigned houses, or re-balances all of them, so each agent gets a
  similar amount of work left (voters not yet reached plus follow-ups), handing out the biggest
  houses first.
- **One voter at a time:** open a voter and pick an agent under **Assigned agent**, or in Search open
  **Assign to an agent**, tick voters (or choose "all matching voters", up to 5,000) and assign. A voter
  handed out this way belongs to that agent even if their house belongs to someone else; pick
  "back to their house's agent" to undo. Cards show the agent's name, the Agents page shows
  "+N one by one", and every change is in the activity log. The CSV/Excel export has **Agent** (the
  voter's agent) and **Agent via** ("One by one" or "House") columns, plus **Status by**, **Last edited** and **Last edited by**. Times are Maldives time.
- **Agent filter:** in Search, **More filters → Agent** shows the voters an agent is responsible for
  (their houses plus voters handed to them, minus voters handed to someone else), or **No agent**.
  The CSV/Excel buttons export exactly what the search shows, so use it to export one agent's list.
- **Contacted by filter:** **More filters → Contacted by** lists the voters a person last marked as
  contacted, follow up or no answer (anyone who has done so appears in the list). Exports follow it too.
- **Who changed it:** each voter card shows who last set the canvassing status (and when), and who
  last edited the voter's details.
- **What agents see:** only the voters in their own houses (minus any handed to another agent) plus
  the voters handed to them one by one, in search, voter pages, photos and status
  updates. They don't see the dashboard and can't export.
- **My houses:** an agent lands here after signing in. It shows their progress, follow-ups and
  not-contacted counts (tap to list them), and a **Suggested next** house: follow-ups first, then the
  house with the most voters not yet reached.
- **Admins** can open any agent's page from the Agents list.

| | Admin | User | Agent |
|---|---|---|---|
| Search and view voters | all | all | assigned houses only |
| Set canvassing status and notes | ✓ | ✓ | assigned houses only |
| Dashboard | ✓ | ✓ | (My houses) |
| Edit voters, export, import, users, agents, activity log | ✓ | — | — |

## Security

- Passwords are hashed with **Argon2id** and must be at least 10 characters. Seeded and
  admin-created accounts start with a temporary password that must be changed at first sign-in.
- Sessions are signed, HttpOnly, `Secure`, `SameSite=Lax` cookies and last 12 hours. A password
  change, a reset or a deactivation ends that user's existing sessions.
- Every form is CSRF-protected.
- After 5 wrong passwords the account is locked for 15 minutes. Nginx also limits `/login` to
  10 requests per minute per IP.
- Responses carry a CSP and the `X-Frame-Options`, `nosniff`, `noindex` and HSTS headers.
- Photos are served only to signed-in users, never as public static files. Uploads are checked,
  re-encoded to JPEG, and their EXIF/GPS data is removed.
- Exports guard against spreadsheet formula injection.
- The activity log records sign-ins, failed sign-ins, every create/update (with a before/after
  diff) and delete, every export (with filters and row count), and all user changes.

## Code layout

```
app/__init__.py      app factory, security headers, CLI (create-user, set-password)
app/auth.py          login/logout, Argon2, CSRF, lockout, @login_required / @role_required / @admin_required
app/voters.py        search + filters, detail, photo, add/edit/delete, CSV/Excel export
app/admin.py         user management, activity log
app/normalize.py     cleaning rules shared by the importer and the edit form
app/db.py            SQLite schema (users, voters, audit_log)
app/templates/       Jinja templates        app/static/   CSS + a few lines of JS
scripts/ingest.py    merge / de-duplicate / load the four source files, seed users
deploy/              setup_vps.sh, nginx site, systemd unit, gunicorn config, admin helper
tests/               pytest suite with made-up fixture files (no real voter data)
```

## Importing the voter files

`scripts/ingest.py` reads every voter file in one folder (`.xlsx`, `.xlsm`, `.xls` or `.csv`;
PDF lists through the Import page). Columns are found by their header names and values, so each
candidate's own sheets work as they are.

**Any campaign (generic):** when the same ID card appears in more than one file, the file whose
name sorts first wins, field by field (a blank never erases a value). Number the files, newest
first: `1 voters 2026.xlsx`, `2 party list.csv`, `3 old list.xlsx`. A set of files with no ID card
column stops with an error and changes nothing.

**Gadhdhoo:** when one of the Gadhdhoo file names below is present, the four files are used with this
fixed priority instead (names matched loosely), including the MDP-list rule:

| Priority | File | Used for |
|---|---|---|
| 1 | `Gahdhdhoo.2026` | newest details and photos |
| 2 | `gadhoo-new voters` | new voters |
| 3 | `Gadhdhoo (updated Party list MPD )` | updated party preferences |
| 4 | `final_cleaned_gadhdhoo_updated_party` | full community list |

How it works:
- **Columns** are found by header name (`ID Card No.`, `National ID`, `Name`, `Gender`, `Address`,
  `House`, `Party`, `Photo`, `DOB`, …). The header doesn't need to be in the first row. If a sheet
  has both `Party` and `Updated Party`, the updated one is used. When a header is missing or
  unclear, the ID, sex and age columns are recognised from their values. A serial-number column
  is never taken for age.
- **Cleaning:** whitespace is trimmed and collapsed, and IDs are upper-cased with spaces and
  dashes removed (`a 123456`, `A-123456` and Excel's `123456.0` all become `A123456`). Six bare
  digits get an `A` prefix and are logged. Sex is read from `M/F/Male/Female/ފިރިހެން/އަންހެން`,
  age from a number or a date of birth, and party is upper-cased. Header rows repeated on every
  page are skipped.
- **MDP list:** `Gadhdhoo (updated Party list MPD )` is the current MDP membership list, and it
  decides the party ahead of every other file. Everyone on it is `MDP` (it needs no Party column).
  A voter recorded as MDP in another file but missing from the list has changed party: they are
  set to `OTHER` and logged as `PARTY_LEFT_MDP`. The list can also be imported on its own: voters already in the
  portal as MDP who aren't on it are moved to `OTHER` too, except voters edited by hand in the portal. If the list isn't uploaded, nobody is moved
  (`MDP_LIST_NOT_LOADED`).
- **Location:** where the voter is registered to vote (ballot box), read from a Location / Ballot box
  column. Search has a **Location** filter and a Location sort, the voter page and card show it, admins can
  edit it, and exports have it as the last column.
- **Temporary IDs:** a voter without an ID card can be imported under a temporary ID such as `TP0001`
  (logged as an unusual ID); it can't clash with a real `A123456`. When the real card is known, delete the
  temporary record and add or import the voter under the real ID.
- **Phone:** read from a Phone / Mobile / Contact column. A `960` country code is dropped, so
  `9607902314` becomes `7902314`. A blank in a newer file never erases a number already in the portal.
- **De-duplication** is strictly on the ID. For each field, the highest-priority file that has a
  value wins. A blank in a newer file never erases data from an older one.
- **Photos** come from three places, in this order: pictures embedded in the `Gahdhdhoo.2026`
  workbook (matched to the row they sit on); a photo / file-name column (Windows paths are fine,
  since only the file name is looked up under the data folder); and any image named after the
  ID (`A123456.jpg`) anywhere in the data folder. Photos are stored as `photos/<ID>.jpg`.
- **`merge_conflicts.log`** lists everything worth checking, with a summary at the top:

  | Code | Meaning |
  |---|---|
  | `AMBIGUOUS_NAME_MISMATCH` | same ID, very different names: one file probably has a mistyped ID |
  | `POSSIBLE_DUPLICATE_PERSON` | same name and house under two IDs |
  | `SEX_CONFLICT`, `AGE_CONFLICT` | files disagree (ages one year apart are ignored) |
  | `NAME_VARIANT`, `HOUSE_CHANGED`, `PARTY_CHANGED` | which value was kept, and from which file |
  | `DUPLICATE_IN_FILE` | the same ID twice inside one file |
  | `NO_VALID_ID`, `ID_NORMALISED`, `BAD_SEX`, `BAD_AGE`, `UNDER_18`, `MISSING_NAME` | data quality |
  | `PHOTO_NOT_FOUND`, `UNMATCHED_PICTURES`, `MULTIPLE_PHOTOS`, `BAD_PHOTO` | photo problems |
  | `KEPT_PORTAL_EDIT` | voter was edited in the portal; the re-import didn't overwrite it |

- **Re-running is safe.** The default `--mode upsert` adds new IDs, refreshes imported ones and
  keeps any voter an admin edited in the portal. `--mode replace` wipes the voter table first.
  `--dry-run` writes only the log (add `--csv-out merged.csv` to review the merged list in Excel).
- **Seed users:** `admin` (admin role) and `user` (read-only). Passwords come from
  `SEED_ADMIN_PASSWORD` / `SEED_USER_PASSWORD`, or random ones are printed once. Both must be
  changed at first sign-in, and existing users are never touched.

> Pictures placed *in* a cell with Excel 365's newer "Place in Cell" feature aren't visible to
> the importer: use "Place over Cells", or put a file name in a Photo column.

### PDF voter lists (`scripts/extract_from_pdf.py`)

A voter list that only exists as a PDF is turned into a CSV plus one photo per voter. The
Import page does this by itself when you upload a `.pdf`. On your PC:

```powershell
pip install pymupdf
python scripts\extract_from_pdf.py --data-dir "C:\Users\sofhaz.ahmed\dev\gadhdhoo-voter-portal\Data files"
```

- For each `<name>.pdf` it writes `<name>.csv` and `photos/<ID>.jpg` next to it. A PDF named like a
  source (e.g. `Gahdhdhoo.2026.pdf`) takes that source's place in the merge.
- Rows are found with PyMuPDF's table detection. When the PDF has no ruled table, words are grouped
  into lines and columns by position. Every row must contain an ID card number, so page titles and
  footers drop out. Column titles printed only on page 1 are reused on later pages.
- Each picture goes to the ID row it sits on. Logos repeated on most pages and small icons are skipped.
- **Thaana:** some PDFs store right-to-left text backwards. If names look reversed in the CSV, run again
  with `--reverse-rtl`. Scanned PDFs (pictures of text) have no text layer and need OCR first.
- **Debugging a layout:** `--inspect` prints the first two pages' line and column positions with every
  letter shown as `x` and every digit as `9` (column titles stay readable), so you can share it without
  exposing voter data.

## Import page (admins)

**Import** in the top bar does the same merge from the browser, so no terminal is needed:

1. Upload the four files, or zip the whole `Data files` folder (with photos) and upload that one `.zip`. The limit is 300 MB.
2. Pick the mode: add/update (the default), or replace the whole list.
3. Read the preview. It shows the column mapping, the counts and the conflict log, and you can
   download the full `merge_conflicts.log` and the merged list as CSV. Nothing has changed yet.
4. Click **Apply import**. Replace mode asks you to type `REPLACE` first.

Imports run in the background, and only one runs at a time. The uploaded voter files are deleted
right after a real import, or 24 hours after an unapplied preview. Uploads, previews, imports and
downloads all go into the activity log.

## Try it on your PC first (Windows, PowerShell)

```powershell
cd voter-portal
py -m venv .venv; .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q                                   # 20 tests, made-up data
python scripts\ingest.py --data-dir "C:\Users\sofhaz.ahmed\dev\gadhdhoo-voter-portal\Data files" --dry-run --csv-out merged.csv
notepad merge_conflicts.log                 # read this before loading anything
python scripts\ingest.py --data-dir "C:\Users\sofhaz.ahmed\dev\gadhdhoo-voter-portal\Data files"
$env:VOTER_PORTAL_DEV="1"; flask --app wsgi run    # http://127.0.0.1:5000
```

---

# VPS deployment guide (vote.seenu.online)

## 0. Before you start

1. **DNS:** add an `A` record `vote` → your VPS IP. Check it with `dig +short vote.seenu.online`.
2. **Check who owns ports 80/443:**
   ```bash
   sudo ss -ltnp '( sport = :80 or sport = :443 )'
   ```
   - Nothing, or `nginx`: follow **Option A** (Nginx + systemd).
   - `docker-proxy` / Traefik (likely if this is the same Hostinger box that runs the social hub
     in this repo): follow **Option B**. Nginx can't share those ports with Traefik.

## Option A: Nginx + Certbot + systemd (recommended on a clean server)

### A1. One command

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/shophazahmed/CMS.git ~/CMS     # or git pull if already cloned
cd ~/CMS/voter-portal
sudo bash deploy/setup_vps.sh --email you@example.com
```

The script is safe to re-run for updates. It:
- installs `python3-venv nginx certbot sqlite3`;
- creates a `voterportal` system user;
- copies the code to `/opt/voter-portal` and builds a virtualenv;
- writes `/etc/voter-portal/env` with a random `VOTER_PORTAL_SECRET_KEY` (mode 640);
- installs and starts the `voter-portal` systemd service (Gunicorn on `127.0.0.1:8001`, sandboxed so it can only write to `/var/lib/voter-portal`);
- issues the Let's Encrypt certificate (webroot challenge), then installs the Nginx site;
- adds a renewal hook that reloads Nginx, a nightly backup at 03:15 kept for 14 days, and opens UFW if it's active;
- installs the `voter-portal` admin helper.

### A2. Load the voters

Copy the data folder from Windows to the server. PowerShell has `scp` built in:

```powershell
scp -r "C:\Users\sofhaz.ahmed\dev\gadhdhoo-voter-portal\Data files" root@YOUR_VPS_IP:/root/voter-data
```

Then on the server:

```bash
sudo voter-portal ingest /root/voter-data --dry-run      # writes only the log
sudo less /var/lib/voter-portal/merge_conflicts.log
sudo SEED_ADMIN_PASSWORD='choose-a-long-one' voter-portal ingest /root/voter-data
sudo shred -u -z /root/voter-data/* 2>/dev/null; sudo rm -rf /root/voter-data   # don't leave copies around
```

Open https://vote.seenu.online, sign in as `admin` and set a new password. Then create real
named accounts for the team under **Users**, and deactivate the shared `user` account if you
don't need it.

### A3. Manual steps (what the script does, if you prefer to do it by hand)

```bash
# packages, user, folders
sudo apt-get install -y python3 python3-venv python3-pip nginx certbot sqlite3 rsync
sudo useradd --system --home /var/lib/voter-portal --shell /usr/sbin/nologin voterportal
sudo install -d -o voterportal -g voterportal -m 700 /var/lib/voter-portal /var/lib/voter-portal/photos
sudo install -d -o root -g voterportal -m 750 /etc/voter-portal
sudo install -d -m 755 /var/www/certbot

# code + virtualenv
sudo rsync -a --exclude .venv ~/CMS/voter-portal/ /opt/voter-portal/
sudo python3 -m venv /opt/voter-portal/.venv
sudo /opt/voter-portal/.venv/bin/pip install -r /opt/voter-portal/requirements.txt

# secrets
printf 'VOTER_PORTAL_SECRET_KEY=%s\nVOTER_PORTAL_DATA_DIR=/var/lib/voter-portal\nGUNICORN_BIND=127.0.0.1:8001\n' \
  "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" | sudo tee /etc/voter-portal/env >/dev/null
sudo chown root:voterportal /etc/voter-portal/env && sudo chmod 640 /etc/voter-portal/env

# systemd
sudo cp /opt/voter-portal/deploy/systemd/voter-portal.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now voter-portal
curl -I http://127.0.0.1:8001/login          # expect 200

# certificate: temporary HTTP-only site for the challenge
sudo tee /etc/nginx/sites-available/vote.seenu.online >/dev/null <<'EOF'
server { listen 80; server_name vote.seenu.online;
  location /.well-known/acme-challenge/ { root /var/www/certbot; }
  location / { return 503; } }
EOF
sudo ln -sf /etc/nginx/sites-available/vote.seenu.online /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d vote.seenu.online --email you@example.com --agree-tos -n

# final Nginx site
sudo cp /opt/voter-portal/deploy/nginx/vote.seenu.online.conf /etc/nginx/sites-available/vote.seenu.online
sudo nginx -t && sudo systemctl reload nginx
echo -e '#!/bin/sh\nsystemctl reload nginx' | sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo certbot renew --dry-run
```

The Nginx site (`deploy/nginx/vote.seenu.online.conf`) redirects HTTP to HTTPS, uses TLS
1.2/1.3 only, sends HSTS, rate-limits `/login`, serves `/static/` directly and proxies
everything else to Gunicorn.

### A4. Day-to-day

```bash
sudo voter-portal logs                         # last 100 log lines
sudo systemctl status voter-portal
sudo voter-portal create-user alice --role admin
sudo voter-portal set-password admin           # forgot password / locked out
sudo voter-portal backup                       # also runs nightly → /var/backups/voter-portal
cd ~/CMS && git pull && sudo bash voter-portal/deploy/setup_vps.sh   # update the code
```

Restore a backup: `sudo systemctl stop voter-portal`, then copy the `.db` file over
`/var/lib/voter-portal/voter_portal.db` (owned by `voterportal`, mode 600), then `sudo systemctl start voter-portal`.
Copy the backups off the server regularly too (e.g. `scp` to your PC).

## Many candidates on one VPS

Each candidate is one Docker Compose project (`vp-<slug>`) made from `deploy/candidate-compose.yml`.
Every container, Traefik router, middleware and volume name includes the slug, so candidates never
clash. `deploy/vp.sh` does all of it.

**Naming:** slug = short lowercase name (`gadhdhoo`, `gdh-zr`); domain = `<slug>.vote.seenu.online`
(Gadhdhoo keeps `vote.seenu.online`). DNS: one wildcard record `*.vote.seenu.online  A  <VPS IP>`
plus `vote.seenu.online  A  <VPS IP>`. Traefik answers only the domains it has a candidate for and gets a
Let's Encrypt certificate for each one. Don't put Cloudflare's proxy (orange cloud) in front of the
two-level names unless you have a certificate that covers them.

### First time on the server

```bash
git clone https://github.com/<owner>/Gdh-ZR-campaign /opt/vote && cd /opt/vote
git tag                              # releases, e.g. v1.0
```

### Add a candidate

```bash
cd /opt/vote
deploy/vp.sh new gdh-zr zr.vote.seenu.online "GDh ZR" v1.0
#  -> prints the URL and the one-time admin password (changed at first sign-in)
```

Then the candidate's admin signs in, sets **Settings** (parties, colour, messages) and uploads the
voter files on the **Import** page (see below; any file names work).

### Day-to-day

```bash
deploy/vp.sh list                         # every candidate: domain, version, health
deploy/vp.sh logs gdh-zr
deploy/vp.sh flask gdh-zr create-user aisha --role user
deploy/vp.sh flask gdh-zr set-password admin
deploy/vp.sh ingest gdh-zr /root/gdh-zr-files      # import from the server instead of the web page
deploy/vp.sh backup all                   # /var/backups/vote/<slug>/, newest 14 kept
deploy/vp.sh stop gdh-zr                  # data is kept
```

Nightly backups, copied off the server by your own sync:
`15 2 * * * cd /opt/vote && deploy/vp.sh backup all >/var/log/vote-backup.log 2>&1`

### Releases and upgrades (one candidate at a time)

Each candidate is pinned to a git tag, so pushing code never changes a live portal by itself.

```bash
git tag v1.1 && git push --tags           # on your PC, when a version is ready
cd /opt/vote && git fetch --tags
deploy/vp.sh upgrade demo v1.1            # try it on a demo candidate first
deploy/vp.sh upgrade gdh-zr v1.1          # backs up, rebuilds, restarts; to go back: upgrade gdh-zr v1.0
```

### Moving the live Gadhdhoo portal into this setup

The current Gadhdhoo container (from the CMS repo) keeps its data in a Docker volume, usually
`voter-portal_voter-data` (check with `docker volume ls`). Takes about a minute of downtime:

```bash
cd /opt/vote
VP_NO_START=1 deploy/vp.sh new gadhdhoo vote.seenu.online "Gadhdhoo" v1.0
# optional: keep everyone signed in by reusing the old secret
#   copy VOTER_PORTAL_SECRET_KEY from the old .env into candidates/gadhdhoo/.env
docker stop <old voter-portal container>          # see: docker ps
deploy/vp.sh adopt gadhdhoo voter-portal_voter-data
deploy/vp.sh list                                 # gadhdhoo  vote.seenu.online  v1.0  running (healthy)
```

`adopt` copies the data; the old volume stays untouched until you remove it. To roll back: stop
`vp-gadhdhoo` and start the old container again.

## Privacy

This system holds national ID numbers, photos and party affiliation.
- Never commit the data files, the database, `merge_conflicts.log` or exports (`.gitignore` covers them).
- Give every team member their own account rather than sharing `admin`. The activity log is only useful when names are real.
- Delete uploaded source files from the server after import. Treat downloaded exports as sensitive.
