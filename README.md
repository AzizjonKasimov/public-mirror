# public-mirror

public-mirror publishes a public copy of a private git repo. You and your AI coding agents keep
working in the private repo, which holds everything. When you publish, public-mirror builds the
public copy from it and pushes that copy to a second, public repo.

A repo often mixes work worth sharing with things that must stay private: contact details, notes
about other people, client names, job applications. Moving the private data into a separate repo
by hand is slow, and it hides that data from the agents that need it. public-mirror leaves the
private repo whole. Only the published copy is cleaned.

For a real example, [portfolio-public](https://github.com/AzizjonKasimov/portfolio-public) is the
public copy of a private portfolio and job-search repo that AI agents run.

## How it works

```text
private repo   (you and your agents work here, as before)
   |
   |  public-mirror publish
   |    1. read the files of the last commit (HEAD)
   |    2. keep the files that .public-mirror.toml marks public, and add any
   |       example files and files from other repos it names
   |    3. remove private lines from those files and rewrite private names
   |    4. scan the result, and stop if anything looks private
   |    5. commit the result on top of the public repo and push it
   v
public repo    (generated: one commit per publish)
```

- **Only committed files are published.** Uncommitted changes are left out.
- **The public history starts fresh.** Each publish adds one commit to the public repo. The
  private repo's history, and every file it ever held, stays private.
- **Private content never travels.** git pushes only the objects that the new public commit
  points to, so the contents of private files are never sent.
- **Mistakes fail closed.** A file that no pattern mentions stays private. Any problem the scan
  finds stops the publish. A misspelled setting is an error, not a silent no-op.

## Running it

public-mirror needs git and Python 3.11 or later. It has no required dependencies.

```bash
git clone https://github.com/AzizjonKasimov/public-mirror

# Install a `public-mirror` command, with pypdf so the scan can read the text inside PDFs:
uv tool install --editable path/to/public-mirror --with pypdf

# Or run it without installing, from inside the private repo:
uv run path/to/public-mirror/public-mirror.py check
python path/to/public-mirror/public-mirror.py check   # PDFs stay unscanned unless pypdf is installed
```

The examples below write the command as `public-mirror`. Run it anywhere inside the private repo,
or pass `-C <path>` to point it at one.

## Quick start

1. Run `public-mirror init` in the private repo. It creates `.public-mirror.toml`.
2. List the public and private paths in that file (see [Settings](#settings)).
3. Run `public-mirror files` to see the decision for every file, and `public-mirror check -v` to
   see what the scan finds.
4. Run `public-mirror export ..\preview` and look through the public copy it writes.
5. Create an empty repo on GitHub, without a README or license, and set it as `remote`.
6. Commit, then run `public-mirror publish --dry-run`, and then `public-mirror publish`.

## Commands

| Command | What it does |
|---|---|
| `init` | Creates a starter `.public-mirror.toml`. |
| `check [-v]` | Builds the public copy of HEAD in memory and reports problems. Exits with 1 when there are any. `-v` also lists the removed lines and the files the scan cannot read. |
| `files [--status S] [PATH...]` | Prints each file as `public`, `private`, or `unmatched`, with the pattern that decided it. |
| `export DIR` | Writes the public copy of HEAD into an empty folder outside the repo, for review. |
| `publish [-m MSG] [--dry-run]` | Checks, commits the public copy on top of the public repo, and pushes it. `--dry-run` shows the changes without committing or pushing. |
| `pull` | Applies commits that were made in the public repo directly to the private working tree. See [Changes made in the public repo](#changes-made-in-the-public-repo). |

## Settings

`.public-mirror.toml` sits at the root of the private repo and is never published. public-mirror
reads it from the working tree, so you can edit it and rerun `check` before committing.

```toml
[public]
remote = "https://github.com/you/project.git"  # a URL, a local path, or a git remote name
branch = "main"
message = "Update public mirror"               # default commit message for the public repo
author = "Your Name <you@users.noreply.github.com>"  # optional; defaults to your git identity

[paths]
# .gitignore syntax: a leading "/" anchors a pattern to the repo root, a trailing "/" means a
# folder, "**" crosses folders, and "!" re-includes a file.
public = ["/README.md", "/src/", "/docs/"]
private = ["/docs/internal/", "*.local.md"]
public_versions = "public-versions"  # optional; see "Example files for private files" below

[include]
# Optional: committed files from other local repos, published at the path on the left.
"docs/agent-rules.md" = { repo = "../my-configs", path = "rules/AGENTS.md" }

[transform]
private_markers = true      # remove private blocks (default: true)
prune_private_links = true  # in Markdown, drop lines that link to unpublished files (default: true)

[transform.replace]
# Applied in order to every published text file; put longer phrases before shorter ones.
"Falcon Corp's" = "the client's"
"Falcon Corp" = "a logistics client"

[scan]
deny = ["Project Falcon", "+82 10 1234 5678"]  # must never be published; case and spacing are ignored
deny_files = ["~/.config/public-mirror/project-deny.txt"]  # more terms, one per line, kept out of git
allow = ['you@example\.dev']    # regular expressions for detector matches that may be published
skip_detectors = []             # any of: private-key, token, id-number, phone, email, ip-address
max_file_mb = 50
```

A file is published only when a `public` pattern selects it and no `private` pattern does. Files
that match neither stay private, and `check` lists them so you can decide. A `public` or `private`
pattern that matches no file is reported as a warning, because it is usually a typo.

The patterns behave exactly like `.gitignore` lines, because git matches them. That includes one
gitignore rule that surprises people: `!` cannot re-include a file inside an excluded folder. Write
`/docs/*` together with `!/docs/keep.md`, not `/docs/` with `!/docs/keep.md`.

## Private lines inside public files

Some files are mostly shareable but hold a few private lines. Wrap those lines in markers, and the
public copy leaves them out, markers included:

```markdown
## Contact
Email: me@example.dev
<!-- private:start -->
Phone: +82 10 1234 5678
<!-- private:end -->
```

A marker line holds only the marker, optionally inside a comment of the file's language:
`# private:start` in Python, PowerShell, YAML, and TOML; `// private:start` in JavaScript or
Kotlin; `<# private:start #>` and `/* private:start */` also work. A sentence that merely mentions
a marker is left alone. A block that is never closed, or a stray `private:end`, stops the publish.

## Names that appear in many files

A client's name, a colleague, or a data vendor often shows up in a dozen files. Markers around
every mention would clutter the private files, so `[transform.replace]` rewrites such terms in the
public copy instead: one rule turns "Falcon Corp" into "a logistics client" everywhere.

- Terms match the way deny terms do (see below): case is ignored, and any run of spaces and
  line breaks counts as one space, so a phrase still matches when a line break splits it.
- Rules run in order, so list a longer phrase such as `"Falcon Corp's"` before `"Falcon Corp"`.
- Every replaced term is also a deny term. The scan stops a publish when the term survives
  somewhere the rules do not reach, such as inside a Word file or a PDF.
- `check -v` lists how many terms each file had replaced. Read the result with
  `export` the first time, because a rule can produce clumsy grammar.

## Example files for private files

Leaving a private file out can hide how the project works. A tracker such as `data/leads.csv`
shows its format best with a few rows, but the real rows are private. The `public_versions`
folder solves this: a file in it is published at the same path without the folder name, in place
of the private original.

```text
public-versions/data/leads.csv   ->  published as  data/leads.csv  (made-up rows)
data/leads.csv                   ->  stays private                 (real rows)
public-versions/LICENSE          ->  published as  LICENSE         (exists only in public)
```

- The folder itself is never published under its own name.
- Public versions go through the same private-line removal, replacements, and scan as every
  other public file.
- When both files are CSV and their header rows differ, `check` warns, so the example keeps
  the real columns.

## Files from other repos

`[include]` publishes committed files from other local repos, for example agent instructions
that live in a separate configuration repo. Each publish reads the file from the last commit
of that repo, so there is one copy to maintain. Included files go through the same private-line
removal, replacements, and scan. A missing repo or file stops the check, and so does a public
path that already exists in this repo.

## Markdown links to private files

In a public Markdown file, a line that links to an unpublished file or folder is dropped from the
public copy. This keeps an index such as a README free of broken links and of private file names.
Links to web pages and to files that are not in the repo are left alone. `check -v` lists every
dropped line, and `prune_private_links = false` turns this off.

## What the scan looks for

The scan reads every file of the public copy, after the private lines are removed:

- **Deny terms:** the `deny` list, the `deny_files` lists, and every `replace` term. Matching
  ignores case and treats any run of spaces and line breaks as one space. A term that starts or
  ends with an English letter or a digit matches only as a whole word on that side, so `Storm`
  does not match `brainstorm`. Korean and other scripts match anywhere, because Korean attaches
  particles directly to words. Keep a term that must never sit in git, such as a home address,
  in a deny file outside the repo. A deny file inside the repo is never published.
- **Detectors:**
  - private keys;
  - API keys and tokens (AWS, GitHub, Anthropic, OpenAI, Google, Slack, Telegram);
  - Korean resident and alien registration numbers;
  - phone numbers;
  - email addresses;
  - public IP addresses, such as a server's: IPv4, IPv6, and IPv4 written with dashes in a host
    name, the way sslip.io and AWS host names write it.

  Example, no-reply, and `git@` email addresses are always allowed, and so are private, local, and
  example IP addresses (`192.168.0.10`, `127.0.0.1`, `203.0.113.7`). Allow your own public email
  address, or a version number that looks like an IP address, with `allow`.
- **Documents:** Word, Excel, and PowerPoint files are scanned through their text. PDFs are too,
  when pypdf is available.
- **Photos:** JPEG and PNG files that store a GPS location are blocked.
- **Size:** files over `max_file_mb` are blocked.

The report masks what it finds (`"+8..." (16 characters)`) and gives the file and line, so
private values do not end up in logs.

The scan finds patterns, not meaning. A colleague's name or a client mentioned in a sentence
passes unless it is a deny or replace term. Images and scanned PDFs cannot be read at all: `check -v` lists
them, and you should look at each one before its first publish.

## Changes made in the public repo

Every commit that public-mirror makes ends with a `Public-Mirror-Source:` line. A public commit
without that line was made in the public repo directly, for example a merged pull request.
Publishing over it would undo it, so `publish` stops and lists such commits. Then:

1. Run `public-mirror pull`. It applies those commits to the private working tree with a 3-way
   merge. Private lines that are missing from the public copy merge like any other local change.
2. Review the result, resolve any conflicts, and commit it.
3. Run `public-mirror publish` again.

`pull` leaves out changes to public versions and included files, because applying them would
change the private file at the same path. It lists them, so you can copy what you want by hand.

To discard such commits instead, for example the README that GitHub creates with a new repo, run
`publish --overwrite-public-changes`.

## Limits

- Submodules and Git LFS files are published as pointers, without their content.
- The public commit message comes from `-m` or the settings. Private commit messages never leave.
- The scan supports the review of what you publish. It does not replace that review.

## Development

```bash
uv sync
uv run pytest
```

## License

MIT. See [LICENSE](LICENSE).
