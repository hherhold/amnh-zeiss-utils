# Setting up amnh-zeiss-utils (first-time setup)

This guide walks you through installing Python (via Miniconda) and getting the
Globus tools in this repo running: `globus-tree.py`, `globus-find.py`,
`globus-clone.py`, and the `tree-viewer.py` GUI. It assumes you're comfortable
at a command prompt and already have a Globus account and Globus Connect
Personal installed, but that you've never set up Python or conda before.

The main instructions are for **Windows**. If you're on a Mac, read
[Mac setup](#mac-setup) — the steps are the same, only a few details differ.

You only do the setup once. After that, see [Everyday use](#everyday-use).

> **Don't use `amnh-zeiss-utils.yaml` for now.** The repo contains a conda
> environment file, but it's currently out of date and may not install cleanly.
> Follow the steps below instead.

---

## What you're installing, and why

- **Miniconda** is a small installer for Python plus `conda`, a tool that
  manages separate, self-contained Python setups called *environments*. It's a
  slimmed-down version of Anaconda without the Navigator app and hundreds of
  preinstalled packages you don't need.
- **An environment called `amnh-zeiss-utils`** holds the Python version and
  add-on packages these scripts need, kept apart from anything else on your
  machine. You "activate" it before running the scripts.
- **The repo itself**: just a folder of Python scripts. You can download it as a
  ZIP file or get it with git (better, because updating is then one command).

---

## Windows setup

### 1. Install Miniconda

1. Download the Windows installer:
   <https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe>
2. Run it and accept the defaults, specifically:
   - **Install for: Just Me**. This doesn't need administrator rights.
   - **Destination folder:** leave it as suggested, typically
     `C:\Users\<you>\miniconda3`.
   - **Advanced options:** leave **"Add Miniconda3 to my PATH"** *unchecked*
     (the installer recommends that too). Leave **"Create shortcuts"** checked.
3. When it finishes, open the **Start menu** and run
   **Anaconda Prompt (miniconda3)**.

   Use this prompt for everything below. A plain Command Prompt or PowerShell
   window won't know about conda. (You can also use **Anaconda PowerShell
   Prompt (miniconda3)** if you prefer PowerShell. The commands are the same.)

4. The prompt should start with `(base)`. Check that conda works:

   ```bat
   conda --version
   ```

   You should see something like `conda 25.x.x`.

### 2. Create the environment

Still in the Anaconda Prompt, run these one at a time. Answer `y` if asked to
proceed.

```bat
conda create -n amnh-zeiss-utils --override-channels -c conda-forge python=3.12
```

```bat
conda activate amnh-zeiss-utils
```

The start of the prompt should change from `(base)` to `(amnh-zeiss-utils)`.
Now install the Globus library, and the two packages the tree viewer GUI needs:

```bat
pip install globus-sdk pyside6 numpy
```

> **Why `--override-channels -c conda-forge`?** It tells conda to get Python
> from *conda-forge*, a free, community-run package source, instead of
> Anaconda's own. That avoids the Anaconda terms-of-service prompts and
> licensing questions. Include it whenever you run `conda create` or
> `conda install`.

### 3. Get the code

Pick **one** of these.

#### Option A: with git (recommended)

If you don't already have git, install it into the environment. Make sure the
prompt still shows `(amnh-zeiss-utils)`:

```bat
conda install --override-channels -c conda-forge git
```

Then download the repo into your home folder:

```bat
cd %USERPROFILE%
git clone https://github.com/hherhold/amnh-zeiss-utils.git
cd amnh-zeiss-utils
```

The code is now in `C:\Users\<you>\amnh-zeiss-utils`.

#### Option B: download a ZIP

1. Go to <https://github.com/hherhold/amnh-zeiss-utils>.
2. Click the green **Code** button, then **Download ZIP**.
3. Extract it into `C:\Users\<you>\`. The extracted folder is called
   `amnh-zeiss-utils-main`. Rename it to `amnh-zeiss-utils` so the paths in
   this guide match.
4. In the Anaconda Prompt:

   ```bat
   cd %USERPROFILE%\amnh-zeiss-utils
   ```

### 4. Check that everything works

```bat
python globus-tree.py -h
```

This should print the help text for `globus-tree.py`. If you see an error
instead, check [Troubleshooting](#troubleshooting).

### 5. Log in to Globus (first run only)

The first time you run any of the `globus-*` scripts on a collection, they ask
you to log in:

```text
Please go to this URL and log in:

https://auth.globus.org/v2/oauth2/authorize?client_id=...

Enter the authorization code here:
```

1. **Copy the URL.** In the Anaconda Prompt, select the whole URL with the
   mouse and press **Enter** or right-click to copy it. (In Windows Terminal,
   use **Ctrl+Shift+C**. Don't press a plain **Ctrl+C**: with nothing selected
   it stops the script.)
2. Paste it into your web browser, log in with your usual Globus identity, and
   click **Allow**.
3. Globus then shows an **authorization code**. Copy it, go back to the prompt,
   paste it (right-click, or **Ctrl+V**), and press **Enter**.

Your login is saved in `C:\Users\<you>\.globus-tree-tokens.json`, and all
three scripts share it. You won't be asked again unless you delete that file
or the login expires.

**You may be asked to log in a second time.** Some collections, including
most institutional storage (Globus Connect Server "mapped" collections), need
an extra "data access" permission. When that happens the script prints
`This collection needs additional consent; re-authenticating...` and shows a
new URL. That's expected: do the same steps again. It happens once per
collection.

### 6. Find your collection IDs

The scripts identify collections by their **UUID**, a long ID like
`a1b2c3d4-...`, not by name. To find one:

1. Go to <https://app.globus.org> and click **Collections** in the left
   sidebar.
2. Search for the collection, or for your own Globus Connect Personal
   collection, use the **Administered by You** filter.
3. Click the collection. The **UUID** is on its overview page. Copy it.

Keep the UUIDs you use often in a text file so you can paste them in.

### 7. Try it

The examples below use `SOURCE_ID` for the remote collection's UUID and
`LOCAL_ID` for your Globus Connect Personal collection's UUID. Replace both
with real UUIDs.

**Put each command on a single line.** The main README breaks long commands
across lines with `\`, which works on a Mac but not in the Windows prompt.

**Make a directory listing** of a remote path, four levels deep, and browse it:

```bat
python globus-tree.py -c SOURCE_ID -p /John_Flynn -d 4 -o flynn.txt
```

```bat
python tree-viewer.py flynn.txt
```

Large listings can take a long time. The status line shows progress. Leave the
window open, and make sure the computer doesn't go to sleep.

**Find files** matching a pattern (here, all `.pca` and `.pcr` files):

```bat
python globus-find.py "*.pc[a,r]" -c SOURCE_ID -p /John_Flynn -o found.txt
```

**Copy matching files** to your computer, keeping their folder structure.
Always do a dry run first with `-n`: it lists what would be copied and
transfers nothing.

```bat
python globus-clone.py "*.pc[a,r]" -c SOURCE_ID -p /John_Flynn -C LOCAL_ID -P /~/pca_files -n
```

If the list looks right, run the same command without `-n` to start the
transfer. You can follow its progress in the Globus web app under
**Activity**.

A few notes about the destination:

- `/~/` means your home folder (`C:\Users\<you>`) on your Globus Connect
  Personal collection, so `/~/pca_files` ends up in `C:\Users\<you>\pca_files`.
  To write to another drive, use the form `/D/some/folder`.
- Globus Connect Personal must be **running and not paused**. The destination
  folder must also be one it's allowed to write to: check **Options** →
  **Access** in Globus Connect Personal. By default that's your home folder.

The main [README](README.md#globus-utilities--globus-treepy-globus-findpy-globus-clonepy-tree-viewerpy)
lists every option each script takes. You can also run any script with `-h`.

---

## Everyday use

Once setup is done, each session is:

1. Open **Anaconda Prompt (miniconda3)** from the Start menu.
2. Run:

   ```bat
   conda activate amnh-zeiss-utils
   cd %USERPROFILE%\amnh-zeiss-utils
   ```

3. Run whichever script you need, e.g. `python globus-find.py ...`.

### Getting updates

- **If you used git:** activate the environment, `cd` into the folder, and run

  ```bat
  git pull
  ```

- **If you downloaded a ZIP:** download a fresh ZIP and replace the folder. If
  you saved your own files (listings, etc.) inside the old folder, move them
  somewhere else first.

If an updated script fails with `ModuleNotFoundError: No module named 'xyz'`,
it needs a new package. Install it with `pip install xyz` (with the
environment active) and try again.

---

## Mac setup

The steps are the same as for Windows, with these differences.

**Installing Miniconda.** Find out which kind of Mac you have: Apple menu →
**About This Mac**. If the **Chip** line says Apple M-something, it's Apple
Silicon; if it says Intel, it's Intel. Download the matching installer:

- Apple Silicon: <https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-arm64.pkg>
- Intel: <https://repo.anaconda.com/miniconda/Miniconda3-latest-MacOSX-x86_64.pkg>

Run it and accept the defaults. Then open a **new** Terminal window (from
Applications → Utilities). The prompt should start with `(base)`. If it
doesn't, run `~/miniconda3/bin/conda init zsh`, then close and reopen
Terminal.

**Everything else:**

- Use **Terminal** wherever this guide says Anaconda Prompt.
- Creating the environment, `conda activate`, and `pip install` work exactly
  as written above.
- **git:** macOS may offer to install its developer tools the first time you
  type `git`. Accepting works, or use the `conda install ... git` step above.
- **Paths:** use `cd ~` instead of `cd %USERPROFILE%`, and
  `cd ~/amnh-zeiss-utils` instead of `cd %USERPROFILE%\amnh-zeiss-utils`.
- **Copy and paste** with **Cmd+C** and **Cmd+V**. **Ctrl+C** stops a running
  script.
- **Quotes around patterns are required** on the Mac. Without them, the shell
  tries to interpret `*.pc[a,r]` itself and fails with `no matches found`.
- Long commands can be split across lines with `\`, as in the main README.
- Your Globus login is saved in `~/.globus-tree-tokens.json`, and `/~/` in
  Globus paths means your Mac home folder.

---

## Troubleshooting

#### `'conda' is not recognized as an internal or external command`

You're in a regular Command Prompt or PowerShell. Open **Anaconda Prompt
(miniconda3)** from the Start menu instead.

#### `ModuleNotFoundError: No module named 'globus_sdk'` (or `PySide6`, etc.)

The environment isn't active. Look at the start of the prompt: it should say
`(amnh-zeiss-utils)`, not `(base)`. Run `conda activate amnh-zeiss-utils`. If
the prompt already shows `(amnh-zeiss-utils)`, the install step was missed: run
`pip install globus-sdk pyside6 numpy`.

#### Typing `python` opens the Microsoft Store

Same cause: the environment isn't active.

#### `python: can't open file '...globus-tree.py': No such file or directory`

You aren't in the repo folder. Run `cd %USERPROFILE%\amnh-zeiss-utils` (Mac:
`cd ~/amnh-zeiss-utils`). The command `dir` (Mac: `ls`) should list the `.py`
files.

#### An error mentioning "Terms of Service" when creating the environment or installing

You left out `--override-channels -c conda-forge`. Run the command again with
it.

#### Login problems, wrong Globus account, or you want to start over

Delete the saved login, and the next run will ask you to log in again:

```bat
del %USERPROFILE%\.globus-tree-tokens.json
```

On a Mac: `rm ~/.globus-tree-tokens.json`

#### `Error accessing collection ...`

Check that the UUID is correct: copy it again from the Globus web app. Also
confirm your Globus account can access that collection in the web app's File
Manager.

#### A clone transfer fails or stalls writing to your computer

Make sure Globus Connect Personal is running and not paused, and that the
destination path is allowed under its **Options** → **Access**. The Globus web
app's **Activity** page shows the detailed error.

#### Stopping a long-running script

Press **Ctrl+C**. `globus-tree.py` keeps the partial listing it had written up
to that point.

---

## Optional: the other tools in this repo

The rest of the repo (Zeiss `.txrm`/`.txm` tools, Slicer utilities, GE scan
database) needs a few more packages. They go in the same environment:

```bat
conda activate amnh-zeiss-utils
pip install olefile numpy tifffile pynrrd tqdm pyside6
```

## Starting over

To delete the environment and build it again from step 2:

```bat
conda deactivate
conda env remove -n amnh-zeiss-utils
```

This removes only the Python environment. Your copy of the repo and any files
you've made aren't touched.
