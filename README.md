# Threads Reply Archiver

This tool saves every reply on **your** Threads posts into a spreadsheet.
It checks for new replies every 15 minutes.

If someone deletes a reply, you still keep a copy.
Your password-like "token" and your saved replies stay on your Mac. Nobody else sees them.

**Mac only. Free. Setup takes about 20 minutes, one time.**

**Read-only:** this tool can only *read* replies. It can't post, delete, or change anything on your account.
It never asks for your Threads password.

> ⚖️ **Not affiliated with Meta or Threads. Use at your own risk.** By using this tool, you agree
> to follow Meta's terms. See **"Terms of use"** near the bottom.

> 🛡️ **Only download this from the original link.** If someone sends you a copy another way,
> don't run it. A changed copy could steal your token. See **"Check your download"** at the bottom.

---

## See an example first

> 🧪 **Everything in this example is made up.** The posts, accounts, and replies are fictional.
> No real person's data is shown here.

Here's what your spreadsheet looks like after the tool has been running for a bit.
The full example file is here: **[examples/sample_archive.csv](examples/sample_archive.csv)** (GitHub shows it as a table).

| Who replied | What happened | status | edited | tag |
|---|---|---|---|---|
| @example_cafe_owner | Asked about custom prints. A business lead! | live | | opportunity |
| @example_troll | Left a rude comment | live | | |
| @example_friend | ↳ Replied to the troll to defend the post | live | | |
| @example_troll | Posted a threat, then **deleted it**. The tool kept a copy. | **missing** | | evidence |
| @example_snacker | Changed their reply after posting. The first version is kept too. | live | **yes** | |
| @example_prankster | Posted a trick formula. The tool made it harmless. | live | | |

### What this tool can and can't see
- ✅ Replies on **your own** posts, and nothing else.
- ❌ It can't see your DMs, other people's posts, or your password.
- ❌ It can't post, delete, hide, or change anything.
- 🔒 For replies from **private accounts**, Meta hides the username and link, so those rows show up without them.
- 💻 Everything it saves stays on **your** Mac. Nothing is sent to the author of this tool or anyone else.

---

## Part 1: Get your Threads "token" (about 15 min)

A token is like a key. It lets this tool read replies on your Threads posts.
You make it on Meta's developer website.

### Step 1: Make an app
1. Go to **developers.facebook.com** and log in with Facebook.
2. Click **My Apps**, then **Create App**.
3. When it asks what you want to do, choose **Access the Threads API**.
4. Give it any name, like "My Reply Archive," and finish.
5. Make sure the permissions **threads_basic** and **threads_read_replies** are turned on.

> ⚠️ You may see a page called **"Testing your use cases"** that says "Testing not started."
> **Ignore it.** You don't need it. It's only for apps that go public.

### Step 2: Add yourself as a tester
1. In the left sidebar, find **App roles**, then **Roles**.
   (If you don't see it, look under the ⚙️ gear icon.)
2. Click **Add People**.
3. Pick the role **Threads Tester**.
4. Type **your Threads username** and click **Add**.

### Step 3: Accept the invite on Threads
Do this **after** Step 2. The invite won't show up before then.
1. Open Threads (the app or threads.com).
2. Go to **Settings → Account → Website permissions → Invites**.
3. Tap **Accept**.
   (If it's not there yet, wait a minute and refresh.)

### Step 4: Make your token
1. Back on the Meta site, click the ✏️ **pencil icon** in the sidebar (Use cases).
2. Open **Access the Threads API**, then **Settings** (or **Customize**).
3. Scroll to **User Token Generator**.
4. Click **Generate Access Token** next to your name and say yes to the popup.
5. Copy the long code it shows you. **That's your token.**

> 🔒 **Treat your token like a password.** Don't text it, email it, or paste it into
> chats, AI tools, or shared docs. The only place it goes is the `config.env` file in Part 2.

---

## Part 2: Install the tool (about 5 min)

### Step 1: Put the folder in the right place
Move this whole folder into your **home folder** and name it **ThreadsArchive**.
In Finder, press **Shift + Cmd + H** to open your home folder.

Don't put it on the Desktop, or in Documents, Downloads, or iCloud.
Your Mac blocks background tools from running there.

### Step 2: Run Start
1. Double-click **Start.command**.
2. If your Mac says it can't open it: **right-click** the file, choose **Open**, then **Open** again.
3. If a box pops up asking to install "Command Line Tools," click **Install**.
   When it finishes, double-click **Start.command** again.

### Step 3: Paste your token
1. The first time, a file called **config.env** opens.
2. Find the line that says `THREADS_ACCESS_TOKEN=paste_your_token_here`.
3. Replace `paste_your_token_here` with your token. No spaces, no quote marks.
4. Save it (**Cmd + S**) and close it.
5. Double-click **Start.command** one more time.

### Step 4: Watch it work
A black window shows what it's doing. It should look like this:

```
Contacting Threads...
Signed in as @yourname. Fetching posts from the last 30 days...
Found 12 posts. Checking replies...
  post 1/12: 8 replies
  post 2/12: 0 replies
  ...
Done. It now runs every 15 minutes while this Mac is awake.
```

The **data** folder opens when it's done. **You're all set.**

---

## Using it day to day

| File | What it does |
|---|---|
| **Open Archive.command** | Checks for new replies right now and opens your spreadsheet. |
| **Stop.command** | Pauses the 15-minute checks. Your saved replies stay safe. |
| **Start.command** | Turns the 15-minute checks back on. |

### Your spreadsheet: `data/archive.csv`
Replies are **grouped by post**. The newest post is at the top.
Under each post, replies go from oldest to newest, so you can read the conversation in order.

The columns go left to right like this:

- **post_label**: the post's date and first few words. This tells you which post a reply belongs to.
- **post_text** and **post_permalink**: the full post and a link to it.
- **username** and **text**: who replied and what they said.
- **reply_type**: `reply to post`, or `↳ reply to @someone` if they answered another comment.
- **status**: `live` means it's still up. `missing` means it disappeared.
- **tag** and **notes**: **these are yours to fill in.** For example, tag replies `evidence` or `opportunity`.
- **edited**: says `yes` if they changed it. The first version is kept in **original_text**.
- The columns at the far right are IDs and timestamps. They're for proof. You can ignore them day to day.

Your tags and notes are kept every time the tool updates.
If you edit in Numbers or Excel, save it as a **CSV** with the **same name**.

**Tip: fold replies under each post in Numbers.** Click any cell in the **post_label** column,
then go to **Organize → Categories → Add Category for "post_label."** Each post becomes a
section you can open and close.

### Want to go back further in time?
Open **config.env** and change `LOOKBACK_DAYS=30` to a bigger number, like `120`.
The first run saves all the old replies on posts from that period, not just new ones.

---

## If something goes wrong

**The black window sits still for more than 2 minutes.**
Press **Ctrl + C** to stop it. Then:
- **Using a VPN, Tailscale, or Cloudflare WARP?** Turn it off and try again.
  If that fixes it, the VPN was blocking the connection.
- Still stuck? Copy the last 10 lines of the window and send them to whoever shared this tool with you.
  (Check that your token isn't in there first.)

**It says "0 posts checked."**
You haven't posted in the last 30 days. Make `LOOKBACK_DAYS` bigger (see above).

**It says "Token expired or revoked."**
Make a new token (Part 1, Step 4) and paste it into **config.env**.
Normally this won't happen, because the tool renews your token by itself every week.

**It says "This archive belongs to a different Threads account."**
You're using a folder that someone else already set up.
Get a fresh copy of the folder and start over.

**A file in `data/raw/` is empty.**
That's normal. It only fills up when new or changed replies are found.

---

## Good to know

- **It only runs while your Mac is awake.** If someone posts and deletes a reply while
  your Mac is asleep, the tool can't catch it.
- **"Missing" doesn't always mean deleted.** The person may have deleted it, Meta may
  have removed it, or their account may have gone private or blocked you.
- **Proof for serious threats:** the `data/raw/` folder keeps exact copies with a digital
  fingerprint (SHA-256) that shows nothing was changed. For real threats, also take your
  own screenshots, report them in the Threads app, and contact the police. Police can
  ask Meta to save records directly, even deleted ones.
- **Safety feature:** if a reply starts with `=`, the spreadsheet shows a `'` in front of it.
  This stops trick replies from running hidden commands in Excel or Numbers.

---

## Terms of use (please read)

- **Not made by Meta.** This is an independent tool. It is not affiliated with,
  endorsed by, or supported by Meta or Threads. "Threads" and "Meta" are their trademarks.
- **You agree to Meta's rules, not just mine.** This tool uses Meta's official Threads API
  with your own developer app. By using it, you're responsible for following
  Meta's Platform Terms, Developer Policies, and the Threads Terms of Use.
- **Saving deleted replies is your choice and your responsibility.** This tool keeps copies
  of replies even after their authors delete them. Meta's terms and privacy laws where you
  live (or where your commenters live) may limit how long you can keep that data or what
  you can do with it. Check before you rely on it.
- **Keep what you collect private.** Replies belong to the people who wrote them. Don't
  publish, sell, or share the archive except with police, lawyers, or when the law allows.
- **Use at your own risk.** No warranty, no guarantees, and no liability for the author
  (see LICENSE). Nothing here is legal advice.

---

## Check your download (optional, but smart)
This makes sure nobody changed the file before it got to you.
1. Open **Terminal** (press **Cmd + Space**, type **Terminal**, and press Enter).
2. Type `shasum -a 256 ` (with a space at the end), then drag the **ThreadsArchive.zip** file into the window. Press Enter.
3. You'll see a long code. It should **exactly match** the code posted next to the download link.
   If it doesn't match, delete the file and don't run it.

---

*Free to use and share under the MIT License (see LICENSE). Provided as-is, with no warranty.
Not affiliated with Meta. You're responsible for following Meta's terms and the law,
and for how you use and store the replies you collect.*